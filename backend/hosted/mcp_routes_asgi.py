"""ASGI surface for user MCP server config (spec 2026-07-08-user-mcp-servers).

Management endpoints (iOS control plane, api-key ONLY — ``require_api_key``
refuses runtime tokens) + the consumer-facing ``/v1/mcp/envelopes`` (api-key OR
a runtime token carrying the ``envelope_decrypt`` scope — the hosted consumer's
token has it). Everything delegates to ``hosted.mcp_core``; blocking work runs
via ``threadpool.run_db``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_api_key, require_scope
from hosted import mcp_core

router = APIRouter()


@router.get("/v1/mcp/servers")
async def mcp_list(auth: AuthResult = Depends(require_api_key)):
    body, status = await threadpool.run_db(mcp_core.list_servers, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/mcp/servers")
async def mcp_upsert(request: Request, auth: AuthResult = Depends(require_api_key)):
    payload = await asgi_http.read_json_silent(request)
    body, status = await threadpool.run_db(mcp_core.upsert_server, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.patch("/v1/mcp/servers/{name}")
async def mcp_patch(name: str, request: Request, auth: AuthResult = Depends(require_api_key)):
    payload = await asgi_http.read_json_silent(request)
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        mcp_core.set_enabled,
        auth.store,
        name,
        payload,
        caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.delete("/v1/mcp/servers/{name}")
async def mcp_delete(name: str, auth: AuthResult = Depends(require_api_key)):
    body, status = await threadpool.run_db(mcp_core.delete_server, auth.store, name)
    return JSONResponse(body, status_code=status)


@router.post("/v1/mcp/servers/{name}/test")
async def mcp_test(name: str, request: Request, auth: AuthResult = Depends(require_api_key)):
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        mcp_core.test_server, auth.store, name, caller_api_key)
    return JSONResponse(body, status_code=status)


@router.get("/v1/mcp/envelopes")
async def mcp_envelopes(auth: AuthResult = Depends(require_scope("envelope_decrypt"))):
    body, status = await threadpool.run_db(mcp_core.envelopes_payload, auth.store)
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
