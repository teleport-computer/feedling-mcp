"""Memory Capture banner counters shared by resident and Runtime V2."""
from __future__ import annotations

import math
from typing import Any, Mapping


def _safe_timestamp(value: Any) -> float:
    try:
        timestamp = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else 0.0


def daily_capture_patch(
    state: Mapping[str, Any],
    *,
    cards_added: int,
    completed_at: float,
) -> dict[str, int | float]:
    """Return the banner patch for one positive Capture completion.

    The timestamp tracks the latest completion that actually inserted cards,
    unlike ``last_capture_completed_at`` which also advances on legal no-ops and
    is used by Capture scheduling. The count is monotonic; clients subtract the
    total saved when the user last dismissed the banner.
    """
    try:
        added = max(0, int(cards_added or 0))
    except (TypeError, ValueError):
        added = 0
    now_ts = _safe_timestamp(completed_at)
    if added <= 0 or now_ts <= 0:
        return {}

    try:
        previous_count = max(0, int(state.get("last_capture_cards_added") or 0))
    except (TypeError, ValueError):
        previous_count = 0
    return {
        "last_capture_cards_added_at": now_ts,
        "last_capture_cards_added": previous_count + added,
    }
