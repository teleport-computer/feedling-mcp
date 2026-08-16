"""Native ASGI identity surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/identity/*`` routes: each requires an authenticated user
(``Depends(require_auth)`` — the ASGI equivalent of ``auth.require_user()``) and
delegates to the framework-neutral ``identity.identity_core`` so the response
bodies are byte-identical to Flask's.

Auth/scope: only ``/v1/identity/actions`` gates on ``runtime_auth.authorize_scope
("identity")`` in Flask, so ONLY that route carries ``Depends(require_scope
("identity"))`` here. The other six routes (get/verify/changes/init/replace/
relationship_anchor) gate on ``auth.require_user()`` alone — adding a scope there
would diverge from the Flask surface.

E2E boundary: identity cards are v1 E2E envelopes, never decrypted server-side.
``actions`` forwards the caller's credential to the enclave exactly as Flask does
— api key from the resolved ``AuthResult`` (the same value ``auth._extract_api_key
()`` returns on the api-key path) and the raw ``X-Feedling-Runtime-Token`` header
(``or ""`` to match Flask's ``extract_runtime_token() or ""``). ``init``/``replace``
persist the ciphertext envelope; the plaintext ``identity`` init path builds one
via the same ``core.envelope`` path Flask uses. The server never sees plaintext.

All store / enclave / envelope-build work is blocking, so it runs off the event
loop via ``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth, require_scope
from identity import identity_core

router = APIRouter()


@router.get("/v1/identity/get")
async def identity_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(identity_core.get_identity, auth.store)
    return JSONResponse(body, status_code=status)


@router.get("/v1/identity/verify")
async def identity_verify(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(identity_core.verify_identity, auth.store)
    return JSONResponse(body, status_code=status)


@router.get("/v1/identity/changes")
async def identity_changes(request: Request, auth: AuthResult = Depends(require_auth)):
    """List content-free identity mutations, newest first.

    Newly written records include ``fields`` with canonical changed-field
    labels. Legacy records may omit it and clients must tolerate that shape.
    """
    body, status = await threadpool.run_db(
        identity_core.list_changes,
        auth.store,
        limit_raw=request.query_params.get("limit", 50),
        since=request.query_params.get("since", ""),
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/identity/init")
async def identity_init(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(identity_core.init_identity, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/identity/replace")
async def identity_replace(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(identity_core.replace_identity, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/identity/relationship_anchor")
async def identity_relationship_anchor(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        identity_core.update_relationship_anchor, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/identity/actions")
async def identity_actions(request: Request, auth: AuthResult = Depends(require_scope("identity"))):
    payload = (await asgi_http.read_json_silent(request)) or {}
    runtime_token = auth_core.extract_runtime_token(request.headers)
    body, status = await threadpool.run_db(
        identity_core.run_actions,
        auth.store,
        payload,
        api_key=auth.api_key,
        runtime_token=runtime_token or "",
    )
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
