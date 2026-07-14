"""serve_worker._apply_memory_actions must NOT fail a chat turn on a permanent
(4xx) memory-action rejection — a tool-initiated background memory write that
hits e.g. not_found (the model re-referenced an already-deleted/updated card in
a multi-round turn) is dropped+logged, not raised. Only transient (5xx) failures
raise so the outbox retries.

Regression: before this, ANY status>=400 raised, and the raise propagated
through `_sink_memory` -> on_reply's awaited `apply_pending_effects`, turning the
whole turn into turn_failed:runtimeerror EVEN AFTER the intended write committed
(verified live on pre: an explicit-id delete removed the card but the turn still
failed).
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import serve_worker


@pytest.fixture(autouse=True)
def _stub_store(monkeypatch):
    monkeypatch.setattr(serve_worker.core_store, "get_store",
                        lambda uid: types.SimpleNamespace(user_id=uid))


def _stub_actions(monkeypatch, body, status):
    monkeypatch.setattr(serve_worker.memory_core, "actions", lambda store, api_key, payload: (body, status))


def test_4xx_not_found_is_dropped_not_raised(monkeypatch, caplog):
    _stub_actions(monkeypatch, {"status": "error", "error": "not_found"}, 404)
    # Must not raise — a permanent rejection can't take down the user's turn.
    out = serve_worker._apply_memory_actions("u1", [{"type": "memory.delete", "memory_id": "gone"}])
    assert out == {"status": "error", "error": "not_found"}


def test_4xx_validation_reject_is_dropped_not_raised(monkeypatch):
    _stub_actions(monkeypatch, {"status": "error", "error": "title_required"}, 400)
    serve_worker._apply_memory_actions("u1", [{"type": "memory.add", "memory": {}}])  # no raise


def test_5xx_transient_raises_for_retry(monkeypatch):
    _stub_actions(monkeypatch, {"error": "db down"}, 503)
    with pytest.raises(RuntimeError, match="memory_actions_failed"):
        serve_worker._apply_memory_actions("u1", [{"type": "memory.add", "memory": {"summary": "s", "content": "c"}}])


def test_success_returns_body(monkeypatch):
    _stub_actions(monkeypatch, {"status": "ok", "results": []}, 200)
    assert serve_worker._apply_memory_actions("u1", [{"type": "memory.add"}]) == {"status": "ok", "results": []}
