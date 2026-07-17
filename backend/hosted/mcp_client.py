"""Async MCP JSON-RPC client for the V2 hosted runtime.

`mcp_probe.probe` is a one-shot connectivity check that returns tool *names*.
The V2 tool loop needs more: the full tool *schemas* (to offer them as
model-facing tools) and the ability to *invoke* a tool (`tools/call`). This
module adds exactly those two operations, reusing mcp_probe's SSRF guard,
response parser, error taxonomy, and protocol constants — the same hand-rolled
streamable-HTTP JSON-RPC, deliberately not the `mcp` SDK.

Trust domain: identical to probe. Every call re-runs `blocked_url_kind` before
connecting (non-global host → refuse) and disables redirects outright, because
this runs in the backend/serve-worker trust domain and the result text is fed
back to the model. CA pinning (`ca_pem`) is honored the same way.
"""
from __future__ import annotations

import json
import ssl
from urllib.parse import urlparse

import httpx

from hosted.mcp_probe import (
    _CONNECT_TIMEOUT,
    _PROTOCOL_VERSION,
    _TOTAL_TIMEOUT,
    ProbeError,
    _classify_http,
    _parse_rpc_response,
    blocked_url_kind,
)


def _guard(url: str) -> None:
    kind = blocked_url_kind(url)
    if kind in ("blocked_url", "dns", "unreachable_from_backend"):
        raise ProbeError(kind, urlparse(url).hostname or "")


def _client_kwargs(ca_pem, transport) -> dict:
    kwargs = {
        "timeout": httpx.Timeout(_TOTAL_TIMEOUT, connect=_CONNECT_TIMEOUT),
        "follow_redirects": False,
        "transport": transport,
    }
    if ca_pem:
        # Add-trust (verify AGAINST the user's CA), never skip-verify — mirrors
        # mcp_probe. A self-signed server still has to prove it holds the key.
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=ca_pem)
        kwargs["verify"] = ctx
    return kwargs


def _send_headers(headers) -> dict:
    out = {str(k): str(v) for k, v in (headers or {}).items()}
    out.setdefault("Accept", "application/json, text/event-stream")
    out["Content-Type"] = "application/json"
    return out


async def _post(client, url, send_headers, payload, extra) -> httpx.Response:
    try:
        return await client.post(url, json=payload, headers={**send_headers, **extra})
    except httpx.ConnectTimeout:
        raise ProbeError("timeout", "connect timeout")
    except httpx.TimeoutException:
        raise ProbeError("timeout", "read timeout")
    except httpx.ConnectError as e:
        detail = str(e)[:160]
        raise ProbeError("tls" if "ssl" in detail.lower() else "dns", detail)


async def _handshake(client, url, send_headers) -> dict:
    """initialize → capture session-id → notifications/initialized. Returns the
    session headers to carry on the subsequent call."""
    resp = await _post(client, url, send_headers, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "feedling-mcp-client", "version": "1.0"}},
    }, {})
    if resp.status_code in (301, 302, 307, 308):
        raise ProbeError("protocol", "redirects not allowed")
    if resp.status_code >= 400:
        raise ProbeError(_classify_http(resp.status_code), resp.text[:160])
    _parse_rpc_response(resp)  # validates the handshake JSON-RPC envelope
    session = {}
    sid = resp.headers.get("mcp-session-id")
    if sid:
        session["Mcp-Session-Id"] = sid
    # spec-required before further requests; tolerate servers that 4xx it
    await _post(client, url, send_headers,
                {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    return session


def _content_text(content) -> str:
    """Flatten an MCP tool-result content array to text. Non-text blocks (image,
    resource) are noted but not inlined — the model gets the text parts."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type"):
            parts.append(f"[{block.get('type')} content omitted]")
    return "\n".join(p for p in parts if p)


async def list_tools(url, headers, *, ca_pem=None, transport=None) -> list[dict]:
    """Return the server's full tool objects ``[{name, description, inputSchema}]``.

    Raises ``ProbeError`` on SSRF refusal, transport error, HTTP error, or a
    JSON-RPC error response.
    """
    _guard(url)
    send_headers = _send_headers(headers)
    async with httpx.AsyncClient(**_client_kwargs(ca_pem, transport)) as client:
        session = await _handshake(client, url, send_headers)
        resp = await _post(client, url, send_headers,
                           {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        if resp.status_code >= 400:
            raise ProbeError(_classify_http(resp.status_code), resp.text[:160])
        body = _parse_rpc_response(resp)
        if "error" in body:
            raise ProbeError("protocol", json.dumps(body["error"])[:160])
        tools = (body.get("result") or {}).get("tools") or []
        return [t for t in tools if isinstance(t, dict) and t.get("name")]


async def call_tool(url, headers, name, arguments, *, ca_pem=None, transport=None) -> dict:
    """Invoke ``name`` with ``arguments``. Returns ``{"is_error": bool, "text": str}``.

    A JSON-RPC transport/HTTP failure raises ``ProbeError``; a tool-level failure
    (``result.isError`` or a JSON-RPC ``error`` body) is returned as
    ``is_error=True`` so the loop can hand it back to the model rather than abort
    the turn.
    """
    _guard(url)
    send_headers = _send_headers(headers)
    async with httpx.AsyncClient(**_client_kwargs(ca_pem, transport)) as client:
        session = await _handshake(client, url, send_headers)
        resp = await _post(client, url, send_headers, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": str(name), "arguments": arguments or {}},
        }, session)
        if resp.status_code >= 400:
            raise ProbeError(_classify_http(resp.status_code), resp.text[:160])
        body = _parse_rpc_response(resp)
        if "error" in body:
            return {"is_error": True, "text": json.dumps(body["error"])[:2000]}
        result = body.get("result") or {}
        return {"is_error": bool(result.get("isError")),
                "text": _content_text(result.get("content"))[:20000]}
