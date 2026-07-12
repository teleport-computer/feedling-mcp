"""Native ASGI chat long-poll (ASGI-migration plan §9.1) — the migration payoff.

The Flask route parks a gunicorn thread on a ``threading.Event`` for up to 30s;
this route parks an ``asyncio`` future in ``runtime.waiters`` instead, so N idle
polls cost N futures, not N OS threads. The pending-check / claim / response
shape all come from the framework-neutral ``chat.poll_core`` (identical payload
to Flask); only the wait primitive changes.

DB rule (plan §5.5): no connection is held while waiting — each pending check is
a short ``run_db`` hop, then the coroutine parks holding nothing.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth
from asgi.settings import settings
from bootstrap import gates as boot_gates
from chat import chat_core
from chat import consumer as chat_consumer
from chat import poll_core as chat_poll_core
from chat import service as chat_service
from runtime.waiters import registry

router = APIRouter()


@router.get("/v1/chat/poll")
async def chat_poll(request: Request, auth: AuthResult = Depends(require_auth)):
    store = auth.store
    # Read consumer identity from the ASGI request ON the loop (cheap), then do
    # the DB write + context build OFF the loop.
    remote_addr = request.client.host if request.client else ""
    consumer_info = chat_consumer._consumer_headers_from_map(request.headers, remote_addr)
    await threadpool.run_db(chat_consumer._record_consumer_event, store, "poll", info=consumer_info)
    context = await threadpool.run_db(chat_poll_core.poll_context, store)

    try:
        since = float(request.query_params.get("since", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid since"}, status_code=400)
    # Match the Flask route's clamp exactly (no lower bound; garbage -> 500).
    timeout = min(float(request.query_params.get("timeout", 30)), 60)
    consumer_id = chat_service._parse_consumer_id(request.headers, request.query_params)
    claim = chat_service._parse_bool_arg(request.query_params, "claim", default=True)

    async def _check():
        return await threadpool.run_db(
            chat_poll_core.pending_messages,
            store,
            since=since,
            consumer_id=consumer_id,
            claim=claim,
        )

    def _response(messages, timed_out, ctx):
        return chat_poll_core.build_response(
            messages=messages,
            context=ctx,
            consumer_id=consumer_id,
            claim=claim,
            timed_out=timed_out,
        )

    waiter = registry.register(
        "chat", store.user_id, per_user_max=settings.poller_max_per_user_chat
    )
    if waiter is None:
        # Cap hit — shed to an immediate timed-out response; consumer re-polls.
        return _response([], timed_out=True, ctx=context)
    try:
        # Check AFTER registering the waiter. asyncio.Event.set() latches, so a
        # write that wakes in the gap between check and park is NOT lost: if it
        # lands before this check we return its message; if it lands between the
        # check and wait(), the event is already set and wait() returns at once,
        # then the re-check below delivers it. (Checking before registering — the
        # old order — forfeited that latch and could park for the full timeout.)
        pending = await _check()
        if pending:
            # Immediate delivery before parking: the pre-park context is still
            # the freshest snapshot, so reuse it (no extra DB read).
            return _response(pending, timed_out=False, ctx=context)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=max(0.0, timeout))
            notified = True
        except asyncio.TimeoutError:
            notified = False
    finally:
        # Always unregister — a cancelled poll (client disconnect) must not leak
        # a waiter (plan §14.6).
        registry.unregister(waiter)

    # Refresh the context after the wait: config changes that landed while we
    # were parked (a moved user_mcp fingerprint etc.) must be reflected in this
    # response rather than the stale pre-park snapshot.
    fresh = await threadpool.run_db(chat_poll_core.poll_context, store)
    if notified:
        return _response(await _check(), timed_out=False, ctx=fresh)
    return _response([], timed_out=True, ctx=fresh)


# --------------------------------------------------------------------------- #
# Remaining chat routes (append migration): message / response / history /
# clear / message-body / verify_loop. Each delegates to the framework-neutral
# ``chat.chat_core`` off the event loop via ``run_db`` (plan §5.2) so the
# envelope validation, append/claim, wakes (notify_chat_waiters / wake_bus) and
# debug_trace events are byte-identical to Flask. All use ``require_auth`` (the
# ASGI equivalent of ``auth.require_user()``) — none of these six carry a scope,
# matching the Flask surface. E2E: envelopes are opaque, never decrypted here.
# --------------------------------------------------------------------------- #


@router.post("/v1/chat/message")
async def chat_message(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(chat_core.write_message, auth.store, payload)
    return JSONResponse(body, status_code=status)


def _allow_verify_reply_with_fresh_pending_check(store) -> bool:
    """Check pending verify ping, reloading once before a negative decision.

    In multi-worker deployments, verify_loop inserts a synthetic ping in one
    worker and the resident consumer may answer through another before
    LISTEN/NOTIFY has evicted that second worker's cached store. The fast path
    stays cache-only; the negative path reloads once so we do not reject a valid
    verify reply based on stale chat_messages.
    """
    if boot_gates._reply_is_for_pending_verify_ping(store):
        return True
    if boot_gates._chat_loop_verified_by_server(store):
        return False
    store.reload()
    return boot_gates._reply_is_for_pending_verify_ping(store)


@router.post("/v1/chat/response")
async def chat_response(request: Request, auth: AuthResult = Depends(require_auth)):
    store = auth.store
    payload = (await asgi_http.read_json_silent(request)) or {}
    # Consumer identity read on the loop (cheap), used off the loop.
    remote_addr = request.client.host if request.client else ""
    consumer_info = chat_consumer._consumer_headers_from_map(request.headers, remote_addr)
    consumer_id = chat_service._parse_consumer_id(request.headers, request.query_params)
    allow_verify_reply = await threadpool.run_db(
        _allow_verify_reply_with_fresh_pending_check, store
    )
    gated = await threadpool.run_db(
        chat_core.gate_response_dict, store, allow_verify_reply
    )
    if gated is not None:
        await threadpool.run_db(chat_core.trace_response_gated, store, payload, allow_verify_reply)
        body, status = gated
        return JSONResponse(body, status_code=status)
    body, status = await threadpool.run_db(
        chat_core.write_response,
        store,
        payload,
        consumer_id=consumer_id,
        consumer_info=consumer_info,
        allow_verify_reply=allow_verify_reply,
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/chat/history")
async def chat_history(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        chat_core.history,
        auth.store,
        query=dict(request.query_params),
        user_agent=request.headers.get("User-Agent", ""),
        remote_addr=request.client.host if request.client else "",
    )
    return JSONResponse(body, status_code=status)


@router.delete("/v1/chat/history")
async def chat_history_clear(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(chat_core.clear_history, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.get("/v1/chat/messages/{message_id}/body")
async def chat_message_body(message_id: str, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(chat_core.message_body, auth.store, message_id)
    return JSONResponse(body, status_code=status)


@router.post("/v1/chat/verify_loop")
async def chat_verify_loop(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(chat_core.verify_loop, auth.store, payload)
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
