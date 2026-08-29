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
