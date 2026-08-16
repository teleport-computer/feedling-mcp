"""Timestamp parsing and ordering for Memory Garden metadata.

Stored cards predate a single timestamp format.  Reads must therefore compare
instants, not their wire representations, while writes are migrated separately.
Naive historical values are interpreted as UTC, matching the backend's legacy
comparison convention.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any


_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def now_iso() -> str:
    """Return the canonical timestamp for a newly written Memory Garden card."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize(raw: Any) -> str:
    """Normalize a supplied card timestamp without inventing missing precision.

    Date-only values stay date-only. Datetimes are converted to their UTC
    instant and emitted with ``Z``; supplied microseconds are preserved.
    Empty or malformed values stay empty so callers never silently invent a
    date for undated material.
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    if _DATE_ONLY_RE.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return ""
        return value
    parsed = parse_ts(value)
    if parsed is None:
        return ""
    return parsed.isoformat().replace("+00:00", "Z")
