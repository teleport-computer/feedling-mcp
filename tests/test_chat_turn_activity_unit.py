from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import activity_core  # noqa: E402


def test_read_turn_activity_scopes_query_to_authenticated_store(monkeypatch):
    seen = {}

    def rows(user_id, turn_id):
        seen.update(user_id=user_id, turn_id=turn_id)
        return (
            [{"id": 8, "status": "running"}],
            [
                {
                    "id": 1,
                    "job_id": 8,
                    "kind": "tool_activity",
                    "created_at": 10.0,
                    "detail_json": {
                        "activity_id": "8:1:1",
                        "tool_name": "memory_search",
                        "call_id": "call-8",
                        "state": "running",
                    },
                }
            ],
        )

    monkeypatch.setattr(activity_core.jobs_store, "chat_turn_activity_rows", rows)
    body, status = activity_core.read_turn_activity(
        SimpleNamespace(user_id="usr_owner"), "turn-1"
    )
    assert status == 200
    assert seen == {"user_id": "usr_owner", "turn_id": "turn-1"}
    assert body["runtime"] == "v2"
    assert body["events"][0]["call_id"] == "call-8"


def test_read_turn_activity_rejects_invalid_id_without_query(monkeypatch):
    monkeypatch.setattr(
        activity_core.jobs_store,
        "chat_turn_activity_rows",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not query")),
    )
    body, status = activity_core.read_turn_activity(
        SimpleNamespace(user_id="usr_owner"), "bad turn/id"
    )
    assert (body, status) == ({"error": "invalid_turn_id"}, 400)


def test_read_turn_activity_hides_missing_or_other_user_turns(monkeypatch):
    monkeypatch.setattr(
        activity_core.jobs_store, "chat_turn_activity_rows", lambda *_args: ([], [])
    )
    monkeypatch.setattr(
        activity_core.activity_store, "resident_turn_rows", lambda *_args: (None, [])
    )
    body, status = activity_core.read_turn_activity(
        SimpleNamespace(user_id="usr_owner"), "turn-other"
    )
    assert (body, status) == ({"error": "turn_activity_not_found"}, 404)


def test_read_turn_activity_falls_back_to_v1_resident_rows(monkeypatch):
    monkeypatch.setattr(
        activity_core.jobs_store, "chat_turn_activity_rows", lambda *_args: ([], [])
    )
    monkeypatch.setattr(
        activity_core.activity_store,
        "resident_turn_rows",
        lambda *_args: (
            {"role": "user", "reply_status": "replied", "reply_message_id": "reply-1"},
            [{
                "id": 1,
                "job_id": None,
                "kind": "tool_activity",
                "created_at": 10.0,
                "detail_json": {
                    "activity_id": "v1:call-1",
                    "tool_name": "memory_search",
                    "call_id": "v1:call-1",
                    "state": "success",
                    "memory_count": 4,
                    "memory_categories": [
                        {"key": "relationship", "count": 3},
                        {"key": "family", "count": 1},
                    ],
                },
            }],
        ),
    )

    body, status = activity_core.read_turn_activity(
        SimpleNamespace(user_id="usr_owner"), "turn-v1"
    )

    assert status == 200
    assert body["runtime"] == "v1"
    assert body["complete"] is True
    assert body["phase"] == "done"
    assert body["events"][0]["memory_count"] == 4


def test_write_turn_activity_projects_only_safe_metadata(monkeypatch):
    captured = {}

    def append(user_id, turn_id, **kwargs):
        captured.update(user_id=user_id, turn_id=turn_id, **kwargs)
        return 9, True

    monkeypatch.setattr(
        activity_core.activity_store, "append_resident_tool_event", append
    )
    body, status = activity_core.write_turn_activity(
        SimpleNamespace(user_id="usr_owner"),
        "turn-v1",
        {
            "activity_id": "v1:call-1",
            "tool_name": "memory_search",
            "state": "success",
            "duration_ms": 12.5,
            "result_code": "ok",
            "memory_count": 4,
            "memory_categories": [
                {"key": "relationship", "count": 3},
                {"key": "family", "count": 1},
            ],
        },
    )

    assert (body, status) == ({"status": "ok", "event_id": 9, "inserted": True}, 200)
    assert captured["detail"]["memory_count"] == 4
    assert captured["detail"]["memory_categories"][0] == {
        "key": "relationship", "count": 3,
    }
    assert "private" not in repr(captured)

    rejected, rejected_status = activity_core.write_turn_activity(
        SimpleNamespace(user_id="usr_owner"),
        "turn-v1",
        {
            "activity_id": "v1:call-2",
            "tool_name": "memory_fetch",
            "state": "success",
            "result_body": "private memory body",
        },
    )
    assert (rejected, rejected_status) == ({"error": "invalid_activity_event"}, 400)
