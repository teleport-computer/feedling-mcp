"""Timestamp parsing and ordering for Memory Garden metadata.

Stored cards predate a single timestamp format.  Reads must therefore compare
instants, not their wire representations, while writes are migrated separately.
Naive historical values are interpreted as UTC, matching the backend's legacy
comparison convention.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)


def parse_ts(raw: Any) -> datetime | None:
    """Parse every historical Memory Garden timestamp shape as a UTC instant."""
    value = str(raw or "").strip()
    if not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sort_key(raw: Any) -> tuple[bool, datetime]:
    """Descending-sort key: valid instants first, malformed/empty values last."""
    parsed = parse_ts(raw)
    return parsed is not None, parsed or _MIN_UTC
