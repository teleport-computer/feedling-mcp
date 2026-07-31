"""Native ASGI memory surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/memory/*`` routes: each requires an authenticated user
(``Depends(require_auth)`` — the ASGI equivalent of ``auth.require_user()``) and
delegates to the framework-neutral ``memory.memory_core`` so the response bodies
are byte-identical to Flask's.

Auth/scope: the Flask routes gate three write surfaces on
``runtime_auth.authorize_scope("memory")`` — ``/actions``, ``/legacy_batch`` and
the **POST** side of ``/migration_state`` — so those carry
``Depends(require_scope("memory"))`` here. The GET side of ``/migration_state``
and every read route gate on auth only, matching Flask exactly.

E2E boundary: ``body_ct`` fields are v1 E2E envelopes, never decrypted
server-side. The readside (index/fetch/buckets/threads) and the migration
decrypt (legacy_batch) forward the caller's credential to the enclave exactly as
Flask does — api key from the resolved ``AuthResult`` (the same value
``auth._extract_api_key()`` returns on the api-key path) and the raw
``X-Feedling-Runtime-Token`` header (which the enclave prefers when present, so
it is forwarded even when the token was not the auth path — matching Flask). The
credential is threaded through a per-request ``post_enclave`` closure so the
enclave call is identical to the Flask ``_memory_readside_post_enclave``.

All store / enclave work is blocking, so it runs off the event loop via
``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth, require_scope
from asgi import http as asgi_http
from memory import memory_core
import memory_readside_core

router = APIRouter()


def _post_enclave_for(runtime_token):
    """Bind the request's runtime token into a ``post_enclave`` callable with the
    same signature the readside cores expect. Mirrors the Flask
    ``_memory_readside_post_enclave`` (which reads the token off ``flask.request``)
    — here the token is captured on the loop and passed explicitly."""

    def _post(api_key, candidates, *, operation, payload=None):
        return memory_readside_core.post_enclave_readside(
            api_key,
            candidates,
            operation=operation,
            payload=payload,
            runtime_token=runtime_token,
        )

    return _post


# --------------------------------------------------------------------------- #
# readside (auth only; forwards api key + runtime token to the enclave)
# --------------------------------------------------------------------------- #

@router.post("/v1/memory/index")
async def memory_index(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    runtime_token = auth_core.extract_runtime_token(request.headers)
    body, status = await threadpool.run_db(
        memory_core.index, auth.store, auth.api_key, payload,
        post_enclave=_post_enclave_for(runtime_token),
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/memory/fetch")
async def memory_fetch(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    runtime_token = auth_core.extract_runtime_token(request.headers)
    body, status = await threadpool.run_db(
        memory_core.fetch, auth.store, auth.api_key, payload,
        post_enclave=_post_enclave_for(runtime_token),
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/memory/buckets")
async def memory_buckets(request: Request, auth: AuthResult = Depends(require_auth)):
    runtime_token = auth_core.extract_runtime_token(request.headers)
    body, status = await threadpool.run_db(
        memory_core.buckets, auth.store, auth.api_key,
        post_enclave=_post_enclave_for(runtime_token),
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/memory/threads")
async def memory_threads(request: Request, auth: AuthResult = Depends(require_auth)):
    runtime_token = auth_core.extract_runtime_token(request.headers)
    body, status = await threadpool.run_db(
        memory_core.threads, auth.store, auth.api_key,
        post_enclave=_post_enclave_for(runtime_token),
    )
    return JSONResponse(body, status_code=status)


# --------------------------------------------------------------------------- #
# write actions (scope: memory)
# --------------------------------------------------------------------------- #

@router.post("/v1/memory/actions")
async def memory_actions(request: Request, auth: AuthResult = Depends(require_scope("memory"))):
    # The runtime token is forwarded for the SAME reason as the four readside
    # routes above: a hosted caller (Stage D swaps X-API-Key for the token) has no
    # per-user api_key, and supersede/patch/upgrade must decrypt the OLD card via
    # the enclave before rewriting it. Without it that decrypt raises
    # api_key_unavailable → 409 memory_decrypt_failed for every hosted user.
    runtime_token = auth_core.extract_runtime_token(request.headers) or ""
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        memory_core.actions, auth.store, auth.api_key, payload,
        runtime_token=runtime_token)
    return JSONResponse(body, status_code=status)


# --------------------------------------------------------------------------- #
# migration state — GET auth-only, POST scope:memory (two handlers, same path)
# --------------------------------------------------------------------------- #

@router.get("/v1/memory/migration_state")
async def memory_migration_state_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(memory_core.migration_state_get, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/memory/migration_state")
async def memory_migration_state_post(
    request: Request, auth: AuthResult = Depends(require_scope("memory"))
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        memory_core.migration_state_post, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/memory/legacy_batch")
async def memory_legacy_batch(
    request: Request, auth: AuthResult = Depends(require_scope("memory"))
):
    runtime_token = auth_core.extract_runtime_token(request.headers) or ""
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        memory_core.legacy_batch, auth.store, auth.api_key, runtime_token, payload)
    return JSONResponse(body, status_code=status)


# --------------------------------------------------------------------------- #
# plain store reads / writes (auth only)
# --------------------------------------------------------------------------- #

@router.get("/v1/memory/list")
async def memory_list(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        memory_core.list_moments,
        auth.store,
        limit_raw=request.query_params.get("limit", 50),
        since=request.query_params.get("since", ""),
        include_archived_raw=request.query_params.get("include_archived"),
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/memory/get")
async def memory_get(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        memory_core.get_moment, auth.store, request.query_params.get("id", ""))
    return JSONResponse(body, status_code=status)


@router.post("/v1/memory/add")
async def memory_add(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(memory_core.add, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/memory/retype")
async def memory_retype(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(memory_core.retype, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.delete("/v1/memory/delete")
async def memory_delete(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        memory_core.delete_moment, auth.store, request.query_params.get("id", ""))
    return JSONResponse(body, status_code=status)


@router.get("/v1/memory/verify")
async def memory_verify(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(memory_core.verify, auth.store)
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
