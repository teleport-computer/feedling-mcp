import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import mcp_probe  # noqa: E402

from _ca_helpers import self_signed_ca_pem  # noqa: E402


def _fake_mcp_app(require_auth: str | None = None, tools: list[dict] | None = None):
    """进程内 fake streamable-HTTP MCP server（JSON 响应模式）。"""
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
        req = json.loads(body) if body else {}
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": tools if tools is not None else [
                {"name": "search", "description": "d", "inputSchema": {}},
                {"name": "fetch", "description": "d", "inputSchema": {}},
            ]}
        elif method == "notifications/initialized":
            await _respond(send, 202, None)
            return
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


def test_probe_happy_path(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    out = mcp_probe.probe("https://mcp.example.com/mcp", {}, transport=transport)
    assert out == {
        "ok": True,
        "tool_count": 2,
        "tool_names": ["search", "fetch"],
        "read_only_tool_fingerprints": {},
        "transport": "http",
    }


def test_probe_read_only_candidates_are_always_patch_compatible(monkeypatch):
    from hosted import mcp_core

    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    annotations = {"readOnlyHint": True}
    tools = [
        {"name": "bad\x00name", "inputSchema": {}, "annotations": annotations},
        {"name": "x" * 257, "inputSchema": {}, "annotations": annotations},
        *[
            {
                "name": f"read_{index}",
                "inputSchema": {"type": "object"},
                "annotations": annotations,
            }
            for index in range(70)
        ],
    ]
    transport = httpx.ASGITransport(app=_fake_mcp_app(tools=tools))

    out = mcp_probe.probe(
        "http://mcp.example.com/mcp", {}, transport=transport)

    candidates = out["read_only_tool_fingerprints"]
    assert list(candidates) == [f"read_{index}" for index in range(64)]
    assert len(candidates) == mcp_core.MAX_READ_ONLY_TOOL_APPROVALS
    assert mcp_core._validate_read_only_approvals(candidates) is None


def test_probe_duplicate_tool_names_fingerprint_first_routed_entry(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    first = {
        "name": "search",
        "inputSchema": {"type": "object", "required": ["first"]},
        "annotations": {"readOnlyHint": True},
    }
    duplicate = {
        "name": "search",
        "inputSchema": {"type": "object", "required": ["second"]},
        "annotations": {"readOnlyHint": True},
    }
    first_mutating = {
        "name": "write",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": False},
    }
    duplicate_read = {
        "name": "write",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }
    transport = httpx.ASGITransport(
        app=_fake_mcp_app(tools=[
            first,
            duplicate,
            first_mutating,
            duplicate_read,
        ]))

    out = mcp_probe.probe(
        "https://mcp.example.com/mcp", {}, transport=transport)

    assert out["read_only_tool_fingerprints"] == {
        "search": mcp_probe.catalog_tool_fingerprint(first)}


def test_probe_filters_malformed_catalog_entries_from_names_and_count(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    valid = {
        "name": "search",
        "inputSchema": {},
        "annotations": {"readOnlyHint": True},
    }
    transport = httpx.ASGITransport(app=_fake_mcp_app(tools=[
        None,
        "scalar",
        {},
        {"name": ""},
        valid,
    ]))

    out = mcp_probe.probe(
        "https://mcp.example.com/mcp", {}, transport=transport)

    assert out["tool_names"] == ["search"]
    assert out["tool_count"] == 1
    assert out["read_only_tool_fingerprints"] == {
        "search": mcp_probe.catalog_tool_fingerprint(valid)}


def test_probe_rejects_compressed_response_before_body_iteration(monkeypatch):
    monkeypatch.setattr(
        mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    seen_accept_encoding = []

    class MustNotRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("compressed probe response body must never be read")
            yield b"unreachable"

        async def aclose(self):
            return None

    async def handler(request):
        seen_accept_encoding.append(request.headers.get("accept-encoding"))
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": "br",
            },
            stream=MustNotRead(),
        )

    with pytest.raises(mcp_probe.ProbeError) as exc:
        mcp_probe.probe(
            "https://mcp.example.com/mcp",
            {"Accept-Encoding": "gzip"},
            transport=httpx.MockTransport(handler),
        )
    assert exc.value.kind == "protocol"
    assert exc.value.detail == "compressed MCP responses are not allowed"
    assert seen_accept_encoding == ["identity"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_probe_still_rejects_redirects(monkeypatch, status):
    monkeypatch.setattr(
        mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])

    async def handler(_request):
        return httpx.Response(
            status,
            headers={"location": "https://redirect.example/mcp"},
        )

    with pytest.raises(mcp_probe.ProbeError) as exc:
        mcp_probe.probe(
            "https://mcp.example.com/mcp",
            {},
            transport=httpx.MockTransport(handler),
        )
    assert exc.value.kind == "protocol"
    assert exc.value.detail == "redirects not allowed"


def test_probe_forwards_headers(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    transport = httpx.ASGITransport(app=_fake_mcp_app(require_auth="Bearer tok"))
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe.probe("https://mcp.example.com/mcp", {}, transport=transport)
    assert e.value.kind == "http_401"
    out = mcp_probe.probe("https://mcp.example.com/mcp",
                          {"Authorization": "Bearer tok"}, transport=transport)
    assert out["ok"] is True


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/mcp",
    "https://10.1.2.3/mcp",
    "https://192.168.1.1/mcp",
    "https://169.254.169.254/latest",   # 云元数据
    "https://[::1]/mcp",
    "http://192.168.1.5:8080/mcp",      # 放开 http 后依然不许后端去探
])
def test_backend_never_probes_non_global(url):
    """SSRF 回归守卫 —— 请勿删除、请勿弱化。

    probe 跑在后端信任域；放开它会把对内网/enclave/云元数据发起带用户头部的
    JSON-RPC 请求做成产品功能。存储层放开了私网 URL，但控制面**永远**不去连它。
    """
    assert mcp_probe.blocked_url_kind(url) == "unreachable_from_backend"
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe.probe(url, {})
    assert e.value.kind == "unreachable_from_backend"


def test_empty_host_still_blocked_url():
    # 防御性分支：upsert 的 validate_url_syntax 已挡成 invalid_url，且 probe 的
    # URL 来自存储，正常不可达。保留但不作为用户可见路径。
    assert mcp_probe.blocked_url_kind("https:///mcp") == "blocked_url"


def test_non_http_scheme_is_blocked_before_resolution(monkeypatch):
    monkeypatch.setattr(
        mcp_probe,
        "_resolve_ips",
        lambda host: (_ for _ in ()).throw(
            AssertionError("unsupported scheme must not resolve")),
    )
    assert mcp_probe.blocked_url_kind("ftp://mcp.example.com/mcp") == "blocked_url"


def test_probe_uses_ca_pem_for_verification(monkeypatch):
    """给了 ca_pem 就必须据其建 ssl context 传给 httpx，而不是沿用 certifi。"""
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    seen = {}
    real_client = httpx.AsyncClient

    def _spy(*args, **kwargs):
        seen["verify"] = kwargs.get("verify")
        seen["trust_env"] = kwargs.get("trust_env")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _spy)
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    out = mcp_probe.probe("https://mcp.example.com/mcp", {},
                          ca_pem=self_signed_ca_pem(), transport=transport)
    assert out["ok"] is True
    import ssl as _ssl
    assert isinstance(seen["verify"], _ssl.SSLContext)
    assert seen["trust_env"] is False


