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


_UNSET = object()


def _fake_mcp_app(*, require_auth=None, tools=None, call_result=None, call_is_error=False,
                  fail_status=None, instructions=_UNSET):
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
            # spec 的可选字段:服务器自己写的使用说明。_UNSET = 整个字段不出现
            # (绝大多数服务器就是这样),None/""/非字符串各自是独立的一种坏形状。
            if instructions is not _UNSET:
                result["instructions"] = instructions
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


# ---------------------------------------------------------------------------
# Legacy HTTP+SSE transport (mcp_transport="sse")
#
# httpx.ASGITransport buffers the whole response, so a long-lived legacy GET
# stream can't be faked in-process with it. httpx.MockTransport, by contrast,
# hands back a streaming Response whose custom AsyncByteStream is consumed
# lazily — the GET stream generator awaits a shared asyncio.Queue that the
# interleaved POST handler feeds, exactly reproducing the GET→endpoint→POST→
# read-stream code path in mcp_client's SSE session (no real socket needed).
# ---------------------------------------------------------------------------

SSE_URL = "https://mcp.example.com/sse"


class _FakeSseServer:
    """MockTransport handler for one legacy HTTP+SSE MCP server.

    GET (any path) → event-stream: first an ``endpoint`` event, then whatever
    JSON-RPC replies the interleaved POSTs enqueue. POST to the message path →
    enqueue the matching reply + 202. POST to the base path (a streamable
    initialize probe) → ``base_post_status`` (405 = standard legacy 'use GET').
    """

    def __init__(self, *, endpoint="/message?session_id=s1", base_path="/sse",
                 tools=None, call_result="tool-said-hi", call_is_error=False,
                 base_post_status=405):
        self.endpoint = endpoint
        self.base_path = base_path
        self.tools = tools if tools is not None else [
            {"name": "search",
             "inputSchema": {"type": "object",
                             "properties": {"q": {"type": "string"}},
                             "required": ["q"]}}]
        self.call_result = call_result
        self.call_is_error = call_is_error
        self.base_post_status = base_post_status
        self.queue: asyncio.Queue = asyncio.Queue()
        self.post_urls: list[str] = []
        self.methods: list[str] = []

    def _stream(self):
        server = self

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield f"event: endpoint\ndata: {server.endpoint}\n\n".encode()
                while True:
                    doc = await server.queue.get()
                    yield (b"event: message\ndata: "
                           + json.dumps(doc).encode() + b"\n\n")

            async def aclose(self):
                return None

        return Stream()

    async def handler(self, request):
        self.methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=self._stream())
        self.post_urls.append(str(request.url))
        # The base-path POST is a streamable-transport probe, not a message.
        if request.url.path == self.base_path:
            return httpx.Response(self.base_post_status, json={"error": "use GET"})
        req = json.loads(request.content)
        method = req.get("method")
        if method == "initialize":
            await self.queue.put({"jsonrpc": "2.0", "id": req["id"], "result": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "serverInfo": {"name": "legacy", "version": "0"}}})
        elif method == "tools/list":
            await self.queue.put({"jsonrpc": "2.0", "id": req["id"],
                                  "result": {"tools": self.tools}})
        elif method == "tools/call":
            await self.queue.put({"jsonrpc": "2.0", "id": req["id"], "result": {
                "content": [{"type": "text", "text": self.call_result}],
                "isError": self.call_is_error}})
        return httpx.Response(202)


def test_sse_list_tools_returns_full_schemas(monkeypatch):
    _global_ip(monkeypatch)
    srv = _FakeSseServer()
    tools = asyncio.run(mcp_client.list_tools(
        SSE_URL, {}, transport=httpx.MockTransport(srv.handler),
        mcp_transport="sse"))
    assert len(tools) == 1
    assert tools[0]["name"] == "search"
    assert tools[0]["inputSchema"]["properties"]["q"]["type"] == "string"
    # never POSTed to the streamable base path — went straight to the endpoint
    assert all("/message" in u for u in srv.post_urls)


def test_sse_call_tool_happy_path(monkeypatch):
    _global_ip(monkeypatch)
    srv = _FakeSseServer(call_result="weather is sunny")
    out = asyncio.run(mcp_client.call_tool(
        SSE_URL, {}, "search", {"q": "weather"},
        transport=httpx.MockTransport(srv.handler), mcp_transport="sse"))
    assert out["is_error"] is False
    assert "weather is sunny" in out["text"]


