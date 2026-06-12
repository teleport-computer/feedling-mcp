"""MCP tools: screen frames, analyze, decrypt."""

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
    name="feedling_screen_latest_frame",
    description=(
        "Metadata ONLY for the most recent screen frame (timestamp, frame id, "
        "filename, envelope url). Every frame is a v1 envelope, so app/ocr_text "
        "come back empty and there is no plaintext image here. To actually SEE "
        "the screen — pixels + real OCR text — call feedling_screen_decrypt_frame "
        "(it defaults to the latest frame)."
    ),
)
def screen_latest_frame(ctx: Context = None) -> dict:
    return client._get("/v1/screen/frames/latest", ctx=ctx)


@mcp.tool(
    name="feedling_screen_frames_list",
    description=(
        "List recent screen frame metadata (timestamp, frame id, filename) from "
        "the user's iOS device. Frames are v1 envelopes so app and ocr_text in "
        "this listing are always empty — use feedling_screen_decrypt_frame with a "
        "specific frame_id to get real OCR + app + pixels for any frame. limit "
        "defaults to 20, max 100."
    ),
)
def screen_frames_list(limit: int = 20, ctx: Context = None) -> dict:
    return client._get("/v1/screen/frames", {"limit": max(1, min(limit, 100))}, ctx=ctx)


@mcp.tool(
    name="feedling_screen_analyze",
    description=(
        "Get a structured analysis of the user's current screen activity: "
        "foreground app, OCR summary, and whether the push cooldown has elapsed."
    ),
)
def screen_analyze(ctx: Context = None) -> dict:
    return client._get("/v1/screen/analyze", ctx=ctx)


@mcp.tool(
    name="feedling_screen_summary",
    description=(
        "Get today's screen-time rollup for the user (iOS + Mac): total minutes, "
        "top app, top category, pickups. Aggregated server-side from the last 24h "
        "of frames. Use for daily-report-style questions."
    ),
)
def screen_summary(ctx: Context = None) -> dict:
    return client._get("/v1/screen/summary", ctx=ctx)


@mcp.tool(
    name="feedling_screen_decrypt_frame",
    description=(
        "Decrypt a screen-frame envelope and return the actual pixels + OCR "
        "text so the Agent can SEE the frame. Runs inside the enclave — the "
        "plaintext never leaves the TDX boundary except on the wire back to "
        "the authenticated caller. If frame_id is omitted, the most recent "
        "frame is used. Returns a list with the JPEG image (so vision "
        "activates) and a text block containing ocr_text + app + ts metadata."
    ),
    output_schema=None,
)
def screen_decrypt_frame(
    frame_id: str = "",
    include_image: bool = True,
    ctx: Context = None,
):
    """Resolve a frame id (or pick the latest), ask the enclave to
    decrypt, and return an MCP content list the agent can consume:

        [ Image(jpeg_bytes, format="jpeg"),   # vision block
          "{json metadata with ocr_text}"     # text block ]

    If include_image is False, returns a dict with ocr_text + metadata
    only — useful when the caller just wants text and wants to avoid the
    bandwidth cost of shipping JPEG base64.
    """
    if not client.ENCLAVE_BASE:
        return {"error": "enclave not configured — FEEDLING_ENCLAVE_URL missing"}

    # Resolve frame_id lazily — empty means "latest".
    fid = (frame_id or "").strip()
    if not fid:
        try:
            listing = client._get("/v1/screen/frames", {"limit": 1}, ctx=ctx)
        except httpx.HTTPError as e:
            return {"error": f"frames_list_failed: {e}"}
        frames = listing.get("frames") or []
        if not frames:
            return {"error": "no frames on record yet"}
        fid = frames[0].get("id") or ""
        if not fid:
            return {"error": "latest frame has no id"}

    try:
        r = client._ENCLAVE_HTTP.get(
            f"{client.ENCLAVE_BASE}/v1/screen/frames/{fid}/decrypt",
            headers=client._headers(ctx),
            params={"include_image": "true" if include_image else "false"},
        )
        r.raise_for_status()
        payload = r.json()
    except httpx.HTTPError as e:
        return {"error": f"enclave_decrypt_failed: {e}", "frame_id": fid}

    if payload.get("error"):
        return payload

    metadata = {k: v for k, v in payload.items() if k not in ("image_b64",)}
    if not include_image:
        # Return a plain dict so callers don't need to special-case list payloads.
        return metadata

    img_b64 = payload.get("image_b64") or ""
    if not img_b64:
        return {"warning": "decrypt ok but no image_b64 in plaintext", **metadata}

    try:
        jpeg_bytes = base64.b64decode(img_b64)
    except Exception as e:
        return {"error": f"image_b64_decode: {e}", **metadata}

    api_key = client._current_api_key(ctx)
    if api_key:
        client._recent_decrypt_by_api_key[api_key] = {
            "frame_id": fid,
            "ts": time.time(),
            "include_image": bool(include_image),
            "ocr_chars": len(metadata.get("ocr_text") or ""),
        }

    print(f"[mcp] decrypt_frame id={fid} bytes={len(jpeg_bytes)} ocr_chars={len(metadata.get('ocr_text') or '')}")
    # FastMCP serializes list returns as a multi-block MCP tool result:
    # the Image becomes an ImageContent the agent's vision reads, and the
    # dict becomes structuredContent + a JSON-serialized text block.
    return [Image(data=jpeg_bytes, format="jpeg"), metadata]


# ---------------------------------------------------------------------------
# Chat tools
