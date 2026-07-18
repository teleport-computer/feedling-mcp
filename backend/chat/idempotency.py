"""Shared request parsing for client-identified chat sends."""

from __future__ import annotations

import uuid


CLIENT_MSG_ID_WINDOW_SEC = 600


def parse_client_msg_id(payload: dict) -> tuple[str | None, tuple[dict, int] | None]:
    """Return a canonical optional UUID or the public 400 response."""
    if "client_msg_id" not in payload:
        return None, None
    raw = payload.get("client_msg_id")
    if not isinstance(raw, str) or not raw.strip():
        return None, (
            {
                "error": "client_msg_id_invalid",
                "detail": "client_msg_id must be a UUID string",
            },
            400,
        )
    try:
        normalized = str(uuid.UUID(raw.strip()))
    except (ValueError, AttributeError):
        return None, (
            {
                "error": "client_msg_id_invalid",
                "detail": "client_msg_id must be a UUID string",
            },
            400,
        )
    return normalized, None
