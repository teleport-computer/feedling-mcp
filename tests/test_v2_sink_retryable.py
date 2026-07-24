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
