"""Control-plane connectivity probe + SSRF guard for user MCP servers.

This is the explicit API-backend probe in the user_mcp feature (spec §6).
Hand-rolled single-shot JSON-RPC over streamable HTTP — initialize →
notifications/initialized → tools/list — deliberately NOT the `mcp` SDK (one
control-plane endpoint doesn't justify the dependency + requirements.lock churn).

SSRF guard: the URL host must resolve to global addresses only.  The selected
validated address is then used as the actual connection target for the whole
JSON-RPC exchange while the original hostname remains the HTTP Host and TLS
SNI value.  This closes the validate-then-resolve DNS-rebinding gap. Redirects
are disabled outright.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import ipaddress
import json
import ssl
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from core import net_safety
from hosted.mcp_approvals import MAX_READ_ONLY_TOOL_APPROVALS, valid_tool_name

_CONNECT_TIMEOUT = 10.0
_TOTAL_TIMEOUT = 30.0
_PROTOCOL_VERSION = "2025-03-26"
_MAX_RESPONSE_BYTES = 1024 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DNS_WORKERS = 8
_DNS_MAX_PENDING = 32
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_DNS_WORKERS,
    thread_name_prefix="feedling-mcp-dns",
)
_DNS_SUBMISSION_SLOTS = threading.BoundedSemaphore(_DNS_MAX_PENDING)


def catalog_tool_fingerprint(tool: dict) -> str:
    """Change-sensitive approval key for one remote tool's read semantics.

    Free-form descriptions are deliberately excluded because Runtime V2 never
    injects them. Name, schema, and a strict boolean readOnlyHint are the exact
    catalog fields whose change must invalidate a user's read-only approval.
    """
    annotations = tool.get("annotations")
    semantic = {
        "name": str(tool.get("name") or ""),
        "inputSchema": tool.get("inputSchema"),
        "readOnlyHint": (
            isinstance(annotations, dict)
            and annotations.get("readOnlyHint") is True
        ),
    }
    try:
        encoded = json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        encoded = b"invalid-mcp-tool-catalog-entry"
    return hashlib.sha256(encoded).hexdigest()


class ProbeError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _resolve_ips(host: str) -> list[str]:
    return net_safety.resolve_ips(host)


@dataclass(frozen=True)
class _PinnedTarget:
    """One SSRF-validated network target.

    ``request_url`` contains the literal validated address so the HTTP stack
    cannot perform a second DNS lookup. ``host_header`` and ``sni_hostname``
    preserve virtual-host routing and certificate verification against the
    configured hostname.
    """

    request_url: httpx.URL
    host_header: str
    sni_hostname: str


def _validated_public_ips(url: str) -> tuple[str, list[str]]:
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        # Accessing .port can itself reject malformed authorities.
        parsed.port
    except ValueError:
        raise ProbeError("blocked_url", "invalid URL") from None
    if scheme not in ("http", "https") or not host:
        raise ProbeError("blocked_url", "missing host")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            raw_ips = _resolve_ips(host)
        except OSError:
            raise ProbeError("dns", "DNS resolution failed") from None
        if not raw_ips:
            raise ProbeError("dns", "DNS resolution failed")
        ips: list[str] = []
        try:
            for raw in raw_ips:
                address = ipaddress.ip_address(raw)
                if not address.is_global:
                    raise ProbeError(
                        "unreachable_from_backend", "non-public address")
                normalized = str(address)
                if normalized not in ips:
                    ips.append(normalized)
        except ValueError:
            raise ProbeError("dns", "DNS resolution failed") from None
        if not ips:
            raise ProbeError("dns", "DNS resolution failed")
        return host, ips

    if not literal.is_global:
        raise ProbeError("unreachable_from_backend", "non-public address")
    return host, [str(literal)]


def _pin_public_target(url: str) -> _PinnedTarget:
    _resolved_host, ips = _validated_public_ips(url)
    try:
        configured_url = httpx.URL(url)
    except (TypeError, ValueError):
        raise ProbeError("blocked_url", "invalid URL") from None
    if configured_url.scheme not in ("http", "https"):
        raise ProbeError("blocked_url", "unsupported URL scheme")
    # httpx has already IDNA-normalized this ASCII hostname. Use it for Host/SNI
    # while DNS validation may have accepted the original Unicode spelling.
    host = configured_url.raw_host.decode("ascii")

    # A single operation deliberately pins one validated address for initialize,
    # initialized, and the subsequent list/call. A new operation resolves afresh.
    request_url = configured_url.copy_with(host=ips[0])
    host_header = f"[{host}]" if ":" in host else host
    if configured_url.port is not None:
        default_port = 443 if configured_url.scheme == "https" else 80
        if configured_url.port != default_port:
            host_header = f"{host_header}:{configured_url.port}"
    return _PinnedTarget(
        request_url=request_url,
        host_header=host_header,
        sni_hostname=host,
    )


async def _pin_public_target_async(url: str) -> _PinnedTarget:
    """Resolve on a dedicated, submission-bounded executor.

    Cancelling ``getaddrinfo`` cannot stop its native worker thread. Keeping
    those calls off asyncio's shared default executor prevents hostile MCP DNS
    from silently exhausting unrelated provider/enclave work. The submission
    semaphore also prevents an unbounded executor queue while the resolver is
    degraded; excess work fails closed and the server is skipped for this turn.
    """
    if not _DNS_SUBMISSION_SLOTS.acquire(blocking=False):
        raise ProbeError("dns_busy", "resolver capacity exhausted")
    try:
        future = _DNS_EXECUTOR.submit(_pin_public_target, url)
    except Exception:
        _DNS_SUBMISSION_SLOTS.release()
        raise ProbeError("dns", "DNS resolver unavailable") from None

    future.add_done_callback(lambda _future: _DNS_SUBMISSION_SLOTS.release())
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        # This cancels queued work when possible. A running getaddrinfo remains
        # bounded by the dedicated worker count and releases its slot on finish.
        future.cancel()
        raise


def blocked_url_kind(url: str) -> str | None:
    """"unreachable_from_backend" when the host resolves to any non-global
    address, "blocked_url" when the URL has no host at all, "dns" when it
    doesn't resolve, None when clean.

    NOTE: non-global hosts are storable (mcp_core no longer pre-checks — the
    agent, not the backend, makes the real MCP call). The backend still refuses
    to CONNECT because this function runs in the backend trust domain. Do not
    remove or weaken this guard.
    """
    try:
        _validated_public_ips(url)
        return None
    except ProbeError as exc:
        return exc.kind


def _classify_http(status: int) -> str:
    if status in (401, 403, 404):
        return f"http_{status}"
    if 400 <= status < 500:
        return "http_4xx"
    return "http_5xx"


def _parse_rpc_response(resp: httpx.Response) -> dict:
    """Streamable HTTP servers answer either application/json or a one-shot
    SSE stream; take the first `data:` event in the latter case."""
    ctype = resp.headers.get("content-type", "").lower()
    if "text/event-stream" in ctype:
        try:
            text = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            raise ProbeError("protocol", "invalid SSE encoding") from None
        # SSE joins consecutive data fields with newlines and terminates an
        # event with a blank line. Accept a final unterminated event as a
        # compatibility concession for simple one-shot servers.
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for event in normalized.split("\n\n"):
            data = [
                line[5:].lstrip(" ")
                for line in event.split("\n")
                if line.startswith("data:")
            ]
            if data:
                try:
                    body = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    raise ProbeError("protocol", "invalid SSE JSON") from None
                if not isinstance(body, dict):
                    raise ProbeError("protocol", "JSON-RPC response must be an object")
                return body
        raise ProbeError("protocol", "empty SSE stream")
    try:
        body = json.loads(resp.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProbeError("protocol", "non-JSON response") from None
    if not isinstance(body, dict):
        raise ProbeError("protocol", "JSON-RPC response must be an object")
    return body


def _client_kwargs(ca_pem, transport) -> dict:
    kwargs = {
        "timeout": httpx.Timeout(_TOTAL_TIMEOUT, connect=_CONNECT_TIMEOUT),
        "follow_redirects": False,
        # User-controlled MCP traffic must never inherit HTTP(S)_PROXY or
        # SSL_CERT_FILE from the worker environment.
        "trust_env": False,
        "transport": transport,
    }
    if ca_pem:
        # Add-trust (verify AGAINST the user's CA), never skip-verify.
        try:
            # Build the same certifi-backed context as httpx trust_env=False,
            # then add the user CA. ssl.create_default_context() can consult
            # OpenSSL's SSL_CERT_FILE/SSL_CERT_DIR environment variables.
            ctx = httpx.create_ssl_context(verify=True, trust_env=False)
            ctx.load_verify_locations(cadata=ca_pem)
        except (ssl.SSLError, TypeError, ValueError):
            raise ProbeError("tls", "invalid CA bundle") from None
        kwargs["verify"] = ctx
    return kwargs


def _send_headers(headers) -> dict:
    out = {
        str(k): str(v)
        for k, v in (headers or {}).items()
        if str(k).strip().lower() not in {"host", "accept-encoding"}
    }
    out.setdefault("Accept", "application/json, text/event-stream")
    # Bound the wire representation itself. httpx's decoded-byte iterator can
    # materialize one arbitrarily large gzip/zstd expansion before our running
    # size check sees that chunk, so compressed MCP responses are not accepted.
    # A peer that ignores this request header is rejected from response headers
    # before any body iterator (and therefore any decoder) is entered below.
    out["Accept-Encoding"] = "identity"
    out["Content-Type"] = "application/json"
    return out


def _first_complete_sse_event(content: bytes) -> bool:
    normalized = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for event in normalized.split(b"\n\n")[:-1]:
        if any(line.startswith(b"data:") for line in event.split(b"\n")):
            return True
    return False


async def _read_bounded_response(resp: httpx.Response) -> httpx.Response:
    """Read raw identity bytes incrementally and fail before JSON/SSE parse."""
    content_encoding = resp.headers.get("content-encoding", "").strip().lower()
    if content_encoding not in {"", "identity"}:
        # Check before touching the body. ``aiter_bytes`` would invoke httpx's
        # decoder first, and a single compressed chunk can expand far past the
        # cap in memory before Python regains control.
        raise ProbeError("protocol", "compressed MCP responses are not allowed")
    content = bytearray()
    is_sse = "text/event-stream" in resp.headers.get("content-type", "").lower()
    # Mock/custom transports may hand httpx a pre-buffered identity response.
    # The real network path entered through ``client.stream`` is unconsumed and
    # always takes the raw iterator below.
    if resp.is_stream_consumed:
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise ProbeError("response_too_large", "MCP response exceeded limit")
        content.extend(resp.content)
        return httpx.Response(
            status_code=resp.status_code,
            headers=resp.headers,
            content=bytes(content),
            request=resp.request,
            extensions=dict(resp.extensions),
        )
    async for chunk in resp.aiter_raw():
        if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise ProbeError("response_too_large", "MCP response exceeded limit")
        content.extend(chunk)
        # Streamable-HTTP SSE connections may remain open after the response
        # event. Stop at the first complete data event instead of waiting for EOF.
        if is_sse and _first_complete_sse_event(content):
            break
    return httpx.Response(
        status_code=resp.status_code,
        headers=resp.headers,
        content=bytes(content),
        request=resp.request,
        extensions=dict(resp.extensions),
    )


def _contains_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


async def _post_bounded(
    client: httpx.AsyncClient,
    target: _PinnedTarget,
    send_headers: dict,
    payload: dict,
    extra: dict,
) -> httpx.Response:
    headers = {**send_headers, **extra}
    for key in list(headers):
        if key.lower() == "host":
            del headers[key]
    headers["Host"] = target.host_header
    try:
        # httpx's read timeout resets after every chunk. The outer deadline also
        # bounds an SSE peer that sends keepalives forever.
        async with asyncio.timeout(_TOTAL_TIMEOUT):
            async with client.stream(
                "POST",
                target.request_url,
                json=payload,
                headers=headers,
                extensions={"sni_hostname": target.sni_hostname},
            ) as resp:
                return await _read_bounded_response(resp)
    except ProbeError:
        raise
    except httpx.ConnectTimeout:
        raise ProbeError("timeout", "connect timeout") from None
    except (httpx.TimeoutException, TimeoutError):
        raise ProbeError("timeout", "request timeout") from None
    except httpx.DecodingError:
        raise ProbeError("protocol", "invalid response encoding") from None
    except httpx.ConnectError as exc:
        if _contains_tls_error(exc):
            raise ProbeError("tls", "TLS connection failed") from None
        raise ProbeError("transport", "connection failed") from None
    except httpx.RemoteProtocolError:
        raise ProbeError("protocol", "invalid HTTP response") from None
    except httpx.TransportError:
        raise ProbeError("transport", "connection failed") from None
    except (TypeError, ValueError, UnicodeError):
        raise ProbeError("protocol", "invalid MCP request") from None


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code in _REDIRECT_STATUSES:
        raise ProbeError("protocol", "redirects not allowed")
    if resp.status_code >= 400:
        raise ProbeError(
            _classify_http(resp.status_code),
            f"upstream HTTP {resp.status_code}",
        )


def probe(url: str, headers: dict, *, ca_pem: str | None = None,
          transport=None) -> dict:
    """Sync entry point (the callers — routes/CLI — are sync). ``httpx.ASGITransport``
    (used by tests to hit an in-process fake server) is async-only in this httpx
    version, so the actual work runs on a throwaway event loop via ``asyncio.run``
    — the same pattern ``backend/asgi_test_client.py`` uses for the same reason."""
    return asyncio.run(_probe_operation(url, headers, ca_pem, transport))


async def _probe_operation(
    url: str,
    headers: dict,
    ca_pem: str | None,
    transport,
) -> dict:
    try:
        async with asyncio.timeout(_TOTAL_TIMEOUT):
            target = await _pin_public_target_async(url)
            return await _probe_async(target, headers, ca_pem, transport)
    except ProbeError:
        raise
    except TimeoutError:
        raise ProbeError("timeout", "operation timeout") from None


async def _probe_async(target: _PinnedTarget, headers: dict, ca_pem: str | None,
                       transport) -> dict:
    send_headers = _send_headers(headers)
    async with httpx.AsyncClient(**_client_kwargs(ca_pem, transport)) as client:
        resp = await _post_bounded(client, target, send_headers, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "feedling-probe", "version": "1.0"}},
        }, {})
        _raise_for_status(resp)
        _parse_rpc_response(resp)  # validates the handshake succeeded
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
        result = body.get("result") or {}
        if not isinstance(result, dict):
            raise ProbeError("protocol", "tools/list result must be an object")
        raw_tools = result.get("tools") or []
        if not isinstance(raw_tools, list):
            raise ProbeError("protocol", "tools/list tools must be an array")
        tools = [
            tool
            for tool in raw_tools
            if isinstance(tool, dict) and str(tool.get("name") or "")
        ]
        names = [str(tool.get("name") or "") for tool in tools]
        read_only_fingerprints: dict[str, str] = {}
        seen_tool_names: set[str] = set()
        for tool in tools:
            name = str(tool.get("name") or "")
            # Runtime routing is first-name-wins when a broken catalog repeats a
            # tool name. Mark the name seen even when the first entry is not a
            # read-only candidate, so a later duplicate cannot masquerade as it.
            if name in seen_tool_names:
                continue
            seen_tool_names.add(name)
            annotations = tool.get("annotations")
            if (
                not valid_tool_name(name)
                or not isinstance(annotations, dict)
                or annotations.get("readOnlyHint") is not True
            ):
                continue
            # PATCH accepts at most 64 unique approvals. Keep probing/tool_names
            # lossless, but expose only candidates that can be sent back as-is.
            if len(read_only_fingerprints) >= MAX_READ_ONLY_TOOL_APPROVALS:
                continue
            read_only_fingerprints[name] = catalog_tool_fingerprint(tool)
        return {
            "ok": True,
            "tool_count": len(names),
            "tool_names": names,
            "read_only_tool_fingerprints": read_only_fingerprints,
        }
