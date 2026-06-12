"""Proactive wake substrate: device events, settings normalization, constants."""

import os
import re
import time
from datetime import datetime
from typing import Any

from core import util
from core.store import UserStore

PROACTIVE_JOB_SOURCE = "agent_initiated_proactive"
PROACTIVE_V2_WAKE_TTL_SEC = float(os.environ.get("FEEDLING_PROACTIVE_V2_WAKE_TTL_SEC", "180"))
PROACTIVE_WAKE_MAX_FRAMES = int(os.environ.get("FEEDLING_PROACTIVE_WAKE_MAX_FRAMES", "5"))

_DEVICE_EVENT_ALLOWED_KEYS = {
    "permission",
    "status",
    "source",
    "type",
    "category",
    "place_type",
    "workout_type",
    "duration_min",
    "distance_bucket",
    "starts_in_min",
    "ended_min_ago",
    "is_busy",
    "has_location",
    "motion",
    "time_of_day",
    "scene_tags",
    "scene_phase",
    "phase",
    "app_state",
    "broadcast_state",
    "user_state",
    "ai_state",
    "wake_trigger",
    "is_foreground",
    "selected_tab",
    "tab",
    "is_chat_visible",
    "is_in_detail",
    "reason",
    "app_version",
    "build",
}

_DEVICE_EVENT_DROP_RE = re.compile(
    r"(raw|text|content|title|name|address|photo|image|lat|lng|lon|coordinate|phone|email)",
    re.IGNORECASE,
)


def _redact_device_payload(payload: dict) -> dict:
    """Persist only coarse device-event facts.

    Raw location/photo/calendar text belongs in the user device or encrypted
    frame store, not in server-side proactive logs. The wake scheduler only
    needs coarse state; the real agent can ask for richer context later through
    the normal encrypted path.
    """
    if not isinstance(payload, dict):
        return {}

    redacted: dict = {}
    for key, value in payload.items():
        skey = str(key)
        if _DEVICE_EVENT_DROP_RE.search(skey):
            continue
        if skey not in _DEVICE_EVENT_ALLOWED_KEYS and not skey.startswith("safe_"):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            redacted[skey] = value
        elif isinstance(value, str):
            redacted[skey] = value[:120]
        elif isinstance(value, list):
            safe_items = []
            for item in value[:12]:
                if isinstance(item, (str, int, float, bool)):
                    safe_items.append(item if not isinstance(item, str) else item[:80])
            redacted[skey] = safe_items
    return redacted


def _make_device_event(source: str, event_type: str, payload: dict) -> dict:
    now = time.time()
    return {
        "event_id": util._new_public_id("evt"),
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "source": (source or "ios").strip()[:80],
        "type": (event_type or "unknown").strip()[:80],
        "payload": _redact_device_payload(payload),
    }





def _recent_device_events_for_wake(store: UserStore, now: float, window_sec: float) -> list[dict]:
    since = max(0.0, now - window_sec)
    return store.list_device_events(since_epoch=since, limit=25)


def _payload_float(payload: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(payload.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _normalize_proactive_state(value: Any, allowed: set[str], default: str) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in allowed else default


def _proactive_bool(payload: dict, *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if isinstance(value, (int, float)) and value:
            return True
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y", "on"}:
            return True
    return False
