"""Control-plane connectivity probe + SSRF guard for user MCP servers.

The backend-originated outbound calls in the user_mcp feature (spec §6).
Hand-rolled single-shot JSON-RPC — initialize → notifications/initialized →
tools/list — deliberately NOT the `mcp` SDK (one control-plane endpoint
doesn't justify the dependency + requirements.lock churn).

Two MCP transports are spoken (2026-07-19, SSE-transport batch):
  - streamable HTTP (2025-03-26): POST each JSON-RPC to the URL, answer comes
    back as JSON or a one-shot SSE body.
  - legacy HTTP+SSE (2024-11-05): GET the URL → long-lived event stream whose
    first event is ``endpoint`` (a same-origin POST target carrying the
    session); requests POST there, replies arrive back on the GET stream.
    Chinese providers (Tencent/AMap maps) still ship this as their
    advertised ``…/sse`` URL.
The probe auto-detects: a ``transport_hint`` ("sse" from the stored record /
URL heuristic) picks which handshake to try first, and each path falls back
to the other on that transport's signature failure. The result dict reports
the transport that actually worked so callers can persist it, plus the
read-only tool fingerprints eligible for parallel-read approval.

SSRF guard: the URL host must resolve to global addresses only. The pinned
helpers (``_pin_public_target``, used by the runtime-V2 MCP client) make the
validated address the actual connection target while the original hostname
stays the HTTP Host and TLS SNI value, closing the validate-then-resolve
DNS-rebinding gap. The probe path checks ``blocked_url_kind`` immediately
before connecting (small TOCTOU residual risk — spec §6). Redirects are
disabled outright, and the legacy ``endpoint`` event — server-controlled
data — is only followed when it targets the same scheme/host/port as the SSE
URL itself.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import ipaddress
import json
import socket
import ssl
import threading
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from core import net_safety
from hosted.mcp_approvals import MAX_READ_ONLY_TOOL_APPROVALS, valid_tool_name


@contextlib.asynccontextmanager
async def _safe_stream(cm):
    """Wrap an ``httpx`` streaming context manager so that closing the response
    can never mask the error the body raised. Tearing down a stream while the
    peer is still flooding (our size-budget abort) makes httpx's ``aclose``
    raise a ``ReadError``; without this, that teardown error would replace the
    ``ProbeError`` we actually want to report."""
    stream = await cm.__aenter__()
    try:
        yield stream
    except BaseException as e:
        try:
            await cm.__aexit__(type(e), e, e.__traceback__)
        except Exception:
            pass  # teardown error on an aborted stream — keep the real error
        raise
    else:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass  # clean-path close error is not worth failing a good probe


_CONNECT_TIMEOUT = 10.0
_TOTAL_TIMEOUT = 30.0
# Hard wall-clock deadline for the WHOLE probe. httpx's read timeout is
# per-socket-read: a legacy HTTP+SSE endpoint (e.g. mcp.map.qq.com/sse,
# 2026-07-19) answers the initialize POST with 200 + an event-stream that
# trickles keep-alive pings forever — every read completes well inside
# _TOTAL_TIMEOUT, so without this cap the probe (and the threadpool thread
# running it, routes go through threadpool.run_db) hangs indefinitely and
# iOS sits on "保存中…". 45s is a deliberate PRODUCT deadline for the whole
# 3-RPC handshake — well below the ~90s theoretical worst case of three
# back-to-back 30s reads: a server that needs longer than 45s to answer a
# handshake is unusable as an interactive chat tool anyway.
_WALL_TIMEOUT = 45.0
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
# Ceiling on bytes read from any one SSE stream while hunting for a frame —
# a stream that pings forever must exhaust this (or the wall clock), not RAM.
_MAX_SSE_BYTES = 262144


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


class _LegacySseEndpoint(Exception):
    """Streamable handshake hit the legacy transport's signature: the reply
    is an event stream whose FIRST event is ``endpoint`` (not a JSON-RPC
    frame). Carries nothing — the fallback re-connects with a fresh GET, so
    the session this POST may have opened is abandoned deliberately."""


class _NotSseServer(Exception):
    """The legacy-SSE handshake found this is NOT an SSE server (GET is not an
    event stream / 4xx / no endpoint event) — a "wrong transport" signal, so
    the caller should try streamable. Distinct from a ProbeError raised INSIDE
    a confirmed SSE session (rpc error, tools error, or the security refusal of
    a cross-origin endpoint), which must propagate and never trigger fallback."""


class _NotStreamableServer(Exception):
    """The streamable ``initialize`` POST returned a 4xx. MCP 2025-03-26
    backwards-compatibility says a standard legacy HTTP+SSE server answers that
    POST with a 4xx (405/404) and the client should then open the SSE GET —
    so this is a "try SSE" signal. Carries the original HTTP ProbeError so the
    dispatcher can surface it if the SSE attempt ALSO fails (a 401/404 the
    server means is more diagnostic than "not an SSE server").
    https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#backwards-compatibility
    """

    def __init__(self, err: "ProbeError"):
        super().__init__(str(err))
        self.err = err


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


def leaf_is_ca(url: str, *, timeout: float = 3.0) -> bool | None:
    """Does the server's LEAF cert have basicConstraints CA:TRUE? Read-only:
    a CERT_NONE handshake just to inspect the presented cert (never trusts it).
    True ⇒ a rustls-based agent (codex) will reject it as CaUsedAsEndEntity.
    None ⇒ non-global/unreachable/no cert (don't warn). SSRF guard reused."""
    if blocked_url_kind(url):
        return None
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or 443
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
        if not der:
            return None
        from cryptography import x509  # noqa: PLC0415 — backend dep, lazy
        cert = x509.load_der_x509_certificate(der)
        try:
            bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
            return bool(bc.ca)
        except x509.ExtensionNotFound:
            return False
    except Exception:  # noqa: BLE001 — read-only probe, never raise
        return None


def _classify_http(status: int) -> str:
    if status in (401, 403, 404):
        return f"http_{status}"
    if 400 <= status < 500:
        return "http_4xx"
    return "http_5xx"


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _effective_origin(parsed) -> tuple:
    """(scheme, lowercased host, effective port) with the scheme's default port
    filled in, so ``https://x`` and ``https://x:443`` compare equal. Used for
    the legacy SSE endpoint's same-origin check — a mismatch there is only real
    when the port genuinely differs, not when one side omitted the default."""
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port if parsed.port is not None else _DEFAULT_PORTS.get(scheme)
    return (scheme, host, port)


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


class _SseReader:
    """Incremental SSE event parser over ``resp.aiter_bytes()``.

    Frames lines from the RAW byte stream, not ``aiter_lines()``: httpx's line
    decoder only yields once it sees a newline, so a hostile server streaming
    megabytes with no newline would grow httpx's internal buffer unbounded
    while our per-line budget check never runs (codex3 P1). Working on bytes
    lets us cap the UNCONSUMED buffer on every chunk — ``_MAX_SSE_BYTES`` is a
    true memory guard (bytes, not codepoints) and the wall clock is the time
    guard.

    ONE reader per stream — the byte iterator can't be restarted without
    dropping buffered data.
    """

    def __init__(self, bytes_aiter):
        self._bytes = bytes_aiter
        self._buf = b""

    async def _next_line(self):
        """Next line (newline stripped) as bytes, or None at EOF. Raises if the
        unconsumed buffer grows past the budget without a line terminator."""
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1:]
                return line[:-1] if line.endswith(b"\r") else line
            if len(self._buf) > _MAX_SSE_BYTES:
                raise ProbeError("protocol", "SSE stream exceeded size budget")
            try:
                chunk = await self._bytes.__anext__()
            except StopAsyncIteration:
                if self._buf:
                    line, self._buf = self._buf, b""
                    return line[:-1] if line.endswith(b"\r") else line
                return None
            self._buf += chunk

    async def next_event(self) -> tuple[str, str]:
        """(event_name, data) of the next complete non-comment event.
        Absent ``event:`` field defaults to "message" per the SSE spec."""
        event, data, event_bytes = "", [], 0
        while True:
            raw = await self._next_line()
            if raw is None:  # EOF
                if data:
                    return (event or "message"), "\n".join(data)
                raise ProbeError("protocol", "SSE stream ended without a frame")
            # Cap the WHOLE event's bytes, counted BEFORE the field dispatch, so
            # a single oversized event:/comment/unknown line trips this too — not
            # only data: lines (millions of tiny data: lines, or one huge event:
            # line, both must fail).
            event_bytes += len(raw)
            if event_bytes > _MAX_SSE_BYTES:
                raise ProbeError("protocol", "SSE stream exceeded size budget")
            line = raw.decode("utf-8", errors="replace")
            if not line:
                if data:
                    return (event or "message"), "\n".join(data)
                event, data, event_bytes = "", [], 0
                continue
            if line.startswith(":"):
                continue  # comment/keep-alive ping
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].strip())

    async def next_rpc(self, want_id: int) -> dict:
        """Skip pings/notifications/other-id frames until ``want_id``'s reply."""
        while True:
            event, data = await self.next_event()
            if event == "endpoint":
                raise _LegacySseEndpoint()
            try:
                doc = json.loads(data)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("id") == want_id:
                return doc

    async def first_rpc_or_endpoint(self, want_id: int) -> dict:
        """For sniffing a streamable initialize reply that arrived as an
        event stream: the legacy transport betrays itself by making the
        FIRST event ``endpoint`` instead of a JSON-RPC frame."""
        return await self.next_rpc(want_id)


