"""Database invariants for pause-continuation voice ASR revisions."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _envelope(user_id: str, msg_id: str) -> dict:
    return {
        "v": 1,
        "id": msg_id,
        "body_ct": _b64(f"ciphertext:{msg_id}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared",
        "owner_user_id": user_id,
    }


@pytest.fixture()
def store(backend_env):
    response = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "zh"},
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    return core_store.get_store(response.get_json()["user_id"])


def _append_revision(
    store,
    *,
    msg_id: str,
    client_msg_id: str,
    revision_turn_id: str,
) -> tuple[dict, bool]:
    return store.append_chat_idempotent(
        "user",
        "model_api",
        _envelope(store.user_id, msg_id),
        client_msg_id=client_msg_id,
        window_sec=3600,
        extra={
            "voice_call_id": "call-pause-continuation",
            "voice_turn_id": revision_turn_id,
            "voice_logical_turn_id": "2",
            "voice_turn_status": "current",
        },
    )


def test_new_voice_revision_supersedes_partial_and_exact_retry_is_idempotent(store):
    partial, partial_inserted = _append_revision(
        store,
        msg_id="voice-partial",
        client_msg_id="voice-client-partial",
        revision_turn_id="2.partial",
    )
    complete, complete_inserted = _append_revision(
        store,
        msg_id="voice-complete",
        client_msg_id="voice-client-complete",
        revision_turn_id="2.complete",
    )

    assert partial_inserted is True
    assert complete_inserted is True
    persisted_partial = db.chat_get_strict(store.user_id, partial["id"])
    persisted_complete = db.chat_get_strict(store.user_id, complete["id"])
    assert persisted_partial["voice_turn_status"] == "superseded"
    assert persisted_partial["voice_superseded_by"] == complete["id"]
    assert persisted_complete["voice_turn_status"] == "current"

    retry, retry_inserted = _append_revision(
        store,
        msg_id="voice-complete-retry-envelope",
        client_msg_id="voice-client-complete",
        revision_turn_id="2.complete",
    )

    assert retry_inserted is False
    assert retry["id"] == complete["id"]
    persisted_complete = db.chat_get_strict(store.user_id, complete["id"])
    assert persisted_complete["voice_turn_status"] == "current"


def test_superseded_voice_revision_cannot_be_claimed_or_answered(store):
    partial, _ = _append_revision(
        store,
        msg_id="voice-stale-parent",
        client_msg_id="voice-stale-client",
        revision_turn_id="2.stale",
    )
    current, _ = _append_revision(
        store,
        msg_id="voice-current-parent",
        client_msg_id="voice-current-client",
        revision_turn_id="2.current",
    )

    stale_claim = db.chat_try_claim_reply(
        store.user_id,
        partial["id"],
        "resident-stale",
        100.0,
        {
            "reply_claimed_by": "resident-stale",
            "reply_claimed_at": "100.000",
            "reply_claim_expires_at": "700.000",
        },
    )
    current_claim = db.chat_try_claim_reply(
        store.user_id,
        current["id"],
        "resident-current",
        100.0,
        {
            "reply_claimed_by": "resident-current",
            "reply_claimed_at": "100.000",
            "reply_claim_expires_at": "700.000",
        },
    )

    assert stale_claim is None
    assert current_claim is not None

    reply = store._build_chat_message(
        "openclaw",
        "chat",
        _envelope(store.user_id, "voice-stale-reply"),
        extra={"reply_to_message_id": partial["id"]},
    )
    finalized = db.chat_finalize_reply_once(
        store.user_id,
        partial["id"],
        reply["id"],
        float(reply["ts"]),
        reply,
        {
            "reply_status": "replied",
            "reply_message_id": reply["id"],
            "replied_by": "resident-stale",
            "replied_at": f"{float(reply['ts']):.3f}",
        },
    )

    assert finalized is None
    assert db.chat_get_strict(store.user_id, reply["id"]) is None
