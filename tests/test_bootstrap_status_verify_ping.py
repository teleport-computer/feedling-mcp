"""Regression: /v1/bootstrap/status must not count synthetic verify-loop
liveness replies as real agent messages.

Prod bug (frontend report 2026-07-11, screenshot 20260711-194735):

  17:03:21  /bootstrap/status -> agentMessages=1  (App shows "new message" bubble)
  17:03:26  tap bubble -> /chat/history total=0    (empty chat)

Root cause: a verify-loop liveness reply (role=openclaw/agent, source="verify_ping")
that the verify_loop GC skipped (e.g. mid-run SIGTERM) lingers in store.chat_messages.
/v1/chat/history hides it via _hide_verify_ping_from_feed (total=0, correct), but
bootstrap_status_payload counted it by role only, without excluding source="verify_ping"
— the single outlier vs gates.py / chat.service / db.py, all of which already exclude it.
Result: a "new message" bubble that opens onto an empty chat.

These tests pin the two endpoints to agree: a lingering verify_ping reply is invisible
to BOTH agent_messages_count and /chat/history total.
"""

from __future__ import annotations

import base64
import itertools
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from bootstrap.status_core import bootstrap_status_payload  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _env(msg_id: str, user_id: str) -> dict:
    return {
        "id": msg_id,
        "v": 1,
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def test_bootstrap_status_ignores_lingering_verify_ping_reply(client):
    """A verify_ping openclaw reply left in chat_messages (GC skipped) must be
    invisible to agent_messages_count — matching /chat/history, which hides it."""
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    # Simulate the un-GC'd liveness reply: role=openclaw, source="verify_ping".
    store.append_chat("openclaw", "verify_ping", _env("lingering_ack", user_id))

    # Direct payload call (unit): the count must exclude the synthetic reply.
    payload = bootstrap_status_payload(store)
    assert payload["agent_messages_count"] == 0, (
        f"verify_ping reply leaked into agent_messages_count: {payload}"
    )

    # End-to-end: /bootstrap/status and /chat/history must AGREE — no phantom bubble.
    status = client.get("/v1/bootstrap/status", headers={"X-API-Key": api_key}).get_json()
    assert status["agent_messages_count"] == 0

    hist = client.get("/v1/chat/history", headers={"X-API-Key": api_key}).get_json()
    assert hist["total"] == 0, f"verify_ping reply leaked into history: {hist}"


def test_bootstrap_status_still_counts_real_agent_reply(client):
    """Guard against over-filtering: a genuine agent reply (source="chat")
    still counts, so the fix does not suppress real 'new message' bubbles."""
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    store.append_chat("openclaw", "chat", _env("real_reply", user_id))

    payload = bootstrap_status_payload(store)
    assert payload["agent_messages_count"] == 1

    hist = client.get("/v1/chat/history", headers={"X-API-Key": api_key}).get_json()
    assert hist["total"] == 1
