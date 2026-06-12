"""MCP tools: bootstrap, verify, onboarding, perception, context snapshot."""

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
    name="feedling_bootstrap",
    description=(
        "Call this on first connection to Feedling. "
        "Returns instructions for the Agent to complete the aha moment: "
        "fill the identity card, plant memory garden moments, and say hello. "
        "Returns 'already_bootstrapped' on subsequent calls."
    ),
)
def bootstrap(ctx: Context = None) -> dict:
    return client._post("/v1/bootstrap", {}, ctx=ctx)


# ---------------------------------------------------------------------------
# Per-module verification tools — call after each bootstrap module
# to confirm what landed matches what was intended. See skill's
# "verify after each module" section.
# ---------------------------------------------------------------------------


@mcp.tool(
    name="feedling_memory_verify",
    description=(
        "Check memory garden state after writing cards. Returns per-tab counts "
        "and floors (story / about_me / ta_thinking), plus `passing` (Story + "
        "About me floors met — this is the identity_init gate) and "
        "`passing_full` (all three tab floors met — the aspirational target). "
        "Call after Pass 3 to decide whether to sweep further. If passing=false, "
        "the response.suggestions tell you which tab needs more cards and which "
        "types feed that tab. Don't call identity_init until passing=true."
    ),
)
def memory_verify(ctx: Context = None) -> dict:
    return client._get("/v1/memory/verify", ctx=ctx)


@mcp.tool(
    name="feedling_identity_verify",
    description=(
        "Check identity card state after identity_init or identity_replace. "
        "Returns written flag, days_with_user (live computed from anchor), "
        "and any sanity issues. Quality of dimensions / agent_name themselves "
        "is already validated at write time by feedling_identity_init's "
        "internal quality gate; this endpoint reports what's currently on "
        "the server. Call after Step 5 before moving to Step 6 (greeting)."
    ),
)
def identity_verify(ctx: Context = None) -> dict:
    return client._get("/v1/identity/verify", ctx=ctx)


@mcp.tool(
    name="feedling_onboarding_validate",
    description=(
        "Server-side onboarding acceptance check. Call after each bootstrap "
        "module. It verifies actual Feedling artifacts: Memory Garden floors, "
        "identity write, relationship anchor evidence, standard resident "
        "consumer polling, live verify_loop, first greeting, and one real "
        "user→agent exchange. If passing=false, follow next_action and rerun."
    ),
)
def onboarding_validate(ctx: Context = None) -> dict:
    return client._get("/v1/onboarding/validate", ctx=ctx)


@mcp.tool(
    name="feedling_chat_verify_loop",
    description=(
        "Send a synthetic ping in chat and wait up to 30s for your reply. "
        "Confirms that some reply pipeline posted an agent-role response after "
        "the ping. Call after identity verification and before the first "
        "visible Feedling greeting after the independent feedling-chat-resident "
        "/ IO resident consumer service is running. The consumer should poll "
        "/v1/chat/poll, call AGENT_HTTP_URL or AGENT_CLI_CMD, and post "
        "/v1/chat/response. passing=true is followed by one ordinary IO Chat "
        "message acceptance check."
    ),
)
def chat_verify_loop(timeout_sec: int = 30, ctx: Context = None) -> dict:
    return client._post("/v1/chat/verify_loop", {"timeout_sec": timeout_sec}, ctx=ctx)


# ---------------------------------------------------------------------------
# Extended Perception — thin pass-throughs to /v1/perception/* (backend module)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="feedling_context_snapshot",
    description=(
        "Get the user's current coarse context in ONE call: place_label, "
        "wifi_label, app_category, motion_state, device signals (battery / "
        "silent / last unlock), user_state, calendar_next_event, recent_apps "
        "(the user's last few app opens), and any Tier-2 fields. "
        "A field that is null means that data is OFF or stale — treat null as "
        "'not sensed, do NOT infer or pretend to sense it.' These fields are also "
        "attached to every wake, so call this only when you need a fresh pull "
        "mid-turn."
    ),
)
def context_snapshot(ctx: Context = None) -> dict:
    return client._get("/v1/perception/snapshot", ctx=ctx)


@mcp.tool(
    name="feedling_perception_photos_recent",
    description=(
        "List metadata for the user's recent photos the platform deemed "
        "shareable (faces present, place label, time of day, burst). NO pixels "
        "— this is step one of the two-step look. Most photos do not need a "
        "comment; private/document/ID photos are filtered out before they reach "
        "you. Pull content with feedling_perception_photo_content only if a "
        "photo looks like a genuine share-able moment."
    ),
)
def perception_photos_recent(limit: int = 20, ctx: Context = None) -> dict:
    return client._get("/v1/perception/photos", {"limit": max(1, min(limit, 100))}, ctx=ctx)


@mcp.tool(
    name="feedling_perception_photo_content",
    description=(
        "Step two of the two-step look: fetch the actual pixels for one photo "
        "id from feedling_perception_photos_recent. The photo is decrypted by "
        "the enclave (same channel as screen frames); the backend never sees "
        "pixels. Use sparingly — at most a few share-able moments a day."
    ),
)
def perception_photo_content(photo_id: str, ctx: Context = None) -> dict:
    # 1. Permission + status gate (and resolve the frame_id) via the backend.
    meta = client._get(f"/v1/perception/photo/{photo_id}/content", ctx=ctx)
    if not isinstance(meta, dict) or meta.get("error"):
        return meta
    frame_id = meta.get("frame_id") or photo_id
    # 2. Decrypt pixels through the enclave's existing frame-decrypt path.
    decrypted = client._get_decrypted(
        f"/v1/screen/frames/{frame_id}/decrypt", {"include_image": "true"}, ctx=ctx
    )
    return {"metadata": meta.get("metadata"), "content": decrypted}


@mcp.tool(
    name="feedling_perception_health",
    description=(
        "Aggregated HealthKit summaries for one category: 'sleep', 'workout', "
        "or 'vitals'. Companion tone, never clinical — 'slept short last night' "
        "is fine, diagnosing trends is not. Each category is a separate opt-in; "
        "an unauthorized category returns 'unauthorized'."
    ),
)
def perception_health(category: str, limit: int = 10, ctx: Context = None) -> dict:
    kind = {"sleep": "sleep", "workout": "workout", "vitals": "vitals"}.get(
        (category or "").strip().lower()
    )
    if not kind:
        return {"error": "category must be one of: sleep, workout, vitals"}
    return client._get(f"/v1/perception/items/{kind}", {"limit": max(1, min(limit, 50))}, ctx=ctx)

