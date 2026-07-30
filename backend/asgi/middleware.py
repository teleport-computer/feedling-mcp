"""Access-log middleware + exception handlers (ASGI-migration plan §5.9 / §3.1).

The access log is a pure-ASGI middleware (not Starlette ``BaseHTTPMiddleware``)
so it can observe client-disconnect **cancellation** and still emit a line — the
exact blind spot that misled the 2026-07-02 long-poll investigation, where
Flask's ``after_request`` only logged requests that returned normally.

Three hard parity/security requirements from the Flask ``after_request`` (§5.9):
1. ``?key=`` is REDACTED case-insensitively — legacy auth allows the API key in
   the URL and it must never reach the logs.
2. Cancelled / errored requests get a line too (``status=cancelled`` / ``500``).
3. (follow-up) periodic dump of slow in-flight requests — UvicornWorker has no
   per-request timeout to reap a wedged handler (§5.2). Deferred until there are
   real long-running native routes (PR 5+); the redact + cancellation lines are
   the load-bearing parity items and are done here.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import parse_qsl, urlencode

from accounts import auth_core
from asgi import context as asgi_context
from asgi import responses
from asgi.context import current_user_id
from asgi.settings import settings
from fastapi.exceptions import RequestValidationError
from starlette.requests import ClientDisconnect
from starlette.responses import Response

try:  # Starlette re-exports the same class; import defensively.
    from starlette.exceptions import HTTPException as StarletteHTTPException
except Exception:  # pragma: no cover
    from fastapi import HTTPException as StarletteHTTPException  # type: ignore

log = logging.getLogger("feedling.asgi")


_SECRET_QUERY_SUBSTRINGS = ("key", "token", "secret", "password", "passwd", "auth")


def _is_secret_param(name: str) -> bool:
    """按**子串**判定参数名是否携带凭据，而不是精确等于 ``key``。

    2026-07-30 审计实证：admin 后台把 ``admin_key`` 放进每个链接的 query string
    （``data_track._data_track_qs`` 的保留列表第一项），而原判据是
    ``k.lower() == "key"``——``admin_key`` != ``key``，于是可复用的 admin 凭据
    原样进了访问日志。精确匹配单个名字，等于把"以后不会出现别的别名"当作前提，
    而这个前提在同一个仓库里当时就已经不成立：``admin_key`` / ``admin_token``
    / ``api_key`` 三个都在用。宁可多脱敏一个无害参数，也不能漏一个凭据。
    """
    lowered = name.lower()
    return any(needle in lowered for needle in _SECRET_QUERY_SUBSTRINGS)


def _display_path(scope) -> str:
    """path (+ query)，参数名含凭据语义的一律脱敏（大小写无关）。"""
    path = scope.get("path", "")
    qs = scope.get("query_string", b"").decode("latin-1")
    if not qs:
        return path
    pairs = parse_qsl(qs, keep_blank_values=True)
    if any(_is_secret_param(k) for k, _ in pairs):
        redacted = [
            (k, "REDACTED" if _is_secret_param(k) else v) for k, v in pairs
        ]
        return f"{path}?{urlencode(redacted)}"
    return f"{path}?{qs}"


class AccessLogMiddleware:
    """One structured ``[req]`` line per request (handler time + on-wire size).

    Mirrors app.py's ``_access_log_end`` format so ``phala cvms logs`` reads the
    same across both backends; skips ``/healthz`` (probe noise) like Flask does.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.access_log or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return

        req_id = asgi_context.new_request_id()
        asgi_context.current_request_id.set(req_id)

        start = time.monotonic()
        info = {"status": 0, "bytes": "?", "enc": "-"}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                info["status"] = message["status"]
                for key, value in message.get("headers", []):
                    kl = key.decode("latin-1").lower()
                    if kl == "content-length":
                        info["bytes"] = value.decode("latin-1")
                    elif kl == "content-encoding":
                        info["enc"] = value.decode("latin-1")
                hdrs = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in hdrs):
                    hdrs.append((b"x-request-id", req_id.encode("ascii")))
                message["headers"] = hdrs
            await send(message)

        method = scope.get("method", "-")
        disp = _display_path(scope)

        def _emit(status_field) -> None:
            dur_ms = int((time.monotonic() - start) * 1000)
            print(
                f"[req] uid={current_user_id.get()} {method} {disp} "
                f"status={status_field} bytes={info['bytes']} enc={info['enc']} dur_ms={dur_ms} "
                f"rid={req_id}",
                flush=True,
            )

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:  # includes asyncio.CancelledError
            # CancelledError (client disconnect / shutdown) is a BaseException,
            # not Exception — catch broadly, log, and re-raise so cancellation
            # semantics are preserved.
            import asyncio

            _emit("cancelled" if isinstance(exc, asyncio.CancelledError) else "500")
            raise
        else:
            _emit(info["status"])


