"""Fix B2: capability sinks honor CapabilityResult retryable — a non-retryable
(4xx) failure raises EffectTerminalError (→ discard); a retryable one raises a
plain RuntimeError (→ retry). effect_sink_release still runs on failure."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
from capabilities.types import err as cap_err
from model_api_runtime.v2 import serve_worker as sw


def _wire(monkeypatch, result):
    monkeypatch.setattr(sw.db, "effect_sink_claim", lambda eid: True)
    released = []
    monkeypatch.setattr(sw.db, "effect_sink_release", lambda eid: released.append(eid))
    monkeypatch.setattr(sw.db, "effect_sink_complete", lambda eid: None)
    monkeypatch.setattr(sw.core_store, "get_store", lambda uid: object())
    monkeypatch.setattr(sw.cap_registry, "run_capability", lambda *a, **k: result)
    return released


def test_non_retryable_capability_failure_is_terminal(monkeypatch):
    released = _wire(monkeypatch, cap_err("conflict", "identity_not_initialized", retryable=False))
    with pytest.raises(db.EffectTerminalError):
        sw._sink_identity("u1", {"effect_id": "e1", "self_introduction": "hi"}, runtime_token="t")
    assert released == ["e1"]   # claim released so the discard path is clean


def test_retryable_capability_failure_is_plain_runtime(monkeypatch):
    released = _wire(monkeypatch, cap_err("upstream", "enclave 503", retryable=True))
    with pytest.raises(RuntimeError) as ei:
        sw._sink_identity("u1", {"effect_id": "e2", "self_introduction": "hi"}, runtime_token="t")
    assert not isinstance(ei.value, db.EffectTerminalError)   # keeps the retry path
    assert released == ["e2"]


def test_capability_success_completes(monkeypatch):
    from capabilities.types import ok as cap_ok
    completed = []
    monkeypatch.setattr(sw.db, "effect_sink_claim", lambda eid: True)
    monkeypatch.setattr(sw.db, "effect_sink_release", lambda eid: pytest.fail("no release on success"))
    monkeypatch.setattr(sw.db, "effect_sink_complete", lambda eid: completed.append(eid))
    monkeypatch.setattr(sw.core_store, "get_store", lambda uid: object())
    monkeypatch.setattr(sw.cap_registry, "run_capability", lambda *a, **k: cap_ok({"status": "ok"}))
    sw._sink_identity("u1", {"effect_id": "e3", "self_introduction": "hi"}, runtime_token="t")
    assert completed == ["e3"]


def test_self_wake_guard_consumes_rejected_effect_without_timer(monkeypatch):
    completed = []
    monkeypatch.setattr(sw.db, "effect_sink_claim", lambda _eid: True)
    monkeypatch.setattr(sw.db, "effect_sink_release", lambda _eid: pytest.fail(
        "guard rejection is a completed no-op"
    ))
    monkeypatch.setattr(sw.db, "effect_sink_complete", completed.append)
    monkeypatch.setattr(
        sw.jobs_store,
        "reserve_self_wake",
        lambda *_args, **_kwargs: {
            "accepted": False,
            "streak": 3,
            "reason": "self_wake_loop_guard",
        },
    )
    monkeypatch.setattr(
        sw.core_store,
        "get_store",
        lambda _uid: pytest.fail("rejected self-wake must not load a store"),
    )
    monkeypatch.setattr(
        sw.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: pytest.fail(
            "rejected self-wake must not create a timer"
        ),
    )

    sw._sink_schedule(
        "u_self_wake",
        {
            "effect_id": "e-self-4",
            "op": "schedule_wake",
            "at": "2026-07-26T10:00:00Z",
            "_self_wake": True,
        },
    )

    assert completed == ["e-self-4"]


def test_only_marked_wake_schedule_reserves_streak(monkeypatch):
    from capabilities.types import ok as cap_ok

    reserved = []
    calls = []
    completed = []
    monkeypatch.setattr(sw.db, "effect_sink_claim", lambda _eid: True)
    monkeypatch.setattr(sw.db, "effect_sink_release", lambda _eid: pytest.fail(
        "successful schedule must not release"
    ))
    monkeypatch.setattr(sw.db, "effect_sink_complete", completed.append)
    monkeypatch.setattr(sw.core_store, "get_store", lambda _uid: object())

    def reserve(*_args, **kwargs):
        reserved.append(kwargs)
        return {"accepted": True, "streak": 1, "reason": ""}

    def run_capability(name, _store, **kwargs):
        calls.append((name, kwargs["params"]))
        return cap_ok({"status": "ok"})

    monkeypatch.setattr(sw.jobs_store, "reserve_self_wake", reserve)
    monkeypatch.setattr(sw.cap_registry, "run_capability", run_capability)

    sw._sink_schedule(
        "u_self_wake",
        {
            "effect_id": "e-marked",
            "op": "schedule_wake",
            "at": "2026-07-26T10:00:00Z",
            "_self_wake": True,
        },
    )
    sw._sink_schedule(
        "u_self_wake",
        {
            "effect_id": "e-chat",
            "op": "schedule_wake",
            "at": "2026-07-26T11:00:00Z",
        },
    )
    sw._sink_schedule(
        "u_self_wake",
        {
            "effect_id": "e-cancel",
            "op": "cancel_wake",
            "wake_id": "wake-1",
        },
    )

    assert reserved == [{
        "effect_id": "e-marked",
        "max_consecutive": sw._MAX_CONSECUTIVE_SELF_WAKES,
    }]
    assert calls == [
        (
            "schedule_wake",
            {"at": "2026-07-26T10:00:00Z", "_self_wake": True},
        ),
        ("schedule_wake", {"at": "2026-07-26T11:00:00Z"}),
        ("cancel_wake", {"wake_id": "wake-1"}),
    ]
    assert completed == ["e-marked", "e-chat", "e-cancel"]
