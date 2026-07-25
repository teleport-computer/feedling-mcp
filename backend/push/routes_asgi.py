"""Native ASGI /v1/push/* routes (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``push`` blueprint one-for-one: every route requires an
authenticated user (``Depends(require_auth)`` — the same gate as the Flask
``auth.require_user()`` call; none of these routes enforce a runtime-token
scope), and the response body is built by the framework-neutral
``push.push_core`` so it is byte-for-byte identical to Flask's.

The POST bodies are decoded with the same tolerance as Flask's
``request.get_json(silent=True) or {}`` (a malformed/empty body degrades to an
empty dict, never a 400). The core work — token DB writes and outbound APNs
HTTP — is blocking, so it runs through ``threadpool.run_db`` off the event loop
(plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth, require_scope
from asgi import http as asgi_http
from push import push_core

router = APIRouter()


async def _json_body(request: Request):
    # Flask's ``request.get_json(silent=True) or {}`` incl. the content-type gate
    # (asgi.http.read_json_silent): non-JSON content-type -> {}; truthy non-dict
    # passes through unchanged.
    return (await asgi_http.read_json_silent(request)) or {}


@router.get("/v1/push/tokens")
async def list_tokens(request: Request, auth: AuthResult = Depends(require_auth)):
    active_only = request.query_params.get("active_only", "false").lower() == "true"
    return await threadpool.run_db(push_core.list_tokens, auth.store, active_only=active_only)


@router.post("/v1/push/register-token")
async def register_token(request: Request, auth: AuthResult = Depends(require_auth)):
    body = await _json_body(request)
    return await threadpool.run_db(push_core.register_token, auth.store, payload=body)


@router.post("/v1/push/notification")
async def notification(request: Request, auth: AuthResult = Depends(require_auth)):
    body = await _json_body(request)
    return await threadpool.run_db(push_core.notification, auth.store, payload=body)


@router.post("/v1/push/dynamic-island")
async def dynamic_island(request: Request, auth: AuthResult = Depends(require_auth)):
    body = await _json_body(request)
    return await threadpool.run_db(push_core.dynamic_island, auth.store, payload=body)


@router.post("/v1/push/live-activity")
async def live_activity(request: Request, auth: AuthResult = Depends(require_auth)):
    body = await _json_body(request)
    return await threadpool.run_db(push_core.live_activity_update, auth.store, payload=body)


@router.post("/v1/push/live-start")
async def live_start(request: Request, auth: AuthResult = Depends(require_auth)):
    body = await _json_body(request)
    return await threadpool.run_db(push_core.live_start, auth.store, payload=body)


@router.post("/v1/internal/push/ai_reply")
async def internal_ai_reply_push(
    request: Request, auth: AuthResult = Depends(require_scope("chat_push"))
):
    """Runtime-internal: V2 serve-worker 把一条已落库回复的明文正文交给 backend
    发推送。不是产品 API（已排除出公开 OpenAPI 契约）。"""
    body = await _json_body(request)
    return await threadpool.run_db(push_core.ai_reply_push, auth.store, payload=body)


def register_asgi(app) -> None:
    app.include_router(router)
