#!/usr/bin/env python3
"""
Feedling MCP Server — SSE transport with per-connection API keys.

Architecture:
  Claude.ai / Claude Desktop / OpenClaw  →  mcp_server.py  →  app.py

Connection string (multi-tenant hosted mode):
    claude mcp add feedling --transport sse "https://mcp.feedling.app/sse?key=<api_key>"

The `?key=` query parameter is read by an ASGI middleware on every incoming
HTTP request (both the SSE GET and the tool-call POSTs) and cached against the
current MCP session_id. Each tool invocation reads the key back and forwards
it as `X-API-Key` to the Flask backend, which performs the actual bcrypt-style
user lookup.

Self-hosted mode: set `FEEDLING_API_KEY=<shared>` on both the backend and this
process. The backend still requires an api_key on every request — there is no
unauthenticated fallback.
"""

import os

from mcpsrv import all_tools  # noqa: F401 — registers every tool on the mcp instance
from mcpsrv import client, session, tls
from mcpsrv.server import mcp

# COMPAT re-exports（迁移期）: tests and tools reach these through mcp_server.*
FLASK_BASE = client.FLASK_BASE
ENCLAVE_BASE = client.ENCLAVE_BASE
FALLBACK_API_KEY = client.FALLBACK_API_KEY
_session_keys = session._session_keys
_session_keys_lock = session._session_keys_lock
_remember = session._remember
_resolve_for_session = session._resolve_for_session
KeyCaptureMiddleware = session.KeyCaptureMiddleware
_split_tagged_reasoning = __import__("mcpsrv.tools_chat", fromlist=["x"])._split_tagged_reasoning
chat_get_history = __import__("mcpsrv.tools_chat", fromlist=["x"]).chat_get_history
_acquire_tls_cert = tls._acquire_tls_cert

if __name__ == "__main__":
    port = int(os.environ.get("FEEDLING_MCP_PORT", 5002))
    transport = os.environ.get("FEEDLING_MCP_TRANSPORT", "sse").lower()
    cert_path, key_path = _acquire_tls_cert()
    tls_on = cert_path is not None
    scheme = "https" if tls_on else "http"
    print(f"Feedling MCP server: transport={transport} port={port} scheme={scheme} "
          f"flask={FLASK_BASE}")

    if transport == "sse":
        # Build a Starlette app so we can attach the key-capture middleware,
        # then run it with uvicorn. GZipMiddleware compresses tool-call
        # responses above 500 B — decrypt_frame with include_image=true
        # ships ~470 KB of base64 JPEG inside JSON, and CVM egress is
        # ~30-50 KB/s without compression; gzip cuts the wire payload
        # by ~35-45% and turns a 6-10s call into ~2-3s.
        import uvicorn
        from starlette.middleware import Middleware as StarletteMW
        from starlette.middleware.gzip import GZipMiddleware
        app = mcp.http_app(
            transport="sse",
            middleware=[
                StarletteMW(GZipMiddleware, minimum_size=500),
                StarletteMW(KeyCaptureMiddleware),
            ],
        )
        if tls_on:
            uvicorn.run(app, host="0.0.0.0", port=port,
                        ssl_certfile=cert_path, ssl_keyfile=key_path,
                        log_level="info")
        else:
            uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    else:
        mcp.run(transport=transport, host="0.0.0.0", port=port)