def test_probe_without_ca_pem_leaves_verify_default(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    seen = {}
    real_client = httpx.AsyncClient

    def _spy(*args, **kwargs):
        seen["verify"] = kwargs.get("verify", "<absent>")
        seen["trust_env"] = kwargs.get("trust_env")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _spy)
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    mcp_probe.probe("https://mcp.example.com/mcp", {}, transport=transport)
    assert seen["verify"] == "<absent>"   # 不传 = httpx 默认 certifi 全校验
    assert seen["trust_env"] is False


def test_blocked_url_kind_public_ok(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    assert mcp_probe.blocked_url_kind("https://mcp.example.com/x") is None


def test_dns_failure(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips",
                        lambda host: (_ for _ in ()).throw(OSError("nx")))
    assert mcp_probe.blocked_url_kind("https://no-such.example.invalid/") == "dns"


def _trickling_sse_app(chunk_sleep: float, max_chunks: int = 500,
                       flags: dict | None = None):
    """进程内复刻 legacy HTTP+SSE 端点对 initialize POST 的真实行为
    （mcp.map.qq.com/sse，2026-07-19 实测）：立刻 200 + text/event-stream，
    然后只滴心跳、永不给 JSON-RPC 应答、永不主动关流。

    每个 chunk 间隔 << 单次 read 超时，所以 httpx 的 per-read 超时永远不会触发
    —— 只有 wall-clock 上限能救。max_chunks 是测试套件自保（wall-clock 若被
    回归删掉，本 app 最多滴 max_chunks 次后收流，测试转为断言失败而不是挂死）。

    flags（可选）记录取消收栈证据：wall-clock 切断时本 app 必须收到
    CancelledError（"cancelled"）且 finally 必须执行（"unwound"）——
    这是「不留 pending task/连接」主张的长期守卫。
    """
    import asyncio as _asyncio

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        try:
            while True:
                event = await receive()
                if not event.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            for _ in range(max_chunks):
                await send({"type": "http.response.body",
                            "body": b": ping\n\n", "more_body": True})
                await _asyncio.sleep(chunk_sleep)
            await send({"type": "http.response.body", "body": b""})
        except _asyncio.CancelledError:
            if flags is not None:
                flags["cancelled"] = True
            raise
        finally:
            if flags is not None:
                flags["unwound"] = True

    return app


def test_probe_wall_clock_bounds_trickling_stream(monkeypatch):
    """SSE 挂流回归守卫:滴流让 per-read 超时永不触发,wall-clock 必须兜底。

    时序设定(全部确定性,无真实网络):chunk 间隔 0.02s < read 超时,
    wall-clock 压到 0.3s。断言:① 报 timeout;② 在远小于「滴完 500 个
    chunk(10s)」的时间内返回 —— 证明是 wall-clock 切的,不是流自己走完的。
    """
    import time

    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(mcp_probe, "_WALL_TIMEOUT", 0.3)
    flags: dict = {}
    transport = httpx.ASGITransport(app=_trickling_sse_app(chunk_sleep=0.02, flags=flags))
    t0 = time.monotonic()
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe.probe("https://mcp.example.com/sse", {}, transport=transport)
    elapsed = time.monotonic() - t0
    assert e.value.kind == "timeout"
    assert "wall clock" in e.value.detail
    assert elapsed < 3.0, f"probe took {elapsed:.1f}s — wall clock did not cut in"
    # 取消收栈证据:probe()(同步)返回时 asyncio.run 的 loop 已关闭,所有任务
    # 已结算 —— 上游 app 必须已收到 CancelledError 且 finally 已执行,
    # 否则就是留了 pending task/连接。
    assert flags.get("cancelled") is True
    assert flags.get("unwound") is True


def test_probe_wall_clock_leaves_fast_servers_alone(monkeypatch):
    """正常 streamable 服务器在 wall-clock 收紧后依然全绿(不误伤)。"""
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(mcp_probe, "_WALL_TIMEOUT", 5.0)
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    out = mcp_probe.probe("https://mcp.example.com/mcp", {}, transport=transport)
    assert out["ok"] is True and out["tool_count"] == 2


# ---------------------------------------------------------------------------
# Legacy HTTP+SSE transport (SSE-transport batch, 2026-07-19)
#
# httpx 0.28's ASGITransport buffers the WHOLE app response before returning
# (handle_async_request asserts response_complete), so a long-lived legacy
# stream cannot be faked in-process — these tests run a real loopback HTTP
# server (the pattern test_pi_mcp_bridge.py already uses) and neutralize the
# SSRF guard for 127.0.0.1 (the guard itself is covered by
# test_backend_never_probes_non_global above).
# ---------------------------------------------------------------------------

import queue as _queue  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


@pytest.fixture()
def _direct_loopback(monkeypatch):
    """These tests hit a real 127.0.0.1 server, and the probe's httpx client
    honors ambient proxies (trust_env — a dev machine's macOS system proxy /
    Clash surfaces via urllib.getproxies() even with no *_PROXY env var). Force
    a direct connection for loopback. Production behavior is untouched: a
    self-hosted resident behind a real proxy still uses it."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _legacy_sse_server(*, endpoint_override: str | None = None,
                       standard_legacy: bool = False):
    """Loopback fake of the legacy transport, modeled on mcp.map.qq.com/sse
    (2026-07-19 实测):
      GET  /sse       → event-stream: endpoint 事件,随后从队列吐 JSON-RPC 回复
      POST /sse       → (默认,腾讯式)event-stream: endpoint 事件——streamable
                        首连嗅探要识别的签名
      POST /messages  → 202;把对应回复放进 GET 流的队列

    standard_legacy=True 改为 MCP 官方规定的合规行为:POST /sse → 405
    (backwards-compat:客户端应据此转去 GET /sse)。
    """
    replies: _queue.Queue = _queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence per-request stderr noise
            pass

        def _sse_headers(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

        def _endpoint_event(self):
            data = endpoint_override or "/messages?session_id=s1"
            self.wfile.write(f"event:endpoint\ndata:{data}\n\n".encode())
            self.wfile.flush()

        def do_GET(self):
            self._sse_headers()
            self._endpoint_event()
            try:
                while True:
                    doc = replies.get(timeout=5)
                    self.wfile.write(
                        b"event:message\ndata:" + json.dumps(doc).encode() + b"\n\n")
                    self.wfile.flush()
            except (_queue.Empty, BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if self.path.startswith("/messages"):
                req = json.loads(body)
                if req.get("method") == "initialize":
                    replies.put({"jsonrpc": "2.0", "id": req["id"], "result": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "serverInfo": {"name": "legacy", "version": "0"}}})
                elif req.get("method") == "tools/list":
                    replies.put({"jsonrpc": "2.0", "id": req["id"], "result": {
                        "tools": [{"name": "geocode", "description": "d",
                                   "inputSchema": {}}]}})
                self.send_response(202)
                self.end_headers()
                return
            if standard_legacy:
                # MCP-compliant legacy server: POST to the SSE URL is 405; the
                # client must fall back to opening the GET stream.
                self.send_response(405)
                self.end_headers()
                return
            # POST to the base /sse URL mimics Tencent: a fresh stream whose
            # first event is `endpoint` — never a JSON-RPC frame.
            self._sse_headers()
            self._endpoint_event()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _streamable_only_server():
    """Loopback streamable-HTTP server that 405s GET — for wrong-hint fallback."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
            if req.get("method") == "initialize":
                doc = {"jsonrpc": "2.0", "id": req.get("id"), "result": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "serverInfo": {"name": "streamable", "version": "0"}}}
            elif req.get("method") == "tools/list":
                doc = {"jsonrpc": "2.0", "id": req.get("id"), "result": {
                    "tools": [{"name": "search", "description": "d",
                               "inputSchema": {}}]}}
            else:  # notifications/initialized
                self.send_response(202)
                self.end_headers()
                return
            payload = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_probe_detects_legacy_sse_without_hint(monkeypatch, _direct_loopback):
    """无 hint 首连:streamable 嗅探撞上 endpoint 签名 → 自动切 legacy 握手,
    全程走通并报 transport=sse。这是腾讯 /sse 用户路径的直接回归。"""
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    srv = _legacy_sse_server()
    try:
        out = mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/sse", {})
    finally:
        srv.shutdown()
    assert out["ok"] is True
    assert out["transport"] == "sse"
    assert out["tool_names"] == ["geocode"]


