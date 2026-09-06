"""把老路的日聚合搬进 kit 的表。

搬错了**不会报错**，只会让「你这周比上周多」算出一个错的答案 —— 而没有任何
东西会告诉你它错了。所以这里逐个信号钉住转换结果，而不是只测「跑完没抛异常」。

三条硬约束各有一条：可重复跑、不动老表、认不出来的不猜。
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

psycopg = pytest.importorskip("psycopg")

from perception.perceptkit_adapter import backfill  # noqa: E402


# --------------------------------------------------------------------------
# 逐个信号：转换结果
# --------------------------------------------------------------------------

def test_a_plain_signal_only_gets_its_names_changed():
    """绝大多数信号是改个名，数字一个不动 —— 两边本来就用同一批算法。"""
    doc = {"temperature_c": {"min": 20.0, "max": 28.0, "sum": 96.0, "count": 4}}
    assert backfill.convert("weather", doc) == [("weather", doc)]


def test_blood_pressure_gets_its_unit_into_the_name():
    out = dict(backfill.convert("health_metabolic",
                                {"blood_pressure_systolic": {"max": 118}}))
    # 血压拆到了自己的信号（来源侧是一次 correlation 读数）。
    assert "blood_pressure_systolic_mmhg" in out["health_blood_pressure"]


def test_body_fat_is_converted_not_just_renamed():
    """老的存百分比，kit 存比率。只改名的话历史里的体脂会变成 1840%。"""
    out = dict(backfill.convert("health_body",
                                {"body_fat_pct": 18.4, "weight_kg": 68.2}))
    assert out["health_body_fat"]["body_fat_ratio"] == pytest.approx(0.184)
    assert out["health_weight"]["weight_kg"] == 68.2


def test_steps_move_out_of_vitals_into_their_own_signal():
    """步数在老路里住在 health_vitals 里，kit 给了它独立信号。

    不拆的话，用户几个月的步数历史一条都不会出现在新表里 ——
    而「这周比上周多」问的就是它。
    """
    out = dict(backfill.convert("health_vitals", {
        "step_count": {"min": 0, "max": 9000, "sum": 20000, "count": 5},
        "resting_heart_rate": {"min": 58, "max": 62, "sum": 240, "count": 4},
    }))
    assert out["steps"] == {"step_count": {"total": 9000}}
    # 拆出去之后，原信号里不该再留一份
    assert "step_count" not in out["health_resting_hr"]
    assert "resting_heart_rate" in out["health_resting_hr"]


def test_sleep_stages_are_not_added_on_top_of_the_total():
    """有分期就用分期，只有总数才记一条 asleep。

    两个都记的话，「昨晚睡了多久」会翻倍。
    """
    out = dict(backfill.convert("health_sleep", {
        "asleep_minutes": 430, "core_minutes": 250,
        "deep_minutes": 70, "rem_minutes": 110}))
    assert out["health_sleep"]["minutes"] == {"core": 250.0, "deep": 70.0, "rem": 110.0}
    assert "asleep" not in out["health_sleep"]["minutes"]


def test_sleep_without_stages_still_keeps_the_total():
    """没有分期数据的那些天不能整天丢掉。"""
    out = dict(backfill.convert("health_sleep", {"asleep_minutes": 400}))
    assert out["health_sleep"]["minutes"] == {"asleep": 400.0}


def test_mood_entries_become_a_distribution():
    out = dict(backfill.convert("health_mood", {"entries": [
        {"valence": 0.4}, {"valence": -0.2}, {"kind": "no valence here"}]}))
    v = out["health_mood"]["valence"]
    assert (v["count"], v["min"], v["max"]) == (2, -0.2, 0.4)


# --------------------------------------------------------------------------
# 认不出来的不猜
# --------------------------------------------------------------------------

def test_signals_whose_history_cannot_move_say_why():
    """「跳过」必须带上真实理由。

    location 被跳过不是因为「kit 没这个信号」—— 是老的按 place_label 分桶
    （在家 6 小时）、kit 按城市分桶（在上海 6 小时），从前者推不出后者。
    理由写错了比不写更糟：排查「这段历史怎么没了」时它就是答案。
    """
    assert set(backfill.UNCONVERTIBLE) == {
        "location_signal", "calendar_next_event", "reminders"}
    assert all(len(v) > 20 for v in backfill.UNCONVERTIBLE.values())
    assert "place_label" in backfill.UNCONVERTIBLE["location_signal"]


def test_an_unknown_signal_produces_nothing():
    assert backfill.convert("something_we_removed_last_year", {"x": 1}) == []


# --------------------------------------------------------------------------
# 真库：可重复跑、不动老表
# --------------------------------------------------------------------------

DSN = os.environ.get("PERCEPTKIT_TEST_PG")
needs_pg = pytest.mark.skipif(not DSN, reason="需要真库")


@pytest.fixture
def conn():
    from perception.perceptkit_adapter import schema
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(schema.DDL)
        c.execute(schema.TRUNCATE)
        c.execute("""CREATE TABLE IF NOT EXISTS perception_daily (
                       user_id TEXT, date DATE, signal TEXT, doc JSONB,
                       PRIMARY KEY (user_id, date, signal))""")
        c.execute("DELETE FROM perception_daily")
        yield c


def _old_row(conn, signal, doc, user="u1", day="2026-08-01"):
    from psycopg.types.json import Jsonb
    conn.execute("INSERT INTO perception_daily VALUES (%s,%s,%s,%s)",
                 (user, day, signal, Jsonb(doc)))


@needs_pg
def test_a_dry_run_writes_nothing(conn):
    """先看一眼数字，再决定要不要真跑。"""
    _old_row(conn, "weather", {"temperature_c": {"max": 28.0}})
    plan = backfill.run(conn, dry_run=True)
    assert plan.total == 1 and plan.applied is False
    assert conn.execute("SELECT count(*) FROM perceptkit_daily_aggregate"
                        ).fetchone()[0] == 0


@needs_pg
def test_running_it_twice_does_not_duplicate(conn):
    _old_row(conn, "weather", {"temperature_c": {"max": 28.0}})
    backfill.run(conn, dry_run=False)
    backfill.run(conn, dry_run=False)
    rows = conn.execute("SELECT typed_aggregate FROM perceptkit_daily_aggregate"
                        ).fetchall()
    assert len(rows) == 1 and rows[0][0] == {"temperature_c": {"max": 28.0}}


@needs_pg
def test_rerunning_after_a_fix_overwrites_the_old_value(conn):
    """**这才是可重复跑的意义所在。**

    「跑两遍结果一样」用 `ON CONFLICT DO NOTHING` 也满足 —— 但那种写法下，
    发现某个转换写错了、修好重跑之后，**库里留的还是那批错值**，而且不报错。
    真正要保证的是：第二遍的结果覆盖第一遍。
    """
    from psycopg.types.json import Jsonb
    _old_row(conn, "weather", {"temperature_c": {"max": 28.0}})
    backfill.run(conn, dry_run=False)
    conn.execute("UPDATE perception_daily SET doc = %s",
                 (Jsonb({"temperature_c": {"max": 31.5}}),))
    backfill.run(conn, dry_run=False)
    rows = conn.execute("SELECT typed_aggregate FROM perceptkit_daily_aggregate"
                        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == {"temperature_c": {"max": 31.5}}, "重跑必须覆盖，不能保留旧值"


@needs_pg
def test_it_never_touches_the_old_table(conn):
    """只读老的、只写新的。真出问题，把 kit 的日聚合清掉重来即可。"""
    _old_row(conn, "weather", {"temperature_c": {"max": 28.0}})
    before = conn.execute("SELECT user_id, date, signal, doc FROM perception_daily"
                          ).fetchall()
    backfill.run(conn, dry_run=False)
    assert conn.execute("SELECT user_id, date, signal, doc FROM perception_daily"
                        ).fetchall() == before


@needs_pg
def test_migrated_rows_are_marked_as_migrated(conn):
    """哪天发现某个转换写错了，靠这个标记能精确找回受影响的行 ——
    而不用重扫全表猜哪些是搬来的、哪些是管线自己算的。"""
    _old_row(conn, "weather", {"temperature_c": {"max": 28.0}})
    backfill.run(conn, dry_run=False)
    coverage = conn.execute("SELECT source_coverage FROM perceptkit_daily_aggregate"
                            ).fetchone()[0]
    assert coverage["backfilled_from"] == "perception_daily"


@needs_pg
def test_it_can_be_scoped_to_one_person(conn):
    _old_row(conn, "weather", {"temperature_c": {"max": 28.0}}, user="u1")
    _old_row(conn, "weather", {"temperature_c": {"max": 30.0}}, user="u2")
    backfill.run(conn, subject_id="u1", dry_run=False)
    subjects = [r[0] for r in conn.execute(
        "SELECT subject_id FROM perceptkit_daily_aggregate").fetchall()]
    assert subjects == ["u1"]