def _init_payload() -> dict:
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "feedling-probe", "version": "1.0"}},
    }


def _tools_from_rpc(doc: dict) -> dict:
    if "error" in doc:
        raise ProbeError("protocol", json.dumps(doc["error"])[:160])
    result = doc.get("result") or {}
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


async def _bounded_body(resp) -> str:
    """First ≤4KB of a (possibly streaming) response body, for error details.
    Never reads an unbounded stream into memory, and never turns a clean HTTP
    error into a transport error: reading the body is best-effort (a
    connection-close-delimited empty body can raise ReadError), so a read
    failure just yields whatever was collected so far."""
    raw = b""
    try:
        async for chunk in resp.aiter_bytes():
            raw += chunk
            if len(raw) >= 4096:
                break
    except httpx.HTTPError:
        pass
    return raw[:4096].decode("utf-8", errors="replace")


class _SseSession:
    """An initialized legacy HTTP+SSE session.

    Replies arrive on the shared long-lived GET stream (via ``_SseReader``);
    requests/notifications are POSTed to the same-origin ``endpoint`` the
    handshake validated. Used by BOTH the control-plane probe and the
    runtime-V2 ``mcp_client`` so the SSE wire logic lives in one place.
    """

    def __init__(self, reader: "_SseReader", msg_url: str, post):
        self._reader = reader
        self._msg_url = msg_url
        self._post = post

    async def request(self, rpc_id: int, method: str, params=None) -> dict:
        """POST one JSON-RPC request and return its matching reply frame."""
        payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._post(self._msg_url, payload)
        return await self._reader.next_rpc(rpc_id)

    async def notify(self, method: str) -> None:
        """POST a JSON-RPC notification, tolerating a 4xx (some servers reject
        ``notifications/initialized``)."""
        await self._post(
            self._msg_url,
            {"jsonrpc": "2.0", "method": method},
            tolerate_4xx=True,
        )


