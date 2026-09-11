"""T564:pull 快照要按**字段**看单指标 Capability 的 query_tool。

背景(T561,2026-09-11):perceptkit 0.4.0(提交 9f999e4)把健康信号拆成单指标后,
上报键 ``health_vitals / health_body / health_metabolic`` 的 Capability 不再带
``query_tool``,标志挪到了 ``health_resting_hr`` 等单指标 Capability 上——而那些
Capability 不被任何 Signal 引用。``_wanted_snapshot_fields`` 只按 Signal 的
Capability 判,于是这三组共 14 个输出字段从不进 pull 快照,agent 投影恒 None,
kit / live 两条路一样。

这些测试跑在**真 catalog**(requirements 钉的 perceptkit)上,不造假 catalog:
被测的正是「io 的过滤逻辑 × 真目录」这个组合。
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import perception.service as service  # noqa: E402
from perceptkit import catalog  # noqa: E402

# 三组上报信号(拆分后 Capability 无 context_field / query_tool)的全部输出。
# 从 catalog **派生**而不是写死,catalog 增减字段时这里跟着变;
# 派生集非空由 test_split_report_signals_are_still_flagless 钉住。
_SPLIT_REPORT_SIGNALS = ("health_vitals", "health_body", "health_metabolic")


def _split_outputs() -> set[str]:
    out: set[str] = set()
    for key in _SPLIT_REPORT_SIGNALS:
        sig = catalog.SIGNALS[key]
        out.update(f for f in sig.outputs if f != "user_state")
    return out


def _legacy_wanted(*, include_query_tools: bool) -> dict[str, float]:
    """修复前的判定,逐字照抄,作为「cheap now 分支不变」的冻结基线。"""
    wanted: dict[str, float] = {}
    for sig in catalog.SIGNALS.values():
        cap = catalog.CAPABILITIES.get(sig.capability)
        if not cap or not (cap.context_field or (include_query_tools and cap.query_tool)):
            continue
        for f in sig.outputs:
            if f != "user_state":
                wanted[f] = sig.ttl_sec
    return wanted


def test_split_report_signals_are_still_flagless():
    """前提钉子:三组上报 Capability 仍然两个标志都 False,且派生集非空。
    perceptkit 若加回 query_tool,这条会红——那时下面的修复可以退役。"""
    for key in _SPLIT_REPORT_SIGNALS:
        cap = catalog.CAPABILITIES[catalog.SIGNALS[key].capability]
        assert cap.context_field is False and cap.query_tool is False, key
    outs = _split_outputs()
    assert len(outs) >= 14, outs
    assert {"step_count", "resting_heart_rate", "weight_kg", "blood_glucose_mmol_l"} <= outs


def test_pull_snapshot_wants_every_split_health_field():
    """核心:include_query_tools=True 时,三组的每个输出都在 wanted 里(修前红)。"""
    wanted = service._wanted_snapshot_fields(include_query_tools=True)
    missing = sorted(_split_outputs() - set(wanted))
    assert not missing, f"pull 快照漏了这些字段 ⇒ agent 投影恒 None: {missing}"


def test_split_fields_keep_their_signal_ttl():
    """进 wanted 的字段沿用其上报 Signal 的 ttl(过期判据仍按老路目录)。"""
    wanted = service._wanted_snapshot_fields(include_query_tools=True)
    for key in _SPLIT_REPORT_SIGNALS:
        sig = catalog.SIGNALS[key]
        for f in sig.outputs:
            if f != "user_state" and f in wanted:
                assert wanted[f] == sig.ttl_sec, (f, wanted[f], sig.ttl_sec)


def test_cheap_now_snapshot_is_unchanged():
    """边界:include_query_tools=False(cheap `now`)的集合与修前逐项相同。"""
    assert service._wanted_snapshot_fields(include_query_tools=False) == _legacy_wanted(include_query_tools=False)


def test_query_branch_is_a_superset_of_legacy_only_by_split_fields():
    """边界:新逻辑只多出三组的字段,⛔不顺带放开别的东西。"""
    new = set(service._wanted_snapshot_fields(include_query_tools=True))
    old = set(_legacy_wanted(include_query_tools=True))
    assert old <= new
    assert (new - old) <= _split_outputs(), sorted(new - old - _split_outputs())


def test_metric_capability_table_is_consistent_with_catalog():
    """映射表完整性:每个映射的单指标 Capability 存在且 query_tool=True;
    三组的每个输出要么在表里,要么是显式登记的例外(step_count:catalog 没有 steps Capability)。"""
    table = service.QUERY_METRIC_CAPABILITY_BY_FIELD
    for field, cap_name in table.items():
        if cap_name is None:
            assert field in service.QUERY_FIELDS_WITHOUT_METRIC_CAPABILITY, field
            continue
        cap = catalog.CAPABILITIES.get(cap_name)
        assert cap is not None, (field, cap_name)
        assert cap.query_tool is True, (field, cap_name)
    uncovered = sorted(_split_outputs() - set(table))
    assert not uncovered, uncovered
    assert set(service.QUERY_FIELDS_WITHOUT_METRIC_CAPABILITY) == {"step_count"}


def test_table_has_no_entries_outside_split_signals():
    """双向:表里不许有三组之外的字段(多余项也报警)。"""
    extra = sorted(set(service.QUERY_METRIC_CAPABILITY_BY_FIELD) - _split_outputs())
    assert not extra, extra


def test_unmapped_field_is_never_admitted(monkeypatch):
    """未知字段(不在表里)⛔不能被顺带放入:给 health_vitals 临时加一个未映射输出,它不该进 wanted。"""
    sig = catalog.SIGNALS["health_vitals"]
    patched = dataclasses.replace(sig, outputs=tuple(sig.outputs) + ("made_up_metric",))
    monkeypatch.setitem(catalog.SIGNALS, "health_vitals", patched)
    wanted = service._wanted_snapshot_fields(include_query_tools=True)
    assert "made_up_metric" not in wanted
    assert "resting_heart_rate" in wanted  # 同组已映射字段不受影响


def test_turning_off_one_metric_capability_only_drops_that_field(monkeypatch):
    """负向:把某个单指标 Capability 的 query_tool 改成 False,只有对应字段退出 wanted,同组其它字段不受影响。"""
    cap = catalog.CAPABILITIES["health_bmi"]
    off = dataclasses.replace(cap, query_tool=False)
    monkeypatch.setitem(catalog.CAPABILITIES, "health_bmi", off)
    wanted = service._wanted_snapshot_fields(include_query_tools=True)
    assert "bmi" not in wanted
    for sibling in ("weight_kg", "body_fat_pct", "height_cm"):      # 同组 health_body
        assert sibling in wanted, sibling
    for other in ("resting_heart_rate", "step_count", "blood_glucose_mmol_l"):
        assert other in wanted, other


def test_removing_a_metric_capability_drops_only_its_fields(monkeypatch):
    """负向:单指标 Capability 整个不存在 ⇒ 对应字段退出(blood_pressure 两字段共一个 cap,一起退),其它不动。"""
    monkeypatch.delitem(catalog.CAPABILITIES, "health_blood_pressure")
    wanted = service._wanted_snapshot_fields(include_query_tools=True)
    assert "blood_pressure_systolic" not in wanted and "blood_pressure_diastolic" not in wanted
    assert "blood_glucose_mmol_l" in wanted                           # 同组 health_metabolic
    assert "step_count" in wanted and "resting_heart_rate" in wanted


# ---------------------------------------------------------------------------
# 真实 readback 回归:新进 wanted 的字段走 kit 路时,observed / unavailable / 过期
# 三态与老字段行为一致。用真 merged_snapshot + 真 CurrentProjection,不 mock。
# ---------------------------------------------------------------------------
import time as _time  # noqa: E402
from datetime import datetime, timedelta, timezone as _tz  # noqa: E402

from perception.perceptkit_adapter import readback  # noqa: E402
from perceptkit.contracts.records import CurrentProjection  # noqa: E402


def _proj(signal: str, typed: dict | None, availability: str, age_sec: float) -> CurrentProjection:
    at = datetime.now(_tz.utc) - timedelta(seconds=age_sec)
    return CurrentProjection(
        subject_id="usr_t", signal=signal, dimension_key=signal, typed_value=typed,
        availability=availability, observed_at=at, received_at=at, expires_at=None,
        source_observation_id="obs", source_revision=1, source="ios", source_event_id=None,
        version=1, content_digest="d",
    )


def _merge(rows_by_signal: dict, live_state: dict) -> dict:
    wanted = service._wanted_snapshot_fields(include_query_tools=True)
    snap, _sources, _conflicts = readback.merged_snapshot(
        live_state, rows_by_signal, wanted=wanted, now=_time.time(),
        stable_fields=service._STABLE_CONTEXT_FIELDS)
    return snap


def test_kit_observed_fresh_value_reaches_snapshot():
    snap = _merge({"steps": [_proj("steps", {"step_count": 4321}, "observed", 5)],
                   "health_resting_hr": [_proj("health_resting_hr", {"resting_heart_rate": 61}, "observed", 5)]},
                  live_state={})
    assert snap["step_count"] == 4321 and snap["resting_heart_rate"] == 61


def test_kit_unavailable_wins_over_fresh_live_value():
    """撤权→None 的安全语义不变:kit 见过且说 unavailable,live 里还有新鲜旧值也报 None。"""
    now = _time.time()
    snap = _merge({"steps": [_proj("steps", None, "unavailable", 5)]},
                  live_state={"step_count": {"v": 4321, "ts": now}})
    assert snap["step_count"] is None


def test_kit_expired_value_is_null():
    """过期判据按上报 Signal 的 ttl(health_vitals=3600s):观测 2h 前 ⇒ None。"""
    ttl = catalog.SIGNALS["health_vitals"].ttl_sec
    snap = _merge({"steps": [_proj("steps", {"step_count": 4321}, "observed", ttl + 3600)]}, live_state={})
    assert snap["step_count"] is None


def test_no_kit_rows_falls_back_to_live():
    """kit 一条行都没有(影子开跑前的老用户)⇒ 走老路:live 新鲜值可见,live 过期为 None。"""
    now = _time.time(); ttl = catalog.SIGNALS["health_vitals"].ttl_sec
    snap = _merge({}, live_state={"step_count": {"v": 4321, "ts": now},
                                  "resting_heart_rate": {"v": 61, "ts": now - ttl - 60}})
    assert snap["step_count"] == 4321 and snap["resting_heart_rate"] is None


def test_kit_unavailable_with_stale_typed_value_is_still_null():
    """撤权后 kit 行可能仍带着上一次的 typed_value(last_known):availability=unavailable 就必须 None,
    ⛔不能把留着的旧值当当前值报出去。"""
    now = _time.time()
    snap = _merge({"steps": [_proj("steps", {"step_count": 4321}, "unavailable", 5)]},
                  live_state={"step_count": {"v": 4321, "ts": now}})
    assert snap["step_count"] is None


def test_kit_expired_does_not_fall_back_to_fresh_live():
    """kit 有行但过期、live 却新鲜:值来自 kit 就按 kit 的时刻算过期 ⇒ None,⛔不回落到 live。"""
    now = _time.time(); ttl = catalog.SIGNALS["health_vitals"].ttl_sec
    snap = _merge({"steps": [_proj("steps", {"step_count": 9999}, "observed", ttl + 3600)]},
                  live_state={"step_count": {"v": 4321, "ts": now}})
    assert snap["step_count"] is None
