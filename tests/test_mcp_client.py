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
        seen["trust_env"] = k.get("trust_env")
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _spy)
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    asyncio.run(mcp_client.list_tools(URL, {}, ca_pem=self_signed_ca_pem(),
                                      transport=transport))
    import ssl as _ssl
    assert isinstance(seen["verify"], _ssl.SSLContext)
    assert seen["trust_env"] is False


def test_server_5xx_raises_probeerror(monkeypatch):
    _global_ip(monkeypatch)
    transport = httpx.ASGITransport(app=_fake_mcp_app(fail_status=503))
    with pytest.raises(mcp_probe.ProbeError) as e:
        asyncio.run(mcp_client.list_tools(URL, {}, transport=transport))
    assert e.value.kind == "http_5xx"
    assert e.value.detail == "upstream HTTP 503"
    assert "boom" not in e.value.detail


@pytest.mark.parametrize("operation", ["list", "call"])
def test_validated_ip_is_connect_target_while_host_and_sni_are_preserved(
    monkeypatch, operation,
):
    monkeypatch.setattr(
        mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    seen = []

    async def handler(request):
        seen.append({
            "url_host": request.url.host,
            "host": request.headers.get("host"),
            "sni": request.extensions.get("sni_hostname"),
            "accept_encoding": request.headers.get("accept-encoding"),
        })
        method = json.loads(request.content).get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            result = {"tools": [{"name": "search", "inputSchema": {}}]}
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": "done"}],
                "isError": False,
            }
        else:
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            }
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": result},
            headers={"mcp-session-id": "s"},
        )

    url = "https://mcp.example.com:8443/mcp"
    transport = httpx.MockTransport(handler)
    if operation == "list":
        result = asyncio.run(mcp_client.list_tools(
            url,
            {"Host": "attacker.invalid", "accept-encoding": "gzip"},
            transport=transport,
        ))
        assert result[0]["name"] == "search"
    else:
        result = asyncio.run(mcp_client.call_tool(
            url,
            {"Host": "attacker.invalid", "accept-encoding": "gzip"},
            "search",
            {},
            transport=transport,
        ))
        assert result == {"is_error": False, "text": "done"}

    assert len(seen) == 3
    assert {item["url_host"] for item in seen} == {"93.184.216.34"}
    assert {item["host"] for item in seen} == {"mcp.example.com:8443"}
    assert {item["sni"] for item in seen} == {"mcp.example.com"}
    assert {item["accept_encoding"] for item in seen} == {"identity"}


@pytest.mark.parametrize("operation", ["list", "call"])
def test_compressed_response_is_rejected_before_body_iteration(
    monkeypatch, operation,
):
    _global_ip(monkeypatch)

    class MustNotRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("compressed response body must never be read")
            yield b"unreachable"

        async def aclose(self):
            return None

    async def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
            stream=MustNotRead(),
        )

    with pytest.raises(mcp_probe.ProbeError) as exc:
        if operation == "list":
            asyncio.run(mcp_client.list_tools(
                URL,
                {},
                transport=httpx.MockTransport(handler),
            ))
        else:
            asyncio.run(mcp_client.call_tool(
                URL,
                {},
                "search",
                {},
                transport=httpx.MockTransport(handler),
            ))
    assert exc.value.kind == "protocol"
    assert exc.value.detail == "compressed MCP responses are not allowed"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_runtime_client_still_rejects_redirects(monkeypatch, status):
    _global_ip(monkeypatch)

    async def handler(_request):
        return httpx.Response(
            status,
            headers={"location": "https://redirect.example/mcp"},
        )

    with pytest.raises(mcp_probe.ProbeError) as exc:
        asyncio.run(mcp_client.list_tools(
            URL,
            {},
            transport=httpx.MockTransport(handler),
        ))
    assert exc.value.kind == "protocol"
    assert exc.value.detail == "redirects not allowed"


def test_unencoded_raw_response_limit_is_enforced_before_json_parse(monkeypatch):
    _global_ip(monkeypatch)

    class TooLarge(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * mcp_probe._MAX_RESPONSE_BYTES
            yield b"x"

        async def aclose(self):
            return None

    async def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=TooLarge(),
        )

    with pytest.raises(mcp_probe.ProbeError) as exc:
        asyncio.run(mcp_client.list_tools(
            URL,
            {},
            transport=httpx.MockTransport(handler),
        ))
    assert exc.value.kind == "response_too_large"


def test_whole_operation_deadline_stops_slow_multi_request_exchange(
    monkeypatch,
):
    _global_ip(monkeypatch)
    monkeypatch.setattr(mcp_probe, "_TOTAL_TIMEOUT", 0.05)

    async def handler(request):
        await asyncio.sleep(0.03)
        method = json.loads(request.content).get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            },
        })

    with pytest.raises(mcp_probe.ProbeError) as exc:
        asyncio.run(mcp_client.list_tools(
            URL,
            {},
            transport=httpx.MockTransport(handler),
        ))
    assert exc.value.kind == "timeout"
    assert exc.value.detail == "operation timeout"


def test_whole_operation_deadline_stops_infinite_slow_drip(monkeypatch):
    _global_ip(monkeypatch)
    monkeypatch.setattr(mcp_probe, "_TOTAL_TIMEOUT", 0.05)

    class SlowDrip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                # Every chunk arrives before the per-read timeout; only the
                # whole-operation deadline can terminate this peer.
                yield b" "

        async def aclose(self):
            return None

    async def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=SlowDrip(),
        )

    with pytest.raises(mcp_probe.ProbeError) as exc:
        asyncio.run(mcp_client.list_tools(
            URL,
            {},
            transport=httpx.MockTransport(handler),
        ))
    assert exc.value.kind == "timeout"
    assert exc.value.detail == "operation timeout"


def test_dns_submission_capacity_fails_closed_without_default_executor():
    acquired = 0
    try:
        for _ in range(mcp_probe._DNS_MAX_PENDING):
            assert mcp_probe._DNS_SUBMISSION_SLOTS.acquire(blocking=False)
            acquired += 1
        with pytest.raises(mcp_probe.ProbeError) as exc:
            asyncio.run(mcp_client.list_tools(URL, {}))
        assert exc.value.kind == "dns_busy"
        assert exc.value.detail == "resolver capacity exhausted"
    finally:
        for _ in range(acquired):
            mcp_probe._DNS_SUBMISSION_SLOTS.release()


def test_one_shot_sse_is_bounded_then_parsed(monkeypatch):
    _global_ip(monkeypatch)

    async def handler(request):
        method = json.loads(request.content).get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        result = (
            {"tools": [{"name": "search", "inputSchema": {}}]}
            if method == "tools/list"
            else {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            }
        )
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": result,
        })
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {payload}\n\n".encode(),
        )

    tools = asyncio.run(mcp_client.list_tools(
        URL,
        {},
        transport=httpx.MockTransport(handler),
    ))
    assert tools[0]["name"] == "search"
