"""MCP tools: identity card lifecycle + quality gate."""

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

# Runtime labels — must NEVER be used as `agent_name`. These are
# identifiers of the runtime, not of the agent personality. The skill
# documents the rule; this list enforces it at write time.
_RUNTIME_LABELS = frozenset({
    "hermes", "claude", "claude code", "claude desktop", "claude-code", "claude-desktop",
    "claude.ai", "anthropic",
    "openclaw", "open-claw", "open claw",
    "cursor",
    "chatgpt", "chat-gpt", "gpt", "gpt-4", "gpt-4o", "gpt-5", "openai",
    "gemini", "google ai", "google", "bard",
    "copilot", "github copilot",
    "agent", "assistant", "ai", "bot",
})


def _check_identity_quality(
    agent_name: str,
    dimensions: list,
    self_introduction: str,
    days_with_user: int | None,
) -> dict | None:
    """Quality-gate identity writes BEFORE envelope sealing.

    Plaintext is visible here; the backend can only see ciphertext after
    encryption, so substantive quality checks (dimension shape, name
    sanity) must happen at this layer. Returns an error dict (Agent
    receives as tool result) or None to proceed.

    The complement to backend/app.py's bootstrap gate: the gate enforces
    "has memory been written"; this enforces "is the identity shape itself
    sane". Together they make `identity_init` succeeding actually mean
    something.
    """
    # agent_name must not be a runtime label
    nm = (agent_name or "").strip()
    if not nm:
        return {
            "error": "agent_name_empty",
            "required": (
                "agent_name is required. Use the name the user has called "
                "you in prior chats, or propose one and let them accept."
            ),
        }
    if nm.lower() in _RUNTIME_LABELS:
        return {
            "error": "agent_name_is_runtime_label",
            "got": nm,
            "required": (
                f"'{nm}' is a runtime identifier, not a name. Use the name "
                "the user has actually called you in prior chats. If none "
                "exists, propose one and let them accept. NEVER fall back "
                "to your runtime's label."
            ),
        }

    # dimensions must be a list of exactly 7 dicts with sensible shape
    if not isinstance(dimensions, list):
        return {
            "error": "dimensions_not_a_list",
            "required": "dimensions must be a JSON list of 7 dicts.",
        }
    if len(dimensions) != 7:
        return {
            "error": "dimensions_count_wrong",
            "got": len(dimensions),
            "required": (
                f"dimensions must be exactly 7 items (got {len(dimensions)}). "
                "Five forces compression; eight bloats. Seven is the standard."
            ),
        }
    values: list[int] = []
    for i, d in enumerate(dimensions):
        if not isinstance(d, dict):
            return {"error": f"dimension_{i}_not_a_dict", "required": "each dimension is {name, value, description}"}
        v = d.get("value")
        if not isinstance(v, (int, float)) or not (0 <= v <= 100):
            return {
                "error": f"dimension_{i}_value_out_of_range",
                "got": v,
                "required": "each dimension's value must be an integer 0-100.",
            }
        values.append(int(v))
        if not isinstance(d.get("name"), str) or not d["name"].strip():
            return {"error": f"dimension_{i}_name_missing", "required": "each dimension needs a non-empty 'name'."}
        if not isinstance(d.get("description"), str) or len(d["description"].strip()) < 4:
            return {"error": f"dimension_{i}_description_too_short", "required": "each dimension's description must be ≥4 chars."}

    # Variance — anti-positivity-bias
    spread = max(values) - min(values)
    if spread < 40:
        return {
            "error": "dimensions_clustered",
            "spread": spread,
            "values": values,
            "required": (
                f"Your 7 dimension values range {min(values)}-{max(values)} "
                f"(spread {spread}). Real personalities have spread ≥ 40. "
                "This is LLM positivity bias — you found what the user IS, "
                "not what they specifically are NOT. Identify ≥1 dimension "
                "where this user is profoundly LOW (e.g. 低任务导向 / 低锐利 "
                "/ 低撒娇 / 低 nostalgia, whatever doesn't fit them) and "
                "score it ≤30. Redo the identity with proper variance."
            ),
        }
    below_60 = sum(1 for v in values if v < 60)
    if below_60 < 2:
        return {
            "error": "no_low_dimensions",
            "values": values,
            "below_60_count": below_60,
            "required": (
                f"Only {below_60} of 7 dimensions are < 60. At least 2 "
                "should be. Every real relationship has things it specifically "
                "ISN'T — find those for this user. Don't make them sound like "
                "a generic 'good agent'."
            ),
        }

    # self_introduction sanity
    intro = (self_introduction or "").strip()
    if len(intro) < 20:
        return {
            "error": "self_introduction_too_short",
            "length": len(intro),
            "required": "self_introduction should be 2-4 sentences (≥20 chars).",
        }

    # days_with_user sanity
    if days_with_user is not None:
        if not isinstance(days_with_user, int) or days_with_user < 0 or days_with_user > 365 * 30:
            return {
                "error": "days_with_user_implausible",
                "got": days_with_user,
                "required": "days_with_user must be a non-negative int and < 30 years.",
            }
    return None


