"""Hangup bookkeeping: the transcript card, and retiring the per-turn rows.

Formerly ``voice/summary.py``. The model-written 1-3 sentence summary is gone
(2026-08-07): it cost a second provider call per call AND was the only thing
memory ever saw, so a whole conversation was distilled from three sentences.
The chat stream now carries a mechanical preview card whose body lives in
``voice_transcripts``; Capture reads the archive, not the card.

What survives from the summary era is the part that was never about summaries:
deleting the call's per-turn rows without breaking compaction coverage.
"""

from __future__ import annotations

import uuid

import db
from core import envelope as core_envelope


# Deterministic namespace: a retried finalize derives the SAME summary message
# id, so a replay collapses onto one row instead of writing a second summary.
_TRANSCRIPT_NS = uuid.UUID("6f9b2c41-7d3a-4e59-9c21-8a1e0d5f4b77")


def transcript_card_message_id(call_id: str) -> str:
    """Deterministic id for a call's chat card — makes finalize replay-safe.

    Namespace deliberately unchanged from the summary era: a call can only ever
    have had one or the other, and reusing it means a client that retries an
    old finalize lands on the same row instead of creating a duplicate card.
    """
    return str(uuid.uuid5(_TRANSCRIPT_NS, f"voice_call_summary:{call_id}"))


def persist_transcript_card(store, preview: str, message_id: str, call_id: str,
                            *, turn_count: int = 0, duration_sec: int = 0) -> bool:
    """Durably append exactly one ``voice_call_transcript`` chat row.

    ``preview`` is bounded (see ``transcript_store.PREVIEW_MAX_CHARS``) and is
    ALL the chat stream ever carries for this call: the prompt tail is budgeted
    in tokens, and an oversized single row makes compaction raise
    ``compaction_message_exceeds_char_budget``, which would take the user's
    ordinary text chat down with it. The full text belongs in the archive.

    Returns True only when the row is durably present. Idempotent via the
    deterministic ``message_id``; ``strict=True`` raises on a DB failure rather
    than silently dropping the card. The caller must not delete the per-turn
    rows unless this returns True.
    """
    preview = str(preview or "").strip()
    if not preview:
        return False
    try:
        if db.chat_get_strict(store.user_id, str(message_id)) is not None:
            return True
    except Exception:  # noqa: BLE001 — unknown, fall through to the write
        pass
    env, _err = core_envelope._build_shared_envelope_for_store(
        store, preview.encode("utf-8"), item_id=str(message_id)
    )
    if env is None:
        return False
    try:
        store.append_chat(
            "openclaw",
            "voice_call_transcript",
            env,
            content_type="text",
            extra={
                "voice_call_id": str(call_id),
                "voice_turn_count": max(0, int(turn_count or 0)),
                "voice_duration_sec": max(0, int(duration_sec or 0)),
            },
            strict=True,
        )
    except Exception:  # noqa: BLE001 — failed, or raced a concurrent replay
        try:
            return db.chat_get_strict(store.user_id, str(message_id)) is not None
        except Exception:  # noqa: BLE001
            return False
    return True


def _compaction_covered_seq(user_id: str) -> int:
    """Highest chat seq already folded into the V2 conversation summary.

    Rows at-or-below this seq are part of compaction's frozen source ledger:
    deleting one would desync the frontier's frozen counts and break later
    summary reads. Conservative in the DELETE direction — when only a legacy
    ``watermark_ts`` exists, a row exactly at the watermark is treated as
    covered (kept), the opposite rounding of the GC helper which protects the
    other direction.
    """
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(watermark_seq, 0), watermark_ts "
            "FROM v2_conversation_summary WHERE user_id = %s",
            (str(user_id),),
        ).fetchone()
        if row is None:
            return 0
        covered = int(row[0] or 0)
        watermark_ts = row[1]
        if watermark_ts:
            ts_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM chat_messages "
                "WHERE user_id = %s AND ts <= %s",
                (str(user_id), float(watermark_ts)),
            ).fetchone()
            covered = max(covered, int(ts_row[0] or 0))
    return covered


def call_message_rows(user_id: str, call_id: str) -> list[tuple[str, int]]:
    """(msg_id, seq) of every chat row belonging to one voice call.

    Only the SPOKEN USER turn carries ``voice_call_id``; the assistant reply is
    persisted by the ordinary agent path and carries only
    ``reply_to_message_id`` pointing at that user row (verified live: openclaw
    reply rows have no voice metadata). So the call's rows are the tagged rows
    PLUS every reply whose parent is one of them.
    """
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT msg_id, seq FROM chat_messages "
            "WHERE user_id = %s AND doc->>'voice_call_id' = %s",
            (str(user_id), str(call_id)),
        ).fetchall()
        tagged = [(str(r[0]), int(r[1] or 0)) for r in rows]
        parent_ids = [m for m, _s in tagged]
        if parent_ids:
            reply_rows = conn.execute(
                "SELECT msg_id, seq FROM chat_messages "
                "WHERE user_id = %s "
                "AND doc->>'reply_to_message_id' = ANY(%s)",
                (str(user_id), parent_ids),
            ).fetchall()
            tagged.extend(
                (str(r[0]), int(r[1] or 0)) for r in reply_rows
            )
    return tagged


def delete_call_messages(user_id: str, call_id: str) -> dict:
    """Remove the call's per-turn rows once the summary is durable.

    Only rows NOT yet folded by V2 compaction are deleted (per-message
    primitive, same one the verify-loop GC uses); a row already inside the
    frozen summary segment is retained (``retained_covered``) because deleting
    it would corrupt the compaction frontier — it no longer feeds future
    context reads anyway. Returns counts so the route can verify completeness:
    ``remaining`` > 0 means deletable rows survived (DB blips swallowed by
    ``chat_delete``) and the finalize must NOT report success.
    """
    smid = transcript_card_message_id(call_id)
    covered = _compaction_covered_seq(user_id)
    # Snapshot the full row set FIRST: once the tagged user rows are deleted,
    # their replies can no longer be found through the parent predicate, so the
    # recheck must roll-call this same list rather than re-run the query.
    targets = [
        (msg_id, seq)
        for msg_id, seq in call_message_rows(user_id, call_id)
        if msg_id != smid
    ]
    deleted = 0
    retained_covered = 0
    deletable: list[str] = []
    for msg_id, seq in targets:
        if seq <= covered:
            retained_covered += 1
            continue
        deletable.append(msg_id)
        try:
            if db.chat_delete(str(user_id), msg_id):
                deleted += 1
        except Exception:  # noqa: BLE001 — counted below via the recheck
            continue
    remaining = 0
    if deletable:
        with db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM chat_messages "
                "WHERE user_id = %s AND msg_id = ANY(%s)",
                (str(user_id), deletable),
            ).fetchone()
        remaining = int(row[0] or 0)
    return {
        "deleted": deleted,
        "retained_covered": retained_covered,
        "remaining": remaining,
    }
