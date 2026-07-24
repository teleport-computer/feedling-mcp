"""Error-code mapping + output caps/redaction for the capability facade."""
from __future__ import annotations

from typing import Any

UNAVAILABLE = "capability_unavailable"
INVALID = "capability_invalid_input"
NOT_FOUND = "capability_not_found"
FORBIDDEN = "capability_forbidden"
UPSTREAM = "capability_upstream_error"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

MAX_TEXT = 2000
MAX_ITEMS = 50


def code_for_status(status: int) -> str:
    if status in (400, 422):
        return INVALID
    if status in (401, 403):
        return FORBIDDEN
    if status == 404:
        return NOT_FOUND
    if status in _RETRYABLE_STATUS:
        return UPSTREAM
    return UNAVAILABLE


def retryable_for_status(status: int) -> bool:
    return status in _RETRYABLE_STATUS


def cap_text(s: Any, limit: int = MAX_TEXT) -> str:
    s = str(s or "")
    return s if len(s) <= limit else s[:limit] + "…(capped)"


def cap_list(items: Any, limit: int = MAX_ITEMS) -> list:
    if not isinstance(items, list):
        return []
    return items[:limit]


def message_for_body(body: Any, default: str) -> str:
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            v = body.get(key)
            if isinstance(v, str) and v.strip():
                return cap_text(v)
            if isinstance(v, dict):
                inner = v.get("message") or v.get("error")
                if isinstance(inner, str) and inner.strip():
                    return cap_text(inner)
    return default


def cap_data(data):
    """Bound pathological sizes before data reaches status events / responder.
    Recursively caps list lengths to MAX_ITEMS and string values to MAX_TEXT.
    Generous limits — truncates only oversized blobs; leaves normal content intact."""
    if isinstance(data, dict):
        return {k: cap_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [cap_data(v) for v in data[:MAX_ITEMS]]
    if isinstance(data, str):
        return cap_text(data)
    return data
