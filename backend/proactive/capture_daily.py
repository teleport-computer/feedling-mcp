"""Daily Memory Capture banner counters shared by resident and Runtime V2."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _safe_timestamp(value: Any) -> float:
    try:
        timestamp = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else 0.0


def _calendar_timezone(name: Any) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "UTC").strip() or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def device_timezone_name(user_id: str) -> str:
    """Return the stable device-reported IANA zone, or an empty fallback."""
    try:
        from perception import service as perception_service

        name = str(
            perception_service.stable_context_timezone(str(user_id)) or ""
        ).strip()
        ZoneInfo(name)
        return name
    except Exception:
        return ""


def daily_capture_patch(
    state: Mapping[str, Any],
    *,
    cards_added: int,
    completed_at: float,
    timezone_name: Any,
    device_timezone: Any = "",
) -> dict[str, int | float]:
    """Return the banner patch for one positive Capture completion.

    The timestamp tracks the latest completion that actually inserted cards,
    unlike ``last_capture_completed_at`` which also advances on legal no-ops and
    is used by Capture scheduling. The count accumulates within the user's saved
    proactive calendar timezone and resets on its next calendar day. Clients
    still decide whether the timestamp is "today" in their current local zone.
    """
    try:
        added = max(0, int(cards_added or 0))
    except (TypeError, ValueError):
        added = 0
    now_ts = _safe_timestamp(completed_at)
    if added <= 0 or now_ts <= 0:
        return {}

    # The client renders "today" with its device Calendar. Prefer the stable,
    # TTL-exempt device timezone written by app-presence; proactive timezone is
    # the migration/offline fallback, then UTC for malformed or missing zones.
    tz = _calendar_timezone(device_timezone or timezone_name)
    previous_at = _safe_timestamp(state.get("last_capture_cards_added_at"))
    same_day = (
        previous_at > 0
        and datetime.fromtimestamp(previous_at, tz).date()
        == datetime.fromtimestamp(now_ts, tz).date()
    )
    try:
        previous_count = max(0, int(state.get("last_capture_cards_added") or 0))
    except (TypeError, ValueError):
        previous_count = 0
    return {
        "last_capture_cards_added_at": now_ts,
        "last_capture_cards_added": (previous_count + added if same_day else added),
    }
