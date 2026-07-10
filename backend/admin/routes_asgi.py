"""Native ASGI admin data-track routes (ASGI-migration plan §5.3).

Mirrors the Flask ``admin.data_track`` blueprint — admin-token gated
(``FEEDLING_ADMIN_TOKEN``), NOT user auth. Two routes render HTML pages
(``GET /admin/data-track`` + ``GET /admin/data-track/users/{user_id}``); the
five ``/v1/admin/...`` routes return JSON. The admin check replicates
``admin.data_track.require_admin`` as an ``HTTPException`` so the registered
exception handler renders the identical fixed 401/503 bodies
(``asgi.responses.ERROR_BODIES``); a 401 therefore returns JSON on the HTML
routes too, exactly as Flask's ``errorhandler(401)`` does.

Each handler's body is produced by the same ``admin.data_track`` functions the
Flask routes call — via ``admin.admin_core``, which runs them inside a throwaway
Flask request context so ``request.args`` is read from the ASGI query string —
so the output is byte-for-byte the Flask output. All of that is blocking sync
``db.py`` work, so it runs through ``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from admin import admin_core
from admin import tee_replication as admin_tee_replication
from asgi import threadpool
from asgi.http import read_json_silent

router = APIRouter()


def _extract_admin_token(request: Request) -> str:
    # Mirror admin.data_track._extract_admin_token (header, bearer, then query).
    key = (request.headers.get("X-Admin-Token") or "").strip()
    if key:
        return key
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("admin_key") or "").strip()


def _require_admin(request: Request) -> None:
    # Mirror admin.data_track.require_admin: 503 when unconfigured, 401 on
    # missing/mismatched token. The exception handler maps these to the same
    # fixed bodies Flask's errorhandler(401/503) returns.
    configured = os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip()
    if not configured:
        raise HTTPException(status_code=503)
    supplied = _extract_admin_token(request)
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=401)


@router.get("/v1/admin/data-track/summary")
async def data_track_summary(request: Request):
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.summary_payload, request.url.query)
    return JSONResponse(payload)


@router.get("/v1/admin/data-track/users")
async def data_track_users(request: Request):
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.users_payload, request.url.query)
    return JSONResponse(payload)


@router.get("/v1/admin/data-track/dau")
async def data_track_dau(request: Request):
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.dau_payload, request.url.query)
    return JSONResponse(payload)


@router.get("/v1/admin/data-track/debug")
async def data_track_debug(request: Request):
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.debug_payload, request.url.query)
    return JSONResponse(payload)


@router.get("/v1/admin/data-track/users/{user_id}")
async def data_track_user(user_id: str, request: Request):
    _require_admin(request)
    body, status = await threadpool.run_db(admin_core.user_payload, request.url.query, user_id)
    return JSONResponse(body, status_code=status)


@router.get("/admin/data-track")
async def data_track_page(request: Request):
    _require_admin(request)
    html = await threadpool.run_db(admin_core.page_html, request.url.query)
    return HTMLResponse(html)


@router.get("/admin/data-track/users/{user_id}")
async def data_track_user_page(user_id: str, request: Request):
    _require_admin(request)
    kind, body, status = await threadpool.run_db(admin_core.user_page, request.url.query, user_id)
    if kind == "text":
        return PlainTextResponse(body, status_code=status)
    return HTMLResponse(body, status_code=status)


@router.post("/v1/admin/store/evict")
async def store_evict(request: Request):
    _require_admin(request)
    payload = (await read_json_silent(request)) or {}
    user_id = str(payload.get("user_id") or request.query_params.get("user_id") or "").strip()
    if not user_id:
        return JSONResponse({"error": "user_id required"}, status_code=400)
    result = await threadpool.run_db(admin_core.store_evict, user_id)
    return JSONResponse(result)


@router.post("/v1/admin/tee-replication/run")
async def tee_replication_run(request: Request):
    # Deliberately synchronous: a real (non-dry-run) replicate/reconcile pass
    # occupies one anyio worker thread — and holds the module-level run lock —
    # for its whole duration (minutes at the default qps=2). Acceptable at the
    # current tiny prod scale; revisit (background job + status polling) if
    # the user count grows enough for a pass to outlive the HTTP timeout.
    _require_admin(request)
    payload = (await read_json_silent(request)) or {}
    try:
        result = await threadpool.run_db(
            admin_tee_replication.run_action,
            action=payload.get("action"),
            table=payload.get("table"),
            dry_run=payload.get("dry_run", True),
            confirm=payload.get("confirm"),
            qps=payload.get("qps"),
            sample_rate=payload.get("sample_rate"),
        )
    except admin_tee_replication.BadRequest as exc:
        return JSONResponse({"error": exc.error}, status_code=400)
    except admin_tee_replication.AlreadyRunning:
        return JSONResponse({"error": "already_running"}, status_code=409)
    except admin_tee_replication.Unconfigured:
        return JSONResponse({"error": "tee_database_unconfigured"}, status_code=503)
    return JSONResponse(result)


@router.get("/v1/admin/tee-replication/status")
async def tee_replication_status(request: Request):
    _require_admin(request)
    try:
        payload = await threadpool.run_db(admin_tee_replication.status_payload)
    except admin_tee_replication.Unconfigured:
        return JSONResponse({"error": "tee_database_unconfigured"}, status_code=503)
    return JSONResponse(payload)


def register_asgi(app) -> None:
    app.include_router(router)
