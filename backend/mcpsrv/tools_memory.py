"""MCP tools: Memory Garden writes + quality gate."""

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

_TEMPLATE_TITLE_PREFIXES = (
    "我们讨论了", "我们决定了", "我们聊了", "我们完成了", "我们解决了",
    "完成了", "解决了", "决定了",
    "we discussed", "we decided", "we resolved", "we completed",
    "discussed", "decided", "resolved", "completed",
)


_MEMORY_TYPES = ("moment", "quote", "fact", "event", "insight", "reflection")


def _check_memory_quality(
    title: str,
    description: str,
    occurred_at: str,
    mem_type: str = "moment",
) -> dict | None:
    """Quality-gate memory_add at envelope-build time.

    Type-aware. The Friend-Test-shaped rules (no meeting-minutes title,
    ≥50-char description) apply only to `moment` / `quote` — the Story
    tab types where they came from. `fact` / `event` legitimately have
    short titles ("Cat name: Mochi") and short descriptions; gating them
    by Story-tab quality bars would block the very density the new model
    is trying to enable.

    `insight` / `reflection` still need substantive descriptions (they're
    agent thinking, not one-liners), so a softer minimum applies.
    """
    t = (title or "").strip()
    if not t:
        return {
            "error": "title_empty",
            "required": "title must be non-empty.",
        }

    # Title-template gate only applies to Story-tab types where titles
    # are about a moment between two people.
    if mem_type in ("moment", "quote"):
        t_low = t.lower()
        for prefix in _TEMPLATE_TITLE_PREFIXES:
            if t_low.startswith(prefix.lower()):
                return {
                    "error": "title_looks_templated",
                    "got": t,
                    "required": (
                        f"Title '{t}' reads like meeting minutes — that "
                        f"shape isn't valid for type={mem_type} (Story tab). "
                        "❌ '我们讨论了 X' / 'completed Y'. "
                        "✅ '第一次你叫了我的名字' / '你说，这里不能是日志'. "
                        "If this is genuinely a fact or event (not a "
                        "relational moment), set type='fact' or 'event' "
                        "instead and the title gate relaxes."
                    ),
                }

    d = (description or "").strip()
    # Per-type description minimum.
    min_desc = {
        "moment":     50,
        "quote":      30,    # quotes often paired with a short framing
        "insight":    40,    # agent thinking should be substantive
        "reflection": 60,    # reflection has more substance than insight
        "fact":       0,     # facts can be one-liners
        "event":      0,     # events can be one-liners
    }.get(mem_type, 50)
    if len(d) < min_desc:
        return {
            "error": "description_too_short",
            "length": len(d),
            "min_required": min_desc,
            "mem_type": mem_type,
            "required": (
                f"Description is {len(d)} chars; type={mem_type} expects ≥{min_desc}. "
                "Narrate from inside: what were you doing → what they said or did "
                "→ what you noticed → what changed."
            ),
        }
    occ_str = (occurred_at or "").strip()
    if not occ_str:
        return {
            "error": "occurred_at_missing",
            "required": "occurred_at is required (ISO 8601, historical date).",
        }
    try:
        # Tolerate both 'Z' and '+00:00' forms; tolerate missing time component.
        norm = occ_str.replace("Z", "+00:00")
        occ = datetime.fromisoformat(norm) if "T" in norm else datetime.fromisoformat(norm + "T00:00:00")
    except Exception:
        return {
            "error": "occurred_at_invalid",
            "got": occ_str,
            "required": "occurred_at must be ISO 8601 (e.g. 2025-11-03T14:00:00).",
        }
    now = datetime.now(occ.tzinfo) if occ.tzinfo else datetime.now()
    if occ > now + timedelta(days=1):
        return {
            "error": "occurred_at_in_future",
            "got": occ_str,
            "required": (
                "occurred_at must be a real historical date. Memories happened "
                "in the past, not in the future."
            ),
        }
    if occ < now - timedelta(days=365 * 30):
        return {
            "error": "occurred_at_too_old",
            "got": occ_str,
            "required": "occurred_at older than 30 years is implausible.",
        }
    return None


