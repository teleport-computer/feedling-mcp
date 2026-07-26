from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest  # noqa: E402
from core import store as core_store  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


def _env(user_id: str, message_id: str) -> dict:
    return {
        "v": 1,
        "id": message_id,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "key",
        "K_enclave": "enclave-key",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def test_turn_activity_route_reads_only_the_authenticated_v2_job(client, backend_env):
    conftest.seed_user("usr_activity_a")
    conftest.set_v2_runtime_owner("usr_activity_a")
    key_a = conftest.seed_api_key("usr_activity_a")
    job_id, _ = jobs_store.enqueue_job(
        "usr_activity_a", "chat", reason="chat_send", trace_id="turn-a"
    )
    jobs_store.append_status_event(
        "usr_activity_a",
        "tool_activity",
        job_id=job_id,
        label="memory_search",
        detail={
            "activity_id": f"{job_id}:1:1",
            "tool_name": "memory_search",
            "call_id": "call-a",
            "state": "success",
            "result_code": "ok",
        },
    )

    conftest.seed_user("usr_activity_b")
    conftest.set_v2_runtime_owner("usr_activity_b")
    key_b = conftest.seed_api_key("usr_activity_b")

    response = client.get(
        "/v1/chat/turn-activity/turn-a",
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert response.status_code == 200, response.text
    body = response.json
    assert body["runtime"] == "v2"
    assert body["jobs"] == [{"job_id": str(job_id), "status": "pending"}]
    assert body["events"][0]["call_id"] == "call-a"
    assert body["events"][0]["result_code"] == "ok"

    hidden = client.get(
        "/v1/chat/turn-activity/turn-a",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert hidden.status_code == 404
    assert hidden.json["error"] == "turn_activity_not_found"


def test_turn_activity_route_rejects_invalid_turn_id(client, backend_env):
    conftest.seed_user("usr_activity_invalid")
    key = conftest.seed_api_key("usr_activity_invalid")
    response = client.get(
        "/v1/chat/turn-activity/%20",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 400
    assert response.json["error"] == "invalid_turn_id"


def test_turn_activity_route_accepts_and_reads_v1_resident_events(client, backend_env):
    user_id = "usr_activity_v1"
    conftest.seed_user(user_id)
    key = conftest.seed_api_key(user_id)
    store = core_store.get_store(user_id)
    store.append_chat("user", "chat", _env(user_id, "turn-v1"))
    headers = {"Authorization": f"Bearer {key}"}

    running = client.post(
        "/v1/chat/turn-activity/turn-v1/events",
        headers=headers,
        json={
            "activity_id": "v1:call-1",
            "call_id": "v1:call-1",
            "tool_name": "memory_search",
            "state": "running",
        },
    )
    assert running.status_code == 200, running.text
    finished = client.post(
        "/v1/chat/turn-activity/turn-v1/events",
        headers=headers,
        json={
            "activity_id": "v1:call-1",
            "call_id": "v1:call-1",
            "tool_name": "memory_search",
            "state": "success",
            "result_code": "ok",
            "memory_count": 4,
            "memory_categories": [
                {"key": "relationship", "count": 3},
                {"key": "family", "count": 1},
            ],
        },
    )
    assert finished.status_code == 200, finished.text

    response = client.get(
        "/v1/chat/turn-activity/turn-v1", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json
    assert body["runtime"] == "v1"
    assert body["complete"] is False
    assert body["phase"] == "processing"
    assert body["events"][0]["status"] == "success"
    assert body["events"][0]["memory_categories"] == [
        {"key": "relationship", "count": 3},
        {"key": "family", "count": 1},
    ]


def test_v1_activity_event_rejects_v2_owned_turn(client, backend_env):
    user_id = "usr_activity_v2_reject"
    conftest.seed_user(user_id)
    key = conftest.seed_api_key(user_id)
    store = core_store.get_store(user_id)
    store.append_chat("user", "chat", _env(user_id, "turn-v2-reject"))
    conftest.set_v2_runtime_owner(user_id)

    response = client.post(
        "/v1/chat/turn-activity/turn-v2-reject/events",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "activity_id": "v1:call-1",
            "tool_name": "memory_search",
            "state": "running",
        },
    )
    assert response.status_code == 409
    assert response.json["error"] == "activity_event_rejected"
