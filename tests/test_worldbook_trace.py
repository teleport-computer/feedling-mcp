from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track  # noqa: E402
from worldbook import worldbook_core  # noqa: E402


_WRITE_EVENT_KEYS = frozenset({
    "subsystem", "type", "actor", "status", "summary", "explain",
    "trace_id", "turn_id", "detail",
})
_WRITE_DETAIL_KEYS = frozenset({"operation", "outcome", "reason", "counts"})
_MATCH_EVENT_KEYS = _WRITE_EVENT_KEYS | {"job_id"}
_MATCH_DETAIL_KEYS = frozenset({
    "operation", "outcome", "reason", "lane", "counts",
})
_MATCH_COUNT_KEYS = frozenset({
    "candidates", "matched", "rejected", "unavailable", "messages",
    "block_chars",
})
_INJECTED_EVENT_KEYS = frozenset({
    "subsystem", "type", "actor", "summary", "trace_id", "turn_id",
    "job_id", "detail",
})


def _assert_closed_write_event(event):
    assert set(event) == _WRITE_EVENT_KEYS
    assert set(event["detail"]) == _WRITE_DETAIL_KEYS
    assert set(event["detail"]["counts"]) == {"entries"}


def _assert_closed_match_event(event):
    assert set(event) == _MATCH_EVENT_KEYS
    assert set(event["detail"]) == _MATCH_DETAIL_KEYS
    assert set(event["detail"]["counts"]) == _MATCH_COUNT_KEYS


def _assert_closed_legacy_injected_event(event):
    assert set(event) == _INJECTED_EVENT_KEYS
    assert set(event["detail"]) == {"counts"}
    assert set(event["detail"]["counts"]) == {"matched"}


class _Store:
    def __init__(self, rows=None):
        self.user_id = "u-worldbook-trace"
        self.world_books = list(rows or [])
        self.world_books_lock = threading.Lock()

    def upsert_world_book(self, record):
        self.world_books.append(dict(record))
        return record


def _plaintext_row(*, keyword="secret-keyword", content="private lore"):
    return {
        "id": "private-entry-id",
        "owner_user_id": "u-worldbook-trace",
        "visibility": "shared",
        "body": json.dumps({
            "id": "private-entry-id",
            "name": "private entry name",
            "keywords": [keyword],
            "content": content,
            "enabled": True,
        }),
    }


def _sealed_row():
    return {
        "v": 1,
        "id": "private-entry-id",
        "body_ct": "private-ciphertext",
        "nonce": "private-nonce",
        "K_user": "private-user-key",
        "K_enclave": "private-enclave-key",
        "visibility": "shared",
        "owner_user_id": "u-worldbook-trace",
        "enclave_pk_fpr": "private-fingerprint",
    }


def _capture(monkeypatch):
    events = []
    monkeypatch.setattr(
        worldbook_core.debug_trace,
        "trace_event",
        lambda _store, **event: events.append(event),
    )
    return events


def test_upsert_trace_distinguishes_committed_write_without_entry_plaintext(
    monkeypatch,
):
    events = _capture(monkeypatch)
    store = _Store()
    monkeypatch.delenv("FEEDLING_ENCLAVE_URL", raising=False)

    body, status = worldbook_core.upsert(
        store,
        _sealed_row(),
        api_key=None,
        runtime_token=None,
        trace_id="trace-write",
    )

    assert status == 200
    assert body == {"id": "private-entry-id"}
    event = events[-1]
    _assert_closed_write_event(event)
    assert event["type"] == "worldbook.entry.write.completed"
    assert event["trace_id"] == "trace-write"
    assert event["detail"] == {
        "operation": "upsert",
        "outcome": "stored",
        "reason": "committed",
        "counts": {"entries": 1},
    }
    assert "private-entry-id" not in repr(event)
    assert "private-ciphertext" not in repr(event)


