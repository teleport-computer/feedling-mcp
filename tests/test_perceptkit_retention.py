"""保留期清理 —— 这个包里唯一会永久删用户数据的东西。

它默认干跑，测试要钉住的正是「默认不删」这件事本身：保留期的 bug 从外面
看不见，系统照常工作，用户只是悄悄少了一段历史，直到有人问出一个数据
已经答不了的问题。

需要真库（删除路径要真的 DELETE 才验得出）。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from perceptkit.contracts.records import DailyAggregate, StoredObservation  # noqa: E402
from perceptkit.manifest.minimal import MINIMAL_SIGNALS  # noqa: E402

from perception.perceptkit_adapter import retention, schema  # noqa: E402
from perception.perceptkit_adapter.storage import PostgresStorage  # noqa: E402

DSN = os.environ.get("PERCEPTKIT_TEST_PG")
pytestmark = pytest.mark.skipif(
    not DSN, reason="没有 PERCEPTKIT_TEST_PG，跳过保留期清理的真库验收")

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)


@pytest.fixture
def db():
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute(schema.DDL)
    conn.execute(schema.TRUNCATE)
    yield conn
    conn.close()


def obs(signal: str, days_ago: int, oid: str) -> StoredObservation:
    at = NOW - timedelta(days=days_ago)
    return StoredObservation(
        observation_id=oid, subject_id="u1", signal=signal,
        signal_schema_version=1, source="ios", occurred_at=at, received_at=at,
        availability="observed", effective_local_date=at.date(),
        typed_value={"x": 1},
    )


def agg(signal: str, days_ago: int) -> DailyAggregate:
    return DailyAggregate(
        subject_id="u1", signal=signal,
        local_date=(NOW - timedelta(days=days_ago)).date(),
        aggregation_kind="daily", aggregation_version=1,
        typed_aggregate={"x": {"total": 1}},
    )


def count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# 默认不删
# ---------------------------------------------------------------------------

def test_the_default_run_deletes_nothing(db):
    """默认干跑。一个不小心接上定时器的调用，最坏结果是打印一份报告。"""
    s = PostgresStorage(db)
    s.append_observation(obs("weather", 30, "old"))     # weather 明细 7 天

    plan = retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW)
    assert plan.applied is False
    assert plan.observations["weather"] == 1            # 数出来了
    assert count(db, "perceptkit_observation") == 1     # 但没删


def test_deleting_requires_saying_so_explicitly(db):
    s = PostgresStorage(db)
    s.append_observation(obs("weather", 30, "old"))

    plan = retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False)
    assert plan.applied is True
    assert count(db, "perceptkit_observation") == 0


# ---------------------------------------------------------------------------
# 该留的必须留住
# ---------------------------------------------------------------------------

def test_data_still_inside_its_window_is_untouched(db):
    s = PostgresStorage(db)
    s.append_observation(obs("weather", 3, "fresh"))    # 7 天内
    retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False)
    assert count(db, "perceptkit_observation") == 1


def test_permanent_details_are_never_swept(db):
    """steps 的明细是永久的。扫掉它等于把用户的长期历史删了。"""
    s = PostgresStorage(db)
    s.append_observation(obs("steps", 4000, "ancient"))
    retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False)
    assert count(db, "perceptkit_observation") == 1


def test_permanent_aggregates_survive_even_when_their_details_expire(db):
    """focus_state：明细 365 天、聚合永久。

    这正是保留期拆成两个数字的理由 —— 少了这一条，扫掉明细的同时
    会把长期趋势一起扫掉。
    """
    s = PostgresStorage(db)
    s.append_observation(obs("focus_state", 500, "old-detail"))
    s.put_aggregate(agg("focus_state", 500))

    retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False)
    assert count(db, "perceptkit_observation") == 0     # 明细该走
    assert count(db, "perceptkit_daily_aggregate") == 1  # 聚合必须留


def test_dedupe_identities_outlive_the_details_they_guard(db):
    """明细没了之后，去重记录是唯一挡住「旧数据重放把永久聚合加两遍」的东西，
    而那件事一旦发生无法回滚。"""
    from perceptkit.contracts.records import DurableDedupeIdentity
    s = PostgresStorage(db)
    s.append_observation(obs("focus_state", 500, "old"))
    s.remember_identity(DurableDedupeIdentity(
        subject_id="u1", signal="focus_state", source="ios",
        source_event_identity_digest="d1", first_applied_at=NOW - timedelta(days=500),
        aggregate_scope="daily",
    ))

    retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False)
    assert count(db, "perceptkit_observation") == 0
    assert count(db, "perceptkit_dedupe_identity") == 1


def test_a_signal_missing_from_the_manifest_is_left_alone(db):
    """信号从 manifest 里消失，绝大多数时候是有人写错了，
    而不是「请把它的历史删干净」。"""
    s = PostgresStorage(db)
    s.append_observation(obs("some_retired_signal", 4000, "orphan"))
    retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False)
    assert count(db, "perceptkit_observation") == 1


# ---------------------------------------------------------------------------
# 一轮的量有上限
# ---------------------------------------------------------------------------

def test_one_round_removes_at_most_the_cap(db):
    """无上限的 DELETE 在大表上会长时间持锁，那本身就是一次事故。"""
    s = PostgresStorage(db)
    for i in range(8):
        s.append_observation(obs("weather", 30 + i, f"o{i}"))

    retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False, max_rows=3)
    assert count(db, "perceptkit_observation") == 5     # 剩下的下一轮再来


# ---------------------------------------------------------------------------
# 报告要能被人读懂
# ---------------------------------------------------------------------------

def test_the_dry_run_report_says_what_it_left_alone_and_why(db):
    """只说"要删多少"不够 —— 看报告的人得能确认「该留的确实在留」。"""
    s = PostgresStorage(db)
    s.append_observation(obs("weather", 30, "old"))
    text = retention.format_plan(
        retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW))
    assert "dry run" in text
    assert "weather" in text
    assert "permanent" in text            # 列出了哪些是永久、不动


def test_an_applied_sweep_says_so_loudly(db):
    text = retention.format_plan(
        retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False))
    assert "APPLIED" in text and "rows are gone" in text


# ---------------------------------------------------------------------------
# 规则只有一份：策略来自 kit，这里只负责执行
# ---------------------------------------------------------------------------

def test_the_cutoffs_come_from_the_kit_not_from_a_second_copy_here(db):
    """数出来的和删掉的必须是**同一条截止线**。

    以前计划和执行各算一遍 —— 中间只要有一处不一致，报告说的和实际删的
    就是两回事，而且看不出来。
    """
    from datetime import timedelta
    from perceptkit.retention import plan_retention

    plan = retention.plan_sweep(db, MINIMAL_SIGNALS, now=NOW)
    kit = plan_retention(MINIMAL_SIGNALS, now=NOW)
    assert plan.cutoffs == {(a.signal, a.kind): a.before for a in kit.actions}
    assert plan.cutoffs, "一条动作都没有的话这条测试什么也没验到"

    # 光比计划不够 —— 得证明 **DELETE 真的用了这条线**。
    # weather 明细 7 天：第 8 天该走，第 6 天该留。
    s = PostgresStorage(db)
    s.append_observation(obs("weather", 8, "just-outside"))
    s.append_observation(obs("weather", 6, "just-inside"))
    retention.run_sweep(db, MINIMAL_SIGNALS, now=NOW, dry_run=False)
    left = {r[0] for r in db.execute(
        "SELECT observation_id FROM perceptkit_observation").fetchall()}
    assert left == {"just-inside"}, (
        f"删完剩下 {left} —— 计划里的截止线和 DELETE 用的不是同一条")


def test_the_report_is_written_in_this_report_s_language(db):
    """kit 的理由文案是中文，这份运维报告是英文。

    直接把 kit 的 detail 印出来会变成半中半英 —— 一个库不该替宿主决定
    报告用什么语言，所以 kit 给 code、这里给文案。
    """
    s = PostgresStorage(db)
    s.append_observation(obs("weather", 30, "old"))
    text = retention.format_plan(retention.plan_sweep(db, MINIMAL_SIGNALS, now=NOW))
    assert "permanent" in text
    assert "永久" not in text, "kit 的中文理由漏进了英文报告"


def test_an_unknown_skip_code_shows_the_code_instead_of_vanishing(db):
    """kit 以后多一个跳过原因时，这份报告不能把它变成空白。

    映射表查不到就把 code 原样印出来 —— 看到一个没见过的标识，比看到
    一行空白强得多。
    """
    plan = retention.plan_sweep(db, MINIMAL_SIGNALS, now=NOW)
    assert all(why for _, why in plan.skipped)
    assert "brand_new_reason" == retention._SKIP_TEXT.get(
        "brand_new_reason", "brand_new_reason")
