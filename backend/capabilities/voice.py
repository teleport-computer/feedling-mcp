"""Voice transcript capabilities — the agent's pull path into archived calls.

Memory from a call is written for the agent automatically (Capture renders the
archived transcript into its window at hangup). These tools are for *later*:
weeks on, a memory card carries the ``voice_call_id`` it came from, and the
agent can go read what was actually said instead of relying on the card.

Read-only. ``read`` is paged on purpose — a full hour-long call is thousands of
characters, and a tool result that large would displace the conversation it was
meant to inform.
"""
from __future__ import annotations

from typing import Optional

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err

# One page of an archived call. Big enough that a typical call arrives whole,
# small enough that a marathon call cannot flood the context in one call.
_READ_PAGE_CHARS = 6000
_LIST_DEFAULT_LIMIT = 20
_LIST_MAX_LIMIT = 100


def transcript_list(store, *, api_key=None, runtime_token: Optional[str] = None,
                    params=None) -> CapabilityResult:
    """Metadata for this user's archived calls, newest first. No bodies."""
    from voice import transcript_store

    params = params or {}
    try:
        limit = int(params.get("limit") or _LIST_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = _LIST_DEFAULT_LIMIT
    limit = max(1, min(limit, _LIST_MAX_LIMIT))
    items = transcript_store.list_metadata(store.user_id, limit=limit)
    return ok(data=errors.cap_data({"items": items, "returned": len(items)}))


def transcript_read(store, *, api_key=None, runtime_token: Optional[str] = None,
                    params=None) -> CapabilityResult:
    """One archived call's text, paged.

    ``offset`` is a character offset so a follow-up read can continue where the
    last one stopped; the response says whether more remains.
    """
    from voice import transcript_store

    params = params or {}
    call_id = str(params.get("call_id") or "").strip()
    if not call_id:
        return err(errors.INVALID, "voice_transcript_read needs call_id",
                   retryable=False)
    try:
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        text = transcript_store.load_plaintext(
            store.user_id, call_id, runtime_token=runtime_token or "",
            api_key=api_key,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the model as a tool error
        return err(errors.UNAVAILABLE,
                   f"voice transcript unavailable: {str(exc)[:160]}",
                   retryable=True)
    chunk = text[offset:offset + _READ_PAGE_CHARS]
    next_offset = offset + len(chunk)
    return ok(data=errors.cap_data({
        "call_id": call_id,
        "text": chunk,
        "offset": offset,
        "next_offset": next_offset if next_offset < len(text) else None,
        "total_chars": len(text),
        "has_more": next_offset < len(text),
    }))