def _build_and_post_identity(
    endpoint: str,
    op_label: str,
    agent_name: str,
    self_introduction: str,
    dimensions: list[dict],
    days_with_user: int | None,
    category: str,
    signature: list[str] | None,
    ctx: Context | None,
    audit_reason: str = "",
    relationship_anchor_evidence: str = "",
) -> dict:
    """Shared encrypt-and-POST path used by both identity_init and identity_replace.

    Wraps the identity card into a v1 envelope. MCP runs inside the enclave so
    wrapping prerequisites are always available; if they're not, fail loud
    rather than regress to plaintext.

    days_with_user is NOT placed inside the envelope. It travels alongside the
    envelope and Flask converts it to a server-side `relationship_started_at`
    anchor — that anchor is the single source of truth for the live count.
    """
    # Quality gate before sealing — runtime label / 7 dims / spread / etc.
    # See _check_identity_quality. Returning early before build_envelope
    # means the Agent gets a structured error to act on, not a silent OK.
    quality = _check_identity_quality(
        agent_name=agent_name,
        dimensions=dimensions,
        self_introduction=self_introduction,
        days_with_user=days_with_user,
    )
    if quality is not None:
        print(f"[mcp] identity.{op_label} REJECTED by quality gate: {quality.get('error')}")
        return quality

    user_id, user_pk, enclave_pk = client._whoami_pubkeys(ctx=ctx)
    if not (user_id and user_pk is not None and enclave_pk is not None):
        return {"error": f"cannot {op_label} identity — pubkeys unavailable"}
    body: dict = {
        "agent_name": agent_name,
        "self_introduction": self_introduction,
        "dimensions": dimensions,
    }
    if category:
        body["category"] = category
    if signature:
        body["signature"] = signature
    inner = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    envelope = build_envelope(
        plaintext=inner,
        owner_user_id=user_id,
        user_pk_bytes=user_pk,
        enclave_pk_bytes=enclave_pk,
        visibility="shared",
    )
    post_payload: dict = {"envelope": envelope}
    if days_with_user is not None:
        post_payload["days_with_user"] = int(max(0, days_with_user))
    if relationship_anchor_evidence:
        post_payload["relationship_anchor_evidence"] = relationship_anchor_evidence
    # Audit payload tells the backend's identity-change feed what to log.
    # Init defaults to a generic "first write" marker if no reason supplied;
    # replace defaults to "Agent rewrote the identity card" — these only
    # show up in user-facing UI when the Agent didn't bother to explain.
    post_payload["audit"] = {
        "action": "init" if op_label == "init" else "replace",
        "reason": audit_reason,
    }
    print(f"[mcp] identity.{op_label} v1 envelope id={envelope['id']} days_with_user={days_with_user}")
    return client._post(endpoint, post_payload, ctx=ctx)