def register_exception_handlers(app) -> None:
    """Map typed auth errors + bare HTTP errors to the fixed Flask JSON bodies."""

    @app.exception_handler(auth_core.AuthError)
    async def _auth_error(request, exc: auth_core.AuthError):
        return responses.json_error(exc.status_code, {"error": exc.code})

    # Routes that read the body/form directly (copytext, diagnostics, proactive,
    # genesis) hit a raised ClientDisconnect when the peer drops mid-upload —
    # e.g. iOS backgrounding during a log upload. The peer is gone, so the only
    # observable effect of letting it bubble is a 500 traceback in the logs;
    # answer with nginx's 499 instead so the access line stays greppable and
    # distinct from real 4xx/5xx. (read_json_silent additionally swallows it
    # locally to keep Flask's silent=True parity for JSON routes.)
    @app.exception_handler(ClientDisconnect)
    async def _client_disconnect(request, exc):
        return Response(status_code=499)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request, exc: StarletteHTTPException):
        body = responses.ERROR_BODIES.get(exc.status_code)
        if body is not None:
            return responses.json_error(exc.status_code, body)
        return responses.json_error(exc.status_code, {"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc: RequestValidationError):
        # FastAPI 默认 422 {"detail":[...]}——重塑进统一信封，消灭双形状
        # （FRONTEND_ERROR_CONTRACT.md §三：invalid_payload）。
        detail = [
            {"loc": ".".join(str(p) for p in e.get("loc", ())),
             "msg": str(e.get("msg", ""))}
            for e in exc.errors()[:10]
        ]
        return responses.api_error(400, "invalid_payload", detail=detail)

    @app.exception_handler(Exception)
    async def _unhandled(request, exc: Exception):
        # 兜底 500：统一信封 + request_id，traceback 只进服务端日志（同 id 对账）。
        # AccessLogMiddleware 未启用（access_log=False / healthz）时现场补生成。
        rid = asgi_context.current_request_id.get() or asgi_context.new_request_id()
        log.exception("[%s] unhandled exception on %s %s",
                      rid, request.method, request.url.path)
        resp = responses.api_error(500, "internal_error", request_id=rid)
        resp.headers["x-request-id"] = rid
        return resp

    # Degrade a psycopg pool exhaustion to a retryable 503 instead of a bare 500.
    # The run_db thread limiter (FEEDLING_ASGI_DB_THREADS, default 64) admits more
    # concurrent blocking calls than the psycopg pool has connections (db.py
    # max_size=16), so a genuine request burst can hit the pool's acquire timeout.
    # This is a graceful shed, not a fix for the sizing mismatch — tune the two to
    # match for prod (mind RDS max_connections × workers).
    try:
        from psycopg_pool import PoolTimeout as _PoolTimeout
    except Exception:  # pragma: no cover - psycopg_pool always present in deploy
        _PoolTimeout = None
    if _PoolTimeout is not None:
        @app.exception_handler(_PoolTimeout)
        async def _pool_timeout(request, exc):
            return responses.json_error(503, {"error": "service_busy"})
