import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import mcp_probe  # noqa: E402

from _ca_helpers import self_signed_ca_pem  # noqa: E402


def _fake_mcp_app(require_auth: str | None = None):
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
            result = {"tools": [{"name": "search", "description": "d", "inputSchema": {}},
                                {"name": "fetch", "description": "d", "inputSchema": {}}]}
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
    assert out == {"ok": True, "tool_count": 2, "tool_names": ["search", "fetch"]}


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

    probe 跑在后端信任域且把上游响应体回显 160 字节给调用方
    （mcp_probe.py:120/:134），放开它等于把「探测内网/enclave/云元数据并读回
    响应」做成产品功能。存储层放开了私网 URL（Task 2），但后端**永远**不去连它。
    kind 从 blocked_url 改名为 unreachable_from_backend 只是文案诚实化，
    拒绝行为本身不变。
    """
    assert mcp_probe.blocked_url_kind(url) == "unreachable_from_backend"
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe.probe(url, {})
    assert e.value.kind == "unreachable_from_backend"


def test_empty_host_still_blocked_url():
    # 防御性分支：upsert 的 validate_url_syntax 已挡成 invalid_url，且 probe 的
    # URL 来自存储，正常不可达。保留但不作为用户可见路径。
    assert mcp_probe.blocked_url_kind("https:///mcp") == "blocked_url"


def test_probe_uses_ca_pem_for_verification(monkeypatch):
    """给了 ca_pem 就必须据其建 ssl context 传给 httpx，而不是沿用 certifi。"""
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    seen = {}
    real_client = httpx.AsyncClient

    def _spy(*args, **kwargs):
        seen["verify"] = kwargs.get("verify")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _spy)
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    out = mcp_probe.probe("https://mcp.example.com/mcp", {},
                          ca_pem=self_signed_ca_pem(), transport=transport)
    assert out["ok"] is True
    import ssl as _ssl
    assert isinstance(seen["verify"], _ssl.SSLContext)


def test_probe_without_ca_pem_leaves_verify_default(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    seen = {}
    real_client = httpx.AsyncClient

    def _spy(*args, **kwargs):
        seen["verify"] = kwargs.get("verify", "<absent>")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _spy)
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    mcp_probe.probe("https://mcp.example.com/mcp", {}, transport=transport)
    assert seen["verify"] == "<absent>"   # 不传 = httpx 默认 certifi 全校验


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