def test_probe_sse_hint_goes_straight_to_legacy(monkeypatch, _direct_loopback):
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    srv = _legacy_sse_server()
    try:
        out = mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/sse", {},
                              transport_hint="sse")
    finally:
        srv.shutdown()
    assert out["ok"] is True and out["transport"] == "sse"


def test_probe_standard_legacy_post_4xx_falls_back_to_get(monkeypatch, _direct_loopback):
    """MCP 官方 backwards-compat 路径:合规 legacy server 对 streamable
    initialize POST 回 405,客户端应转去 GET SSE 流。无 hint 也要走通
    (不能只认腾讯那种非标准 POST-200+endpoint)。"""
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    srv = _legacy_sse_server(standard_legacy=True)
    try:
        out = mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/sse", {})
    finally:
        srv.shutdown()
    assert out["ok"] is True and out["transport"] == "sse"
    assert out["tool_names"] == ["geocode"]


def test_probe_neither_transport_surfaces_http_error(monkeypatch, _direct_loopback):
    """POST 404 + GET 404(根本不是 MCP server):两次尝试都失败时,应 surface
    最有诊断价值的 HTTP 错误(http_404),而不是笼统的 'no working transport'。"""

    class Dead(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Dead)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    try:
        with pytest.raises(mcp_probe.ProbeError) as e:
            mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/x", {})
    finally:
        srv.shutdown()
    assert e.value.kind == "http_404"


