"""Native ASGI Extended Perception surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/perception/*`` routes: each requires an authenticated
user (``Depends(require_auth)`` — the ASGI equivalent of ``auth.require_user()``)
and delegates to the framework-neutral ``perception.perception_read_core`` so the
response bodies are byte-identical to Flask's.

Auth/scope: read routes accept either authenticated credential. ``/report`` is
API-key-only because the sensitive-signal decrypt adapter forwards API-key
material and cannot safely complete the same flow with a runtime token alone.

E2E boundary: perception signals/photos are v1 E2E envelopes, never decrypted in
this process except via the enclave. ``/report`` writes encrypted perception and,
on the ingress-v2 path, forwards the caller's api key to the enclave (which owns
decryption) exactly as Flask did — matching the worldbook/screen credential
pattern (``auth.api_key`` is the same value the api-key path extracts). The photo
routes store/point at ciphertext only and make no enclave call here; pixels are
decrypted later by the enclave via the ``decrypt_path`` returned by
``/photo/<id>/content``.

All store / service / enclave work is blocking, so it runs off the event loop via
``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_api_key, require_auth
from perception import perception_read_core

router = APIRouter()


@router.post("/v1/perception/report")
async def report(request: Request, auth: AuthResult = Depends(require_api_key)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        perception_read_core.report,
        auth.store,
        payload,
        api_key=auth.api_key,
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/perception/snapshot")
async def snapshot(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(perception_read_core.snapshot, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/perception/photo/evaluate")
async def photo_evaluate(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        perception_read_core.photo_evaluate, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.get("/v1/perception/photos")
async def photos_recent(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        perception_read_core.photos_recent, auth.store, request.query_params.get("limit"))
    return JSONResponse(body, status_code=status)


@router.get("/v1/perception/photo/{photo_id}/content")
async def photo_content(photo_id: str, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        perception_read_core.photo_content, auth.store, photo_id)
    return JSONResponse(body, status_code=status)


@router.get("/v1/perception/items/{kind}")
async def items_recent(kind: str, request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        perception_read_core.items_recent, auth.store, kind, request.query_params.get("limit"))
    return JSONResponse(body, status_code=status)


@router.get("/v1/perception/app_open")
async def app_open(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        perception_read_core.app_open, auth.store, request.query_params)
    return JSONResponse(body, status_code=status)


@router.get("/v1/perception/app_close")
async def app_close(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        perception_read_core.app_close, auth.store, request.query_params)
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