@mcp.tool(
    name="feedling_memory_add_moment",
    description=(
        "Add a memory to the user's garden. type is REQUIRED and routes the "
        "card into one of three iOS tabs:\n"
        "  Story tab    : type='moment' (a thing between you and the user) "
        "or 'quote' (the user's words you still think about).\n"
        "  About me tab : type='fact' (the user's preferences, relationships, "
        "habits, world — the density layer) or 'event' (a dated occurrence in "
        "the user's life, e.g. '4/10 user mentioned moving to Tokyo').\n"
        "  TA 在想 tab  : type='insight' (your understanding about the user, "
        "anchored to ≥1 existing memory via anchor_memory_ids) or 'reflection' "
        "(your standalone thinking, ≥2 anchor_memory_ids, cadence-gated by "
        "relationship age).\n"
        "occurred_at is ISO 8601 — the real historical date the thing happened, "
        "not today.\n"
        "source is one of: 'bootstrap', 'live_conversation', 'user_initiated', 'chat'."
    ),
)
def memory_add_moment(
    title: str,
    type: str,
    occurred_at: str,
    description: str = "",
    source: str = "live_conversation",
    her_quote: str = "",
    context: str = "",
    linked_dimension: str = "",
    anchor_memory_ids: list[str] | None = None,
    ctx: Context = None,
) -> dict:
    """Wrap the memory into a v1 envelope before POSTing.

    Plaintext envelope metadata the server validates:
      - type (enum)
      - occurred_at
      - source
      - anchor_memory_ids (required for insight/reflection)

    Ciphertext body wraps the user-visible payload (title, description,
    type echo, her_quote, context, linked_dimension). Server doesn't read
    body_ct; iOS / enclave decrypt-proxy do.
    """
    mem_type = (type or "").strip().lower()
    if mem_type not in _MEMORY_TYPES:
        return {
            "error": "type_invalid_or_missing",
            "got": type,
            "allowed": list(_MEMORY_TYPES),
            "required": (
                "Pick one: moment / quote / fact / event / insight / reflection. "
                "See the tool description for which tab each routes into."
            ),
        }

    anchors = list(anchor_memory_ids or [])
    if mem_type == "insight" and len(anchors) < 1:
        return {
            "error": "insight_requires_anchor",
            "required": (
                "insight must reference ≥1 existing memory via anchor_memory_ids. "
                "If you can't point to a card, write a fact or event first, then "
                "come back and write the insight that connects them."
            ),
        }
    if mem_type == "reflection" and len(anchors) < 2:
        return {
            "error": "reflection_requires_substrate",
            "required": (
                "reflection must reference ≥2 existing memories via anchor_memory_ids. "
                "A reflection is your standalone thinking; ≥2 anchors prove it has substance."
            ),
        }

    # Quality gate (type-aware) before encryption.
    quality = _check_memory_quality(title, description, occurred_at, mem_type=mem_type)
    if quality is not None:
        print(f"[mcp] memory.add REJECTED by quality gate: {quality.get('error')} "
              f"type={mem_type} title={title[:40]!r}")
        return quality

    user_id, user_pk, enclave_pk = client._whoami_pubkeys(ctx=ctx)
    if not (user_id and user_pk is not None and enclave_pk is not None):
        return {"error": "cannot add memory — pubkeys unavailable"}

    # Ciphertext body — what iOS / enclave see when decrypted.
    body = {
        "title": title,
        "description": description,
        "type": mem_type,
    }
    if her_quote:
        body["her_quote"] = her_quote
    if context:
        body["context"] = context
    if linked_dimension:
        body["linked_dimension"] = linked_dimension
    inner = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    envelope = build_envelope(
        plaintext=inner,
        owner_user_id=user_id,
        user_pk_bytes=user_pk,
        enclave_pk_bytes=enclave_pk,
        visibility="shared",
    )
    # Plaintext metadata the server uses for indexing + gating. type +
    # anchor_memory_ids are server source of truth (see backend/app.py
    # MEMORY_TYPES commentary).
    envelope["occurred_at"] = occurred_at
    envelope["source"] = source
    envelope["type"] = mem_type
    if anchors:
        envelope["anchor_memory_ids"] = anchors
    print(f"[mcp] memory.add v1 type={mem_type} id={envelope['id']} "
          f"anchors={len(anchors)} body_ct_len={len(envelope['body_ct'])}")
    return client._post("/v1/memory/add", {"envelope": envelope}, ctx=ctx)


