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
    body, status = activity_core.read_turn_activity(
        SimpleNamespace(user_id="usr_owner"), "turn-other"
    )
    assert (body, status) == ({"error": "turn_activity_not_found"}, 404)
