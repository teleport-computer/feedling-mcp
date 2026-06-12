"""MCP tools: push (Dynamic Island / Live Activity)."""

import base64
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastmcp import FastMCP, Context
from fastmcp.server.dependencies import get_http_request
from fastmcp.utilities.types import Image
from fastmcp.server.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from content_encryption import build_envelope

from mcpsrv import client
from mcpsrv.server import mcp

@mcp.tool(
    name="feedling_push_dynamic_island",
    description=(
        "Push to the user's iPhone Dynamic Island / Live Activity. "
        "title appears as the heading (e.g. your Agent name). "
        "body is the main message. "
        "subtitle is optional one-line context. "
        "data is a free-form key-value bag. "
        "The platform enforces a cooldown — check feedling_screen_analyze rate_limit_ok before pushing."
    ),
)
def push_dynamic_island(
    title: str,
    body: str,
    subtitle: str = "",
    data: dict | None = None,
    event: str = "update",
    ctx: Context = None,
) -> dict:
    return client._post("/v1/push/dynamic-island", {
        "title": title,
        "body": body,
        "subtitle": subtitle or None,
        "data": data or {},
        "event": event,
    }, ctx=ctx)


@mcp.tool(
    name="feedling_push_live_activity",
    description=(
        "Update the Live Activity on the user's lock screen and Dynamic Island. "
        "By default, the same message is also synced into chat history so lock-screen "
        "and chat stay consistent."
    ),
)
def push_live_activity(
    title: str,
    body: str,
    subtitle: str = "",
    data: dict | None = None,
    event: str = "update",
    sync_chat: bool = True,
    ctx: Context = None,
) -> dict:
    payload_data = dict(data or {})

    api_key = client._current_api_key(ctx)
    rec = client._recent_decrypt_by_api_key.get(api_key) if api_key else None
    if rec:
        payload_data.setdefault("analysis_source", "vision")
        payload_data.setdefault("frame_id", rec.get("frame_id", ""))

    push_result = client._post("/v1/push/live-activity", {
        "title": title,
        "body": body,
        "subtitle": subtitle or None,
        "data": payload_data,
        "event": event,
    }, ctx=ctx)

    if sync_chat and (body or "").strip():
        user_id, user_pk, enclave_pk = client._whoami_pubkeys(ctx=ctx)
        if user_id and user_pk is not None and enclave_pk is not None:
            envelope = build_envelope(
                plaintext=body.encode("utf-8"),
                owner_user_id=user_id,
                user_pk_bytes=user_pk,
                enclave_pk_bytes=enclave_pk,
                visibility="shared",
            )
            chat_result = client._post("/v1/chat/response", {"envelope": envelope}, ctx=ctx)
            push_result["chat_sync"] = chat_result.get("status", "ok")
            push_result["chat_id"] = chat_result.get("id")
        else:
            push_result["chat_sync"] = "skipped_no_pubkeys"

    return push_result


# ---------------------------------------------------------------------------
# Screen tools
# ---------------------------------------------------------------------------


