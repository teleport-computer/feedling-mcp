"""Async MCP JSON-RPC client for the V2 hosted runtime.

`mcp_probe.probe` is a one-shot connectivity check that returns tool *names*.
The V2 tool loop needs more: the full tool *schemas* (to offer them as
model-facing tools) and the ability to *invoke* a tool (`tools/call`). This
module adds exactly those two operations, reusing mcp_probe's SSRF guard,
response parser, error taxonomy, and protocol constants — the same hand-rolled
streamable-HTTP JSON-RPC, deliberately not the `mcp` SDK.

Trust domain: identical to probe. Every operation resolves and validates the
configured host once, then connects to that literal public address for the
whole exchange while preserving the configured Host/SNI. Redirects and ambient
proxy settings are disabled. CA pinning (`ca_pem`) is honored the same way.
"""
from __future__ import annotations

import asyncio
import json

import httpx
from hosted import mcp_probe

from hosted.mcp_probe import (
    _PROTOCOL_VERSION,
    ProbeError,
    _client_kwargs,
    _parse_rpc_response,
    _pin_public_target_async,
    _post_bounded,
    _raise_for_status,
    _send_headers,
)


async def _handshake(client, target, send_headers) -> dict:
    """initialize → capture session-id → notifications/initialized. Returns the
    session headers to carry on the subsequent call."""
    resp = await _post_bounded(client, target, send_headers, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "feedling-mcp-client", "version": "1.0"}},
    }, {})
    _raise_for_status(resp)
    _parse_rpc_response(resp)  # validates the handshake JSON-RPC envelope
    session = {}
    sid = resp.headers.get("mcp-session-id")
    if sid:
        session["Mcp-Session-Id"] = sid
    # spec-required before further requests; tolerate servers that 4xx it
    await _post_bounded(
        client,
        target,
        send_headers,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session,
    )
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
    try:
        async with asyncio.timeout(mcp_probe._TOTAL_TIMEOUT):
            # DNS resolution is blocking in net_safety; keep it off the shared
            # worker loop and inside the same wall deadline as the exchange.
            target = await _pin_public_target_async(url)
            send_headers = _send_headers(headers)
            async with httpx.AsyncClient(**_client_kwargs(ca_pem, transport)) as client:
                session = await _handshake(client, target, send_headers)
                resp = await _post_bounded(
                    client,
                    target,
                    send_headers,
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    session,
                )
                _raise_for_status(resp)
                body = _parse_rpc_response(resp)
                if "error" in body:
                    raise ProbeError("protocol", "JSON-RPC error")
                tools = (body.get("result") or {}).get("tools") or []
                return [t for t in tools if isinstance(t, dict) and t.get("name")]
    except ProbeError:
        raise
    except TimeoutError:
        raise ProbeError("timeout", "operation timeout") from None


async def call_tool(url, headers, name, arguments, *, ca_pem=None, transport=None) -> dict:
    """Invoke ``name`` with ``arguments``. Returns ``{"is_error": bool, "text": str}``.

    A JSON-RPC transport/HTTP failure raises ``ProbeError``; a tool-level failure
    (``result.isError`` or a JSON-RPC ``error`` body) is returned as
    ``is_error=True`` so the loop can hand it back to the model rather than abort
    the turn.
    """
    try:
        async with asyncio.timeout(mcp_probe._TOTAL_TIMEOUT):
            target = await _pin_public_target_async(url)
            send_headers = _send_headers(headers)
            async with httpx.AsyncClient(**_client_kwargs(ca_pem, transport)) as client:
                session = await _handshake(client, target, send_headers)
                resp = await _post_bounded(client, target, send_headers, {
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": str(name), "arguments": arguments or {}},
                }, session)
                _raise_for_status(resp)
                body = _parse_rpc_response(resp)
                if "error" in body:
                    return {
                        "is_error": True,
                        "text": json.dumps(body["error"])[:2000],
                    }
                result = body.get("result") or {}
                return {
                    "is_error": bool(result.get("isError")),
                    "text": _content_text(result.get("content"))[:20000],
                }
    except ProbeError:
        raise
    except TimeoutError:
        raise ProbeError("timeout", "operation timeout") from None