def test_probe_sse_no_newline_flood_hits_byte_budget(monkeypatch, _direct_loopback):
    """恶意 legacy server:endpoint 后 GET 流狂发无换行字节。aiter_lines 永不
    yield,所以必须按字节 framing 的 _MAX_SSE_BYTES 兜底 —— 在远小于 wall-clock
    的时间内以 size budget 失败,并且连接关闭(不吃满内存/时间)。"""
    import time

    class Flood(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            # A wall of bytes with NO newline, forever — the reader can never
            # frame a line, so only the per-chunk byte-budget check (not the
            # newline-gated line yield) can stop it.
            try:
                while True:
                    self.wfile.write(b"x" * 8192)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Flood)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    monkeypatch.setattr(mcp_probe, "_WALL_TIMEOUT", 20.0)  # prove budget, not clock
    t0 = time.monotonic()
    try:
        with pytest.raises(mcp_probe.ProbeError) as e:
            mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/sse", {},
                            transport_hint="sse")
    finally:
        srv.shutdown()
    elapsed = time.monotonic() - t0
    assert e.value.kind == "protocol"
    assert "size budget" in e.value.detail
    assert elapsed < 10.0, f"took {elapsed:.1f}s — byte budget didn't cut in early"


def test_probe_sse_oversized_event_line_hits_byte_budget(monkeypatch, _direct_loopback):
    """One huge newline-terminated `event:` line (no data:) must still trip the
    per-event budget — the cap counts every field, not only data: lines."""

    class BigEvent(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b"event:" + b"A" * (512 * 1024) + b"\n")
                self.wfile.flush()
                while True:
                    self.wfile.write(b":\n")   # comments keep the stream open
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), BigEvent)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    try:
        with pytest.raises(mcp_probe.ProbeError) as e:
            mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/sse", {},
                            transport_hint="sse")
    finally:
        srv.shutdown()
    assert e.value.kind == "protocol"
    assert "size budget" in e.value.detail