@contextlib.asynccontextmanager
async def _sse_session(*, stream_get, post, url: str):
    """Open a legacy HTTP+SSE session and yield an initialized ``_SseSession``.

    ``stream_get()`` returns the httpx streaming context manager for the
    long-lived GET (the caller sets the target/headers so the pinned runtime
    path and the raw probe path both work). ``post(msg_url, payload, *,
    tolerate_4xx=False)`` sends one JSON-RPC frame to the endpoint and enforces
    the HTTP status. ``url`` is the configured URL the ``endpoint`` event must
    stay same-origin with.

    ONE implementation drives both callers, so the security-critical steps —
    redirect refusal, the ``text/event-stream`` requirement, the ``_SseReader``
    byte budget, and the endpoint same-origin (anti-SSRF) check — exist in
    exactly one place. ``_NotSseServer`` is raised on a "wrong transport"
    signal (non-stream GET) so the caller can fall back; a ``ProbeError`` raised
    here (cross-origin refusal, malformed endpoint, rpc error) is a confirmed
    failure that must propagate, never trigger fallback.
    """
    async with _safe_stream(stream_get()) as stream:
        if stream.status_code in _REDIRECT_STATUSES:
            raise ProbeError("protocol", "redirects not allowed")
        if stream.status_code >= 400:
            # "wrong transport" signal — let the caller try streamable.
            raise _NotSseServer()
        if "text/event-stream" not in stream.headers.get("content-type", ""):
            raise _NotSseServer()
        reader = _SseReader(stream.aiter_bytes())
        event, data = await reader.next_event()
        if event != "endpoint":
            raise _NotSseServer()
        # The endpoint value is server-controlled data. Parsing it (or the
        # origin comparison) can raise ValueError — a malformed port
        # (https://h:bad) or IPv6 literal — which must surface as a clean 400
        # protocol error, never a 500 (codex3 R2).
        try:
            msg_url = urljoin(url, data.strip())
            same_origin = (
                _effective_origin(urlparse(msg_url))
                == _effective_origin(urlparse(url)))
        except ValueError:
            raise ProbeError("protocol", "invalid endpoint URI")
        # Refusing a cross-origin target keeps this from becoming an SSRF
        # primitive (the exchange echoes upstream bodies back to the caller).
        # Origins are NORMALIZED so an omitted default port
        # (https://x vs https://x:443) isn't a false mismatch, while a
        # genuinely different port still is.
        if not same_origin:
            raise ProbeError("protocol", "endpoint origin mismatch")
        await post(msg_url, _init_payload())
        init_doc = await reader.next_rpc(1)
        if "error" in init_doc:
            raise ProbeError("protocol", json.dumps(init_doc["error"])[:160])
        session = _SseSession(reader, msg_url, post)
        # spec-required before further requests; tolerate servers that 4xx it
        await session.notify("notifications/initialized")
        yield session


