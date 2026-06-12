"""MCP tools: chat post/history (+ tagged-reasoning split)."""

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

_TAGGED_REASONING_RE = re.compile(
    r"<\s*(?P<tag>think|thinking|reasoning|thought)\s*>\s*"
    r"(?P<body>.*?)"
    r"\s*<\s*/\s*(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _split_tagged_reasoning(content: str) -> tuple[str, str]:
    """Split leaked reasoning tags out of user-visible chat text.

    Some upstream runtimes serialize their own reasoning stream into the final
    text as `<think>...</think>` or similar. That is not provider-native
    reasoning from IO's point of view, but it is useful display material and
    should not leak into the visible chat bubble.
    """
    text = str(content or "")
    blocks: list[str] = []

    def _collect(match: re.Match) -> str:
        body = (match.group("body") or "").strip()
        if body:
            blocks.append(body)
        return "\n"

    visible = _TAGGED_REASONING_RE.sub(_collect, text)
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    reasoning = "\n\n".join(blocks).strip()
    return visible, reasoning


@mcp.tool(
    name="feedling_chat_post_message",
    description=(
        "Post a message from the Agent into the Feedling iOS chat window. "
        "Optionally mirror the same text to Live Activity in the same backend call "
        "to reduce chat/live divergence. If your runtime has provider-native "
        "reasoning, a runtime trace, or a display-safe agent summary, pass it "
        "as reasoning_text so IO can show it in the expandable area above the "
        "chat bubble. Do not invent reasoning_text when the runtime did not "
        "produce one."
    ),
)
def chat_post_message(
    content: str,
    push_live_activity: bool = False,
    push_body: str = "",
    title: str = "",
    subtitle: str = "",
    data: dict | None = None,
    reasoning_text: str = "",
    reasoning_kind: str = "",
    reasoning_source: str = "",
    reasoning_model: str = "",
    reasoning_native: bool = False,
    ctx: Context = None,
) -> dict:
    """Agent posts a reply as a v1 envelope.

    Reliability note:
    - When `push_live_activity=True`, this sends chat write + live activity trigger
      through ONE `/v1/chat/response` request (same backend code path), which avoids
      split-brain failures where push succeeds but chat writeback is missed.
    """
    user_id, user_pk, enclave_pk = client._whoami_pubkeys(ctx=ctx)
    if not (user_id and user_pk is not None and enclave_pk is not None):
        return {"error": "cannot post chat — pubkeys unavailable"}

    visible_content, tagged_reasoning = _split_tagged_reasoning(content)
    if tagged_reasoning and not visible_content:
        return {"error": "content contained only reasoning tags; no user-visible reply"}

    envelope = build_envelope(
        plaintext=visible_content.encode("utf-8"),
        owner_user_id=user_id,
        user_pk_bytes=user_pk,
        enclave_pk_bytes=enclave_pk,
        visibility="shared",
    )

    payload: dict = {"envelope": envelope}
    safe_reasoning = str(reasoning_text or "").strip()
    parsed_tagged_reasoning = False
    if not safe_reasoning and tagged_reasoning:
        safe_reasoning = tagged_reasoning
        parsed_tagged_reasoning = True
    if safe_reasoning:
        thinking_envelope = build_envelope(
            plaintext=safe_reasoning.encode("utf-8"),
            owner_user_id=user_id,
            user_pk_bytes=user_pk,
            enclave_pk_bytes=enclave_pk,
            visibility="shared",
        )
        payload["thinking_envelope"] = thinking_envelope
        payload["reasoning_kind"] = str(
            reasoning_kind or ("provider_reasoning_summary" if parsed_tagged_reasoning else "agent_summary")
        ).strip()
        payload["reasoning_source"] = str(
            reasoning_source or ("mcp_tagged_content" if parsed_tagged_reasoning else "mcp")
        ).strip()
        if reasoning_model:
            payload["reasoning_model"] = str(reasoning_model).strip()
        payload["reasoning_native"] = False if parsed_tagged_reasoning else bool(reasoning_native)
    # Plaintext for the APNs alert push. MCP has plaintext at this point
    # (we just sealed it), so we hand it directly to Flask — the server
    # never decrypts the envelope itself. Apple's APNs gateway sees this
    # string, same privacy posture as Live Activity push.
    payload["alert_body"] = visible_content
    if push_live_activity:
        payload["push_live_activity"] = True
        payload["push_body"] = push_body or visible_content
        payload["title"] = title or ""
        if subtitle:
            payload["subtitle"] = subtitle
        if data:
            payload["data"] = data

    print(
        f"[mcp] chat.post_message v1 envelope id={envelope['id']} "
        f"push_live_activity={bool(push_live_activity)} "
        f"reasoning={bool(safe_reasoning)}"
    )
    return client._post("/v1/chat/response", payload, ctx=ctx)


@mcp.tool(
    name="feedling_chat_post_image",
    description=(
        "Post an IMAGE message from the Agent into the user's chat window. "
        "Use this when sharing what you see is genuinely valuable — generated "
        "screenshots, vision-derived images, found images you want the user "
        "to look at. Don't post decorative or redundant images. "
        "Image and text are separate messages: this tool only takes the image. "
        "If you want to caption the image, send `feedling_chat_post_message` "
        "as a separate message. "
        "Privacy hard rule: NEVER include content from the user's screen "
        "(decrypt_frame outputs) — agent seeing the screen ≠ user wanting "
        "the screen archived in their chat history."
    ),
)
def chat_post_image(
    image_b64: str,
    ctx: Context = None,
) -> dict:
    """Agent posts an image (base64-encoded JPEG/PNG, ≤ 1 MB after decode)
    as a v1 chat envelope with content_type=image."""
    if not image_b64 or not isinstance(image_b64, str):
        return {"error": "image_b64 required (non-empty base64 string)"}
    try:
        # Strip optional data-URL prefix if the caller included one.
        b64 = image_b64.split(",", 1)[1] if image_b64.startswith("data:") else image_b64
        image_bytes = base64.b64decode(b64, validate=True)
    except Exception as e:
        return {"error": f"image_b64 base64 decode failed: {e}"}
    if len(image_bytes) == 0:
        return {"error": "image_b64 decoded to 0 bytes"}
    if len(image_bytes) > 1_048_576:
        return {"error": f"image too large: {len(image_bytes)} bytes (max 1 MB)"}

    user_id, user_pk, enclave_pk = client._whoami_pubkeys(ctx=ctx)
    if not (user_id and user_pk is not None and enclave_pk is not None):
        return {"error": "cannot post chat — pubkeys unavailable"}

    envelope = build_envelope(
        plaintext=image_bytes,
        owner_user_id=user_id,
        user_pk_bytes=user_pk,
        enclave_pk_bytes=enclave_pk,
        visibility="shared",
    )
    # Generic alert body for image messages — agent didn't supply a caption
    # (per spec, image and text are separate messages). User taps in to see.
    payload: dict = {
        "envelope": envelope,
        "content_type": "image",
        "alert_body": "[image]",
    }
    print(
        f"[mcp] chat.post_image v1 envelope id={envelope['id']} bytes={len(image_bytes)}"
    )
    return client._post("/v1/chat/response", payload, ctx=ctx)


@mcp.tool(
    name="feedling_chat_get_history",
    description=(
        "Retrieve recent chat history between the user and the Agent. The "
        "response includes a `context_memories` field — up to 8 plaintext "
        "memory cards the server selected as relevant to this conversation "
        "moment (turning points + recent + keyword overlap with the latest "
        "user message). Read both `messages` and `context_memories` before "
        "composing your reply. Weave relevant memories naturally — pretend "
        "you 'just remembered,' not 'looked up.' Don't reference cards by id, "
        "don't say 'according to memory X.' If none feel relevant to the "
        "current exchange, ignore them — irrelevant references hurt more "
        "than they help. "
        "Image messages (content_type='image') return as TWO things: "
        "(1) a marker `<vision_block:N>` in the message's `image_b64` field, "
        "and (2) the actual JPEG as an ImageContent block at index N in this "
        "tool's response. Vision-capable agents see the image automatically. "
        "Acknowledge what you see; do NOT echo the marker text to the user."
    ),
    output_schema=None,
)
def chat_get_history(limit: int = 50, ctx: Context = None):
    """Returns chat history. For text-only history, returns a single dict.
    For history containing image messages, returns a list:
    `[history_dict, Image(jpeg1), Image(jpeg2), ...]` — the dict has its
    image_b64 fields replaced with `<vision_block:N>` markers that index
    into the trailing Image blocks. FastMCP serializes this multi-block
    return so the agent's vision actually activates on each image, rather
    than receiving the base64 as opaque text (which is what happened
    before this change — image messages silently broke agent replies).
    """
    params = {"limit": min(limit, 200)}
    raw = client._get_decrypted("/v1/chat/history", params, ctx=ctx)
    if not isinstance(raw, dict) or "messages" not in raw:
        return raw

    # Synthetic verify pings are intentionally local_only and carry no
    # K_enclave, so the enclave decrypt path cannot recover their sentinel
    # text. The Flask history stores that sentinel as plaintext `content`
    # only for source=verify_ping; merge it back by id so resident consumers
    # using MCP as their decrypt source can answer liveness pings. Do not
    # copy plaintext for normal chat messages.
    try:
        plain = client._get("/v1/chat/history", params, ctx=ctx)
        plain_by_id = {
            m.get("id"): m
            for m in plain.get("messages", [])
            if isinstance(m, dict) and m.get("source") == "verify_ping" and m.get("content")
        }
    except Exception:
        plain_by_id = {}

    if plain_by_id:
        for m in raw.get("messages", []):
            if not isinstance(m, dict) or m.get("content"):
                continue
            plain_msg = plain_by_id.get(m.get("id"))
            if plain_msg:
                m["content"] = plain_msg["content"]
                m["source"] = plain_msg.get("source", m.get("source"))

    image_blocks: list = []
    for m in raw.get("messages", []):
        if not isinstance(m, dict):
            continue
        if m.get("content_type") != "image":
            continue
        b64 = m.get("image_b64") or ""
        if not b64:
            continue
        try:
            jpeg_bytes = base64.b64decode(b64)
        except Exception as e:
            m["image_b64"] = f"<decode_failed: {e}>"
            continue
        marker_idx = len(image_blocks)
        image_blocks.append(Image(data=jpeg_bytes, format="jpeg"))
        # Replace the (large) base64 with a small marker so the JSON text
        # block stays compact; the actual image data is now in the
        # corresponding ImageContent block at position marker_idx + 1.
        m["image_b64"] = f"<vision_block:{marker_idx}>"

    if image_blocks:
        print(f"[mcp] chat_get_history: surfacing {len(image_blocks)} image(s) as ImageContent blocks")
        return [raw, *image_blocks]
    return raw


# ---------------------------------------------------------------------------
# Identity card
# ---------------------------------------------------------------------------

