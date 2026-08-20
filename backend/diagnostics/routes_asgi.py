"""Native ASGI diagnostics routes (ASGI-migration plan §5.3).

Mirrors the Flask ``diagnostics`` blueprint:
  - ``GET/DELETE /v1/debug/trace`` + ``POST /v1/debug/trace/enable`` — user auth
    (``Depends(require_auth)``, same as Flask ``auth.require_user()``).
  - ``POST /v1/diagnostics/logs`` — user auth; multipart upload → R2/Postgres.
  - ``GET /v1/admin/diagnostics/logs/{user_id}`` — admin-token gated
    (``FEEDLING_ADMIN_TOKEN``), replicating ``admin.data_track.require_admin`` as
    an ``HTTPException`` so the registered exception handler renders the identical
    fixed 401/503 bodies (``asgi.responses.ERROR_BODIES``).

Every payload is built by the framework-neutral ``diagnostics.diagnostics_core``
(byte-for-byte the Flask output). All of those cores touch blocking sync
``db.py`` / boto3 R2, so they run through ``threadpool.run_db`` off the event
loop (plan §5.2). The 400/413 upload-validation bodies are not in
``ERROR_BODIES``, so they are returned as explicit ``JSONResponse`` (verbatim).
"""

from __future__ import annotations

import hmac
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth
from diagnostics import diagnostics_core

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


def _content_length(request: Request) -> int | None:
    """Parse the Content-Length header to int|None (Flask ``request.content_length``)."""
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _read_json_silent(request: Request):
    """Mirror Flask ``request.get_json(silent=True)``: parse the JSON body, or
    return None when the content-type isn't JSON, the body is empty, or parsing
    fails — so ``(... or {})`` in the route matches the Flask route exactly."""
    ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if not (ct == "application/json" or ct.endswith("+json")):
        return None
    raw = await request.body()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


@router.post("/v1/diagnostics/logs")
async def upload_logs(request: Request, auth: AuthResult = Depends(require_auth)):
    content_length = _content_length(request)
    # Reject oversized bodies from Content-Length *before* parsing the multipart
    # body (parity with the Flask guard order).
    if diagnostics_core.is_oversized(content_length):
        file_present, file_bytes, meta = False, b"", {}
    else:
        form = await request.form()
        upload = form.get("file")
        file_present = isinstance(upload, UploadFile)
        file_bytes = await upload.read(diagnostics_core._MAX_BYTES + 1) if file_present else b""
        raw_meta = form.get("meta")
        meta = diagnostics_core.parse_meta(raw_meta if isinstance(raw_meta, str) else None)

    body, status = await threadpool.run_db(
        diagnostics_core.upload_logs_payload,
        auth.store,
        content_length=content_length,
        file_present=file_present,
        file_bytes=file_bytes,
        meta=meta,
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/admin/diagnostics/logs/{user_id}")
async def admin_read_logs(user_id: str, request: Request):
    _require_admin(request)
    body, status = await threadpool.run_db(diagnostics_core.admin_read_logs_payload, user_id)
    return JSONResponse(body, status_code=status)


@router.get("/v1/debug/trace")
async def debug_trace_read(request: Request, auth: AuthResult = Depends(require_auth)):
    limit = diagnostics_core.coerce_limit(request.query_params.get("limit"))
    subsystem = str(request.query_params.get("subsystem") or "")
    body, status = await threadpool.run_db(
        diagnostics_core.read_trace_payload, auth.store, limit=limit, subsystem=subsystem
    )
    return JSONResponse(body, status_code=status)


@router.post(
    "/v1/debug/trace/enable",
    responses={500: {"description": "Trace preference could not be persisted"}},
)
async def debug_trace_enable(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await _read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        diagnostics_core.set_trace_enabled_payload, auth.store, payload.get("enabled")
    )
    return JSONResponse(body, status_code=status)


@router.delete("/v1/debug/trace")
async def debug_trace_clear(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(diagnostics_core.clear_trace_payload, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/debug/trace/event")
async def debug_trace_emit(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await _read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        diagnostics_core.emit_trace_event_payload, auth.store, payload
    )
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