def test_probe_wrong_sse_hint_falls_back_to_streamable(monkeypatch, _direct_loopback):
    """存了 sse hint 但服务器其实是 streamable(比如探测前记录被改):GET 405
    → 回落 streamable 全通,报 transport=http 供上游纠正持久化。"""
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    srv = _streamable_only_server()
    try:
        out = mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/mcp", {},
                              transport_hint="sse")
    finally:
        srv.shutdown()
    assert out["ok"] is True and out["transport"] == "http"


def test_effective_origin_default_port_equivalence():
    """https://x and https://x:443 (and http/:80) are the same origin; a
    genuinely different port is not. Guards the endpoint same-origin check
    against false mismatches on an omitted default port (codex3 P2)."""
    from urllib.parse import urlparse as _up
    eo = mcp_probe._effective_origin
    assert eo(_up("https://x/sse")) == eo(_up("https://x:443/messages"))
    assert eo(_up("http://x/sse")) == eo(_up("http://x:80/messages"))
    # host case-insensitive
    assert eo(_up("https://X.Example/sse")) == eo(_up("https://x.example/m"))
    # genuinely different port still differs
    assert eo(_up("https://x/sse")) != eo(_up("https://x:8443/messages"))
    # scheme difference differs (and http default 80 != https default 443)
    assert eo(_up("http://x/sse")) != eo(_up("https://x/messages"))


