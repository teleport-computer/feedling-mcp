"""Connectivity probe + SSRF guard for user MCP servers.

The ONLY backend-originated outbound call in the user_mcp feature (spec §6).
Hand-rolled single-shot JSON-RPC over streamable HTTP — initialize →
notifications/initialized → tools/list — deliberately NOT the `mcp` SDK
(one endpoint doesn't justify the dependency + requirements.lock churn).

SSRF guard: the URL host must resolve to global addresses only. Checked
immediately before connecting (small TOCTOU/DNS-rebinding window is a
documented residual risk — spec §6); redirects are disabled outright.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import httpx

from core import net_safety

_CONNECT_TIMEOUT = 10.0
_TOTAL_TIMEOUT = 30.0
_PROTOCOL_VERSION = "2025-03-26"


class ProbeError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _resolve_ips(host: str) -> list[str]:
    return net_safety.resolve_ips(host)


def blocked_url_kind(url: str) -> str | None:
    """"blocked_url" when the host resolves to any non-global address,
    "dns" when it doesn't resolve, None when clean."""
    return net_safety.blocked_url_kind(url, resolve=_resolve_ips)


def _classify_http(status: int) -> str:
    if status in (401, 403, 404):
        return f"http_{status}"
    if 400 <= status < 500:
        return "http_4xx"
    return "http_5xx"


def _parse_rpc_response(resp: httpx.Response) -> dict:
    """Streamable HTTP servers answer either application/json or a one-shot
    SSE stream; take the first `data:` event in the latter case."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise ProbeError("protocol", "empty SSE stream")
    try:
        return resp.json()
    except json.JSONDecodeError:
        raise ProbeError("protocol", f"non-JSON response ({ctype})")


def probe(url: str, headers: dict, *, transport=None) -> dict:
    """Sync entry point (the callers — routes/CLI — are sync). ``httpx.ASGITransport``
    (used by tests to hit an in-process fake server) is async-only in this httpx
    version, so the actual work runs on a throwaway event loop via ``asyncio.run``
    — the same pattern ``backend/asgi_test_client.py`` uses for the same reason."""
    kind = blocked_url_kind(url)
    if kind in ("blocked_url", "dns"):
        raise ProbeError(kind, urlparse(url).hostname or "")
    return asyncio.run(_probe_async(url, headers, transport))


async def _probe_async(url: str, headers: dict, transport) -> dict:
    send_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    send_headers.setdefault("Accept", "application/json, text/event-stream")
    send_headers["Content-Type"] = "application/json"

    async def _post(client: httpx.AsyncClient, payload: dict, extra: dict) -> httpx.Response:
        try:
            return await client.post(url, json=payload, headers={**send_headers, **extra})
        except httpx.ConnectTimeout:
            raise ProbeError("timeout", "connect timeout")
        except httpx.TimeoutException:
            raise ProbeError("timeout", "read timeout")
        except httpx.ConnectError as e:
            detail = str(e)[:160]
            raise ProbeError("tls" if "ssl" in detail.lower() else "dns", detail)

    timeout = httpx.Timeout(_TOTAL_TIMEOUT, connect=_CONNECT_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 transport=transport) as client:
        resp = await _post(client, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "feedling-probe", "version": "1.0"}},
        }, {})
        if resp.status_code >= 400:
            raise ProbeError(_classify_http(resp.status_code), resp.text[:160])
        if resp.status_code in (301, 302, 307, 308):
            raise ProbeError("protocol", "redirects not allowed")
        _parse_rpc_response(resp)  # validates the handshake succeeded
        session = {}
        sid = resp.headers.get("mcp-session-id")
        if sid:
            session["Mcp-Session-Id"] = sid
        # spec-required before further requests; tolerate servers that 4xx it
        await _post(client, {"jsonrpc": "2.0",
                             "method": "notifications/initialized"}, session)
        resp = await _post(client, {"jsonrpc": "2.0", "id": 2,
                                    "method": "tools/list"}, session)
        if resp.status_code >= 400:
            raise ProbeError(_classify_http(resp.status_code), resp.text[:160])
        body = _parse_rpc_response(resp)
        if "error" in body:
            raise ProbeError("protocol", json.dumps(body["error"])[:160])
        tools = (body.get("result") or {}).get("tools") or []
        names = [str(t.get("name") or "") for t in tools]
        return {"ok": True, "tool_count": len(names), "tool_names": names}
