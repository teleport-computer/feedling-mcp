"""Async MCP JSON-RPC client for the V2 hosted runtime.

`mcp_probe.probe` is a one-shot connectivity check that returns tool *names*.
The V2 tool loop needs more: the full tool *schemas* (to offer them as
model-facing tools) and the ability to *invoke* a tool (`tools/call`). This
module adds exactly those two operations, reusing mcp_probe's SSRF guard,
response parser, error taxonomy, and protocol constants — the same hand-rolled
JSON-RPC, deliberately not the `mcp` SDK.

Two MCP transports are spoken, matching mcp_probe:
  - streamable HTTP (2025-03-26): POST each JSON-RPC to the URL.
  - legacy HTTP+SSE (2024-11-05): GET the URL → event stream whose first event
    is a same-origin ``endpoint`` POST target; requests POST there, replies
    arrive on the GET stream. AMap/Tencent maps still ship this as ``…/sse``.
The persisted ``transport`` (``mcp_transport``, from the probe result stored in
the config envelope) picks which handshake to try first; each path falls back
to the other ONLY on that transport's "wrong transport" signature — a
``ProbeError`` raised inside a confirmed session (rpc error, cross-origin
refusal) propagates and is never masked by a second attempt.

Trust domain: identical to probe. Every operation resolves and validates the
configured host once, then connects to that literal public address for the
whole exchange while preserving the configured Host/SNI. Redirects and ambient
proxy settings are disabled. CA pinning (`ca_pem`) is honored the same way.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
from hosted import mcp_probe

from hosted.mcp_probe import (
    _PROTOCOL_VERSION,
    ProbeError,
    _LegacySseEndpoint,
    _NotSseServer,
    _NotStreamableServer,
    _PinnedTarget,
    _REDIRECT_STATUSES,
    _SseReader,
    _classify_http,
    _client_kwargs,
    _contains_tls_error,
    _parse_rpc_response,
    _pin_public_target_async,
    _post_bounded,
    _raise_for_status,
    _send_headers,
    _sse_session,
)


def _initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "feedling-mcp-client", "version": "1.0"}},
    }


async def _once_aiter(data: bytes):
    """Yield an already-buffered response body once so ``_SseReader`` can frame
    it — used to sniff a streamable initialize reply that arrived as an event
    stream (the legacy transport betrays itself by making the FIRST event
    ``endpoint`` instead of a JSON-RPC frame)."""
    yield data


def _instructions_from_init_doc(init_doc) -> list[str]:
    """Pull the spec's optional ``result.instructions`` out of an initialize doc.

    A separate pure function on purpose: the whole handshake path runs through
    ``asyncio.timeout`` (3.11+), so on a 3.10 box the network-level tests cannot
    execute at all — and an untestable extraction is one nobody notices breaking.
    Here the parsing rules are unit-testable on any interpreter.

    Returns a list so the caller can ``extend`` unconditionally: no instructions,
    a blank one, and a non-string all mean "this server said nothing", which is
    genuinely different from "this server sent an empty guide" — the latter would
    render an empty section in the system prompt.
    """
    if not isinstance(init_doc, dict):
        return []
    result = init_doc.get("result")
    text = result.get("instructions") if isinstance(result, dict) else None
    if isinstance(text, str) and text.strip():
        return [text]
    return []


async def _handshake(client, target, send_headers, *,
                     instructions_out=None) -> dict:
    """Streamable initialize → capture session-id → notifications/initialized.

    ``instructions_out`` (a list used as a box) collects the spec's optional
    ``result.instructions`` — "Instructions describing how to use the server and
    its features", which the MCP spec exists for clients to feed the model.
    Claude Code puts it in the system prompt; we used to drop the whole response
    body on the floor and keep only the session id. Ombre Brain ships a separate
    `CLAUDE_PROMPT.md` for exactly this purpose, and its user had nowhere to put
    it — the model then had to guess how to treat those memories, and guessed
    "someone else's".

    Raises the transport-detection signals ``_NotStreamableServer`` (a 4xx
    initialize — the MCP backwards-compat cue to try the SSE GET) and
    ``_LegacySseEndpoint`` (the reply is an event stream whose first event is
    ``endpoint``) so the caller can fall back to the legacy transport. Returns
    the session headers to carry on the subsequent call.
    """
    resp = await _post_bounded(client, target, send_headers, _initialize_payload(), {})
    if resp.status_code in _REDIRECT_STATUSES:
        raise ProbeError("protocol", "redirects not allowed")
    if resp.status_code >= 400:
        err = ProbeError(_classify_http(resp.status_code),
                         f"upstream HTTP {resp.status_code}")
        # A 4xx here is the MCP backwards-compat signal to try the legacy SSE
        # GET; 5xx is a genuine server failure, not a transport hint.
        if resp.status_code < 500:
            raise _NotStreamableServer(err)
        raise err
    ctype = resp.headers.get("content-type", "").lower()
    if "text/event-stream" in ctype:
        # The body was already bounded-read (stopping at the first complete SSE
        # event). Re-frame it: an ``endpoint`` first event means legacy.
        reader = _SseReader(_once_aiter(resp.content))
        init_doc = await reader.first_rpc_or_endpoint(1)  # may raise _LegacySseEndpoint
    else:
        init_doc = _parse_rpc_response(resp)  # validates the JSON-RPC envelope
    if "error" in init_doc:
        raise ProbeError("protocol", "JSON-RPC error")
    if instructions_out is not None:
        instructions_out.extend(_instructions_from_init_doc(init_doc))
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
    return session


def _content_text(content) -> str:
    """Flatten an MCP tool-result content array to text. Non-text blocks (image,
    resource) are noted but not inlined — the model gets the text parts."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type"):
            parts.append(f"[{block.get('type')} content omitted]")
    return "\n".join(p for p in parts if p)


