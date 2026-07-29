"""Authenticated runtime-neutral chat activity read/write model."""
from __future__ import annotations

import re

from core import chat_activity
from core.store import UserStore
from chat import activity_store
from model_api_runtime.v2 import jobs_store


_TURN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_EVENT_FIELDS = frozenset({
    "activity_id", "call_id", "tool_name", "state", "duration_ms",
    "result_code", "memory_count", "memory_categories",
})


def read_turn_activity(store: UserStore, turn_id: str) -> tuple[dict, int]:
    normalized = str(turn_id or "").strip()
    if not _TURN_ID_RE.fullmatch(normalized):
        return {"error": "invalid_turn_id"}, 400
    jobs, rows = jobs_store.chat_turn_activity_rows(store.user_id, normalized)
    if jobs:
        return chat_activity.turn_response(normalized, jobs, rows), 200
    parent, resident_rows = activity_store.resident_turn_rows(store.user_id, normalized)
    if parent is None:
        return {"error": "turn_activity_not_found"}, 404
    return chat_activity.resident_turn_response(normalized, parent, resident_rows), 200


def write_turn_activity(store: UserStore, turn_id: str, payload: dict) -> tuple[dict, int]:
    """Accept one fixed-shape V1 resident tool transition."""
    normalized = str(turn_id or "").strip()
    if not _TURN_ID_RE.fullmatch(normalized):
        return {"error": "invalid_turn_id"}, 400
    if not isinstance(payload, dict):
        return {"error": "invalid_activity_event"}, 400
    if set(payload) - _EVENT_FIELDS:
        return {"error": "invalid_activity_event"}, 400
    raw_activity_id = str(payload.get("activity_id") or "").strip()
    raw_tool_name = str(payload.get("tool_name") or "").strip()
    raw_call_id = str(payload.get("call_id") or raw_activity_id).strip()
    activity_id = chat_activity.safe_token(raw_activity_id, max_len=160)
    tool_name = chat_activity.safe_token(raw_tool_name, max_len=120)
    state = chat_activity.safe_token(payload.get("state"), max_len=24).lower()
    call_id = chat_activity.safe_token(raw_call_id, max_len=160)
    if (
        not activity_id
        or activity_id != raw_activity_id
        or not tool_name
        or tool_name != raw_tool_name
        or not call_id
        or call_id != raw_call_id
        or state not in {"running", "success", "failure"}
    ):
        return {"error": "invalid_activity_event"}, 400
    detail: dict = {
        "activity_id": activity_id,
        "tool_name": tool_name,
        "call_id": call_id,
        "state": state,
    }
    duration = chat_activity.safe_duration_ms(payload.get("duration_ms"))
    if duration is not None:
        detail["duration_ms"] = duration
    result_code = chat_activity.safe_token(payload.get("result_code"), max_len=64)
    if result_code:
        detail["result_code"] = result_code
    detail.update(chat_activity.safe_memory_metadata(payload))
    try:
        event_id, inserted = activity_store.append_resident_tool_event(
            store.user_id,
            normalized,
            activity_id=activity_id,
            tool_name=tool_name,
            state=state,
            call_id=call_id,
            detail=detail,
        )
    except ValueError as exc:
        reason = str(exc)
        if reason == "activity turn is not a user message":
            return {"error": "turn_activity_not_found"}, 404
        return {"error": "activity_event_rejected"}, 409
    return {"status": "ok", "event_id": event_id, "inserted": inserted}, 200