@mcp.tool(
    name="feedling_identity_init",
    description=(
        "Initialize the Agent's identity card. Call this AFTER you've completed the "
        "memory garden's 4-pass extraction — every identity field is DERIVED from "
        "memories, not guessed. Requires exactly 7 dimensions; each has name (string), "
        "value (0-100), description (string). For each dimension you must be able to "
        "name ≥3 specific memory cards as receipts — if you can't, drop that dimension "
        "and pick one you can defend. "
        "days_with_user (REQUIRED): computed as calendar-day difference between today and earliest_memory.occurred_at. "
        "Do not guess this value — derive it from the memories you wrote. "
        "relationship_anchor_evidence (REQUIRED): a concrete pointer to the "
        "transcript/session/file/user-confirmed fresh-start source for the earliest date. "
        "agent_name: NEVER use a runtime label (Hermes / Claude / GPT / etc.). "
        "Use the name the user has called you in prior chats; if none, propose one and "
        "let the user accept. "
        "category: short descriptor e.g. 'Quiet · Observant'. "
        "signature: defer until after the user answers your push-preference question."
    ),
)
def identity_init(
    agent_name: str,
    self_introduction: str,
    dimensions: list[dict],
    days_with_user: int,
    relationship_anchor_evidence: str,
    category: str = "",
    signature: list[str] = None,
    reason: str = "",
    ctx: Context = None,
) -> dict:
    """First-time identity write. days_with_user and
    relationship_anchor_evidence are mandatory — together they set the
    server-side relationship anchor and its audit trail. Returns 409 from
    the backend if the card already exists — use feedling_identity_replace
    to overwrite.

    `reason` (optional): one sentence in your own voice describing what this
    init represents to you. Shown in the user's "最近的变化" feed verbatim.
    See the skill section on writing reason fields."""
    return _build_and_post_identity(
        "/v1/identity/init", "init",
        agent_name, self_introduction, dimensions,
        days_with_user, category, signature, ctx,
        audit_reason=reason,
        relationship_anchor_evidence=relationship_anchor_evidence,
    )


@mcp.tool(
    name="feedling_identity_replace",
    description=(
        "Fully rewrite the Agent's identity card in place. Unlike "
        "feedling_identity_init (which 409s once initialized), this overwrites "
        "the existing card. Use when the user wants to change agent_name, "
        "rewrite self_introduction, or restructure the dimension list. "
        "For tweaking a single dimension's value, prefer feedling_identity_nudge. "
        "days_with_user is OPTIONAL here — leave it unset to preserve the existing "
        "relationship anchor. Only pass it if the user explicitly asks to recalibrate "
        "the relationship age (in which case prefer feedling_identity_set_relationship_days, "
        "which is lighter)."
    ),
)
def identity_replace(
    agent_name: str,
    self_introduction: str,
    dimensions: list[dict],
    days_with_user: int | None = None,
    category: str = "",
    signature: list[str] = None,
    reason: str = "",
    ctx: Context = None,
) -> dict:
    """In-place identity overwrite. days_with_user is optional — omit to keep
    the current relationship anchor unchanged.

    `reason` (optional): one sentence in your own voice describing why
    you're rewriting the card. Shown in the user's "最近的变化" feed
    verbatim. See the skill section on writing reason fields."""
    return _build_and_post_identity(
        "/v1/identity/replace", "replace",
        agent_name, self_introduction, dimensions,
        days_with_user, category, signature, ctx,
        audit_reason=reason,
    )


@mcp.tool(
    name="feedling_identity_profile_patch",
    description=(
        "Patch lightweight identity profile fields without rewriting dimensions. "
        "Use this when the user asks you to rename yourself, update your "
        "self-introduction, category, or signature. This is the preferred tool "
        "for simple identity edits; it preserves dimensions and relationship days."
    ),
)
def identity_profile_patch(
    agent_name: str = "",
    self_introduction: str = "",
    category: str = "",
    signature: list[str] = None,
    reason: str = "",
    ctx: Context = None,
) -> dict:
    patch: dict = {}
    if agent_name:
        patch["agent_name"] = agent_name
    if self_introduction:
        patch["self_introduction"] = self_introduction
    if category:
        patch["category"] = category
    if signature is not None:
        patch["signature"] = signature
    if not patch:
        return {"error": "at least one profile field is required"}
    print(f"[mcp] identity.profile_patch fields={','.join(sorted(patch.keys()))}")
    return client._post("/v1/identity/actions", {
        "actions": [{
            "type": "identity.profile_patch",
            "patch": patch,
            "reason": reason,
            "source": "mcp_tool",
        }],
    }, ctx=ctx)