def _wants_sse(mcp_transport) -> bool:
    return str(mcp_transport or "").strip().lower() == "sse"


@contextlib.asynccontextmanager
async def _sse_pinned_session(client, url, target, send_headers):
    """Drive ``mcp_probe._sse_session`` with the SSRF-pinned runtime transport.

    The GET stream and every endpoint POST connect to the single validated IP
    (``target.request_url``) while carrying the configured Host/SNI, so the
    server-controlled ``endpoint`` cannot trigger a second DNS lookup. The
    endpoint is already same-origin-checked against ``url`` by ``_sse_session``;
    each POST reuses the pinned host and only substitutes the endpoint's
    path/query, closing the DNS-rebinding gap for the message channel too.
    """
    def _stream_get():
        get_headers = {k: v for k, v in send_headers.items()
                       if k.lower() not in ("content-type", "host")}
        get_headers["Accept"] = "text/event-stream"
        get_headers["Host"] = target.host_header
        return client.stream(
            "GET", target.request_url, headers=get_headers,
            extensions={"sni_hostname": target.sni_hostname})

    async def _post(msg_url, payload, *, tolerate_4xx=False):
        parsed = httpx.URL(msg_url)
        msg_target = _PinnedTarget(
            request_url=target.request_url.copy_with(raw_path=parsed.raw_path),
            host_header=target.host_header,
            sni_hostname=target.sni_hostname,
        )
        resp = await _post_bounded(client, msg_target, send_headers, payload, {})
        if resp.status_code in _REDIRECT_STATUSES:
            raise ProbeError("protocol", "redirects not allowed")
        if resp.status_code >= 400 and not tolerate_4xx:
            raise ProbeError(_classify_http(resp.status_code),
                             f"upstream HTTP {resp.status_code}")

    try:
        async with _sse_session(stream_get=_stream_get, post=_post, url=url) as session:
            yield session
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        # The GET-stream connection (TLS handshake / TCP connect) is opened
        # inside _sse_session and, unlike the streamable path's _post_bounded,
        # is not otherwise mapped. Classify it exactly like _post_bounded so a
        # self-signed SSE server surfaces ProbeError(kind="tls") — the signal
        # load_turn_mcp needs to trigger the auto-CA fetch (POST-side errors
        # already arrive as ProbeError via _post_bounded and are not re-caught).
        raise _map_sse_connect_error(exc) from None


def _map_sse_connect_error(exc: Exception) -> ProbeError:
    if isinstance(exc, httpx.ConnectTimeout):
        return ProbeError("timeout", "connect timeout")
    if isinstance(exc, httpx.TimeoutException):
        return ProbeError("timeout", "request timeout")
    if _contains_tls_error(exc):
        return ProbeError("tls", "TLS connection failed")
    return ProbeError("transport", "connection failed")


async def _streamable_list(client, target, send_headers,
                           *, instructions_out=None) -> list[dict]:
    session = await _handshake(
        client, target, send_headers, instructions_out=instructions_out)
    resp = await _post_bounded(
        client, target, send_headers,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    _raise_for_status(resp)
    body = _parse_rpc_response(resp)
    if "error" in body:
        raise ProbeError("protocol", "JSON-RPC error")
    tools = (body.get("result") or {}).get("tools") or []
    return [t for t in tools if isinstance(t, dict) and t.get("name")]


async def _sse_list(client, url, target, send_headers) -> list[dict]:
    async with _sse_pinned_session(client, url, target, send_headers) as session:
        body = await session.request(2, "tools/list")
        if "error" in body:
            raise ProbeError("protocol", "JSON-RPC error")
        tools = (body.get("result") or {}).get("tools") or []
        return [t for t in tools if isinstance(t, dict) and t.get("name")]


async def _streamable_call(client, target, send_headers, name, arguments) -> dict:
    session = await _handshake(client, target, send_headers)
    resp = await _post_bounded(client, target, send_headers, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": str(name), "arguments": arguments or {}},
    }, session)
    _raise_for_status(resp)
    return _call_result(_parse_rpc_response(resp))


