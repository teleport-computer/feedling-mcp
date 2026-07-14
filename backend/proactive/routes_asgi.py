"""Native ASGI proactive-jobs long-poll (ASGI-migration plan §9.2).

Async twin of the Flask ``/v1/proactive/jobs/poll``: same stale-reclaim, same
pollable-pending selection, same limit clamp and response shape (all from the
framework-neutral ``proactive.poll_core``) — only the wait becomes an asyncio
future instead of a parked thread.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth
from asgi.settings import settings
from proactive import poll_core, proactive_core
from runtime.waiters import registry

router = APIRouter()


@router.get("/v1/proactive/jobs/poll")
async def proactive_jobs_poll(request: Request, auth: AuthResult = Depends(require_auth)):
    store = auth.store
    await threadpool.run_db(poll_core.reclaim_stale_resident_jobs, store)
    runtime_profile = await threadpool.run_db(poll_core.runtime_profile, store)

    try:
        since = float(request.query_params.get("since", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid since"}, status_code=400)
    try:
        timeout = max(0.0, min(float(request.query_params.get("timeout", 30)), 60))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid timeout"}, status_code=400)
    try:
        limit = int(request.query_params.get("limit", poll_core.LIMIT_DEFAULT))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid limit"}, status_code=400)
    limit = poll_core.clamp_limit(limit)

    async def _check():
        return await threadpool.run_db(
            poll_core.resident_pollable_pending_jobs,
            store,
            since=since,
            limit=limit,
            runtime_profile=runtime_profile,
        )

    waiter = registry.register(
        "proactive", store.user_id, per_user_max=settings.poller_max_per_user_proactive
    )
    if waiter is None:
        return poll_core.build_response(jobs=[], runtime_profile=runtime_profile, timed_out=True)
    try:
        # Check AFTER registering: asyncio.Event.set() latches, so a job enqueued
        # in the gap between check and park is not lost (see chat/routes_asgi poll).
        pending = await _check()
        if pending:
            return poll_core.build_response(jobs=pending, runtime_profile=runtime_profile, timed_out=False)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=timeout)
            notified = True
        except asyncio.TimeoutError:
            notified = False
    finally:
        registry.unregister(waiter)

    if notified:
        return poll_core.build_response(jobs=await _check(), runtime_profile=runtime_profile, timed_out=False)
    return poll_core.build_response(jobs=[], runtime_profile=runtime_profile, timed_out=True)


# --------------------------------------------------------------------------- #
# Remaining proactive routes (plan §7.4): thin async adapters over the same
# framework-neutral ``proactive_core`` the Flask routes call, so bodies/statuses
# are byte-identical. All are gated on ``require_auth`` only (the Flask routes
# call ``auth.require_user()`` and none call ``authorize_scope``), and every core
# call is blocking sync/DB work, so it runs off the loop via ``threadpool.run_db``
# (plan §5.2). The two ``/debug`` routes render HTML; everything else is JSON.
# --------------------------------------------------------------------------- #


@router.get("/v1/proactive/settings")
async def proactive_settings_get(auth: AuthResult = Depends(require_auth)):
    body = await threadpool.run_db(proactive_core.settings_get, auth.store)
    return JSONResponse(body)


@router.post("/v1/proactive/settings")
async def proactive_settings_post(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body = await threadpool.run_db(proactive_core.settings_save, auth.store, payload)
    return JSONResponse(body)


@router.get("/v1/proactive/state")
async def proactive_state_get(auth: AuthResult = Depends(require_auth)):
    body = await threadpool.run_db(proactive_core.state_get, auth.store)
    return JSONResponse(body)


@router.post("/v1/proactive/state")
async def proactive_state_post(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body = await threadpool.run_db(proactive_core.state_save, auth.store, payload)
    return JSONResponse(body)


@router.get("/v1/device/events")
async def device_events_get(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        proactive_core.device_events_list,
        auth.store,
        since_arg=request.query_params.get("since", 0),
        limit_arg=request.query_params.get("limit", 100),
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/device/events")
async def device_events_post(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body = await threadpool.run_db(proactive_core.device_events_append, auth.store, payload)
    return JSONResponse(body)


@router.post("/v1/capture/tick")
async def capture_tick(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(proactive_core.capture_tick, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/capture/force")
async def capture_force(auth: AuthResult = Depends(require_auth)):
    body = await threadpool.run_db(proactive_core.capture_force, auth.store)
    return JSONResponse(body)


@router.post("/v1/dream/tick")
async def dream_tick(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(proactive_core.dream_tick, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.get("/v1/dream/status")
async def dream_status(auth: AuthResult = Depends(require_auth)):
    body = await threadpool.run_db(proactive_core.dream_status, auth.store)
    return JSONResponse(body)


@router.post("/v1/proactive/tick")
async def proactive_tick(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    # Mirror the Flask route's ``auth._extract_api_key()`` (X-API-Key / Bearer /
    # legacy ?key=). Forwarded into the wake-decision builder unchanged.
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body = await threadpool.run_db(proactive_core.proactive_tick, auth.store, payload, api_key=api_key)
    return JSONResponse(body)


@router.post("/v1/proactive/jobs/{job_id}/claim")
async def proactive_job_claim(job_id: str, request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body = await threadpool.run_db(proactive_core.job_claim, auth.store, job_id, payload)
    return JSONResponse(body)


@router.post("/v1/proactive/jobs/{job_id}/status")
async def proactive_job_status(job_id: str, request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(proactive_core.job_status, auth.store, job_id, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/proactive/scheduled/actions")
async def proactive_scheduled_actions(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(proactive_core.scheduled_actions, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/proactive/scheduled/fire")
async def proactive_scheduled_fire(auth: AuthResult = Depends(require_auth)):
    body = await threadpool.run_db(proactive_core.scheduled_fire, auth.store)
    return JSONResponse(body)


@router.get("/v1/proactive/decisions")
async def proactive_decisions(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        proactive_core.list_decisions,
        auth.store,
        since_arg=request.query_params.get("since", 0),
        limit_arg=request.query_params.get("limit", 100),
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/proactive/decisions/{decision_id}/review")
async def proactive_decision_review(decision_id: str, request: Request, auth: AuthResult = Depends(require_auth)):
    # Match the Flask route's request.is_json branch: json body → parsed dict,
    # anything else → form fields (the dashboard's review form posts urlencoded).
    is_json = asgi_http._is_json_content_type(request.headers.get("content-type", ""))
    if is_json:
        payload = (await asgi_http.read_json_silent(request)) or {}
    else:
        form = await request.form()
        payload = {key: form[key] for key in form}
    accept = request.headers.get("accept", "")
    kind, body, status = await threadpool.run_db(
        proactive_core.decision_review,
        auth.store,
        decision_id,
        payload,
        ts=time.time(),
        is_json=is_json,
        accept=accept,
    )
    if kind == "html":
        return HTMLResponse(body, status_code=status)
    return JSONResponse(body, status_code=status)


@router.get("/v1/proactive/reviews")
async def proactive_reviews(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        proactive_core.list_reviews,
        auth.store,
        since_arg=request.query_params.get("since", 0),
        limit_arg=request.query_params.get("limit", 200),
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/proactive/debug")
async def proactive_debug_json(auth: AuthResult = Depends(require_auth)):
    body = await threadpool.run_db(proactive_core.debug_snapshot, auth.store)
    return JSONResponse(body)


@router.get("/debug/proactive")
async def proactive_debug_page(request: Request, auth: AuthResult = Depends(require_auth)):
    html = await threadpool.run_db(
        proactive_core.debug_page_html,
        auth.store,
        query_string=request.url.query,
        accept_language=request.headers.get("accept-language", ""),
    )
    return HTMLResponse(html)


def register_asgi(app) -> None:
    app.include_router(router)
