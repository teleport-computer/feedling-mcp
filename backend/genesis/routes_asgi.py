"""Native ASGI genesis import surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/genesis/*`` routes: each requires an authenticated user
(``Depends(require_auth)`` — the ASGI equivalent of ``auth.require_user()``) and
delegates to the framework-neutral ``genesis.genesis_core`` so the response
bodies are byte-identical to Flask's.

Auth/scope: only ``/v1/genesis/imports/<job_id>/outputs`` and
``/v1/genesis/persona_backfill`` gate on ``runtime_auth.authorize_scope("genesis")``
in Flask, so ONLY those two carry ``Depends(require_scope("genesis"))`` here. The
other six routes gate on ``auth.require_user()`` alone — adding a scope there would
diverge from the Flask surface.

E2E boundary (unchanged): genesis chunks are v1 E2E ciphertext; the server never
decrypts. ``finalize`` / ``outputs`` / ``persona_backfill`` forward the caller's
credential (api key from the resolved ``AuthResult`` + the runtime token) to the
enclave-owned apply/backfill exactly as Flask does. ``/plaintext`` is the
plaintext-compat ingest path — it ENQUEUES a background distill job via the SAME
mechanism Flask uses (the routes-resident ``_start_plaintext_genesis_job`` daemon
thread, injected into the core); the heavy import never runs inline on the event
loop. All store / enclave / enqueue work is blocking, so it runs off the event
loop via ``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth, require_scope
from genesis import genesis_core
from genesis import plaintext as genesis_flask_routes

router = APIRouter()


@router.post("/v1/genesis/imports")
async def genesis_import_create(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(genesis_core.create_import, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/genesis/imports/plaintext")
async def genesis_import_plaintext(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        genesis_core.plaintext_import,
        auth.store,
        payload,
        api_key=auth.api_key,
        # Inject the routes-resident helpers so the enqueue mechanism
        # (``_start_plaintext_genesis_job`` — a daemon thread) is the SAME as
        # Flask and stays monkeypatchable via ``routes._…``.
        prepare=genesis_flask_routes._prepare_plaintext_import,
        find_reusable=genesis_flask_routes._find_reusable_plaintext_job,
        plaintext_mode=genesis_flask_routes._plaintext_mode,
        job_metadata=genesis_flask_routes._plaintext_job_metadata,
        start_job=genesis_flask_routes._start_plaintext_genesis_job,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/genesis/imports/plaintext/estimate")
async def genesis_import_plaintext_estimate(
    request: Request, auth: AuthResult = Depends(require_auth)
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        genesis_core.plaintext_estimate,
        auth.store,
        payload,
        api_key=auth.api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/genesis/imports/plaintext/commit")
async def genesis_import_plaintext_commit(
    request: Request, auth: AuthResult = Depends(require_auth)
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        genesis_core.plaintext_commit,
        auth.store,
        payload,
        api_key=auth.api_key,
        prepare=genesis_flask_routes._prepare_plaintext_import,
        find_reusable=genesis_flask_routes._find_reusable_plaintext_job,
        plaintext_mode=genesis_flask_routes._plaintext_mode,
        job_metadata=genesis_flask_routes._plaintext_job_metadata,
        start_job=genesis_flask_routes._start_plaintext_genesis_job,
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/genesis/imports")
async def genesis_import_list(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        genesis_core.list_imports, auth.store, limit_raw=request.query_params.get("limit"))
    return JSONResponse(body, status_code=status)


@router.put("/v1/genesis/imports/{job_id}/chunks/{seq}")
async def genesis_import_put_chunk(
    job_id: str, seq: int, request: Request, auth: AuthResult = Depends(require_auth)
):
    is_json = asgi_http._is_json_content_type(request.headers.get("content-type", ""))
    raw_body = b""
    json_body = None
    if is_json:
        # Mirror Flask ``request.get_json(silent=True)`` — the core applies ``or {}``.
        body_bytes = await request.body()
        try:
            json_body = json.loads(body_bytes) if body_bytes else None
        except (ValueError, TypeError):
            json_body = None
    else:
        raw_body = await request.body()
    body, status = await threadpool.run_db(
        genesis_core.put_chunk,
        auth.store,
        job_id,
        seq,
        is_json=is_json,
        json_body=json_body,
        raw_body=raw_body,
        headers=request.headers,
        query=request.query_params,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/genesis/imports/{job_id}/finalize")
async def genesis_import_finalize(
    job_id: str, request: Request, auth: AuthResult = Depends(require_auth)
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        genesis_core.finalize, auth.store, job_id, payload, api_key=auth.api_key)
    return JSONResponse(body, status_code=status)


@router.post("/v1/genesis/imports/{job_id}/outputs")
async def genesis_import_apply_outputs(
    job_id: str, request: Request, auth: AuthResult = Depends(require_scope("genesis"))
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    runtime_token = auth_core.extract_runtime_token(request.headers) or ""
    body, status = await threadpool.run_db(
        genesis_core.apply_outputs,
        auth.store,
        job_id,
        payload,
        api_key=auth.api_key,
        runtime_token=runtime_token,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/genesis/persona_backfill")
async def genesis_persona_backfill(
    request: Request, auth: AuthResult = Depends(require_scope("genesis"))
):
    # Flask reads the header directly (``.get(name, "")``), NOT extract_runtime_token.
    runtime_token = request.headers.get("X-Feedling-Runtime-Token", "")
    body, status = await threadpool.run_db(
        genesis_core.persona_backfill,
        auth.store,
        api_key=auth.api_key,
        runtime_token=runtime_token,
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/genesis/imports/{job_id}")
async def genesis_import_status(
    job_id: str, request: Request, auth: AuthResult = Depends(require_auth)
):
    body, status = await threadpool.run_db(
        genesis_core.get_import_status,
        auth.store,
        job_id,
        include_missing_raw=request.query_params.get("include_missing"),
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/genesis/resident/pending")
async def genesis_resident_pending(request: Request, auth: AuthResult = Depends(require_auth)):
    # Per-user: the resident consumer authenticates as its own user (same credential it uses
    # for chat poll) and only ever claims that user's awaiting_resident jobs.
    body, status = await threadpool.run_db(
        genesis_core.resident_pending,
        auth.store,
        consumer_id=request.query_params.get("consumer_id") or "",
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/genesis/resident/{job_id}/heartbeat")
async def genesis_resident_heartbeat(
    job_id: str, request: Request, auth: AuthResult = Depends(require_auth)
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        genesis_core.resident_heartbeat,
        auth.store,
        job_id,
        consumer_id=str(payload.get("consumer_id") or ""),
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/genesis/resident/{job_id}/complete")
async def genesis_resident_complete(
    job_id: str, request: Request, auth: AuthResult = Depends(require_auth)
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        genesis_core.resident_complete, auth.store, job_id, payload
    )
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