def test_match_trace_distinguishes_no_entries_from_no_match(monkeypatch):
    events = _capture(monkeypatch)
    empty = _Store()
    worldbook_core.match(
        empty,
        {"message": "private query"},
        api_key=None,
        runtime_token=None,
        trace_id="trace-empty",
        lane="chat",
    )
    _assert_closed_match_event(events[-1])
    assert events[-1]["detail"]["outcome"] == "no_entries"
    assert events[-1]["detail"]["counts"] == {
        "candidates": 0,
        "matched": 0,
        "rejected": 0,
        "unavailable": 0,
        "messages": 1,
        "block_chars": 0,
    }

    store = _Store([_plaintext_row()])
    body, status = worldbook_core.match(
        store,
        {"message": "unrelated private query"},
        api_key=None,
        runtime_token=None,
        trace_id="trace-no-match",
        lane="chat",
    )
    assert status == 200
    assert body["block"] == ""
    event = events[-1]
    _assert_closed_match_event(event)
    assert event["type"] == "worldbook.match.completed"
    assert event["trace_id"] == "trace-no-match"
    assert event["detail"]["outcome"] == "no_match"
    assert event["detail"]["counts"]["candidates"] == 1
    assert "private query" not in repr(event)
    assert "private entry name" not in repr(event)


def test_upsert_storage_failure_has_distinct_trace_and_no_plaintext(monkeypatch):
    events = _capture(monkeypatch)
    store = _Store()
    store.upsert_world_book = lambda _record: (_ for _ in ()).throw(
        RuntimeError("private database failure")
    )
    monkeypatch.delenv("FEEDLING_ENCLAVE_URL", raising=False)

    body, status = worldbook_core.upsert(
        store,
        _sealed_row(),
        api_key=None,
        runtime_token=None,
        trace_id="trace-write-failed",
    )

    assert status == 500
    assert body == {"error": "worldbook_write_failed"}
    event = events[-1]
    _assert_closed_write_event(event)
    assert event["detail"] == {
        "operation": "upsert",
        "outcome": "failed",
        "reason": "storage_error",
        "counts": {"entries": 1},
    }
    assert "private database failure" not in repr(event)
    assert "private-ciphertext" not in repr(event)


def test_match_trace_has_closed_content_free_schema_for_hit_and_legacy_event(
    monkeypatch,
):
    events = _capture(monkeypatch)
    store = _Store([_plaintext_row()])

    body, status = worldbook_core.match(
        store,
        {"message": "the secret-keyword appeared"},
        api_key=None,
        runtime_token=None,
        trace_id="trace-hit",
        job_id="job-hit",
        lane="scheduled",
        actor="host_agent_runtime",
    )

    assert status == 200
    assert "private lore" in body["block"]
    assert [event["type"] for event in events] == [
        "worldbook.match.completed",
        "worldbook_injected",
    ]
    match_event, injected_event = events
    _assert_closed_match_event(match_event)
    _assert_closed_legacy_injected_event(injected_event)
    assert match_event["actor"] == "host_agent_runtime"
    assert match_event["job_id"] == "job-hit"
    assert match_event["detail"]["outcome"] == "matched"
    assert match_event["detail"]["counts"]["matched"] == 1


def test_worldbook_admin_projection_exposes_only_closed_enums_and_counts():
    event = {
        "type": "worldbook.match.completed",
        "detail": {
            "operation": "match",
            "outcome": "matched",
            "reason": "",
            "lane": "chat",
            "counts": {"matched": 1},
            "name": "private entry name",
        },
    }
    public = data_track._debug_event_public_json(event)["detail"]
    assert public == {
        "operation": "match",
        "outcome": "matched",
        "reason": "",
        "lane": "chat",
        "counts": {"matched": 1},
        "name": "<redacted string len=18>",
    }

    forged = {
        **event,
        "detail": {
            **event["detail"],
            "outcome": "private outcome",
            "lane": "private lane",
        },
    }
    forged_public = data_track._debug_event_public_json(forged)["detail"]
    assert forged_public["outcome"] == "<redacted string len=15>"
    assert forged_public["lane"] == "<redacted string len=12>"
