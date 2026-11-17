"""Native ASGI content surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/users/public-key``, ``/v1/content/swap``,
``/v1/content/rewrap-to-current-key``, ``/v1/content/export`` and
``/v1/account/reset`` routes: each requires an authenticated user
(``Depends(require_auth)`` — the ASGI equivalent of ``auth.require_user()``) and
delegates to the framework-neutral ``content.content_core`` so the response
bodies/bytes are byte-identical to Flask's. ``/healthz`` is intentionally NOT
here — it is already served by ``asgi/health.py``.

Auth/scope: ordinary content routes accept either authenticated credential.
``account/reset`` is API-key-only because a short-lived hosted runtime must not
be able to delete the account or invalidate every long-lived credential.

User-requested export-jobs explicitly authorize server-side decryption into
a private seven-day Markdown archive; only account API keys may request it.
The legacy export below remains ciphertext JSON for compatibility.

E2E boundary: chat / memory / identity / frame ``content`` fields are v1 E2E
envelopes, never decrypted server-side. ``export`` returns the stored ciphertext
verbatim. ``rewrap`` forwards the caller's api key to the enclave — resolved with
``auth_core.extract_api_key`` (the exact function the Flask route's
``auth._extract_api_key()`` wraps) so credential forwarding is identical; Flask
never forwarded a runtime token to this enclave call, so neither do we.

``account/reset`` reuses the Flask adapter's ``_purge_onboarding_archives_with_retry``
(referenced at call time, so its monkeypatchable retry constants still apply),
keeping the destructive delete path byte-for-byte identical across frameworks.

All store / DB / enclave / R2 / filesystem work is blocking, so it runs off the
event loop via ``threadpool.run_db`` (plan §5.2). ``export`` may return a large
non-JSON attachment body — rendered as a raw ``Response`` with the exact media
type + headers + status the core returns.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_api_key, require_auth
from asgi import http as asgi_http
from content import content_core, export_service

router = APIRouter()


@router.post("/v1/users/public-key")
async def users_set_public_key(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(content_core.set_public_key, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/content/swap")
async def content_swap(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(content_core.swap, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/content/rewrap-to-current-key")
async def content_rewrap_to_current_key(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    # Exact mirror of the Flask route's ``auth._extract_api_key()`` (X-API-Key /
    # Bearer / legacy ?key=). Forwarded to the enclave decrypt call unchanged.
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        content_core.rewrap_to_current_key, auth.store, payload, api_key=api_key)
    return JSONResponse(body, status_code=status)


@router.get("/v1/content/export")
async def content_export(auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(content_core.export_data, auth.store)
    if result.raw_body is not None:
        return Response(
            content=result.raw_body,
            status_code=result.status,
            media_type=result.media_type,
            headers=result.headers or None,
        )
    return JSONResponse(result.json_body, status_code=result.status)


@router.post("/v1/content/export-jobs")
async def create_export_job(request: Request, auth: AuthResult = Depends(require_api_key)):
    payload = await asgi_http.read_json_silent(request)
    body, status = await threadpool.run_db(export_service.submit, auth.store.user_id, payload)
    return JSONResponse(body, status_code=status)


@router.get("/v1/content/export-jobs/latest")
async def latest_export_job(auth: AuthResult = Depends(require_api_key)):
    body, status = await threadpool.run_db(export_service.status, auth.store.user_id)
    return JSONResponse(body, status_code=status)


@router.get("/v1/content/export-jobs/{job_id}")
async def get_export_job(job_id: str, auth: AuthResult = Depends(require_api_key)):
    body, status = await threadpool.run_db(export_service.status, auth.store.user_id, job_id)
    return JSONResponse(body, status_code=status)


@router.post("/v1/account/reset")
async def account_reset(request: Request, auth: AuthResult = Depends(require_api_key)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        content_core.account_reset,
        auth.store,
        payload,
        purge_archives=content_core._purge_onboarding_archives_with_retry,
    )
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