def probe(url: str, headers: dict, *, ca_pem: str | None = None,
          transport=None, transport_hint: str = "") -> dict:
    """Sync entry point (the callers — routes/CLI — are sync). ``httpx.ASGITransport``
    (used by tests to hit an in-process fake server) is async-only in this httpx
    version, so the actual work runs on a throwaway event loop via ``asyncio.run``
    — the same pattern ``backend/asgi_test_client.py`` uses for the same reason.

    ``transport`` is the httpx transport injection (tests). ``transport_hint``
    is the MCP transport to try first ("sse"/"http"/""); the result's
    ``transport`` key reports what actually worked.
    """
    kind = blocked_url_kind(url)
    if kind in ("blocked_url", "dns", "unreachable_from_backend"):
        raise ProbeError(kind, urlparse(url).hostname or "")
    return asyncio.run(_probe_bounded(url, headers, ca_pem, transport, transport_hint))


async def _probe_bounded(url: str, headers: dict, ca_pem: str | None,
                         transport, transport_hint: str = "") -> dict:
    """Wall-clock guard around ``_probe_async``: per-read timeouts alone don't
    bound a stream that trickles (see ``_WALL_TIMEOUT``). Cancellation unwinds
    through the ``async with httpx.AsyncClient`` in ``_probe_async``, so the
    connection is closed, not leaked. The wall clock covers BOTH transport
    attempts when detection falls back — 45s total, not 45s per attempt."""
    try:
        return await asyncio.wait_for(
            _probe_async(url, headers, ca_pem, transport, transport_hint),
            timeout=_WALL_TIMEOUT)
    except asyncio.TimeoutError:
        raise ProbeError("timeout", f"no reply within {_WALL_TIMEOUT:.0f}s wall clock")


