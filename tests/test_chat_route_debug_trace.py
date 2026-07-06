from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import debug_trace  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    debug_trace._flag_cache.clear()
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c
    debug_trace._flag_cache.clear()


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _env(user_id: str, marker: str = "msg") -> dict:
    return {
        "v": 1,
        "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def _enable_trace(client, api_key: str) -> None:
    res = client.post("/v1/debug/trace/enable", headers=_headers(api_key), json={"enabled": True})
    assert res.status_code == 200, res.get_data(as_text=True)


def _route_events(client, api_key: str) -> list[dict]:
    res = client.get("/v1/debug/trace?subsystem=route", headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()["events"]


def test_resident_chat_message_and_poll_emit_route_trace(client):
    user_id, api_key = _register(client)
    _enable_trace(client, api_key)

    msg = client.post(
        "/v1/chat/message",
        headers=_headers(api_key),
        json={"envelope": _env(user_id, "user-msg")},
    )
    assert msg.status_code == 200, msg.get_data(as_text=True)

    poll = client.get(
        "/v1/chat/poll?since=0&timeout=0",
        headers={**_headers(api_key), "X-Feedling-Consumer-Id": "resident-test"},
    )
    assert poll.status_code == 200, poll.get_data(as_text=True)
    assert len(poll.get_json()["messages"]) == 1

    events = _route_events(client, api_key)
    assert [event["type"] for event in events[:2]] == ["chat.poll.delivered", "chat.message"]
    assert events[0]["actor"] == "consumer"
    assert events[0]["detail"]["count"] == 1
    assert events[0]["detail"]["consumer_id"] == "resident-test"
    assert events[1]["actor"] == "ios"
    message_id = msg.get_json()["id"]
    assert events[1]["trace_id"] == message_id
    assert events[1]["turn_id"] == message_id
    assert events[1]["explain"]


def test_resident_chat_response_emits_route_trace(client, monkeypatch):
    monkeypatch.setattr(
        boot_gates,
        "_gate_bootstrap_for_chat",
        lambda store, allow_verify_reply=False: None,
    )
    user_id, api_key = _register(client)
    _enable_trace(client, api_key)

    res = client.post(
        "/v1/chat/response",
        headers=_headers(api_key),
        json={
            "envelope": _env(user_id, "reply"),
            "source": "chat",
            "reply_to_message_id": "user-msg-1",
        },
    )
    assert res.status_code == 200, res.get_data(as_text=True)

    event = _route_events(client, api_key)[0]
    assert event["type"] == "chat.response"
    assert event["actor"] == "agent"
    assert event["detail"]["source"] == "chat"
    assert event["trace_id"] == "user-msg-1"
    assert event["turn_id"] == "user-msg-1"
    assert event["explain"]


def test_resident_chat_response_gate_emits_route_trace(client, monkeypatch):
    def fake_gate(_store, *, allow_verify_reply=False):
        return {"error": "bootstrap_incomplete"}, 409

    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", fake_gate)
    _user_id, api_key = _register(client)
    _enable_trace(client, api_key)

    res = client.post(
        "/v1/chat/response",
        headers=_headers(api_key),
        json={"reply_to_message_id": "user-msg-gated"},
    )
    assert res.status_code == 409, res.get_data(as_text=True)

    event = _route_events(client, api_key)[0]
    assert event["type"] == "chat.response.gated"
    assert event["status"] == "blocked"
    assert event["trace_id"] == "user-msg-gated"
    assert event["turn_id"] == "user-msg-gated"
