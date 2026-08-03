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
from model_api_runtime.v2 import serve_worker, worker


@pytest.fixture(autouse=True)
def _stub_store(monkeypatch):
    monkeypatch.setattr(serve_worker.core_store, "get_store",
                        lambda uid: types.SimpleNamespace(user_id=uid))


def _stub_actions(monkeypatch, body, status):
    monkeypatch.setattr(
        serve_worker.memory_core,
        "actions",
        lambda store, api_key, payload, **kwargs: (body, status),
    )


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


def test_more_than_twenty_actions_are_chunked_without_truncation(monkeypatch):
    seen: list[list[dict]] = []

    def _actions(store, api_key, payload, **kwargs):
        batch = list(payload["actions"])
        seen.append(batch)
        return ({
            "status": "ok",
            "results": [{"status": "ok"} for _action in batch],
            "effects": [{"type": "memory_superseded"} for _action in batch],
            "total_count": len(batch),
            "applied_count": len(batch),
            "skipped_count": 0,
            "failed_count": 0,
        }, 200)

    monkeypatch.setattr(serve_worker.memory_core, "actions", _actions)
    actions = [{"type": "memory.supersede", "memory_id": str(index)} for index in range(45)]

    body = serve_worker._apply_memory_actions("u1", actions)

    assert [len(batch) for batch in seen] == [20, 20, 5]
    assert body["total_count"] == body["applied_count"] == 45
    assert len(body["results"]) == len(body["effects"]) == 45


def test_partial_200_returns_item_details_instead_of_raising(monkeypatch, caplog):
    body = {
        "status": "partial",
        "results": [
            {"status": "error", "error": "not_found", "http_status": 404},
            {"status": "ok", "action": "memory.add", "http_status": 200},
        ],
        "effects": [{"type": "memory_added", "memory_id": "mem_ok"}],
        "applied_count": 1,
        "skipped_count": 0,
        "failed_count": 1,
    }
    _stub_actions(monkeypatch, body, 200)

    assert serve_worker._apply_memory_actions(
        "u1",
        [{"type": "memory.supersede"}, {"type": "memory.add"}],
    ) == body
    assert "memory batch had item failures" in caplog.text


def test_v2_worker_counts_partial_results_without_failing_successful_items():
    actions = [{"type": "memory.add"}, {"type": "memory.add"}]
    result = {
        "status": "partial",
        "results": [
            {"status": "error", "error": "source_invalid", "http_status": 400},
            {"status": "ok", "action": "memory.add", "http_status": 200},
        ],
    }
    assert worker._memory_write_result_counts(actions, result) == (
        1, 0, 1, "source_invalid"
    )


def test_v2_all_failed_400_body_survives_apply_and_drives_f5_counts(monkeypatch):
    actions = [
        {"type": "memory.add"},
        {"type": "memory.delete", "memory_id": "missing"},
    ]
    body = {
        "status": "failed",
        "error": "anchor_required",
        "detail": {"mem_type": "insight"},
        "results": [
            {
                "status": "error",
                "error": "anchor_required",
                "detail": {"mem_type": "insight"},
                "http_status": 400,
            },
            {"status": "error", "error": "not_found", "http_status": 404},
        ],
        "effects": [],
        "total_count": 2,
        "applied_count": 0,
        "skipped_count": 0,
        "failed_count": 2,
    }
    _stub_actions(monkeypatch, body, 400)

    write_result = serve_worker._apply_memory_actions("u1", actions)

    assert write_result is body
    assert worker._memory_write_result_counts(actions, write_result) == (
        0, 0, 2, "anchor_required"
    )
