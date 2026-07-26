"""Authenticated read model for Runtime V2 chat activity."""
from __future__ import annotations

import re

from core import chat_activity
from core.store import UserStore
from model_api_runtime.v2 import jobs_store


_TURN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def read_turn_activity(store: UserStore, turn_id: str) -> tuple[dict, int]:
    normalized = str(turn_id or "").strip()
    if not _TURN_ID_RE.fullmatch(normalized):
        return {"error": "invalid_turn_id"}, 400
    jobs, rows = jobs_store.chat_turn_activity_rows(store.user_id, normalized)
    if not jobs:
        return {"error": "turn_activity_not_found"}, 404
    return chat_activity.turn_response(normalized, jobs, rows), 200
