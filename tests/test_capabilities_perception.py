import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from agent import perception_core  # noqa: E402
from capabilities import perception as cap_perc  # noqa: E402


def test_snapshot_joins_signal_list_and_wraps(monkeypatch):
    seen = {}
    def fake(store, *, signals_raw):
        seen["signals_raw"] = signals_raw
        return {"ok": True, "signals": {"now": {"t": 1}}}
    monkeypatch.setattr(perception_core, "agent_perception_payload", fake)
    r = cap_perc.snapshot("STORE", params={"signals": ["now", "calendar"]})
    assert r.ok is True
    assert r.data["signals"] == {"now": {"t": 1}}
    assert seen["signals_raw"] == "now,calendar"  # list coerced to CSV


def test_snapshot_maps_agent_route_error(monkeypatch):
    def boom(store, *, signals_raw):
        raise perception_core.AgentRouteError(403, {"error": "not_permitted"})
    monkeypatch.setattr(perception_core, "agent_perception_payload", boom)
    r = cap_perc.snapshot("STORE", params={})
    assert r.ok is False
    assert r.error["code"] == "capability_forbidden"
    assert r.error["message"] == "not_permitted"
    assert r.error["retryable"] is False


def test_trend_threads_params(monkeypatch):
    seen = {}
    def fake(store, *, signal_raw, field_raw, days_raw):
        seen.update(signal=signal_raw, field=field_raw, days=days_raw)
        return {"ok": True, "trend": {}}
    monkeypatch.setattr(perception_core, "perception_trend_payload", fake)
    r = cap_perc.trend("STORE", params={"signal": "vitals", "field": "hr", "days": 30})
    assert r.ok is True
    assert seen == {"signal": "vitals", "field": "hr", "days": 30}


def test_snapshot_caps_large_signal_list(monkeypatch):
    monkeypatch.setattr(perception_core, "agent_perception_payload",
                        lambda store, *, signals_raw: {"ok": True, "items": list(range(1000))})
    r = cap_perc.snapshot("STORE", params={})
    assert r.ok is True and len(r.data["items"]) == 50
