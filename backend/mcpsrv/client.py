"""HTTP plumbing to the Flask backend / enclave + vision gate."""

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

from mcpsrv import session

FLASK_BASE = os.environ.get("FEEDLING_FLASK_URL", "http://127.0.0.1:5001")
# When set, MCP routes content reads (chat history, memory list,
# identity get) through the enclave's decrypt endpoints so agents see
# plaintext rather than opaque ciphertext envelopes.
# verify=False on these calls because the enclave's TLS cert is
# self-signed; trust is REPORT_DATA-pinned from outside, not a PKI
# property of the in-cluster hop.
ENCLAVE_BASE = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
FALLBACK_API_KEY = os.environ.get("FEEDLING_API_KEY", "").strip()



def _current_api_key(ctx: Context | None = None) -> str:
    """Best-effort lookup of the current caller's API key."""
    # 1. Try the active HTTP request headers/query
    try:
        req = get_http_request()
        k = (req.query_params.get("key") or "").strip()
        if not k:
            auth = req.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                k = auth[7:].strip()
        if not k:
            k = (req.headers.get("x-api-key") or "").strip()
        if k:
            return k
        peer = req.client.host if req.client else ""
        session_id = (req.query_params.get("session_id") or "").strip() or None
        cached = session._resolve_for_session(session_id, peer=peer)
        if cached:
            return cached
    except Exception:
        pass

    # 2. Try FastMCP Context session
    if ctx is not None and ctx.session_id:
        cached = session._resolve_for_session(ctx.session_id)
        if cached:
            return cached

    # 3. Env fallback (self-hosted default)
    return FALLBACK_API_KEY


def _headers(ctx: Context | None = None) -> dict:
    key = _current_api_key(ctx)
    h = {"Content-Type": "application/json"}
    if key:
        h["X-API-Key"] = key
    return h


# Module-level pooled clients — avoid paying a fresh TCP+TLS handshake
# on every tool call. The backend is in-cluster plain HTTP; the enclave
# is reached over its self-signed TLS surface so verify=False.
_FLASK_HTTP = httpx.Client(
    timeout=60,
    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
)
_ENCLAVE_HTTP = httpx.Client(
    timeout=60,
    verify=False,
    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
)

# Proactive-push safety gate: require a recent vision-capable decrypt before
# posting Live Activity. Keyed by caller api_key so multi-tenant sessions don't mix.
_REQUIRE_VISION_BEFORE_PUSH = os.environ.get("FEEDLING_REQUIRE_VISION_BEFORE_PUSH", "true").lower() == "true"
_VISION_DECRYPT_TTL_SEC = int(os.environ.get("FEEDLING_VISION_DECRYPT_TTL_SEC", "180"))
_recent_decrypt_by_api_key: dict[str, dict] = {}


def _check_vision_gate(api_key: str | None) -> dict | None:
    """Return a block-reason dict if the vision gate is active and not satisfied,
    or None if the caller may proceed with a Live Activity push."""
    if not _REQUIRE_VISION_BEFORE_PUSH:
        return None
    rec = _recent_decrypt_by_api_key.get(api_key) if api_key else None
    if not rec:
        return {
            "status": "blocked",
            "reason": "vision_gate_missing_decrypt",
            "hint": "Call feedling_screen_decrypt_frame(include_image=true) before pushing to Live Activity.",
        }
    age = time.time() - float(rec.get("ts", 0.0) or 0.0)
    if age > _VISION_DECRYPT_TTL_SEC:
        return {
            "status": "blocked",
            "reason": "vision_gate_stale_decrypt",
            "age_sec": round(age, 2),
            "ttl_sec": _VISION_DECRYPT_TTL_SEC,
            "hint": "Frame analysis is stale. Re-run feedling_screen_decrypt_frame(include_image=true).",
        }
    if not rec.get("include_image"):
        return {
            "status": "blocked",
            "reason": "vision_gate_no_image",
            "hint": "decrypt_frame must be called with include_image=true.",
        }
    return None

