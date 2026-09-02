"""趋势工具改成读 kit 的日聚合。

「我最近睡得比以前少吗」走的就是这条路。老路一停写，老表就冻在那天 ——
历史搬得过去，但**新的每天不再往老表写**，用户从那天起再问就答不出来。

验收标准只有一条：**同一份数据，读老表和读 kit 得到逐字段相同的文档**。
不是「数值差不多」——趋势数学按具体的键读（`{min,max,sum,count}`、
`asleep_minutes`），少一个键、整数变浮点，都可能让下游算出别的东西
或者干脆读不到。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception.perceptkit_adapter import backfill, history  # noqa: E402


def roundtrip(old_signal: str, old_doc: dict) -> dict:
    """老文档 → kit → 老文档。两个方向用的是同一张映射表。"""
    produced = dict(backfill.convert(old_signal, old_doc))
    kit_signal = backfill._key_map()[old_signal]
    main = produced[kit_signal]
    if old_signal == "health_sleep":
        return history._sleep_back(main)
    if old_signal == "health_vitals":
        return history._vitals_back(main, produced.get("steps"))
    return history._unrename(main, backfill._FIELD_RENAMES.get(old_signal, {}),
                             scale=backfill._FIELD_SCALES.get(old_signal, {}))


def test_a_plain_signal_survives_the_round_trip():
    doc = {"temperature_c": {"min": 20.0, "max": 28.0, "sum": 96.0, "count": 4}}
    assert roundtrip("weather", doc) == doc


def test_body_fat_comes_back_as_a_percentage():
    """老路存百分比、kit 存比率。翻不回去的话趋势会说「体脂 0.18%」。"""
    doc = {"body_fat_pct": 18.4, "weight_kg": 68.2, "_at": 1788176264.0}
    assert roundtrip("health_body", doc) == doc


def test_blood_pressure_comes_back_without_the_unit_suffix():
    doc = {"blood_pressure_systolic": {"max": 118, "min": 110,
                                       "sum": 456, "count": 4}}
    assert roundtrip("health_metabolic", doc) == doc


def test_sleep_comes_back_with_all_four_numbers():
    """kit 按阶段存分钟，老路要四个总数 —— 包括算出来的 asleep_minutes。"""
    doc = {"_at": 1788176264.0, "core_minutes": 250, "deep_minutes": 70,
           "rem_minutes": 110, "asleep_minutes": 430}
    assert roundtrip("health_sleep", doc) == doc


def test_sleep_minutes_stay_integers():
    """110 变成 110.0 数值上一样，但「老路给什么、新路就给什么」是这次切换
    唯一的验收标准；放过一个类型差异，标准就松一格。"""
    out = roundtrip("health_sleep", {"core_minutes": 250, "deep_minutes": 70,
                                     "rem_minutes": 110, "asleep_minutes": 430})
    assert all(isinstance(v, int) for k, v in out.items() if k.endswith("_minutes"))


def test_steps_come_back_inside_vitals():
    """步数在 kit 里是独立信号，趋势工具却按 `vitals.step_count` 问。

    不接回去的话，「这周步数比上周多吗」直接读不到字段。
    """
    doc = {"step_count": {"min": 4211, "max": 4211, "sum": 4211, "count": 1},
           "resting_heart_rate": {"min": 58, "max": 58, "sum": 58, "count": 1}}
    out = roundtrip("health_vitals", doc)
    assert out["step_count"]["max"] == 4211
    assert out["resting_heart_rate"] == doc["resting_heart_rate"]


def test_a_signal_whose_history_cannot_move_is_not_read_from_the_kit():
    """位置的两种分桶维度不同（在家 6 小时 vs 在上海 6 小时）。

    硬翻会给出一个看起来对、实际是编的数字 —— 那些信号照旧读老表。
    """
    assert history._kit_signal("location_signal") is None
    assert history._kit_signal("calendar_next_event") is None
    assert history._kit_signal("weather") == "weather"


def test_the_unconvertible_guard_is_what_stops_it(monkeypatch):
    """上面那条其实证不了守卫在起作用 —— 那三个信号本来也不在映射表里，
    守卫是多余的一层。

    多余的一层正是最容易在重构里被顺手删掉的东西，而删掉之后要等到某个
    信号**同时**进了映射表和「翻不过去」名单，才会以一个编出来的趋势
    数字的形式暴露。所以这里直接钉守卫本身。
    """
    monkeypatch.setitem(backfill.UNCONVERTIBLE, "weather", "假装它翻不过去")
    assert history._kit_signal("weather") is None


def test_the_switch_defaults_on_and_can_be_turned_off(monkeypatch):
    monkeypatch.delenv(history.ENV_FLAG, raising=False)
    monkeypatch.setattr(history, "store", None, raising=False)
    monkeypatch.setenv(history.ENV_FLAG, "0")
    assert history.enabled() is False


# ---------------------------------------------------------------------------
# 切换那一刻，趋势不能从 90 天塌成 1 天
#
# 2026-09-02 在 prod 上真的发生了：kit 当天上午才开始落数据，而读这一层是
# 「kit 有数据就整份用 kit」——于是每个信号的历史都只剩当天那一行。
# 不报错，agent 只会说"数据不够"，没有任何东西说得清为什么。
# ---------------------------------------------------------------------------

def _legacy(n=30):
    return [{"date": f"2026-08-{d:02d}", "doc": {"m": 400 + d}} for d in range(1, n + 1)]


def test_the_first_kit_day_does_not_erase_the_legacy_history():
    from perception.store import _merge_daily
    got = _merge_daily(_legacy(), [{"date": "2026-09-02", "doc": {"m": 431}}], 30)
    assert len(got) == 30, f"只剩 {len(got)} 天 —— 历史被 kit 那一行盖掉了"
    assert got[-1]["date"] == "2026-09-02"
    assert got[0]["date"] == "2026-08-02"          # 按 days 截断，保留最近的


def test_a_day_both_sides_have_goes_to_the_kit():
    """混着用会让同一条曲线上半段一个口径、下半段另一个，比少几天更难发现。"""
    from perception.store import _merge_daily
    got = _merge_daily([{"date": "2026-09-02", "doc": {"m": 1}}],
                       [{"date": "2026-09-02", "doc": {"m": 2}}], 30)
    assert len(got) == 1 and got[0]["doc"]["m"] == 2


def test_an_empty_side_changes_nothing():
    from perception.store import _merge_daily
    legacy = _legacy()
    assert _merge_daily(legacy, [], 30) == legacy
    kit = [{"date": "2026-09-02", "doc": {"m": 431}}]
    assert _merge_daily([], kit, 30) == kit


def test_a_legacy_read_failure_still_returns_what_the_kit_has():
    """老表读挂了也别把 kit 已经有的那几天一起丢掉。"""
    from unittest.mock import patch
    import perception.store as store
    kit = [{"date": "2026-09-02", "doc": {"m": 431}}]
    with patch("perception.perceptkit_adapter.history.enabled", return_value=True), \
         patch("perception.perceptkit_adapter.history.daily_rollups", return_value=kit), \
         patch("perception.store.get_pool", side_effect=RuntimeError("db down")):
        assert store.list_perception_daily("u", "health_sleep", days=30) == kit
