"""Unit tests for the trend-model dispatch in ``perception_trend_payload``.

Pure unit: ``perception_store.list_perception_daily`` is monkeypatched to a
fake, so this never touches Postgres. Covers:
  - fluctuating signals (e.g. health_vitals) stay byte-identical to the
    pre-dispatch behavior (no "model" key, same read_trend body).
  - drifting signals (health_body) route through trend_models.read_drift and
    carry model="drifting".
  - cyclical signals (health_cycle) intentionally fall back to read_trend
    (no onset-event data reachable here) but are still tagged
    model="cyclical", fallback="read_trend" so a caller can tell.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent import perception_core  # noqa: E402
from perception import history as perception_history  # noqa: E402
from perception import store as perception_store  # noqa: E402
from perceptkit import trend_models  # noqa: E402


class _Store:
    user_id = "u_trend_dispatch"


def _patch_rows(monkeypatch, rows_by_signal):
    def fake_list(user_id, signal, days):
        return list(rows_by_signal.get(signal, []))
    monkeypatch.setattr(perception_store, "list_perception_daily", fake_list)


def test_fluctuating_signal_is_byte_identical_to_plain_read_trend(monkeypatch):
    rows = [
        {"date": "2026-06-20", "doc": {"resting_heart_rate": {"sum": 60, "count": 1, "min": 60, "max": 60}}},
        {"date": "2026-06-21", "doc": {"resting_heart_rate": {"sum": 62, "count": 1, "min": 62, "max": 62}}},
        {"date": "2026-06-22", "doc": {"resting_heart_rate": {"sum": 70, "count": 1, "min": 70, "max": 70}}},
    ]
    _patch_rows(monkeypatch, {"health_vitals": rows})

    body = perception_core.perception_trend_payload(
        _Store(), signal_raw="vitals", field_raw="resting_heart_rate", days_raw="30"
    )

    expected_trend = perception_history.read_trend(rows, "health_vitals", "resting_heart_rate")
    assert body == {"ok": True, "trend": expected_trend}
    assert "model" not in body
    assert trend_models.model_for("health_vitals") == trend_models.FLUCTUATING


def test_drifting_signal_routes_through_read_drift(monkeypatch):
    rows = [
        {"date": "2025-08-01", "doc": {"weight_kg": 80.0}},
        {"date": "2026-02-01", "doc": {"weight_kg": 70.0}},
        {"date": "2026-06-01", "doc": {"weight_kg": 65.0}},
        {"date": "2026-08-01", "doc": {"weight_kg": 60.0}},
    ]
    _patch_rows(monkeypatch, {"health_body": rows})
    assert trend_models.model_for("health_body") == trend_models.DRIFTING

    body = perception_core.perception_trend_payload(
        _Store(), signal_raw="body", field_raw="weight_kg", days_raw="365"
    )

    assert body["ok"] is True
    assert body["model"] == "drifting"
    trend = body["trend"]
    assert trend["model"] == "drifting"
    assert trend["first"] == {"date": "2025-08-01", "value": 80.0}
    assert trend["last"] == {"date": "2026-08-01", "value": 60.0}
    assert trend["total_delta"] == -20.0
    # A caller assuming the fluctuating shape would find these keys absent
    # rather than silently reading a stale/median-shaped answer.
    assert "baseline" not in trend
    assert "median" not in trend


def test_cyclical_signal_falls_back_to_read_trend_but_is_tagged(monkeypatch):
    rows = [
        {"date": "2026-06-01", "doc": {"flow_level": 2}},
        {"date": "2026-06-02", "doc": {"flow_level": 1}},
    ]
    _patch_rows(monkeypatch, {"health_cycle": rows})
    assert trend_models.model_for("health_cycle") == trend_models.CYCLICAL

    body = perception_core.perception_trend_payload(
        _Store(), signal_raw="cycle", field_raw="flow_level", days_raw="90"
    )

    assert body["ok"] is True
    assert body["model"] == "cyclical"
    assert body["fallback"] == "read_trend"
    # The fallback body is exactly what plain read_trend would produce — no
    # invented onset/interval fields.
    assert body["trend"] == perception_history.read_trend(rows, "health_cycle", "flow_level")
    assert "intervals" not in body["trend"]
    assert "typical_interval" not in body["trend"]


def test_unlisted_signal_defaults_to_fluctuating(monkeypatch):
    # health_metabolic has no entry-worthy special model; TREND_MODEL still
    # lists it explicitly as fluctuating, but the dispatch must behave the
    # same for any signal absent from TREND_MODEL too (model_for's default).
    assert trend_models.model_for("no_such_signal_in_table") == trend_models.FLUCTUATING


# ---------------------------------------------------------------------------
# Cutover read-side guard: old arrival-tagged rows must never blend into a
# drift/trend series alongside new measurement-time-tagged rows.
# ---------------------------------------------------------------------------

def test_drifting_trend_drops_arrival_tagged_rows_once_any_row_is_measured(monkeypatch):
    # 8 months of the OLD bug: a January weigh-in re-uploaded and re-stamped
    # with each day's arrival time, keyed by whatever day the phone happened
    # to send it (no `_ts_kind` tag at all) — looks like a steady 68.4kg every
    # day for 8 months, which is fabricated freshness, not a real trend.
    poisoned_rows = [
        {"date": d, "doc": {"weight_kg": 68.4}}
        for d in ("2026-01-15", "2026-03-01", "2026-05-01", "2026-07-01")
    ]
    # After deploy, the real January sample lands under its true date, tagged
    # `_ts_kind: "measured"` — plus a second, later genuine weigh-in.
    measured_rows = [
        {"date": "2026-01-14", "doc": {"weight_kg": 68.4, "_ts_kind": "measured"}},
        {"date": "2026-08-20", "doc": {"weight_kg": 66.0, "_ts_kind": "measured"}},
    ]
    rows = sorted(poisoned_rows + measured_rows, key=lambda r: r["date"])
    _patch_rows(monkeypatch, {"health_body": rows})

    body = perception_core.perception_trend_payload(
        _Store(), signal_raw="body", field_raw="weight_kg", days_raw="365"
    )

    # The drift computation must see ONLY the two measured-tagged points —
    # the four arrival-tagged rows never entered the series at all.
    expected = trend_models.read_drift(measured_rows, "health_body", "weight_kg")
    assert body["trend"] == expected
    assert body["trend"]["first"] == {"date": "2026-01-14", "value": 68.4}
    assert body["trend"]["last"] == {"date": "2026-08-20", "value": 66.0}


def test_trend_keeps_all_rows_when_none_are_measurement_aware_yet(monkeypatch):
    # No row anywhere carries `_ts_kind` (old app / pre-cutover traffic only)
    # -> the filter is a no-op, byte-identical to today's behavior.
    rows = [
        {"date": "2026-06-20", "doc": {"resting_heart_rate": {"sum": 60, "count": 1, "min": 60, "max": 60}}},
        {"date": "2026-06-21", "doc": {"resting_heart_rate": {"sum": 62, "count": 1, "min": 62, "max": 62}}},
    ]
    _patch_rows(monkeypatch, {"health_vitals": rows})

    body = perception_core.perception_trend_payload(
        _Store(), signal_raw="vitals", field_raw="resting_heart_rate", days_raw="30"
    )
    assert body["trend"] == perception_history.read_trend(rows, "health_vitals", "resting_heart_rate")
