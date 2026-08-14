"""Native ASGI admin data-track routes (ASGI-migration plan §5.3).

Mirrors the Flask ``admin.data_track`` blueprint. Protected routes accept the
legacy ``FEEDLING_ADMIN_TOKEN`` channels or a signed password-login session;
neither mechanism is user auth. Two routes render HTML pages
(``GET /admin/data-track`` + ``GET /admin/data-track/users/{user_id}``); the
five ``/v1/admin/...`` routes return JSON. The admin check replicates
``admin.data_track.require_admin`` as an ``HTTPException`` so the registered
exception handler renders the identical fixed 401/503 bodies
(``asgi.responses.ERROR_BODIES``); a 401 therefore returns JSON on the HTML
routes too, exactly as Flask's ``errorhandler(401)`` does.

``GET /v1/admin/data-track/verdicts`` is ASGI-native (no Flask twin): the
machine-readable home health check, admin-gated exactly like its siblings.

Each handler's body is produced by the same ``admin.data_track`` functions the
Flask routes call — via ``admin.admin_core``, which runs them inside a throwaway
Flask request context so ``request.args`` is read from the ASGI query string —
so the data-track output is byte-for-byte the Flask output. Blocking sync
``db.py`` work runs through ``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import time
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import db
from admin import admin_core
from admin import memory_metadata
from admin import tee_replication as admin_tee_replication
from asgi import threadpool
from asgi.http import read_json_silent
from model_api_runtime.v2 import jobs_store

router = APIRouter()

_ADMIN_SESSION_COOKIE = "admin_session"
_ADMIN_SESSION_MAX_AGE = 7 * 24 * 60 * 60


def _admin_session_secret() -> bytes | None:
    raw = (
        os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip()
        or os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip()
    )
    if not raw:
        return None
    return hmac.new(
        raw.encode("utf-8"), b"feedling-admin-session-v1", hashlib.sha256
    ).digest()


def _sign_admin_session(*, expires_at: int | None = None) -> str | None:
    secret = _admin_session_secret()
    if secret is None:
        return None
    expiry = int(expires_at if expires_at is not None else time.time() + _ADMIN_SESSION_MAX_AGE)
    payload = f"v1.{expiry}"
    signature = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _valid_admin_session(value: str, *, now: float | None = None) -> bool:
    secret = _admin_session_secret()
    if secret is None:
        return False
    try:
        version, expiry_text, supplied_signature = str(value or "").split(".", 2)
        expiry = int(expiry_text)
    except (TypeError, ValueError):
        return False
    if version != "v1" or expiry <= int(time.time() if now is None else now):
        return False
    payload = f"{version}.{expiry}"
    expected_signature = hmac.new(
        secret, payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


def _safe_admin_next(value: str) -> str:
    candidate = str(value or "").strip()
    if candidate.startswith("/admin/") and not candidate.startswith("//"):
        return candidate
    return "/admin/data-track"


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
    if _valid_admin_session(request.cookies.get(_ADMIN_SESSION_COOKIE, "")):
        return
    configured = os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip()
    if not configured:
        raise HTTPException(status_code=503)
    supplied = _extract_admin_token(request)
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=401)


@router.get("/admin/login")
async def admin_login_page(request: Request):
    next_url = _safe_admin_next(request.query_params.get("next") or "")
    html = admin_core.login_page(
        error=bool(request.query_params.get("error")), next_url=next_url
    )
    return HTMLResponse(html)


@router.post("/admin/login")
async def admin_login(request: Request):
    raw_form = (await request.body()).decode("utf-8", errors="replace")
    form = parse_qs(raw_form, keep_blank_values=True)
    supplied = str((form.get("password") or [""])[0])
    next_url = _safe_admin_next(str((form.get("next") or [""])[0]))
    configured = os.environ.get("FEEDLING_ADMIN_PASSWORD", "")
    valid_password = bool(configured) and hmac.compare_digest(supplied, configured)
    session = _sign_admin_session() if valid_password else None
    if session is None:
        return HTMLResponse(admin_core.login_page(error=True, next_url=next_url), status_code=401)

    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        key=_ADMIN_SESSION_COOKIE,
        value=session,
        max_age=_ADMIN_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(
        key=_ADMIN_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


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
    try:
        payload = await threadpool.run_db(admin_core.dau_payload, request.url.query)
    except admin_core.InvalidDauDay:
        return JSONResponse({"error": "invalid_day"}, status_code=400)
    return JSONResponse(payload)


@router.get("/v1/admin/data-track/growth")
async def data_track_growth(request: Request):
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.growth_payload, request.url.query)
    return JSONResponse(payload)


@router.get("/v1/admin/data-track/debug")
async def data_track_debug(request: Request):
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.debug_payload, request.url.query)
    return JSONResponse(payload)


@router.get("/v1/admin/data-track/verdicts")
async def data_track_verdicts(request: Request):
    # ASGI-native (no Flask twin): machine-readable home verdicts — system/
    # growth/cost/evidence + queue + pulse, same builders as view=home.
    # Deliberately NOT page-cached: the 60s cache is HTML-only (its honesty
    # mechanism, the cache-note, is injected into <main>), JSON has no channel
    # to declare data age; the underlying queries are all bounded and already
    # throttled by the shared 4-worker admin-ops executor. Rationale continues
    # in admin_core.verdicts_payload's docstring.
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.verdicts_payload, request.url.query)
    return JSONResponse(payload)


@router.get("/v1/admin/route-fence-audit")
async def route_fence_audit(request: Request):
    """Read-only L1 inventory; remediation intentionally remains CLI-only."""
    _require_admin(request)
    rows = await threadpool.run_db(
        db.audit_resident_active_model_routes,
        apply=False,
    )
    return JSONResponse(
        {
            "mode": "dry_run",
            "conflicts": len(rows),
            "rows": rows,
            "lease_source": {
                "table": "agent_runtime_instances",
                "cardinality": "one row per user (user_id primary key)",
                "live_when": "lease_owner is set and lease_expires_at >= database now()",
            },
        }
    )


@router.get("/v1/admin/data-track/users/{user_id}")
async def data_track_user(user_id: str, request: Request):
    _require_admin(request)
    body, status = await threadpool.run_db(admin_core.user_payload, request.url.query, user_id)
    return JSONResponse(body, status_code=status)


@router.get("/v1/admin/users/{user_id}/memory-card-metadata")
async def memory_card_metadata(user_id: str, request: Request):
    """Paginated card lifecycle metadata; encrypted bodies are not selected."""
    _require_admin(request)
    limit, offset = memory_metadata.pagination(
        request.query_params.get("limit"), request.query_params.get("offset")
    )
    payload = await threadpool.run_db(
        memory_metadata.list_card_metadata,
        user_id,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(payload)


@router.get("/v1/admin/memory-dream-jobs")
async def memory_dream_jobs(request: Request):
    """Paginated, content-free dream job timing/failure diagnostics."""
    _require_admin(request)
    limit, offset = memory_metadata.pagination(
        request.query_params.get("limit"),
        request.query_params.get("offset"),
        jobs=True,
    )
    payload = await threadpool.run_db(
        memory_metadata.list_dream_job_metadata,
        limit=limit,
        offset=offset,
        user_id=(request.query_params.get("user_id") or "").strip(),
        status=(request.query_params.get("status") or "").strip(),
    )
    return JSONResponse(payload)


@router.get("/admin/data-track")
async def data_track_page(request: Request):
    _require_admin(request)
    try:
        html = await threadpool.run_db(admin_core.page_html, request.url.query)
    except admin_core.InvalidDauDay:
        return PlainTextResponse("invalid day", status_code=400)
    return HTMLResponse(html)


@router.get("/admin/data-track/users")
async def data_track_user_lookup(request: Request):
    _require_admin(request)
    raw_user_id = request.query_params.get("uid", "")
    try:
        user_id = admin_core.normalize_data_track_user_id(raw_user_id)
    except admin_core.InvalidDataTrackUserId:
        body = await threadpool.run_db(
            admin_core.invalid_user_id_page,
            request.url.query,
            raw_user_id,
        )
        return HTMLResponse(body, status_code=400)

    passthrough = []
    for name in ("admin_key", "days", "events_limit"):
        value = str(request.query_params.get(name, "") or "").strip()
        if value:
            passthrough.append((name, value))
    target = f"/admin/data-track/users/{quote(user_id, safe='')}"
    if passthrough:
        target = f"{target}?{urlencode(passthrough)}"
    return RedirectResponse(target, status_code=303)


@router.get("/admin/data-track/users/{user_id}")
async def data_track_user_page(user_id: str, request: Request):
    _require_admin(request)
    kind, body, status = await threadpool.run_db(admin_core.user_page, request.url.query, user_id)
    if kind == "text":
        return PlainTextResponse(body, status_code=status)
    return HTMLResponse(body, status_code=status)


@router.post("/v1/admin/users/{user_id}/delete")
async def delete_user(user_id: str, request: Request):
    _require_admin(request)
    payload = await read_json_silent(request)
    confirm = payload.get("confirm") if isinstance(payload, dict) else None
    if confirm != user_id:
        return JSONResponse({"error": "confirmation_mismatch"}, status_code=400)
    body, status = await threadpool.run_db(admin_core.delete_user, user_id)
    return JSONResponse(body, status_code=status)


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


@router.post("/v1/admin/hosted-runtime-mode")
async def hosted_runtime_mode_set(request: Request):
    _require_admin(request)
    payload = (await read_json_silent(request)) or {}
    user_id = str(payload.get("user_id") or "").strip()
    mode = str(payload.get("mode") or "").strip()
    if not user_id or not mode:
        return JSONResponse({"error": "user_id and mode required"}, status_code=400)
    body, status = await threadpool.run_db(admin_core.set_runtime_mode, user_id, mode)
    return JSONResponse(body, status_code=status)


@router.get("/v1/admin/hosted-runtime-mode")
async def hosted_runtime_mode_get(request: Request):
    _require_admin(request)
    user_id = (request.query_params.get("user_id") or "").strip()
    if not user_id:
        return JSONResponse({"error": "user_id required"}, status_code=400)
    body, status = await threadpool.run_db(admin_core.get_runtime_mode, user_id)
    return JSONResponse(body, status_code=status)


@router.get("/v1/admin/hosted-runtime-modes")
async def hosted_runtime_modes_list(request: Request):
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.list_runtime_modes)
    return JSONResponse(payload)


@router.post("/v1/admin/runtime-allowlist")
async def runtime_allowlist_set(request: Request):
    _require_admin(request)
    payload = (await read_json_silent(request)) or {}
    user_id = str(payload.get("user_id") or "").strip()
    desired = str(payload.get("desired") or "").strip()
    note = str(payload.get("note") or "")
    if not user_id or not desired:
        return JSONResponse({"error": "user_id and desired required"}, status_code=400)
    body, status = await threadpool.run_db(
        admin_core.set_runtime_allowlist, user_id, desired, note=note)
    return JSONResponse(body, status_code=status)


@router.get("/v1/admin/runtime-allowlist")
async def runtime_allowlist_get(request: Request):
    _require_admin(request)
    payload = await threadpool.run_db(admin_core.get_runtime_allowlist)
    return JSONResponse(payload)


@router.get("/v1/admin/v2-metrics")
async def v2_metrics(request: Request):
    _require_admin(request)
    cache_provider = (request.query_params.get("cache_provider") or "").strip() or None
    cache_model = (request.query_params.get("cache_model") or "").strip() or None
    cache_route_fingerprint = (
        request.query_params.get("cache_route_fingerprint") or ""
    ).strip() or None
    cache_user_id = (request.query_params.get("cache_user_id") or "").strip() or None

    # Python/JSON can represent finite floats far beyond PostgreSQL's timestamp
    # range. Keep the admin proof window inside a deliberately conservative
    # UTC year-9999 boundary so a malformed query returns 400 rather than 500.
    max_cache_ts = 253402300799.0

    def _cache_ts(name: str) -> tuple[float | None, JSONResponse | None]:
        raw = (request.query_params.get(name) or "").strip()
        if not raw:
            return None, None
        try:
            value = float(raw)
        except ValueError:
            return None, JSONResponse({"error": f"invalid_{name}"}, status_code=400)
        if not math.isfinite(value) or value < 0 or value > max_cache_ts:
            return None, JSONResponse({"error": f"invalid_{name}"}, status_code=400)
        return value, None

    cache_since_ts, invalid = _cache_ts("cache_since_ts")
    if invalid is not None:
        return invalid
    cache_until_ts, invalid = _cache_ts("cache_until_ts")
    if invalid is not None:
        return invalid
    if (cache_since_ts is not None and cache_until_ts is not None
            and cache_until_ts < cache_since_ts):
        return JSONResponse({"error": "invalid_cache_window"}, status_code=400)
    payload = await threadpool.run_db(
        admin_core.v2_metrics,
        cache_provider=cache_provider,
        cache_model=cache_model,
        cache_route_fingerprint=cache_route_fingerprint,
        cache_user_id=cache_user_id,
        cache_since_ts=cache_since_ts,
        cache_until_ts=cache_until_ts,
    )
    return JSONResponse(payload)


@router.get("/v1/admin/v2-wake-shadow")
async def v2_wake_shadow(request: Request):
    """Content-free A′ report; the local-hour bucket is caller supplied.

    ``start_hour``/``end_hour`` are observation parameters, not a product
    sleep-window definition and never enter wake policy.
    """
    _require_admin(request)

    def _int_arg(
        name: str,
        minimum: int,
        maximum: int,
    ) -> tuple[int | None, JSONResponse | None]:
        raw = (request.query_params.get(name) or "").strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None, JSONResponse({"error": f"invalid_{name}"}, status_code=400)
        if value < minimum or value > maximum:
            return None, JSONResponse({"error": f"invalid_{name}"}, status_code=400)
        return value, None

    days, invalid = _int_arg("days", 1, 90)
    if invalid is not None:
        return invalid
    start_hour, invalid = _int_arg("start_hour", 0, 23)
    if invalid is not None:
        return invalid
    end_hour, invalid = _int_arg("end_hour", 0, 23)
    if invalid is not None:
        return invalid
    if start_hour == end_hour:
        return JSONResponse({"error": "invalid_hour_bucket"}, status_code=400)
    payload = await threadpool.run_db(
        jobs_store.wake_shadow_report,
        days=days,
        bucket_start_hour=start_hour,
        bucket_end_hour=end_hour,
    )
    return JSONResponse(payload)


def register_asgi(app) -> None:
    app.include_router(router)
