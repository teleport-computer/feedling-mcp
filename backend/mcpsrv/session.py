"""Session-id → api_key cache + key-capture ASGI middleware."""

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



# ---------------------------------------------------------------------------
# Session-id → api_key cache
# ---------------------------------------------------------------------------
# MCP SSE clients open the event stream with `GET /sse?key=xxx`, then POST tool
# calls to `/messages/?session_id=yyy`. Different clients behave differently:
#   - Some forward `?key=` onto every subsequent POST URL.
#   - Some support `Authorization: Bearer <key>` headers end-to-end.
#
# Clients in the third historical category — "forward `?key=` only on the
# initial GET, then nothing on POSTs" — used to be supported via a fallback
# that looked up any pending key from the same `request.client.host`. That
# fallback was a P0 data-isolation bug in any deployment behind a reverse
# proxy: every client appears to come from the same upstream IP, so the
# fallback handed user A's POST authentication to whichever user had most
# recently opened a SSE connection from that same upstream (i.e., everyone).
# Symptoms observed in prod 2026-05-11: users seeing each other's agent
# names, identity cards getting overwritten by strangers' `identity_replace`
# tool calls, chat history apparently mixing across accounts.
#
# Fix: drop peer-based fallback entirely. Every tool call must carry the
# api_key on the request itself (via `?key=` query param, `Authorization:
# Bearer`, or `X-API-Key` header). MCP clients that omit the key on POSTs
# are no longer supported — they were never safe to support in shared
# infrastructure. Resolution path is now strictly:
#   1. key present on the current HTTP request → use it directly
#   2. key previously bound to this exact session_id by a prior request
#      that did carry it → use cached
#   3. else: no key, request is unauthenticated, downstream tool call fails

_session_keys: dict[str, str] = {}
_session_keys_lock = threading.Lock()


def _remember(session_id: str | None, key: str, peer: str = ""):
    """Bind `key` to `session_id` if both are present. peer is accepted for
    backward compat with callers but intentionally ignored — see the
    module-level comment above for why peer-based key recovery is unsafe."""
    if not key or not session_id:
        return
    with _session_keys_lock:
        _session_keys[session_id] = key


def _resolve_for_session(session_id: str | None, peer: str = "") -> str | None:
    """Strict session_id → key lookup. Returns None if no exact match.
    peer is accepted (and intentionally ignored) for caller-signature
    compatibility — see the module-level comment for the data-isolation
    incident that this strictness exists to prevent."""
    if not session_id:
        return None
    with _session_keys_lock:
        return _session_keys.get(session_id)


# ---------------------------------------------------------------------------
# ASGI middleware — runs on every HTTP request
# ---------------------------------------------------------------------------


class KeyCaptureMiddleware(BaseHTTPMiddleware):
    """Extracts api_key from incoming requests and binds it to the right
    MCP SSE session_id.

    Three code paths, in order of preference:

    1. Request carries both `session_id` and `key` (POST /messages/?session_id=Y&key=X
       or POST with `Authorization: Bearer X`): bind immediately, done.

    2. Request is an initial SSE GET (`/sse?key=X`) with NO session_id yet
       — the server is about to assign one inside its SSE response. We wrap
       the response body iterator, peek at the first `event: endpoint`
       chunk, parse the assigned session_id out of its `data:` URL, and
       bind it to the captured key before the chunk leaves the server.
       This is the safe replacement for the old peer-IP fallback: the
       binding uses information that's unique per connection (the
       server-assigned session_id), not the transport-layer peer address
       which is shared behind a reverse proxy.

    3. Request has neither — pass through. Tool calls authenticate via
       whichever path resolves; if nothing resolves, the request fails
       closed (401 from enclave/backend) rather than silently using
       another tenant's key.
    """

    # MCP SSE servers send the first event as:
    #   event: endpoint
    #   data: /messages/?session_id=<uuid>
    # We extract the uuid. Accepting hex+dash matches FastMCP's UUID4
    # session ids; the upper bound on length stops a runaway buffer.
    _ENDPOINT_RE = re.compile(
        rb"event:\s*endpoint\s*\r?\n"
        rb"data:[^\r\n]*?session_id=([A-Za-z0-9\-_]{8,128})"
    )

    async def dispatch(self, request, call_next):
        key = self._extract_key(request)
        session_id = (request.query_params.get("session_id") or "").strip() or None

        # Path 1: key + session_id both present on this request → bind now.
        if key and session_id:
            _remember(session_id, key)

        response = await call_next(request)

        # Path 2: initial SSE GET with key but no session_id yet → sniff
        # the assigned session_id from the response stream and bind it.
        # Path checked structurally rather than by URL string so renames
        # of the SSE endpoint don't silently break this.
        if (
            key
            and not session_id
            and request.method == "GET"
            and hasattr(response, "body_iterator")
            and "text/event-stream" in response.headers.get("content-type", "").lower()
        ):
            response.body_iterator = self._sniff_session_id_and_bind(
                response.body_iterator, key
            )

        return response

    @staticmethod
    def _extract_key(request) -> str:
        # query param "key"
        try:
            k = (request.query_params.get("key") or "").strip()
            if k:
                return k
        except Exception:
            pass
        # Authorization: Bearer <key>
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        # X-API-Key header
        return (request.headers.get("x-api-key") or "").strip()

    @classmethod
    async def _sniff_session_id_and_bind(cls, body_iterator, key: str):
        """Wrap the SSE response stream. While forwarding chunks unchanged,
        peek at the early bytes for the `event: endpoint` line whose data
        carries the server-assigned session_id. Bind that session_id → key
        the first time we see it, then stop buffering. The wrapping is
        transparent to the SSE client — every chunk is yielded immediately,
        not after the binding completes."""
        bound = False
        # Bounded prefix buffer: the endpoint event is the first one MCP
        # SSE servers emit, so a few KB is more than enough. Without a
        # cap an unusual stream could buffer indefinitely.
        sniff_buf = bytearray()
        sniff_cap = 8192

        async for chunk in body_iterator:
            if not bound:
                # Accumulate just enough bytes to scan for the endpoint
                # event. Forward each chunk first; this is for sniffing,
                # not gating.
                try:
                    sniff_buf.extend(chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode("utf-8"))
                except Exception:
                    bound = True  # non-byte stream — give up sniffing
                if len(sniff_buf) <= sniff_cap:
                    m = cls._ENDPOINT_RE.search(sniff_buf)
                    if m:
                        sid = m.group(1).decode("ascii", errors="ignore")
                        if sid:
                            _remember(sid, key)
                            print(f"[mcp] bound key to SSE-assigned session_id={sid[:8]}…")
                        bound = True
                        sniff_buf = bytearray()  # release
                else:
                    # Cap hit without finding endpoint event — stop trying.
                    # The session this client uses for POSTs will need to
                    # carry the key on its own (Authorization / ?key=).
                    bound = True
                    sniff_buf = bytearray()
            yield chunk


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
