import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import inspect

from proactive import capture_scheduler, dream_scheduler


def test_both_ticks_accept_an_optional_submit_kwarg():
    for fn in (capture_scheduler.tick_quiet_capture, dream_scheduler.tick_memory_dream):
        params = inspect.signature(fn).parameters
        assert "submit" in params, f"{fn.__name__} needs a submit seam"
        assert params["submit"].default is None, f"{fn.__name__}: submit must default to None"
        assert params["submit"].kind is inspect.Parameter.KEYWORD_ONLY


def test_capture_submit_replaces_the_legacy_enqueue(monkeypatch):
    """The shared gate/window seam must never call the legacy job writer."""
    legacy_called = {"n": 0}
    monkeypatch.setattr(
        capture_scheduler.capture_jobs,
        "enqueue_memory_capture_job",
        lambda *a, **k: legacy_called.update(n=legacy_called["n"] + 1) or
        (None, False, "forbidden"),
    )

    seen = {}

    def _submit(store, *, trigger, now, window, capture_key):
        seen["trigger"] = trigger
        seen["window"] = window
        seen["capture_key"] = capture_key
        return {"enqueued": True, "reason": "v2", "job": {"id": "j1"}}

    # Drive the gate straight to its enqueue point by stubbing the state it reads.
    monkeypatch.setattr(capture_scheduler, "refresh_capture_state_from_chat",
                        lambda store, now=None: {"last_seen_message_id": "m9",
                                                 "message_count": 3, "last_seen_ts": 0.0,
                                                 "last_captured_until_message_id": ""})
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda store: True)
    monkeypatch.setattr(capture_scheduler, "quiet_sec", lambda: 0)
    monkeypatch.setattr(
        capture_scheduler,
        "_patch_capture_state",
        lambda _store, state, **_kwargs: dict(state),
    )

    out = capture_scheduler.tick_quiet_capture(object(), now=1000.0, submit=_submit)

    assert legacy_called["n"] == 0
    assert out["enqueued"] is True and out["reason"] == "v2"
    assert seen["trigger"] == "quiet_timeout"


def test_capture_without_submit_still_uses_the_legacy_enqueue(monkeypatch):
    """Zero drift: the resident path must be byte-for-byte unchanged."""
    called = {"n": 0}
    monkeypatch.setattr(
        capture_scheduler, "_enqueue_window",
        lambda store, *, trigger, now: called.update(n=called["n"] + 1) or
        {"enqueued": True, "reason": "legacy", "job": None})
    monkeypatch.setattr(capture_scheduler, "refresh_capture_state_from_chat",
                        lambda store, now=None: {"last_seen_message_id": "m9",
                                                 "message_count": 3, "last_seen_ts": 0.0,
                                                 "last_captured_until_message_id": ""})
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda store: True)
    monkeypatch.setattr(capture_scheduler, "quiet_sec", lambda: 0)

    out = capture_scheduler.tick_quiet_capture(object(), now=1000.0)
    assert called["n"] == 1
    assert out["reason"] == "legacy"


def test_capture_gate_still_blocks_before_reaching_submit(monkeypatch):
    """A blocked gate must never call submit — zero pre-activation burn."""
    submitted = {"n": 0}
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda store: False)
    monkeypatch.setattr(capture_scheduler, "refresh_capture_state_from_chat",
                        lambda store, now=None: {})

    out = capture_scheduler.tick_quiet_capture(
        object(), now=1000.0, submit=lambda *a, **k: submitted.update(n=1) or {})
    assert submitted["n"] == 0
    assert out["enqueued"] is False and out["reason"] == "capture_disabled"