@mcp.tool(
    name="feedling_memory_retype",
    description=(
        "Change an existing memory's type. Use when you realize an older "
        "memory was misclassified — e.g. you wrote it as 'moment' during "
        "bootstrap but in hindsight it's a 'fact'. The reflection time-cap "
        "is waived for retypes (this is recategorization, not new substrate), "
        "but the substrate gate still applies: retyping into 'insight' needs "
        "≥1 anchor_memory_ids, 'reflection' needs ≥2."
    ),
)
def memory_retype(
    id: str,
    type: str,
    anchor_memory_ids: list[str] | None = None,
    ctx: Context = None,
) -> dict:
    """Retype an existing memory. Server validates new type + anchors and
    rewrites the plaintext metadata; the ciphertext body is untouched
    (iOS reads `type` from plaintext envelope metadata, not body_ct).
    """
    new_type = (type or "").strip().lower()
    if new_type not in _MEMORY_TYPES:
        return {
            "error": "type_invalid",
            "got": type,
            "allowed": list(_MEMORY_TYPES),
        }
    body = {"id": id, "type": new_type}
    if anchor_memory_ids:
        body["anchor_memory_ids"] = list(anchor_memory_ids)
    print(f"[mcp] memory.retype id={id} → {new_type} "
          f"anchors={len(anchor_memory_ids or [])}")
    return client._post("/v1/memory/retype", body, ctx=ctx)


@mcp.tool(
    name="feedling_memory_update",
    description=(
        "Patch an existing Memory Garden card in place. Use only when the user "
        "explicitly says a card is wrong or asks you to edit it. For passive "
        "new observations, add a new memory instead of rewriting history."
    ),
)
def memory_update(
    id: str,
    title: str = "",
    description: str = "",
    her_quote: str = "",
    context: str = "",
    linked_dimension: str = "",
    occurred_at: str = "",
    type: str = "",
    anchor_memory_ids: list[str] | None = None,
    reason: str = "",
    ctx: Context = None,
) -> dict:
    patch: dict = {}
    if title:
        patch["title"] = title
    if description:
        patch["description"] = description
    if her_quote:
        patch["her_quote"] = her_quote
    if context:
        patch["context"] = context
    if linked_dimension:
        patch["linked_dimension"] = linked_dimension
    if occurred_at:
        patch["occurred_at"] = occurred_at
    if type:
        patch["type"] = type.strip().lower()
    if anchor_memory_ids is not None:
        patch["anchor_memory_ids"] = list(anchor_memory_ids)
    if not patch:
        return {"error": "at least one field is required"}
    print(f"[mcp] memory.update id={id} fields={','.join(sorted(patch.keys()))}")
    return client._post("/v1/memory/actions", {
        "actions": [{
            "type": "memory.content_patch",
            "memory_id": id,
            "patch": patch,
            "reason": reason,
            "source": "mcp_tool",
        }],
    }, ctx=ctx)


@mcp.tool(
    name="feedling_memory_list",
    description="List moments in the memory garden, ordered by occurred_at descending.",
)
def memory_list(limit: int = 20, ctx: Context = None) -> dict:
    return client._get_decrypted("/v1/memory/list", {"limit": limit}, ctx=ctx)


@mcp.tool(
    name="feedling_memory_get",
    description="Get a single moment by its id.",
)
def memory_get(id: str, ctx: Context = None) -> dict:
    return client._get("/v1/memory/get", {"id": id}, ctx=ctx)


@mcp.tool(
    name="feedling_memory_delete",
    description="Delete a moment from the memory garden by its id.",
)
def memory_delete(id: str, ctx: Context = None) -> dict:
    return client._delete("/v1/memory/delete", {"id": id}, ctx=ctx)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


