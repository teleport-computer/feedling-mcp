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
