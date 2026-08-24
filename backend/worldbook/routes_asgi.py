"""Native ASGI world book surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/worldbook/*`` routes: each requires an authenticated
user (``Depends(require_auth)`` — the ASGI equivalent of ``auth.require_user()``)
and delegates to the framework-neutral ``worldbook.worldbook_core`` so the
response bodies are byte-identical to Flask's.

Auth/scope: the Flask routes gate on ``auth.require_user()`` only — none call
``runtime_auth.authorize_scope(...)`` — so there is deliberately NO
``require_scope`` here; adding one would diverge from the Flask surface.

E2E boundary: ``content`` fields are v1 E2E envelopes, never decrypted
server-side. ``upsert``/``match`` forward the caller's credential to the enclave
exactly as Flask does — api key from the resolved ``AuthResult`` (the same value
``auth._extract_api_key()`` returns on the api-key path) and the raw
``X-Feedling-Runtime-Token`` header (which takes precedence in the enclave call,
so it is forwarded even when the token was not the auth path — matching Flask).

All store / enclave work is blocking, so it runs off the event loop via
``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth
from asgi import http as asgi_http
from worldbook import worldbook_core

router = APIRouter()


@router.get("/v1/worldbook/list")
async def worldbook_list(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(worldbook_core.list_envelopes, auth.store)
    return JSONResponse(body, status_code=status)


@router.post(
    "/v1/worldbook/upsert",
    responses={500: {"description": "World Book storage failure"}},
)
async def worldbook_upsert(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    runtime_token = auth_core.extract_runtime_token(request.headers)
    body, status = await threadpool.run_db(
        worldbook_core.upsert,
        auth.store,
        payload,
        api_key=auth.api_key,
        runtime_token=runtime_token,
        trace_id=str(request.headers.get("X-Feedling-Trace-Id") or ""),
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/worldbook/match")
async def worldbook_match(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    runtime_token = auth_core.extract_runtime_token(request.headers)
    body, status = await threadpool.run_db(
        worldbook_core.match,
        auth.store,
        payload,
        api_key=auth.api_key,
        runtime_token=runtime_token,
        trace_id=str(request.headers.get("X-Feedling-Trace-Id") or ""),
        lane="api",
    )
    return JSONResponse(body, status_code=status)


@router.delete(
    "/v1/worldbook/delete",
    responses={
        404: {"description": "World Book entry not found"},
        500: {"description": "World Book storage failure"},
    },
)
async def worldbook_delete(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        worldbook_core.delete, auth.store, request.query_params.get("id"))
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
