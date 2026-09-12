"""Content-free profile refresh scheduling, separate from profile generation.

Healthy and empty profiles follow the Garden's row-count/max-updated witness,
not an age floor. A changed witness is picked up at the next existing post-turn
check; generation remains asynchronous. Failed profiles retain their retry
policy so fresh Garden writes cannot bypass provider backoff or explicit repair.
"""

from __future__ import annotations

import time

import db
from model_api_runtime.v2 import profile_store


def refresh_due(user_id: str, *, enabled: bool, now: float | None = None) -> bool:
    if not enabled:
        return False
    raw = db.get_blob_strict(str(user_id), profile_store.PROFILE_BLOB_KIND)
    if raw is None:
        return True
    document = profile_store.validate_profile_document(raw)
    if document.get("disabled") is True:
        return False
    state = str(document.get("state") or "")
    if state not in {"ok", "empty"}:
        attempt = document.get("last_attempt") or {}
        disposition = str(attempt.get("retry_disposition") or "")
        if disposition in profile_store.PROFILE_STUCK_RETRY_DISPOSITIONS:
            return False
        if disposition != "source_change":
            current_time = float(time.time() if now is None else now)
            return current_time >= float(attempt.get("retry_not_before") or 0)

    # Read errors propagate; an unavailable witness is not an unchanged Garden.
    # Use the same raw-table witness as profile generation (not eligible count).
    source = document.get("source") or {}
    card_count, max_updated_at = db.memory_profile_source_stats(user_id)
    return (
        int(source.get("card_count") or 0) != card_count
        or str(source.get("max_updated_at") or "") != max_updated_at
    )