def test_sse_call_tool_surfaces_tool_error(monkeypatch):
    _global_ip(monkeypatch)
    srv = _FakeSseServer(call_result="nope", call_is_error=True)
    out = asyncio.run(mcp_client.call_tool(
        SSE_URL, {}, "search", {},
        transport=httpx.MockTransport(srv.handler), mcp_transport="sse"))
    assert out["is_error"] is True
    assert "nope" in out["text"]


def test_sse_endpoint_cross_origin_is_refused_and_never_posted(monkeypatch):
    """The endpoint event is server-controlled. A cross-origin target must
    raise (origin mismatch) and NO request may be sent to that other host —
    the anti-SSRF invariant. The refusal is a confirmed-session ProbeError,
    so it must NOT be masked by a fallback attempt."""
    _global_ip(monkeypatch)
    srv = _FakeSseServer(endpoint="https://evil.example/msg")
    with pytest.raises(mcp_probe.ProbeError) as e:
        asyncio.run(mcp_client.list_tools(
            SSE_URL, {}, transport=httpx.MockTransport(srv.handler),
            mcp_transport="sse"))
    assert e.value.kind == "protocol"
    assert "origin mismatch" in e.value.detail
    # the cross-origin endpoint was never contacted, and no streamable
    # fallback masked the refusal
    assert srv.post_urls == []
    assert "GET" in srv.methods and "POST" not in srv.methods


def test_sse_pinned_ip_is_connect_target_host_and_sni_preserved(monkeypatch):
    """The GET stream and every endpoint POST connect to the validated IP while
    carrying the configured Host/SNI — the message channel is pinned too, so a
    same-origin endpoint can't trigger a second DNS lookup."""
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    seen = []
    q: asyncio.Queue = asyncio.Queue()

    def _stream():
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"event: endpoint\ndata: /message?s=1\n\n"
                while True:
                    doc = await q.get()
                    yield b"event: message\ndata: " + json.dumps(doc).encode() + b"\n\n"

            async def aclose(self):
                return None
        return Stream()

    async def handler(request):
        seen.append({
            "url_host": request.url.host,
            "host": request.headers.get("host"),
            "sni": request.extensions.get("sni_hostname"),
        })
        if request.method == "GET":
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=_stream())
        req = json.loads(request.content)
        method = req.get("method")
        if method == "initialize":
            await q.put({"jsonrpc": "2.0", "id": req["id"], "result": {}})
        elif method == "tools/list":
            await q.put({"jsonrpc": "2.0", "id": req["id"],
                         "result": {"tools": [{"name": "search", "inputSchema": {}}]}})
        return httpx.Response(202)

    tools = asyncio.run(mcp_client.list_tools(
        "https://mcp.example.com:8443/sse",
        {"Host": "attacker.invalid"},
        transport=httpx.MockTransport(handler), mcp_transport="sse"))
    assert tools[0]["name"] == "search"
    assert {item["url_host"] for item in seen} == {"93.184.216.34"}
    assert {item["host"] for item in seen} == {"mcp.example.com:8443"}
    assert {item["sni"] for item in seen} == {"mcp.example.com"}


def _streamable_mock_handler(*, get_status=405):
    """MockTransport handler for a streamable-only server: GET is rejected,
    POST speaks JSON-RPC. For the wrong-hint ("sse") fallback test."""
    async def handler(request):
        if request.method == "GET":
            return httpx.Response(get_status, json={"error": "use POST"})
        req = json.loads(request.content)
        method = req.get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            result = {"tools": [{"name": "search", "inputSchema": {}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "done"}], "isError": False}
        else:
            result = {"protocolVersion": "2025-03-26", "capabilities": {},
                      "serverInfo": {"name": "streamable", "version": "0"}}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": req.get("id"), "result": result},
            headers={"mcp-session-id": "s"})
    return handler


@pytest.mark.parametrize("mcp_transport", [None, "http"])
def test_default_and_http_hint_use_streamable(monkeypatch, mcp_transport):
    """Default/'http' goes straight to streamable POST — no wasteful SSE GET."""
    _global_ip(monkeypatch)
    methods = []

    async def handler(request):
        methods.append(request.method)
        return await _streamable_mock_handler()(request)

    tools = asyncio.run(mcp_client.list_tools(
        URL, {}, transport=httpx.MockTransport(handler),
        mcp_transport=mcp_transport))
    assert tools[0]["name"] == "search"
    assert "GET" not in methods  # never probed the SSE transport first


