import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from capabilities import registry  # noqa: E402
from capabilities import memory as cap_memory  # noqa: E402
from capabilities.types import ok  # noqa: E402


def test_all_action_types_registered():
    expected = {
        "identity_get", "identity_patch", "identity_nudge", "memory_index", "memory_fetch", "memory_write",
        "memory_search",
        "perception_snapshot", "perception_trend", "perception_history", "perception_glance",
        "screen_recent", "screen_read", "photo_recent", "photo_read", "chat_image_read",
        "chat_file_read",
        "web_search", "web_fetch",
        "schedule_wake", "cancel_wake",
        "workspace_list", "workspace_read", "workspace_write", "workspace_delete",
    }
    assert set(registry.CAPABILITIES) == expected
    assert registry.WRITE_ACTIONS == frozenset({
        "memory_write", "identity_patch", "identity_nudge", "schedule_wake", "cancel_wake",
        "workspace_write", "workspace_delete"})
    assert "memory_index" in registry.READ_ACTIONS


def test_run_capability_dispatches(monkeypatch):
    monkeypatch.setattr(cap_memory, "index",
                        lambda store, **kw: ok({"items": [1]}))
    r = registry.run_capability("memory_index", "STORE", params={"limit": 1})
    assert r.ok is True and r.data == {"items": [1]}


def test_run_capability_unknown():
    r = registry.run_capability("does_not_exist", "STORE")
    assert r.ok is False and r.error["code"] == "capability_invalid_input"


def test_capabilities_is_a_real_populated_dict():
    assert len(registry.CAPABILITIES) == 25
    assert set(registry.CAPABILITIES.keys()) == {
        "identity_get", "identity_patch", "identity_nudge", "memory_index", "memory_fetch", "memory_write",
        "memory_search",
        "perception_snapshot", "perception_trend", "perception_history", "perception_glance",
        "screen_recent", "screen_read", "photo_recent", "photo_read", "chat_image_read",
        "chat_file_read",
        "web_search", "web_fetch",
        "schedule_wake", "cancel_wake",
        "workspace_list", "workspace_read", "workspace_write", "workspace_delete",
    }
    assert len(list(registry.CAPABILITIES.items())) == 25
    assert bool(registry.CAPABILITIES) is True