def test_probe_sse_endpoint_origin_mismatch_refused(monkeypatch, _direct_loopback):
    """endpoint 事件是服务器控制的数据——跨源指向必须拒绝(SSRF 回声原语),
    不允许把探测请求引到别的主机。"""
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    srv = _legacy_sse_server(
        endpoint_override="http://evil.example.invalid:1/messages")
    try:
        with pytest.raises(mcp_probe.ProbeError) as e:
            mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/sse", {},
                            transport_hint="sse")
    finally:
        srv.shutdown()
    assert e.value.kind == "protocol"
    assert "origin mismatch" in e.value.detail


def test_probe_sse_malformed_endpoint_port_is_400_not_500(monkeypatch, _direct_loopback):
    """A malformed endpoint URI (bad port) is server-controlled data — parsing
    it raises ValueError, which must become a clean 400 protocol error, not a
    500 (codex3 R2)."""
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    srv = _legacy_sse_server(
        endpoint_override="http://127.0.0.1:notaport/messages")
    try:
        with pytest.raises(mcp_probe.ProbeError) as e:
            mcp_probe.probe(f"http://127.0.0.1:{srv.server_port}/sse", {},
                            transport_hint="sse")
    finally:
        srv.shutdown()
    assert e.value.kind == "protocol"
    assert "invalid endpoint" in e.value.detail


# --- IP pinning: prefer a reachable validated IP (dual-stack / IPv4-only env) --
#
# net_safety.resolve_ips returns IPv6 addresses first for dual-stack hosts; a
# runner CVM with no IPv6 egress could never reach them. _pin_public_target used
# to pin ips[0] unconditionally (session-wide, no fallback), so a single
# unreachable IPv6 broke every user-MCP connection. It now pins the first
# validated IP that accepts a TCP connection; all candidates are already
# is_global-validated, so trying them in turn is still SSRF-safe and still pins
# ONE address for the whole session (no DNS-rebinding window).

_V6 = "2600:1f14:36ec:d00:c4d0:fedd:9df1:9d7b"   # global unicast (2000::/3)


def test_pin_single_ip_skips_reachability_probe(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(
        mcp_probe, "_probe_tcp",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("single IP must not be probed")))
    target = mcp_probe._pin_public_target("https://mcp.example.com/mcp")
    assert target.request_url.host == "93.184.216.34"
    assert target.sni_hostname == "mcp.example.com"


def test_pin_prefers_reachable_ip_when_first_is_unreachable(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: [_V6, "93.184.216.34"])
    # IPv4-only env: the IPv6 candidate never connects.
    monkeypatch.setattr(mcp_probe, "_probe_tcp",
                        lambda ip, port, timeout: ip == "93.184.216.34")
    target = mcp_probe._pin_public_target("https://mcp.example.com/mcp")
    assert target.request_url.host == "93.184.216.34"
    assert target.host_header == "mcp.example.com"
    assert target.sni_hostname == "mcp.example.com"


def test_pin_probes_the_configured_port(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: [_V6, "93.184.216.34"])

    def _probe(ip, port, timeout):
        seen["port"] = port
        return ip == "93.184.216.34"

    monkeypatch.setattr(mcp_probe, "_probe_tcp", _probe)
    target = mcp_probe._pin_public_target("https://mcp.example.com:8443/mcp")
    assert seen["port"] == 8443
    assert target.request_url.host == "93.184.216.34"


def test_pin_falls_back_to_first_ip_when_none_reachable(monkeypatch):
    # Nothing connects (probe can't run / all filtered) -> keep ips[0] so the
    # later real attempt surfaces the concrete error, exactly as before.
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: [_V6, "93.184.216.34"])
    monkeypatch.setattr(mcp_probe, "_probe_tcp", lambda ip, port, timeout: False)
    target = mcp_probe._pin_public_target("https://mcp.example.com/mcp")
    assert target.request_url.host == _V6


def test_first_reachable_ip_returns_first_success_and_stops(monkeypatch):
    probed = []

    def _probe(ip, port, timeout):
        probed.append(ip)
        return ip == "8.8.8.8"

    monkeypatch.setattr(mcp_probe, "_probe_tcp", _probe)
    chosen = mcp_probe._first_reachable_ip([_V6, "8.8.8.8", "1.1.1.1"], 443)
    assert chosen == "8.8.8.8"
    assert probed == [_V6, "8.8.8.8"]   # stopped before 1.1.1.1
