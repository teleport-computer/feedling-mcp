"""Keep voice transport artifacts out of ordinary conversation context.

The transcript card is a UI/archive pointer. Capture deliberately expands it
from ``voice_transcripts``; normal chat replay and compaction must not mistake
the mixed-speaker preview for one assistant utterance.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable


VOICE_TRANSCRIPT_SOURCE = "voice_call_transcript"

_VOICE_NOISE_MARKERS = frozenset({
    "backgroundnoise",
    "inaudible",
    "music",
    "silence",
    "无声",
    "杂音",
    "背景噪音",
    "背景杂音",
    "静音",
    "噪音",
})


def normalized_voice_marker(text: str) -> str:
    return "".join(
        character.casefold()
        for character in str(text or "").strip()
        if unicodedata.category(character)[0] not in {"P", "S", "Z"}
    )


def is_meaningful_voice_message(text: str) -> bool:
    marker = normalized_voice_marker(text)
    if not marker or marker in _VOICE_NOISE_MARKERS:
        return False
    return any(character.isalnum() for character in marker)


def _content_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and str(block.get("text") or "").strip()
        )
    return str(content or "")


def conversation_rows(rows: Iterable[dict]) -> list[dict]:
    """Return rows that are real conversational history.

    A punctuation/noise-only row is suppressed only when it carries
    ``voice_call_id``. A user who intentionally types ``...`` in text chat is
    therefore unchanged. For one voice call/logical turn, only the newest ASR
    revision is kept even if shadow metadata replication is still catching up.
    Replies explicitly linked to a suppressed voice row are removed with their
    parent.
    """
    materialized = [row for row in rows if isinstance(row, dict)]
    latest_voice_user_id_by_turn: dict[tuple[str, str], str] = {}
    for row in materialized:
        role = str(row.get("role") or "").strip().lower()
        call_id = str(row.get("voice_call_id") or "").strip()
        logical_turn_id = str(
            row.get("voice_logical_turn_id") or row.get("voice_turn_id") or ""
        ).strip()
        row_id = str(row.get("id") or "").strip()
        if role in {"user", "human"} and call_id and logical_turn_id and row_id:
            latest_voice_user_id_by_turn[(call_id, logical_turn_id)] = row_id

    rejected_voice_user_ids = {
        str(row.get("id") or "").strip()
        for row in materialized
        if str(row.get("role") or "").strip().lower() in {"user", "human"}
        and str(row.get("voice_call_id") or "").strip()
        and (
            str(row.get("voice_turn_status") or "").strip() == "superseded"
            or not is_meaningful_voice_message(_content_text(row.get("content")))
            or latest_voice_user_id_by_turn.get((
                str(row.get("voice_call_id") or "").strip(),
                str(
                    row.get("voice_logical_turn_id")
                    or row.get("voice_turn_id")
                    or ""
                ).strip(),
            ))
            != str(row.get("id") or "").strip()
        )
        and str(row.get("id") or "").strip()
    }

    kept: list[dict] = []
    for row in materialized:
        if str(row.get("source") or "").strip() == VOICE_TRANSCRIPT_SOURCE:
            continue
        row_id = str(row.get("id") or "").strip()
        if row_id in rejected_voice_user_ids:
            continue
        role = str(row.get("role") or "").strip().lower()
        parent_id = str(row.get("reply_to_message_id") or "").strip()
        if role in {"assistant", "agent", "openclaw", "model"} and (
            parent_id in rejected_voice_user_ids
        ):
            continue
        kept.append(row)
    return kept