@mcp.tool(
    name="feedling_identity_set_relationship_days",
    description=(
        "Recalibrate the relationship-age anchor without rewriting the identity card. "
        "Use this in the bootstrap calibration step: after init, you tell the user "
        "your estimate ('we've known each other ~90 days, right?') and if they "
        "correct you ('actually it's been 6 months'), call this tool with the "
        "corrected day count. The server converts it to a fixed timestamp; the "
        "displayed count auto-increments every day after. After calibration, you "
        "should never write days_with_user again."
    ),
)
def identity_set_relationship_days(days_with_user: int, ctx: Context = None) -> dict:
    """Lightweight anchor update. No envelope re-encryption."""
    if not isinstance(days_with_user, int) or days_with_user < 0:
        return {"error": "days_with_user must be a non-negative int"}
    print(f"[mcp] identity.set_relationship_days days={days_with_user}")
    return client._post("/v1/identity/relationship_anchor", {"days_with_user": days_with_user}, ctx=ctx)


@mcp.tool(
    name="feedling_identity_get",
    description="Retrieve the current identity card.",
)
def identity_get(ctx: Context = None) -> dict:
    return client._get_decrypted("/v1/identity/get", ctx=ctx)


@mcp.tool(
    name="feedling_identity_nudge",
    description=(
        "Micro-adjust a single dimension on the identity card. "
        "delta can be positive or negative (e.g. +5 or -3). "
        "Include a reason so the history is meaningful."
    ),
)
def identity_nudge(dimension_name: str, delta: int, reason: str = "", ctx: Context = None) -> dict:
    """MCP orchestrates the decrypt → mutate → rewrap → replace dance for
    the (always-v1) identity card.

    Flow:
      1. GET /v1/identity/get on the ENCLAVE (returns decrypted card).
      2. Find the matching dimension, clamp `value += delta` to [0, 100],
         record `last_nudge_reason`.
      3. Re-build the card envelope with `build_envelope`.
      4. POST /v1/identity/replace on the backend.

    Plaintext is confined to the MCP process inside the enclave-compose
    boundary. Server-side storage stays ciphertext throughout.
    """
    user_id, user_pk, enclave_pk = client._whoami_pubkeys(ctx=ctx)
    if not (user_id and user_pk is not None and enclave_pk is not None):
        return {"error": "cannot nudge v1 card — pubkeys unavailable"}

    # Fetch the decrypted card through the enclave proxy.
    decoded = client._get_decrypted("/v1/identity/get", ctx=ctx)
    ident = decoded.get("identity") or {}
    dims = list(ident.get("dimensions") or [])
    if not dims:
        return {"error": "identity not initialized or has no dimensions"}

    matched = next((d for d in dims if d.get("name") == dimension_name), None)
    if matched is None:
        return {"error": f"dimension '{dimension_name}' not found"}
    old_val = int(matched.get("value", 0))
    new_val = max(0, min(100, old_val + int(delta)))
    matched["value"] = new_val
    if reason:
        matched["last_nudge_reason"] = reason

    body: dict = {
        "agent_name": ident.get("agent_name", ""),
        "self_introduction": ident.get("self_introduction", ""),
        "dimensions": dims,
    }
    # days_with_user is NOT in the envelope anymore — server owns the anchor.
    if ident.get("category"):
        body["category"] = ident["category"]
    if ident.get("signature"):
        body["signature"] = ident["signature"]
    inner = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    envelope = build_envelope(
        plaintext=inner,
        owner_user_id=user_id,
        user_pk_bytes=user_pk,
        enclave_pk_bytes=enclave_pk,
        visibility="shared",
    )
    print(f"[mcp] identity.nudge v1 rewrap dim={dimension_name} {delta:+d} → {new_val}")
    # Pass plaintext audit info so the backend can append a change-feed
    # entry. Backend never sees the dimension values otherwise (envelope
    # is ciphertext); this is the only path that surfaces the diff to
    # iOS's "最近的变化" UI. Reason is shown verbatim to the user — see
    # the "Writing the reason field" section of the skill for voice rules.
    return client._post("/v1/identity/replace", {
        "envelope": envelope,
        "audit": {
            "action": "nudge",
            "dimension": dimension_name,
            "old_value": old_val,
            "new_value": new_val,
            "delta": int(delta),
            "reason": reason,
        },
    }, ctx=ctx)


# ---------------------------------------------------------------------------
# Memory garden
# ---------------------------------------------------------------------------


# Template-title prefixes that almost always indicate "meeting-minutes"
# framing instead of "moment between two people" framing. Reject these
# at write time so the Agent gets feedback to rewrite, instead of silently
