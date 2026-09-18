"""Content-free request accounting, emitted as one flushed stdout JSON line.

Wrap the complete FastAPI stack (including ServerErrorMiddleware), outside
gzip and HEAD stripping: status/resp_bytes describe ASGI messages actually sent.
req_bytes counts consumed receive bytes, not an untrusted Content-Length; early
rejections may consume zero. dur_ms ends at the last response body (or abort),
excluding background work. null status means no response start was sent.

Only registered route templates are emitted, never raw paths or queries.
Purpose is available only after a route reads its JSON body. Timings are null
when unmeasured; decrypt_ms measures the decrypt worker job, including batch
parsing/selection, separately from time waiting to enter its thread.
No body/credential/exception is passed to the output sink. Error JSON is bounded
to 8 KiB in memory and reduced to a closed label before output.

Local sample (45 requests: all purpose labels + 401/404/503): 307–360 UTF-8
bytes/line including newline; at 1,000 requests/hour the sample maximum is
0.36 MB/hour. This is a measured sample maximum, not a universal size bound
(route length, timings and counters vary). stdout is not an external export.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timezone

import anyio.to_thread
from enclave_health_contract import PURPOSE_LABELS
from enclave_reqlog_contract import FIELDS, FAILURE_CLASSES, METHODS, AUTH_KINDS, WHOAMI_SOURCES
from starlette.routing import Route


_ERROR_ALIASES = {
    "missing api_key": "missing_api_key",
    "cannot resolve user_id": "cannot_resolve_user_id",
    "envelope required": "envelope_required",
    "moments must be a list": "invalid_request",
    "world_books must be a list": "invalid_request",
    "messages must be a list": "invalid_request",
    "leaves must be a list": "invalid_request",
    "query required": "invalid_request",
}
_ERROR_PREFIXES = (
    "decrypt_failed", "backend_error", "backend_unreachable",
    "key_derivation_unavailable",
)



def _label(value, allowed, default):
    return value if isinstance(value, str) and value in allowed else default


def _ms(value):
    return round(value, 3) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _failure(body, status):
    if status is not None and status < 400:
        return "none"
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        payload = None
    if isinstance(payload, dict) and "error" in payload:
        error = payload["error"]
        if not isinstance(error, str):
            return "other"
        if error in _ERROR_ALIASES:
            return _ERROR_ALIASES[error]
        for prefix in _ERROR_PREFIXES:
            if error.startswith(prefix + ": "):
                return prefix
        return _label(error, FAILURE_CLASSES - {"none"}, "other")
    return {404: "not_found", 405: "method_not_allowed", 500: "internal_error"}.get(status, "other")


async def decrypt_job(request, func, *args):
    """Keep anyio's limiter/cancellation semantics; time queue and worker apart."""
    metrics = getattr(request.state, "reqlog", None)
    if metrics is None:
        return await anyio.to_thread.run_sync(func, *args)
    queued = time.perf_counter()

    def measured():
        started = time.perf_counter()
        metrics["decrypt_queue_ms"] = (started - queued) * 1000
        try:
            return func(*args)
        finally:
            metrics["decrypt_ms"] = (time.perf_counter() - started) * 1000

    return await anyio.to_thread.run_sync(measured)


class RequestLogMiddleware:
    def __init__(self, app):
        self.app = app
        self.enabled = os.environ.get("FEEDLING_ENCLAVE_REQLOG", "1") != "0"
        self.skip = frozenset(p.strip() for p in os.environ.get(
            "FEEDLING_ENCLAVE_REQLOG_SKIP", "/healthz").split(",") if p.strip())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            return await self.app(scope, receive, send)
        metrics = {}
        scope.setdefault("state", {})["reqlog"] = metrics
        started = time.perf_counter()
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        status = None
        finished = None
        req_bytes = resp_bytes = 0
        error_body = bytearray()
        error_overflow = False

        async def counted_receive():
            nonlocal req_bytes
            message = await receive()
            if message["type"] == "http.request":
                req_bytes += len(message.get("body", b""))
            return message

        async def counted_send(message):
            nonlocal status, resp_bytes, finished, error_overflow
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                resp_bytes += len(chunk)
                if status is not None and status >= 400 and not error_overflow:
                    if len(error_body) + len(chunk) <= 8192:
                        error_body.extend(chunk)
                    else:
                        error_body.clear()
                        error_overflow = True
                if not message.get("more_body", False):
                    finished = time.perf_counter()

        try:
            await self.app(scope, counted_receive, counted_send)
        finally:
            matched = scope.get("route")
            route = matched.path if isinstance(matched, Route) else "<unmatched>"
            if route not in self.skip:
                prefix = metrics.get("user_prefix")
                record = {
                    "ts": ts, "pid": os.getpid(),
                    "method": _label(scope.get("method"), METHODS, "OTHER"),
                    "route": route, "status": status,
                    "dur_ms": round(((finished or time.perf_counter()) - started) * 1000, 3),
                    "purpose": _label(metrics.get("purpose"), PURPOSE_LABELS, "other"),
                    "auth_kind": _label(metrics.get("auth_kind"), AUTH_KINDS, "none"),
                    "whoami_source": _label(metrics.get("whoami_source"), WHOAMI_SOURCES, "none"),
                    "whoami_ms": _ms(metrics.get("whoami_ms")),
                    "decrypt_queue_ms": _ms(metrics.get("decrypt_queue_ms")),
                    "decrypt_ms": _ms(metrics.get("decrypt_ms")),
                    "user_prefix": prefix if isinstance(prefix, str) and re.fullmatch(r"usr_[A-Za-z0-9]{8}", prefix) else None,
                    "failure_class": _failure(error_body, status) if finished is not None else "response_incomplete",
                    "req_bytes": req_bytes, "resp_bytes": resp_bytes,
                }
                # Include newline in the same write (multi-worker stdout pipe).
                print(json.dumps({key: record[key] for key in FIELDS}, separators=(",", ":")) + "\n",
                      end="", flush=True)
