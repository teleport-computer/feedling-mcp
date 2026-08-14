"""Framework-neutral diagnostics payload builders (ASGI-migration plan §5.3).

Lifts the bodies of the diagnostics Flask routes — the client diagnostic-log
upload + admin read, and the v1 flow-trace debug endpoints — out of the request
layer so the native ASGI router reuses byte-identical logic. There is no
Flask/FastAPI request object here: the caller resolves auth + parses the request
(files, form, query, JSON) and hands the store and already-parsed params in.

Every function below touches sync ``db.py`` / boto3 R2 (blocking I/O), so ASGI
callers MUST run these through ``asgi.threadpool.run_db`` — never on the event
loop (plan §5.0/§5.2). Each returns a ``(body_dict, status)`` tuple so both
backends emit the identical JSON body + status code.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import db
import debug_trace
import provider_attempt_ledger

from . import storage

_STREAM = "client_diagnostics"
_MAX_BYTES = 512 * 1024  # mirror the iOS DiagnosticLog ring size
# Hard request-body ceiling, checked from Content-Length *before* the multipart
# body is read into memory — so an oversized (even authenticated) upload is
# rejected 413 rather than buffered. Headroom over _MAX_BYTES covers the
# multipart envelope + meta. Scoped to this route (not a global
# MAX_CONTENT_LENGTH) so it never clips the larger frame/photo upload paths.
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 10           # keep the newest N uploads per user
_PRESIGN_TTL = 3600


def parse_meta(raw: str | None) -> dict:
    """Read the optional ``meta`` form field (a JSON object string). Stored
    as-is; never trusted for control flow."""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


def is_oversized(content_length: int | None) -> bool:
    """True when the request body exceeds the hard ceiling — the caller uses this
    to reject (413) from Content-Length *before* buffering the multipart body."""
    return content_length is not None and content_length > _MAX_REQUEST_BYTES


def coerce_limit(raw) -> int:
    """Mirror Flask's ``int(request.args.get("limit", 200))`` with its fallback:
    a missing (None), empty, or non-numeric value degrades to 200."""
    try:
        return int(raw if raw is not None else 200)
    except (TypeError, ValueError):
        return 200


def upload_logs_payload(store, *, content_length, file_present, file_bytes, meta):
    """Body of ``POST /v1/diagnostics/logs``. ``file_bytes`` is the caller's raw
    read (already bounded to ``_MAX_BYTES + 1``); this truncates by *bytes* to the
    ring-buffer cap. R2 put + Postgres index write happen here (blocking)."""
    uid = store.user_id

    # Reject oversized bodies from Content-Length before touching the upload.
    if content_length is not None and content_length > _MAX_REQUEST_BYTES:
        return {"error": "payload_too_large"}, 413

    if not file_present:
        return {"error": "missing_file"}, 400
    raw = file_bytes[:_MAX_BYTES]
    if not raw:
        return {"error": "empty_file"}, 400

    ts = time.time()
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")

    doc: dict = {"meta": meta, "ts": ts}
    if storage.enabled():
        try:
            doc["r2_key"] = storage.put_log(uid, ts_iso, raw)
        except Exception as e:  # noqa: BLE001 — fall back to inline rather than lose the log
            storage.log.error("[diagnostics] R2 put failed for %s, storing inline: %s", uid, e)
            doc["content"] = raw.decode("utf-8", "replace")
    else:
        doc["content"] = raw.decode("utf-8", "replace")

    db.log_append(uid, _STREAM, doc, ts=ts)
    db.log_trim(uid, _STREAM, _MAX_ROWS)
    return {"status": "ok"}, 201


def admin_read_logs_payload(user_id):
    """Body of ``GET /v1/admin/diagnostics/logs/<user_id>``. Enumerate the newest
    uploads from the Postgres index; presign R2 keys (or return inline content on
    the no-R2 fallback path). Blocking: DB read + optional R2 presign."""
    rows = db.log_read(user_id, _STREAM, limit=_MAX_ROWS)
    entries = []
    for doc in rows:
        entry: dict = {"ts": doc.get("ts"), "meta": doc.get("meta", {})}
        key = doc.get("r2_key")
        if key:
            entry["r2_key"] = key
            entry["download_url"] = storage.presign_get(key, _PRESIGN_TTL)
        elif "content" in doc:
            entry["content"] = doc["content"]
        entries.append(entry)
    return {"user_id": user_id, "logs": entries}, 200


def read_trace_payload(store, *, limit, subsystem):
    """Body of ``GET /v1/debug/trace``. Blocking: per-user blob read."""
    return {
        "enabled": debug_trace.is_enabled(store),
        "deploy_enabled": debug_trace._deploy_enabled(),
        "verbose": debug_trace.verbose_enabled(store),
        "events": debug_trace.read_trace(store, limit=limit, subsystem=subsystem),
    }, 200


def set_trace_enabled_payload(store, enabled):
    """Body of ``POST /v1/debug/trace/enable``. Blocking: per-user blob write."""
    doc = debug_trace.set_enabled(store, bool(enabled))
    return {"enabled": doc["enabled"], "deploy_enabled": debug_trace._deploy_enabled()}, 200


def clear_trace_payload(store):
    """Body of ``DELETE /v1/debug/trace``. Blocking: per-user blob write."""
    debug_trace.clear_trace(store)
    return {"status": "ok"}, 200


# Injected by asgi_app at assembly time (CONTRIBUTING §2: diagnostics must not
# import hosted, so an upward call is wired, not imported — same pattern as
# core/store.py's on_proactive_job_appended). Left None in tests that do not
# care; a missing hook silently skips the projection.
#
# Why here: V1's only per-turn evidence about a user's MCP servers arrives as
# this trace. Without this the `runtime` field of GET /v1/mcp/servers is
# permanently empty for every resident_cli user, so the app cannot tell them
# which of their servers is actually broken.
on_mcp_surface_registered = None

_MCP_REGISTERED_EVENT = "mcp.surface.registered"

log = logging.getLogger("feedling.diagnostics")


def emit_trace_event_payload(store, payload):
    """Body of ``POST /v1/debug/trace/event`` for the HTTP-only resident.

    Provider-attempt metadata takes the always-on append-only ledger path.
    Ordinary flow events retain the existing gated, best-effort trace-ring path.
    Both paths field-pick their input; blocking DB work stays behind the route's
    threadpool boundary.
    """
    if "provider_attempt" in payload or "provider_attempts" in payload:
        return provider_attempt_ledger.record_attempts_payload(store, payload)
    ev = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    dur = ev.get("dur_ms")
    try:
        dur = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    debug_trace.trace_event(
        store,
        subsystem=str(ev.get("subsystem") or ""),
        type=str(ev.get("type") or ""),
        summary=str(ev.get("summary") or ""),
        explain=str(ev.get("explain") or ""),
        detail=ev.get("detail") if isinstance(ev.get("detail"), dict) else None,
        content_excerpt=ev.get("content_excerpt") if isinstance(ev.get("content_excerpt"), dict) else None,
        actor=str(ev.get("actor") or "vps_resident"),
        status=str(ev.get("status") or "ok"),
        trace_id=str(ev.get("trace_id") or ""),
        turn_id=str(ev.get("turn_id") or ""),
        dur_ms=dur,
    )
    if (str(ev.get("type") or "") == _MCP_REGISTERED_EVENT
            and on_mcp_surface_registered is not None):
        try:
            on_mcp_surface_registered(
                store,
                ev.get("detail") if isinstance(ev.get("detail"), dict) else None,
            )
        except Exception as exc:  # noqa: BLE001
            # The trace itself already landed above. A status-projection failure
            # must never turn a successful diagnostics POST into an error — the
            # consumer retries the whole event, which would duplicate the trace
            # ring entry to fix a field nobody is blocked on.
            log.warning("mcp runtime status projection failed user=%s kind=%s",
                        getattr(store, "user_id", ""), type(exc).__name__)
    return {"status": "ok"}, 200