def test_wrong_http_hint_falls_back_to_sse(monkeypatch):
    """Persisted transport says 'http' but the server is actually legacy SSE
    (405 on the streamable POST) → narrow fallback connects over SSE."""
    _global_ip(monkeypatch)
    srv = _FakeSseServer(base_post_status=405)
    tools = asyncio.run(mcp_client.list_tools(
        SSE_URL, {}, transport=httpx.MockTransport(srv.handler),
        mcp_transport="http"))
    assert tools[0]["name"] == "search"


def test_wrong_sse_hint_falls_back_to_streamable(monkeypatch):
    """Persisted transport says 'sse' but the server is streamable (405 on GET)
    → narrow fallback connects over streamable HTTP."""
    _global_ip(monkeypatch)
    tools = asyncio.run(mcp_client.list_tools(
        URL, {}, transport=httpx.MockTransport(_streamable_mock_handler()),
        mcp_transport="sse"))
    assert tools[0]["name"] == "search"


# --- SSE GET-stream connect/TLS errors must map to ProbeError -----------------
#
# The streamable path maps connect errors via _post_bounded, but the SSE GET
# stream is opened inside _sse_session and was left unmapped: a self-signed SSE
# server leaked a raw httpx.ConnectError, so load_turn_mcp's
# `isinstance(exc, ProbeError) and exc.kind == "tls"` gate never fired and
# auto-CA was never attempted for SSE servers. The GET connect error is now
# classified exactly like _post_bounded.


def test_map_sse_connect_error_classifies_by_family():
    import ssl
    tls = httpx.ConnectError("boom")
    tls.__cause__ = ssl.SSLCertVerificationError("self signed certificate")
    assert mcp_client._map_sse_connect_error(tls).kind == "tls"
    assert mcp_client._map_sse_connect_error(httpx.ConnectTimeout("t")).kind == "timeout"
    assert mcp_client._map_sse_connect_error(httpx.ConnectError("refused")).kind == "transport"


def test_sse_get_tls_failure_surfaces_probeerror_tls(monkeypatch):
    """A self-signed SSE server (GET TLS handshake fails) -> ProbeError kind=tls,
    the exact signal load_turn_mcp uses to trigger the auto-CA fetch."""
    import ssl
    _global_ip(monkeypatch)

    def handler(request):
        raise httpx.ConnectError("tls handshake failed") from ssl.SSLCertVerificationError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] self signed certificate")

    with pytest.raises(mcp_client.ProbeError) as e:
        asyncio.run(mcp_client.list_tools(
            SSE_URL, {}, transport=httpx.MockTransport(handler), mcp_transport="sse"))
    assert e.value.kind == "tls"


def test_sse_get_connect_failure_surfaces_probeerror_transport(monkeypatch):
    _global_ip(monkeypatch)

    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(mcp_client.ProbeError) as e:
        asyncio.run(mcp_client.list_tools(
            SSE_URL, {}, transport=httpx.MockTransport(handler), mcp_transport="sse"))
    assert e.value.kind == "transport"


# --- 服务器自己写的使用说明:协议解析 -----------------------------------------
# ⚠️ 这几条锁的是**协议提取本身**。tests/test_v2_mcp_tools 那侧的用例是用 fake
# list_tools 主动往 box 里塞的 —— 把采集整段删掉,那 42 条照样全绿(我跑变异时
# 验证过它存活,codex 也独立指出)。夹具替生产代码把活干了,就等于没测那段代码。
#
# 走的是纯函数而不是整条握手:握手路径全程在 asyncio.timeout 里(3.11+),
# 本机 3.10 上这个文件里 34 条既有用例都跑不了。把解析规则抽出来,任何解释器
# 都能锁住它 —— 与其跟环境较劲,不如让逻辑可测。
# ⚠️ 仍未被本地锁住的一环:`_handshake` 到底有没有调这个函数。那条需要 3.11+,
# 已在双签信里点名交给 codex2 补。

def test_instructions_are_pulled_from_the_initialize_result():
    assert mcp_client._instructions_from_init_doc(
        {"result": {"instructions": "Call breath first."}}
    ) == ["Call breath first."]


def test_missing_blank_or_non_string_instructions_yield_nothing():
    """「没有说明」和「有一份空说明」对模型是两回事:后者会渲染出一个空章节。"""
    for doc in (
        {},
        {"result": {}},
        {"result": {"instructions": ""}},
        {"result": {"instructions": "   "}},
        {"result": {"instructions": None}},
        {"result": {"instructions": {"a": 1}}},
        {"result": {"instructions": 42}},
        {"result": "not-a-dict"},
        None,
        "garbage",
    ):
        assert mcp_client._instructions_from_init_doc(doc) == [], doc
