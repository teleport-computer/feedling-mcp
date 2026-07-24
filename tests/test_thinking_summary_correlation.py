from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import chat_core  # noqa: E402
from core.store import UserStore  # noqa: E402


class _Store:
    user_id = "user-1"


def _envelope(user_id: str, message_id: str) -> dict:
    return {
        "id": message_id,
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped-key",
        "visibility": "local_only",
        "owner_user_id": user_id,
    }


def _write(user_id: str, **correlation) -> tuple[dict, int]:
    assistant_message_id = "assistant-message-1"
    payload = {
        "envelope": _envelope(user_id, assistant_message_id),
        "thinking_envelope": _envelope(user_id, "thinking-1"),
        **correlation,
    }
    return chat_core.write_response(
        _Store(),
        payload,
        consumer_id="agent-1",
        consumer_info={},
        allow_verify_reply=True,
    )


def test_thinking_summary_requires_complete_correlation_ids():
    user_id = _Store.user_id

    body, status = _write(
        user_id,
        thinking_conversation_id="conversation-1",
        thinking_turn_id="user-message-1",
        thinking_assistant_message_id="assistant-message-1",
        # thinking_source_id is intentionally missing.
        thinking_update_seq=1,
    )

    assert status == 400
    assert body == {
        "error": "thinking_correlation_missing_fields",
        "detail": ["thinking_source_id"],
    }


def test_thinking_summary_rejects_cross_message_attachment():
    user_id = _Store.user_id

    body, status = _write(
        user_id,
        thinking_conversation_id="conversation-1",
        thinking_turn_id="user-message-1",
        thinking_source_id="reasoning-1",
        thinking_assistant_message_id="assistant-message-other",
        thinking_update_seq=1,
    )

    assert status == 409
    assert body["error"] == "thinking_assistant_message_id_mismatch"
    assert body["expected_assistant_message_id"] == "assistant-message-1"


def test_thinking_summary_rejects_nonpositive_update_sequence():
    user_id = _Store.user_id

    body, status = _write(
        user_id,
        thinking_conversation_id="conversation-1",
        thinking_turn_id="user-message-1",
        thinking_source_id="reasoning-1",
        thinking_assistant_message_id="assistant-message-1",
        thinking_update_seq=0,
    )

    assert status == 400
    assert body["error"] == "thinking_update_seq_invalid"
    assert body["minimum"] == 1


def test_plaintext_summary_without_correlation_is_rejected(monkeypatch):
    """The legacy server-sealed bridge must fail closed just like envelopes."""
    appended = []

    class _AppendStore(_Store):
        def append_chat(self, role, source, envelope, *, content_type, extra):
            message = {
                **envelope,
                "role": role,
                "source": source,
                "content_type": content_type,
                "ts": 1.0,
                "v": 1,
                **(extra or {}),
            }
            appended.append(message)
            return message

    monkeypatch.setattr(
        chat_core.chat_consumer,
        "_record_consumer_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        chat_core.chat_service,
        "_chat_plaintext_thinking_extra_for_store",
        lambda *args, **kwargs: {"thinking_body_ct": "server-sealed"},
    )
    monkeypatch.setattr(chat_core.debug_trace, "trace_event", lambda *args, **kwargs: None)

    body, status = chat_core.write_response(
        _AppendStore(),
        {
            "envelope": _envelope(_Store.user_id, "assistant-message-1"),
            "reasoning_summary": "A legacy plaintext summary without immutable ids.",
        },
        consumer_id="agent-1",
        consumer_info={},
        allow_verify_reply=True,
    )

    assert status == 400
    assert body == {
        "error": "thinking_correlation_missing_fields",
        "detail": [
            "thinking_conversation_id",
            "thinking_turn_id",
            "thinking_assistant_message_id",
            "thinking_source_id",
            "thinking_update_seq",
        ],
    }
    assert appended == []


def test_store_preserves_positive_thinking_update_sequence():
    store = object.__new__(UserStore)
    store.user_id = "user-1"

    message = store._build_chat_message(
        "openclaw",
        "chat",
        _envelope("user-1", "assistant-message-1"),
        extra={
            "thinking_update_seq": 1,
            "thinking_assistant_message_id": "assistant-message-1",
        },
    )

    assert message["thinking_update_seq"] == 1
    assert message["thinking_assistant_message_id"] == message["id"]
