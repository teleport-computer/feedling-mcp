"""Hangup summary: replace a finished call's per-turn chat rows with ONE summary.

Scope note: the live call is UNCHANGED — voice turns still run through the
normal chat lane with the user's identity, memory and tools. This module only
runs at hangup: it summarizes the call with the user's own model, appends one
``voice_call_summary`` row, deletes that call's per-turn rows (so the raw
back-and-forth stops occupying chat history and the model's context), and nudges
memory capture, whose window then sees the summary.

The transcript comes from the client: chat rows are ciphertext the server cannot
read, while the app already holds the call's plaintext captions.
"""

from __future__ import annotations

import uuid

import db
import provider_client
from core import envelope as core_envelope
from hosted import config_store as hosted_config_store
from proactive import proactive_core
from voice import results


# Deterministic namespace: a retried finalize derives the SAME summary message
# id, so a replay collapses onto one row instead of writing a second summary.
_SUMMARY_NS = uuid.UUID("6f9b2c41-7d3a-4e59-9c21-8a1e0d5f4b77")

_SUMMARY_SYSTEM = (
    "You are the user's AI companion. You just finished a voice call with them. "
    "Write ONE short summary of the call for the chat history — what was "
    "discussed, any decisions or todos, and the emotional tone. Write in the "
    "user's own language, in the second person ('you'), 1-3 sentences. Do not "
    "add greetings or ask questions; output only the summary."
)


def summary_message_id(call_id: str) -> str:
    return str(uuid.uuid5(_SUMMARY_NS, f"voice_call_summary:{call_id}"))


def build_summary_messages(turns: list[dict]) -> list[dict]:
    """Render the client-supplied transcript into provider chat messages.

    ``turns`` is ``[{"role": "user"|"assistant", "text": str}, ...]`` in order.
    """
    lines: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        role = str(turn.get("role") or "").strip().lower()
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {text}")
    return [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {
            "role": "user",
            "content": "Summarize this voice call:\n\n" + "\n".join(lines),
        },
    ]


def generate_summary(store, turns: list[dict]) -> str:
    """Summarize the call with the USER'S OWN model (BYOK, enclave-decrypted).

    Mirrors how every other server-side one-shot resolves a provider; raises on
    an unusable provider config so the caller can leave the call unsummarized
    (and the per-turn rows intact) rather than losing the call.
    """
    token = results.mint_enclave_token(store.user_id)
    runtime = hosted_config_store._load_runtime_provider_config(
        store, None, runtime_token=token
    )
    if isinstance(runtime, tuple):
        raise RuntimeError(
            f"voice_summary_provider_unavailable:{runtime[1].get('error')}"
        )
    result = provider_client.reliable_chat_completion(
        runtime,
        build_summary_messages(turns),
        max_tokens=400,
        temperature=None,
        timeout=45.0,
        include_reasoning=False,
        max_attempts=2,
        base_delay_sec=0.5,
    )
    text = str((result or {}).get("reply") or "").strip()
    if not text:
        raise RuntimeError("voice_summary_empty")
    return text


def persist_summary(store, text: str, message_id: str) -> bool:
    """Durably append exactly one ``voice_call_summary`` chat row.

    Returns True only when the row is durably present. Idempotent via the
    deterministic ``message_id``; ``strict=True`` uses the durable append that
    raises on a DB failure instead of silently dropping the summary. The caller
    must not delete the per-turn rows unless this returns True.
    """
    text = str(text or "").strip()
    if not text:
        return False
    try:
        if db.chat_get_strict(store.user_id, str(message_id)) is not None:
            return True
    except Exception:  # noqa: BLE001 — unknown, fall through to the write
        pass
    env, _err = core_envelope._build_shared_envelope_for_store(
        store, text.encode("utf-8"), item_id=str(message_id)
    )
    if env is None:
        return False
    try:
        store.append_chat(
            "openclaw", "voice_call_summary", env, content_type="text", strict=True
        )
    except Exception:  # noqa: BLE001 — failed, or raced a concurrent replay
        try:
            return db.chat_get_strict(store.user_id, str(message_id)) is not None
        except Exception:  # noqa: BLE001
            return False
    return True


def call_message_ids(user_id: str, call_id: str) -> list[str]:
    """Every chat row belonging to one voice call.

    Both the spoken user turn and IO's reply carry ``voice_call_id`` as
    plaintext routing metadata, so one predicate covers the whole call.
    """
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT msg_id FROM chat_messages "
            "WHERE user_id = %s AND doc->>'voice_call_id' = %s",
            (str(user_id), str(call_id)),
        ).fetchall()
    return [str(row[0]) for row in rows]


def delete_call_messages(user_id: str, call_id: str) -> int:
    """Remove the call's per-turn rows once the summary is durable.

    Uses the per-message primitive the verify-loop GC already deletes rows with.
    After this, the raw back-and-forth is gone from history, from the model's
    context, and from the capture window — only the summary remains.
    """
    deleted = 0
    for msg_id in call_message_ids(user_id, call_id):
        if msg_id == summary_message_id(call_id):
            continue
        try:
            if db.chat_delete(str(user_id), msg_id):
                deleted += 1
        except Exception:  # noqa: BLE001 — best effort, summary already durable
            continue
    return deleted


def nudge_capture(store) -> None:
    """Process the capture window now that it holds the summary. Runtime-aware
    (V2 job pool vs resident legacy queue); best-effort — the normal quiet /
    boundary sweep is the correctness backstop."""
    try:
        proactive_core.capture_force(store)
    except Exception:  # noqa: BLE001 — nudge only
        pass
