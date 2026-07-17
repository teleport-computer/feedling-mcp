"""Async MCP JSON-RPC client (backend/hosted/mcp_client.py): tools/list with full
schemas + tools/call. Mirrors test_user_mcp_probe.py's in-process ASGI fake."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import mcp_client, mcp_probe  # noqa: E402

from _ca_helpers import self_signed_ca_pem  # noqa: E402


def _fake_mcp_app(*, require_auth=None, tools=None, call_result=None, call_is_error=False,
                  fail_status=None):
    """In-process streamable-HTTP MCP server supporting initialize /
    notifications/initialized / tools/list / tools/call."""
    tool_defs = tools if tools is not None else [
        {"name": "search", "description": "web search",
         "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}},
                         "required": ["q"]}},
    ]

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        if require_auth and headers.get("authorization") != require_auth:
            await _respond(send, 401, {"error": "unauthorized"})
            return
        if fail_status:
            await _respond(send, fail_status, {"error": "boom"})
            return
        req = json.loads(body) if body else {}
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "0"}}
        elif method == "notifications/initialized":
            await _respond(send, 202, None)
            return
        elif method == "tools/list":
            result = {"tools": tool_defs}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": call_result or "tool-said-hi"}],
                      "isError": call_is_error}
        else:
            await _respond(send, 400, {"error": "bad method"})
            return
        await _respond(send, 200, {"jsonrpc": "2.0", "id": req.get("id"), "result": result})

    async def _respond(send, status, payload):
        data = json.dumps(payload).encode() if payload is not None else b""
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"mcp-session-id", b"sess-1")]})
        await send({"type": "http.response.body", "body": data})

    return app


def _global_ip(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])


URL = "https://mcp.example.com/mcp"


def test_list_tools_returns_full_schemas(monkeypatch):
    _global_ip(monkeypatch)
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    tools = asyncio.run(mcp_client.list_tools(URL, {}, transport=transport))
    assert len(tools) == 1
    assert tools[0]["name"] == "search"
    # full inputSchema preserved, not just the name (the whole point vs probe)
    assert tools[0]["inputSchema"]["properties"]["q"]["type"] == "string"
    assert tools[0]["inputSchema"]["required"] == ["q"]


def test_call_tool_happy_path_returns_text(monkeypatch):
    _global_ip(monkeypatch)
    transport = httpx.ASGITransport(app=_fake_mcp_app(call_result="weather is sunny"))
    out = asyncio.run(mcp_client.call_tool(URL, {}, "search", {"q": "weather"},
                                           transport=transport))
    assert out["is_error"] is False
    assert "weather is sunny" in out["text"]


def test_call_tool_surfaces_tool_error(monkeypatch):
    _global_ip(monkeypatch)
    transport = httpx.ASGITransport(app=_fake_mcp_app(call_result="nope", call_is_error=True))
    out = asyncio.run(mcp_client.call_tool(URL, {}, "search", {}, transport=transport))
    assert out["is_error"] is True
    assert "nope" in out["text"]


def test_headers_forwarded(monkeypatch):
    _global_ip(monkeypatch)
    transport = httpx.ASGITransport(app=_fake_mcp_app(require_auth="Bearer tok"))
    with pytest.raises(mcp_probe.ProbeError) as e:
        asyncio.run(mcp_client.list_tools(URL, {}, transport=transport))
    assert e.value.kind == "http_401"
    tools = asyncio.run(mcp_client.list_tools(URL, {"Authorization": "Bearer tok"},
                                              transport=transport))
    assert tools[0]["name"] == "search"


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/mcp", "https://169.254.169.254/latest", "http://10.1.2.3/mcp",
])
def test_ssrf_non_global_refused_for_both_ops(url):
    """Same backend-trust-domain SSRF guard as probe — list AND call refuse."""
    with pytest.raises(mcp_probe.ProbeError) as e:
        asyncio.run(mcp_client.list_tools(url, {}))
    assert e.value.kind == "unreachable_from_backend"
    with pytest.raises(mcp_probe.ProbeError) as e:
        asyncio.run(mcp_client.call_tool(url, {}, "t", {}))
    assert e.value.kind == "unreachable_from_backend"


def test_ca_pem_used_for_verification(monkeypatch):
    _global_ip(monkeypatch)
    seen = {}
    real = httpx.AsyncClient

    def _spy(*a, **k):
        seen["verify"] = k.get("verify")
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _spy)
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    asyncio.run(mcp_client.list_tools(URL, {}, ca_pem=self_signed_ca_pem(),
                                      transport=transport))
    import ssl as _ssl
    assert isinstance(seen["verify"], _ssl.SSLContext)


def test_server_5xx_raises_probeerror(monkeypatch):
    _global_ip(monkeypatch)
    transport = httpx.ASGITransport(app=_fake_mcp_app(fail_status=503))
    with pytest.raises(mcp_probe.ProbeError) as e:
        asyncio.run(mcp_client.list_tools(URL, {}, transport=transport))
    assert e.value.kind == "http_5xx"
