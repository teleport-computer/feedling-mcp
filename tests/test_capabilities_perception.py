import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from agent import perception_core  # noqa: E402
from capabilities import perception as cap_perc  # noqa: E402
from capabilities import registry, tool_schema  # noqa: E402


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


def test_glance_wraps_internal_payload(monkeypatch):
    """The internal glance facade exposes only the payload body to callers."""
    seen = {}

    def fake(store, *, days_raw):
        seen.update(store=store, days_raw=days_raw)
        return {"ok": True, "glance": {"weather": {"available": True}}}

    monkeypatch.setattr(perception_core, "perception_glance_payload", fake)

    result = cap_perc.glance("STORE", params={"days": 30})

    assert result.ok is True
    assert result.data == {"glance": {"weather": {"available": True}}}
    assert seen == {"store": "STORE", "days_raw": 30}


def test_perception_glance_is_internal_not_model_callable():
    """Registry-only actions must not become callable by the model."""
    assert "perception_glance" in registry.CAPABILITIES
    assert "perception_glance" not in {spec.name for spec in tool_schema.build_tool_specs()}


def test_glance_payload_projects_permission_gated_signals(monkeypatch):
    """Disabled signals from the authorized snapshot cannot surface in a glance."""
    monkeypatch.setattr(perception_core, "agent_perception_payload", lambda store, *, signals_raw: {
        "ok": True,
        "signals": {
            "steps": {"disabled": True, "reason": "switch_off"},
            "weather": {"temperature": 22.0},
        },
    })
    monkeypatch.setattr(perception_core.perception_store, "list_perception_daily",
                        lambda uid, signal, days: [])

    result = perception_core.perception_glance_payload(
        type("S", (), {"user_id": "u"})(), days_raw="30")

    assert result == {
        "ok": True,
        "glance": {"weather": {"available": True, "notable_change": False}},
    }


def test_glance_payload_reuses_existing_notable_changes(monkeypatch):
    """The glance maps the shared health-history change signal to health."""
    monkeypatch.setattr(perception_core, "agent_perception_payload", lambda store, *, signals_raw: {
        "ok": True, "signals": {"steps": {"step_count": 10}}
    })
    monkeypatch.setattr(perception_core.perception_store, "list_perception_daily",
                        lambda uid, signal, days: [{"doc": {}}])
    monkeypatch.setattr(perception_core.perception_history, "notable_changes",
                        lambda rows, max_changes: [{"signal": "health_vitals"}])

    result = perception_core.perception_glance_payload(
        type("S", (), {"user_id": "u"})(), days_raw=None)

    assert result["glance"] == {"health": {"available": True, "notable_change": True}}
