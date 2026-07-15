import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from capabilities import registry as cap_registry
from capabilities import wake


class _Store:
    user_id = "u_cap_wake"


def test_schedule_requires_at():
    res = wake.schedule(_Store(), params={}).to_dict()
    assert res["ok"] is False
    assert "at" in res["error"]["message"]


def test_cancel_requires_wake_id():
    res = wake.cancel(_Store(), params={}).to_dict()
    assert res["ok"] is False


def test_schedule_forwards_the_action_shape_apply_turn_actions_expects(monkeypatch):
    seen = {}

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def apply_turn_actions(self, user_id, actions, **kw):
            seen["user_id"] = user_id
            seen["actions"] = list(actions)
            seen["submit_wake"] = kw.get("submit_wake")
            return ()

    monkeypatch.setattr("proactive.scheduled_wake_v2.ScheduledWakeServiceV2", _FakeService)
    res = wake.schedule(_Store(), params={"at": "2026-07-11T18:00", "tz": "Asia/Shanghai",
                                          "reason": "check in"}).to_dict()
    assert res["ok"] is True
    assert seen["user_id"] == "u_cap_wake"
    assert seen["actions"] == [{"type": "schedule_wake", "at": "2026-07-11T18:00",
                                "tz": "Asia/Shanghai", "reason": "check in"}]


def test_submit_wake_does_not_enqueue(monkeypatch):
    """Layering: capabilities/* must not import model_api_runtime.v2.jobs_store (that would
    invert the dependency direction and cycle). The timer is persisted; the scheduler picks
    it up within one tick. So submit_wake must accept WITHOUT enqueueing."""
    captured = {}

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def apply_turn_actions(self, user_id, actions, **kw):
            captured["decision"] = kw["submit_wake"](object())
            return ()

    monkeypatch.setattr("proactive.scheduled_wake_v2.ScheduledWakeServiceV2", _FakeService)
    wake.schedule(_Store(), params={"at": "2026-07-11T18:00"})
    assert captured["decision"].accepted is True


def test_registered_as_write_capabilities():
    assert "schedule_wake" in cap_registry.CAPABILITIES
    assert "cancel_wake" in cap_registry.CAPABILITIES
    assert {"schedule_wake", "cancel_wake"} <= cap_registry.WRITE_ACTIONS


def test_unified_tool_catalog_offers_the_registered_wake_capabilities():
    """The model-visible vocabulary comes from the same capability registry the
    executor dispatches; there is no separate planner vocabulary to drift."""
    from capabilities import tool_schema

    specs = {spec.name: spec for spec in tool_schema.build_tool_specs()}
    assert {"schedule_wake", "cancel_wake"} <= specs.keys()
    assert "at" in specs["schedule_wake"].parameters["properties"]
    assert "wake_id" in specs["cancel_wake"].parameters["properties"]


def test_wake_capability_does_not_import_model_api_runtime():
    import pathlib
    src = pathlib.Path(wake.__file__).read_text()
    for forbidden in ("model_api_runtime", "import hosted", "from hosted", "agent_runtime"):
        assert forbidden not in src, f"capabilities/wake.py must not reference {forbidden}"
