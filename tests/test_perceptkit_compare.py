"""影子的对比逻辑 —— 「kit 算出来的，和线上那条路算出来的，一样吗」。

影子跑在旁边只能证明 kit 不崩。**这个文件验的是它算得对不对**，
以及一件更容易被忽略的事：**它有没有把「没比过」伪装成「比过了都一样」**。

三类最容易出错的，各有专门的用例：

    权限拒绝            线上写 v=None，kit 写 availability=unavailable。
                        天真地比值，全系统每个被拒绝的信号都会报「不一致」。
    字段名对不上        temperature / temperature_c 长得像，
                        body_fat_pct / body_fat_ratio 也长得像，但差 100 倍。
                        所以配对全部写死，不靠猜。
    manifest 长出新字段  新字段既不在 COMPARABLE 也不在 KIT_ONLY 时，
                        它不是「不比」，是**没人注意到**。必须有东西红。

需要真库（记录那部分要 upsert 和跨租户隔离）：

    PERCEPTKIT_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/perceptkit \
        pytest tests/test_perceptkit_compare.py
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from perceptkit.contracts.records import CurrentProjection  # noqa: E402
from perceptkit.manifest.minimal import MINIMAL_SIGNALS  # noqa: E402

from perception.perceptkit_adapter import compare, schema  # noqa: E402
from perception.perceptkit_adapter.storage import PostgresStorage  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)

DSN = os.environ.get("PERCEPTKIT_TEST_PG")
needs_pg = pytest.mark.skipif(
    not DSN, reason="没有 PERCEPTKIT_TEST_PG，跳过要真库的那几条")


def connect():
    return psycopg.connect(DSN, autocommit=True)


@pytest.fixture
def conn():
    with connect() as c:
        c.execute(schema.DDL)
        c.execute(schema.TRUNCATE)
        yield c


def cell(v):
    return {"v": v, "ts": 1756600000.0}


def projection(signal, value, *, availability="observed", subject="u1",
               dimension=""):
    return CurrentProjection(
        subject_id=subject, signal=signal, dimension_key=dimension,
        typed_value=value, availability=availability,
        observed_at=T0, received_at=T0, expires_at=None,
        source_observation_id="o1", source_revision=None, version=1,
        content_digest="d",
    )


def verdicts(findings):
    return {(f.signal, f.field): f.verdict for f in findings}


# --------------------------------------------------------------------------
# 比对本身
# --------------------------------------------------------------------------

def test_same_value_agrees():
    live = {"battery_level": cell(0.87), "charging": cell(False)}
    kit = {"battery": [projection("battery", {"level_ratio": 0.87,
                                              "is_charging": False})]}
    v = verdicts(compare.compare(live, kit, signals=["battery"]))
    assert v[("battery", "level_ratio")] == "agree"
    assert v[("battery", "is_charging")] == "agree"


def test_different_value_is_differ_and_carries_both_sides():
    live = {"battery_level": cell(0.87)}
    kit = {"battery": [projection("battery", {"level_ratio": 87})]}
    found = [f for f in compare.compare(live, kit, signals=["battery"])
             if f.field == "level_ratio"][0]
    assert found.verdict == "differ"
    # 比率和百分比差 100 倍，两边的值都要留在结论里，否则查不出是哪种错。
    assert (found.live, found.kit) == (0.87, 87)


def test_floats_are_not_rounded_together():
    """容差只吸收 JSON 往返噪声，不许把「少了一位小数」抹平 ——
    那正是要找的东西。"""
    live = {"temperature": cell(23.45)}
    kit = {"weather": [projection("weather", {"temperature_c": 23.4})]}
    v = verdicts(compare.compare(live, kit, signals=["weather"]))
    assert v[("weather", "temperature_c")] == "differ"

    live = {"temperature": cell(23.45)}
    kit = {"weather": [projection("weather", {"temperature_c": 23.450000000001})]}
    v = verdicts(compare.compare(live, kit, signals=["weather"]))
    assert v[("weather", "temperature_c")] == "agree"


def test_enum_case_is_not_folded():
    live = {"motion_state": cell("walking")}
    kit = {"motion_state": [projection("motion_state", {"state": "Walking"})]}
    v = verdicts(compare.compare(live, kit, signals=["motion_state"]))
    assert v[("motion_state", "state")] == "differ"


def test_permission_denied_is_not_a_mismatch():
    """线上 v=None、kit availability=unavailable —— 这是**同一个结论**。

    比错了的话，每个被拒绝权限的信号都会天天报不一致，
    报告变成噪音，真的差异淹在里面。
    """
    live = {"in_focus": cell(None)}
    kit = {"focus_state": [projection(
        "focus_state",
        # kit 刻意在 unavailable 时把上一个可信值留着当 last_known，
        # 直接读 typed_value 就会拿它当「现在的值」。
        {"is_active": True}, availability="unavailable")]}
    found = compare.compare(live, kit, signals=["focus_state"])
    assert [f for f in found if f.field == "is_active"] == []


def test_kit_dropped_a_field_the_live_path_kept():
    """iOS 把 step_count 塞在 health_vitals 里，manifest 给了 steps 独立信号。
    没人接过去的话，这里必须报 only_live —— 不是「都没有」。"""
    live = {"step_count": cell(4211)}
    v = verdicts(compare.compare(live, {}, signals=["steps"]))
    assert v[("steps", "step_count")] == "only_live"


def test_kit_kept_something_live_did_not():
    live = {}
    kit = {"battery": [projection("battery", {"level_ratio": 0.5})]}
    v = verdicts(compare.compare(live, kit, signals=["battery"]))
    assert v[("battery", "level_ratio")] == "only_kit"


def test_both_absent_is_not_recorded():
    v = verdicts(compare.compare({}, {}, signals=["battery"]))
    assert v == {}


def test_shape_difference_is_declared_not_silently_skipped():
    """睡眠两边根本不是一个形状。**说出来**和**不比**必须能区分开 ——
    覆盖面靠沉默维持的报告，会悄悄停止覆盖。"""
    found = compare.compare({"asleep_minutes": cell(430)}, {},
                            signals=["health_sleep"])
    assert [(f.field, f.verdict) for f in found] == [("*", "declared_gap")]
    assert found[0].note


def test_multi_dimension_signal_compares_the_newest_and_says_so():
    live = {"now_playing": cell({"title": "B", "artist": "x"})}
    old = projection("music_playback", {"title": "A", "artist": "x"},
                     dimension="a")
    new = projection("music_playback", {"title": "B", "artist": "x"},
                     dimension="b")
    object.__setattr__(new, "observed_at", T0.replace(hour=11))
    v = compare.compare(live, {"music_playback": [old, new]},
                        signals=["music_playback"])
    title = [f for f in v if f.field == "title"][0]
    assert title.verdict == "agree"
    assert "several dimensions" in title.note


def test_every_manifest_field_has_an_opinion():
    """新加的 manifest 字段，既没配对也没写明「不比」的，必须让这条红。

    否则它既不会被比，也不会有人发现它没被比 —— 而报告看起来照样是绿的。
    """
    assert compare.undeclared_pairs(MINIMAL_SIGNALS) == []


def test_sample_is_truncated():
    long = "x" * 500
    assert len(compare.sample(long)) <= compare.MAX_SAMPLE_CHARS
    assert compare.sample(None) is None


# --------------------------------------------------------------------------
# 记录与读回（要真库）
# --------------------------------------------------------------------------

@needs_pg
def test_record_counts_up_in_place(conn):
    d = compare.Divergence("battery", "level_ratio", "differ", 0.87, 87)
    compare.record(conn, "u1", [d], now=T0, report_id="r1")
    compare.record(conn, "u1", [d], now=T0, report_id="r2")
    rows = conn.execute(
        "SELECT occurrences, last_report_id FROM perceptkit_shadow_divergence"
    ).fetchall()
    assert rows == [(2, "r2")]


@needs_pg
def test_agreement_stores_no_sample_of_the_reading(conn):
    """一致的字段没什么可查的，就不留用户读数的副本。"""
    compare.record(conn, "u1", [
        compare.Divergence("battery", "level_ratio", "agree", 0.87, 0.87),
        compare.Divergence("battery", "is_charging", "differ", True, False),
    ], now=T0, report_id="r1")
    rows = dict(conn.execute(
        "SELECT field, last_live FROM perceptkit_shadow_divergence").fetchall())
    assert rows["level_ratio"] is None
    assert rows["is_charging"] == "True"


@needs_pg
def test_summary_aggregates_across_people(conn):
    """同一个字段错在所有人身上，是一条结论，不是 N 条。"""
    d = compare.Divergence("weather", "temperature_c", "differ", 23.4, 23)
    compare.record(conn, "u1", [d], now=T0, report_id="r1")
    compare.record(conn, "u2", [d], now=T0, report_id="r2")
    rows = compare.summarize(conn)
    assert len(rows) == 1
    assert rows[0]["n"] == 2 and rows[0]["subjects"] == 2


@needs_pg
def test_summary_can_be_scoped_to_one_person(conn):
    compare.record(conn, "u1", [
        compare.Divergence("battery", "level_ratio", "differ", 1, 2)],
        now=T0, report_id="r1")
    compare.record(conn, "u2", [
        compare.Divergence("weather", "uv_index", "differ", 1, 2)],
        now=T0, report_id="r2")
    assert [r["signal"] for r in compare.summarize(conn, subject_id="u1")] \
        == ["battery"]


@needs_pg
def test_summary_leaves_agreement_out_by_default(conn):
    compare.record(conn, "u1", [
        compare.Divergence("battery", "level_ratio", "agree", 1, 1)],
        now=T0, report_id="r1")
    assert compare.summarize(conn) == []


@needs_pg
def test_purging_a_subject_takes_the_divergences_too(conn):
    """这张表存着真实读数。账号删除忘了它，就是一处泄漏。"""
    compare.record(conn, "u1", [
        compare.Divergence("battery", "level_ratio", "differ", 0.87, 87)],
        now=T0, report_id="r1")
    compare.record(conn, "u2", [
        compare.Divergence("battery", "level_ratio", "differ", 0.5, 50)],
        now=T0, report_id="r2")
    PostgresStorage(conn).purge_subject(subject_id="u1")
    left = conn.execute(
        "SELECT subject_id FROM perceptkit_shadow_divergence").fetchall()
    assert left == [("u2",)]


# --------------------------------------------------------------------------
# 覆盖面本身
# --------------------------------------------------------------------------

def test_coverage_states_what_it_does_not_cover():
    """「kit 全都一致」只有配上「一共比了多少」才有意义。"""
    c = compare.coverage(MINIMAL_SIGNALS)
    assert c["signals_total"] == len(MINIMAL_SIGNALS)
    assert len(c["signals_compared"]) + len(c["not_shadowed"]) \
        + len(c["shape_differs"]) == c["signals_total"]
    assert c["fields_compared"] > 0


def test_signals_from_the_other_entry_points_are_declared_unshadowed():
    """影子只挂在快照那一条入口上。照片、设备事件、app 开关那几条没接，
    这件事必须写在代码里，而不是靠「怎么一条都没报」推断出来。"""
    assert "app_usage" in compare.NOT_SHADOWED
    assert "photo_library_added" in compare.NOT_SHADOWED
    assert all(compare.NOT_SHADOWED.values())     # 每条都得有理由


def test_the_declared_vocabulary_is_applied_to_the_live_side_too():
    """适配层把 iOS 的 `still` 翻成 manifest 的 `stationary`，
    线上那条路存的还是 `still` —— 比对时套同一张**已声明**的表，
    验的是「适配层翻了没有」，不是把差异抹平。"""
    live = {"motion_state": cell({"state": "still", "confidence": "high"})}
    kit = {"motion_state": [projection("motion_state", {"state": "stationary"})]}
    v = verdicts(compare.compare(live, kit, signals=["motion_state"]))
    assert v[("motion_state", "state")] == "agree"


def test_it_goes_red_if_the_adapter_stops_translating():
    live = {"motion_state": cell({"state": "still"})}
    kit = {"motion_state": [projection("motion_state", {"state": "still"})]}
    v = verdicts(compare.compare(live, kit, signals=["motion_state"]))
    assert v[("motion_state", "state")] == "differ"


def test_a_live_field_holding_the_bare_value_is_still_read():
    """解密后单输出的信号会被活路径拆成裸值，`.get("state")` 读不到 ——
    于是两边都显示「没有这个字段」，看起来像一致。"""
    live = {"motion_state": cell("stationary")}
    kit = {"motion_state": [projection("motion_state", {"state": "stationary"})]}
    v = verdicts(compare.compare(live, kit, signals=["motion_state"]))
    assert v[("motion_state", "state")] == "agree"


def test_the_unit_bridge_is_declared_per_pair_not_guessed():
    """体脂两边差 100 倍。换算写死在 UNIT_BRIDGE 里，
    所以「适配层换算对了」和「适配层忘了换算」不会都读成一致。"""
    live = {"body_fat_pct": cell(18.4)}
    kit = {"health_body": [projection("health_body", {"body_fat_ratio": 0.184})]}
    v = verdicts(compare.compare(live, kit, signals=["health_body"]))
    assert v[("health_body", "body_fat_ratio")] == "agree"

    kit = {"health_body": [projection("health_body", {"body_fat_ratio": 18.4})]}
    v = verdicts(compare.compare(live, kit, signals=["health_body"]))
    assert v[("health_body", "body_fat_ratio")] == "differ"
