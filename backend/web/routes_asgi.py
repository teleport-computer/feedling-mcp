"""Web-capability settings routes.

Its own package rather than living under ``chat`` — the toggle is not part of
the chat message protocol; it only ended up next to chat while the runtime
wiring was being written.

api-key ONLY, both verbs: ``enabled`` is the user's own preference, so a hosted
consumer holding a short-lived runtime token must not be able to read or rewrite
it. ``require_auth`` would accept runtime tokens with no scope at all, and
"the model cannot currently obtain a token" is not a security boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_api_key, require_scope
from web import execution_core, settings_core

router = APIRouter()


@router.get("/v1/web/settings")
async def web_settings_get(auth: AuthResult = Depends(require_api_key)):
    body = await threadpool.run_db(settings_core.get_settings, auth.store)
    return JSONResponse(body)


@router.post("/v1/web/settings")
async def web_settings_post(
    request: Request, auth: AuthResult = Depends(require_api_key)
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    try:
        body = await threadpool.run_db(
            settings_core.update_settings, auth.store, payload
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(body)


@router.post("/v1/agent/web/search")
async def web_search_exec(
    request: Request, auth: AuthResult = Depends(require_scope("web"))
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    result = await threadpool.run_db(execution_core.run_search, auth.store, payload)
    return JSONResponse(result.to_dict())


@router.post("/v1/agent/web/fetch")
async def web_fetch_exec(
    request: Request, auth: AuthResult = Depends(require_scope("web"))
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    result = await threadpool.run_db(execution_core.run_fetch, auth.store, payload)
    return JSONResponse(result.to_dict())


def register_asgi(app) -> None:
    app.include_router(router)