async def _sse_call(client, url, target, send_headers, name, arguments) -> dict:
    async with _sse_pinned_session(client, url, target, send_headers) as session:
        body = await session.request(3, "tools/call", {
            "name": str(name), "arguments": arguments or {}})
        return _call_result(body)


def _call_result(body: dict) -> dict:
    if "error" in body:
        return {"is_error": True, "text": json.dumps(body["error"])[:2000]}
    result = body.get("result") or {}
    return {
        "is_error": bool(result.get("isError")),
        "text": _content_text(result.get("content"))[:20000],
    }


async def _with_transport_fallback(mcp_transport, sse_attempt, streamable_attempt):
    """Run the persisted-transport attempt first and fall back on the OTHER
    transport's narrow "wrong transport" signal only. Mirrors
    ``mcp_probe._probe_async``'s dispatcher exactly.

    A ``ProbeError`` from a confirmed session (rpc error, cross-origin endpoint
    refusal) is never caught here, so it propagates untouched — no second
    attempt can mask it.
    """
    if _wants_sse(mcp_transport):
        try:
            return await sse_attempt()
        except _NotSseServer:
            pass
        try:
            return await streamable_attempt()
        except (_LegacySseEndpoint, _NotStreamableServer) as e:
            # Neither transport worked. A concrete streamable HTTP 4xx is more
            # diagnostic than a generic "no transport"; the bare endpoint
            # signature has none.
            if isinstance(e, _NotStreamableServer):
                raise e.err
            raise ProbeError("protocol", "no working MCP transport")
    try:
        return await streamable_attempt()
    except _LegacySseEndpoint:
        # Non-standard legacy server (Tencent): the streamable POST itself
        # answered with the endpoint signature. Fall to SSE.
        try:
            return await sse_attempt()
        except _NotSseServer:
            raise ProbeError("protocol", "no working MCP transport")
    except _NotStreamableServer as e:
        # Standard legacy server: the streamable POST 4xx'd. Try the SSE GET;
        # if that also says "not SSE", the 4xx is the real story.
        try:
            return await sse_attempt()
        except _NotSseServer:
            raise e.err


async def list_tools(url, headers, *, ca_pem=None, transport=None,
                     mcp_transport=None, instructions_out=None) -> list[dict]:
    """Return the server's full tool objects ``[{name, description, inputSchema}]``.

    ``transport`` is the httpx transport injection (tests). ``mcp_transport`` is
    the persisted MCP transport ("sse"/"http"/None) that picks which handshake
    to try first, with a narrow fallback to the other.

    ``instructions_out`` (optional list, used as a box) collects the server's
    spec-defined ``instructions`` string when there is one.
    ⚠️ Only the streamable transport fills it. The legacy SSE path keeps its own
    session helper and is not threaded — a server on that transport simply gets
    no instructions rather than a wrong one. Left explicit here instead of
    silently: if an SSE-only server ever needs it, this is the line to change.

    Raises ``ProbeError`` on SSRF refusal, transport error, HTTP error, or a
    JSON-RPC error response.
    """
    try:
        async with asyncio.timeout(mcp_probe._TOTAL_TIMEOUT):
            # DNS resolution is blocking in net_safety; keep it off the shared
            # worker loop and inside the same wall deadline as the exchange.
            target = await _pin_public_target_async(url)
            send_headers = _send_headers(headers)
            async with httpx.AsyncClient(**_client_kwargs(ca_pem, transport)) as client:
                return await _with_transport_fallback(
                    mcp_transport,
                    lambda: _sse_list(client, url, target, send_headers),
                    lambda: _streamable_list(
                        client, target, send_headers,
                        instructions_out=instructions_out),
                )
    except ProbeError:
        raise
    except TimeoutError:
        raise ProbeError("timeout", "operation timeout") from None


async def call_tool(url, headers, name, arguments, *, ca_pem=None, transport=None,
                    mcp_transport=None) -> dict:
    """Invoke ``name`` with ``arguments``. Returns ``{"is_error": bool, "text": str}``.

    ``mcp_transport`` selects the persisted MCP transport as in ``list_tools``.

    A JSON-RPC transport/HTTP failure raises ``ProbeError``; a tool-level failure
    (``result.isError`` or a JSON-RPC ``error`` body) is returned as
    ``is_error=True`` so the loop can hand it back to the model rather than abort
    the turn.
    """
    try:
        async with asyncio.timeout(mcp_probe._TOTAL_TIMEOUT):
            target = await _pin_public_target_async(url)
            send_headers = _send_headers(headers)
            async with httpx.AsyncClient(**_client_kwargs(ca_pem, transport)) as client:
                return await _with_transport_fallback(
                    mcp_transport,
                    lambda: _sse_call(client, url, target, send_headers, name, arguments),
                    lambda: _streamable_call(client, target, send_headers, name, arguments),
                )
    except ProbeError:
        raise
    except TimeoutError:
        raise ProbeError("timeout", "operation timeout") from None