# (Phase: relationship-anchor) The relationship anchor used to derive
# days_with_user is now owned by the Flask server (per-user
# `relationship_started_at` field, set via /v1/identity/init or
# /v1/identity/relationship_anchor). The old global env-var fallback
# was removed — agents must pass days_with_user at init time.


def _passthrough_4xx(r) -> dict:
    """Convert an httpx Response into a tool-return dict, with special
    handling for 4xx so the Agent sees the structured error body (e.g. the
    `bootstrap_incomplete` 409 from app.py) instead of an httpx exception
    string. 5xx still raises — those are server bugs we want to surface
    loudly to telemetry, not pretend to handle.
    """
    if 400 <= r.status_code < 500:
        try:
            body = r.json()
        except Exception:
            body = {"error": f"http_{r.status_code}", "detail": r.text[:500]}
        if isinstance(body, dict):
            body.setdefault("status_code", r.status_code)
            return body
        return {"error": f"http_{r.status_code}", "body": body}
    r.raise_for_status()
    return r.json()


def _get(path: str, params: dict | None = None, ctx: Context | None = None) -> dict:
    r = _FLASK_HTTP.get(f"{FLASK_BASE}{path}", params=params, headers=_headers(ctx))
    return _passthrough_4xx(r)


def _get_decrypted(path: str, params: dict | None = None, ctx: Context | None = None) -> dict:
    """Read a content endpoint through the enclave's decrypt proxy when
    one is configured, otherwise fall back to Flask.

    The enclave hosts mirrors of /v1/chat/history, /v1/memory/list, and
    /v1/identity/get that unseal K_enclave and AEAD-decrypt the body
    before responding. Agents — which don't hold user_sk — need this
    path to read v1 envelopes at all.
    """
    if not ENCLAVE_BASE:
        return _get(path, params=params, ctx=ctx)
    r = _ENCLAVE_HTTP.get(f"{ENCLAVE_BASE}{path}", params=params, headers=_headers(ctx))
    return _passthrough_4xx(r)


def _post(path: str, body: dict, ctx: Context | None = None) -> dict:
    r = _FLASK_HTTP.post(f"{FLASK_BASE}{path}", json=body, headers=_headers(ctx))
    return _passthrough_4xx(r)


def _delete(path: str, params: dict | None = None, ctx: Context | None = None) -> dict:
    r = _FLASK_HTTP.delete(f"{FLASK_BASE}{path}", params=params, headers=_headers(ctx))
    return _passthrough_4xx(r)


def _whoami_pubkeys(ctx: Context | None = None) -> tuple[str, bytes | None, bytes | None]:
    """Resolve (owner_user_id, user_pk_bytes, enclave_pk_bytes) for the
    current caller by hitting /v1/users/whoami on the backend.

    Returns bytes=None for either pubkey if the backend can't supply it
    (malformed whoami response, or no reachable enclave). In that case
    wrap-required tools fail loud rather than leak plaintext.
    """
    try:
        info = _get("/v1/users/whoami", ctx=ctx)
    except Exception as e:
        print(f"[wrap] whoami failed: {e}")
        return ("", None, None)

    user_id = info.get("user_id", "") or ""
    user_pk_b64 = (info.get("public_key") or "").strip()
    enc_pk_hex = (info.get("enclave_content_public_key_hex") or "").strip()

    try:
        user_pk_bytes = base64.b64decode(user_pk_b64) if user_pk_b64 else None
        if user_pk_bytes is not None and len(user_pk_bytes) != 32:
            user_pk_bytes = None
    except Exception:
        user_pk_bytes = None

    try:
        enc_pk_bytes = bytes.fromhex(enc_pk_hex) if enc_pk_hex else None
        if enc_pk_bytes is not None and len(enc_pk_bytes) != 32:
            enc_pk_bytes = None
    except Exception:
        enc_pk_bytes = None

    return (user_id, user_pk_bytes, enc_pk_bytes)


# ---------------------------------------------------------------------------
# Push tools
# ---------------------------------------------------------------------------