async def _probe_async(url: str, headers: dict, ca_pem: str | None,
                       transport, transport_hint: str = "") -> dict:
    # _send_headers strips Host/Accept-Encoding and forces identity encoding so
    # a compressed reply can't expand past the size budget before we see it.
    send_headers = _send_headers(headers)

    def _map_net(e: Exception) -> ProbeError:
        if isinstance(e, httpx.ConnectTimeout):
            return ProbeError("timeout", "connect timeout")
        if isinstance(e, httpx.TimeoutException):
            return ProbeError("timeout", "read timeout")
        if isinstance(e, httpx.ConnectError):
            detail = str(e)[:160]
            return ProbeError("tls" if "ssl" in detail.lower() else "dns", detail)
        raise e

    async def _post(client: httpx.AsyncClient, payload: dict, extra: dict) -> httpx.Response:
        try:
            return await client.post(url, json=payload, headers={**send_headers, **extra})
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise _map_net(e)

    async def _streamable_flow(client: httpx.AsyncClient) -> dict:
        """POST-per-request handshake. Raises ``_LegacySseEndpoint`` when the
        initialize reply carries the legacy transport's signature."""
        try:
            cm = client.stream("POST", url, json=_init_payload(), headers=send_headers)
            async with _safe_stream(cm) as resp:
                if resp.status_code in _REDIRECT_STATUSES:
                    raise ProbeError("protocol", "redirects not allowed")
                # Reject a compressed body BEFORE any read: httpx's decoder would
                # expand one chunk past the size budget in memory before our
                # running check runs. Accept-Encoding was forced to identity.
                content_encoding = resp.headers.get(
                    "content-encoding", "").strip().lower()
                if content_encoding not in ("", "identity"):
                    raise ProbeError(
                        "protocol", "compressed MCP responses are not allowed")
                if resp.status_code >= 400:
                    err = ProbeError(_classify_http(resp.status_code),
                                     (await _bounded_body(resp))[:160])
                    # A 4xx here is the MCP backwards-compat signal to try the
                    # legacy SSE GET; 5xx is a genuine server failure, not a
                    # transport hint, so surface it directly.
                    if resp.status_code < 500:
                        raise _NotStreamableServer(err)
                    raise err
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    # Sniff incrementally — resp.aread() on a legacy endpoint
                    # would sit on the never-ending stream until the wall clock.
                    reader = _SseReader(resp.aiter_bytes())
                    init_doc = await reader.first_rpc_or_endpoint(1)
                else:
                    raw = await resp.aread()
                    try:
                        init_doc = json.loads(raw)
                    except ValueError:
                        raise ProbeError("protocol", f"non-JSON response ({ctype})")
                if not isinstance(init_doc, dict):
                    raise ProbeError("protocol", "non-object JSON-RPC reply")
                if "error" in init_doc:
                    raise ProbeError("protocol", json.dumps(init_doc["error"])[:160])
                session = {}
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    session["Mcp-Session-Id"] = sid
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise _map_net(e)
        # spec-required before further requests; tolerate servers that 4xx it
        await _post(client, {"jsonrpc": "2.0",
                             "method": "notifications/initialized"}, session)
        resp = await _post(client, {"jsonrpc": "2.0", "id": 2,
                                    "method": "tools/list"}, session)
        if resp.status_code >= 400:
            raise ProbeError(_classify_http(resp.status_code), resp.text[:160])
        body = _parse_rpc_response(resp)
        return {**_tools_from_rpc(body), "transport": "http"}

    async def _sse_flow(client: httpx.AsyncClient) -> dict:
        """Legacy HTTP+SSE handshake: GET stream → ``endpoint`` event →
        POST requests to the (same-origin) endpoint, replies on the stream.
        The GET/endpoint/same-origin/init machinery lives in the shared
        module-level ``_sse_session`` — this closure only supplies the raw
        (unpinned) probe transport and drives tools/list."""
        def _stream_get():
            get_headers = {k: v for k, v in send_headers.items()
                           if k.lower() != "content-type"}
            get_headers["Accept"] = "text/event-stream"
            return client.stream("GET", url, headers=get_headers)

        async def _sse_post(msg_url: str, payload: dict, *, tolerate_4xx: bool = False):
            try:
                resp = await client.post(msg_url, json=payload, headers=send_headers)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                raise _map_net(e)
            if resp.status_code >= 400 and not tolerate_4xx:
                raise ProbeError(_classify_http(resp.status_code), resp.text[:160])

        try:
            async with _sse_session(
                stream_get=_stream_get, post=_sse_post, url=url,
            ) as session:
                body = await session.request(2, "tools/list")
                return {**_tools_from_rpc(body), "transport": "sse"}
        except _LegacySseEndpoint:
            # endpoint events after the handshake are nonsense — treat as broken
            raise ProbeError("protocol", "unexpected endpoint event mid-session")
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise _map_net(e)

    # _client_kwargs pins trust_env=False (never inherit HTTP(S)_PROXY /
    # SSL_CERT_FILE from the worker env) and, when a CA is given, verifies
    # AGAINST it (add-trust, not skip-verify) on a certifi-backed context.
    async with httpx.AsyncClient(**_client_kwargs(ca_pem, transport)) as client:
        if str(transport_hint or "").strip().lower() == "sse":
            # Try SSE first. Fall back to streamable ONLY on the narrow
            # "this isn't an SSE server" signal — a ProbeError raised inside a
            # confirmed SSE session (rpc/tools error, cross-origin refusal)
            # must surface as-is, never be masked by a second attempt.
            try:
                return await _sse_flow(client)
            except _NotSseServer:
                pass
            try:
                return await _streamable_flow(client)
            except (_LegacySseEndpoint, _NotStreamableServer) as e:
                # Neither transport worked. If streamable gave a concrete HTTP
                # error (4xx), surface it — more diagnostic than a generic
                # "no transport". The bare endpoint-signature case has none.
                if isinstance(e, _NotStreamableServer):
                    raise e.err
                raise ProbeError("protocol", "no working MCP transport")
        try:
            return await _streamable_flow(client)
        except _LegacySseEndpoint:
            # Non-standard legacy server (Tencent): the streamable POST itself
            # answered with the endpoint signature. Fall to SSE.
            try:
                return await _sse_flow(client)
            except _NotSseServer:
                raise ProbeError("protocol", "no working MCP transport")
        except _NotStreamableServer as e:
            # Standard legacy server: the streamable POST 4xx'd. Try the SSE
            # GET; if that also says "not SSE", the 4xx is the real story.
            try:
                return await _sse_flow(client)
            except _NotSseServer:
                raise e.err
