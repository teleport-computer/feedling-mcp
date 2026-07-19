"""Connectivity probe + SSRF guard for user MCP servers.

The ONLY backend-originated outbound call in the user_mcp feature (spec §6).
Hand-rolled single-shot JSON-RPC — initialize → notifications/initialized →
tools/list — deliberately NOT the `mcp` SDK (one endpoint doesn't justify the
dependency + requirements.lock churn).

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
the transport that actually worked so callers can persist it.

SSRF guard: the URL host must resolve to global addresses only. Checked
immediately before connecting (small TOCTOU/DNS-rebinding window is a
documented residual risk — spec §6); redirects are disabled outright, and the
legacy ``endpoint`` event — server-controlled data — is only followed when it
targets the same scheme/host/port as the SSE URL itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import ssl
from urllib.parse import urljoin, urlparse

import httpx


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
# Ceiling on bytes read from any one SSE stream while hunting for a frame —
# a stream that pings forever must exhaust this (or the wall clock), not RAM.
_MAX_SSE_BYTES = 262144


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
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def blocked_url_kind(url: str) -> str | None:
    """"unreachable_from_backend" when the host resolves to any non-global
    address, "blocked_url" when the URL has no host at all, "dns" when it
    doesn't resolve, None when clean.

    NOTE: non-global hosts are storable (mcp_core no longer pre-checks — the
    agent, not the backend, makes the real MCP call). The backend still refuses
    to CONNECT: this function runs in the backend trust domain and probe()
    echoes 160 bytes of the upstream body back to the caller, so relaxing it
    would ship an SSRF-with-echo primitive. Do not remove.
    """
    host = urlparse(url).hostname or ""
    if not host:
        return "blocked_url"
    try:
        ip = ipaddress.ip_address(host)
        return None if ip.is_global else "unreachable_from_backend"
    except ValueError:
        pass  # hostname, not a literal IP
    try:
        ips = _resolve_ips(host)
    except OSError:
        return "dns"
    for raw in ips:
        if not ipaddress.ip_address(raw).is_global:
            return "unreachable_from_backend"
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
    tools = (doc.get("result") or {}).get("tools") or []
    names = [str(t.get("name") or "") for t in tools]
    return {"ok": True, "tool_count": len(names), "tool_names": names}


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
    send_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    send_headers.setdefault("Accept", "application/json, text/event-stream")
    send_headers["Content-Type"] = "application/json"

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
                if resp.status_code in (301, 302, 307, 308):
                    raise ProbeError("protocol", "redirects not allowed")
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
        POST requests to the (same-origin) endpoint, replies on the stream."""
        get_headers = {k: v for k, v in send_headers.items()
                       if k.lower() != "content-type"}
        get_headers["Accept"] = "text/event-stream"

        async def _sse_post(msg_url: str, payload: dict, *, tolerate_4xx: bool = False):
            try:
                resp = await client.post(msg_url, json=payload, headers=send_headers)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                raise _map_net(e)
            if resp.status_code >= 400 and not tolerate_4xx:
                raise ProbeError(_classify_http(resp.status_code), resp.text[:160])

        try:
            cm = client.stream("GET", url, headers=get_headers)
            async with _safe_stream(cm) as stream:
                if stream.status_code in (301, 302, 307, 308):
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
                # The endpoint value is server-controlled data. Parsing it (or
                # the origin comparison) can raise ValueError — a malformed port
                # (https://h:bad) or IPv6 literal — which must surface as a clean
                # 400 protocol error, never a 500 (codex3 R2).
                try:
                    msg_url = urljoin(url, data.strip())
                    same_origin = (
                        _effective_origin(urlparse(msg_url))
                        == _effective_origin(urlparse(url)))
                except ValueError:
                    raise ProbeError("protocol", "invalid endpoint URI")
                # Refusing a cross-origin target keeps this from becoming an SSRF
                # primitive (probe echoes upstream bodies back to the caller).
                # Origins are NORMALIZED so an omitted default port
                # (https://x vs https://x:443) isn't a false mismatch, while a
                # genuinely different port still is.
                if not same_origin:
                    raise ProbeError("protocol", "endpoint origin mismatch")
                await _sse_post(msg_url, _init_payload())
                init_doc = await reader.next_rpc(1)
                if "error" in init_doc:
                    raise ProbeError("protocol", json.dumps(init_doc["error"])[:160])
                # spec-required; tolerate servers that reject the notification
                await _sse_post(msg_url, {"jsonrpc": "2.0",
                                          "method": "notifications/initialized"},
                                tolerate_4xx=True)
                await _sse_post(msg_url, {"jsonrpc": "2.0", "id": 2,
                                          "method": "tools/list"})
                body = await reader.next_rpc(2)
                return {**_tools_from_rpc(body), "transport": "sse"}
        except _LegacySseEndpoint:
            # endpoint events after the handshake are nonsense — treat as broken
            raise ProbeError("protocol", "unexpected endpoint event mid-session")
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise _map_net(e)

    timeout = httpx.Timeout(_TOTAL_TIMEOUT, connect=_CONNECT_TIMEOUT)
    client_kwargs = {"timeout": timeout, "follow_redirects": False,
                     "transport": transport}
    if ca_pem:
        # The user pinned their own CA: verify AGAINST IT rather than certifi.
        # This is "add trust", not "skip verification" — a self-signed server
        # still has to prove it holds the matching key.
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=ca_pem)
        client_kwargs["verify"] = ctx
    async with httpx.AsyncClient(**client_kwargs) as client:
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
