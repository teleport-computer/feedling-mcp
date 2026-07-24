"""v1 flow trace — per-user observability ring buffer (M0).

Turns "I feel like the flow ran" into "I did X, the panel shows event Y, so the
path objectively ran." Covers host(agent_runtime) + vps(resident consumer) +
genesis/memory/identity/perception/proactive because the events are recorded
where the flow actually happens (backend turn internals + incoming tool calls +
the resident consumer).

PRIVACY: `detail` should stay metadata-oriented. Plaintext snippets belong in
`content_excerpt`, which is capped and can be stripped deploy-wide with
FEEDLING_DEBUG_VERBOSE=0. During beta, trace recording is default-on for every
user so admin can inspect real failures; FEEDLING_V1_FLOW_TRACE=0 remains the
deploy-wide kill switch, and a per-user flag can still opt a user out.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any

import db

DEBUG_TRACE_BLOB = "v1_flow_trace"
DEBUG_TRACE_FLAG_BLOB = "v1_flow_trace_enabled"
_MAX_EVENTS = 500
_MAX_EVENTS_VERBOSE = 200
_EXCERPT_FIELD_MAX = 2048
_EXCERPT_EVENT_MAX = 8192
_TRUNC_MARK = "…(truncated)"
_TTL_SEC = 48 * 3600  # beta debug retention: enough to inspect yesterday's reports
_FLAG_CACHE_TTL = 30.0
_QUEUE_MAX = 5000
_FLUSH_BATCH_MAX = 100
_FLUSH_WAIT_SEC = 0.25

# subsystem ∈ memory|genesis|identity|route|voice|perception|proactive|fallback|account
# actor     ∈ host_agent_runtime|vps_resident|backend|ios

_flag_cache: dict[str, tuple[bool, float]] = {}
_event_queue: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue(maxsize=_QUEUE_MAX)
_worker_lock = threading.Lock()
_worker_started = False
_dropped_lock = threading.Lock()
_dropped_by_uid: dict[str, int] = {}
# Count events from enqueue until either the background worker or a synchronous
# debug read has finished persisting them. Queue emptiness is insufficient: the
# worker removes an item before its 250ms batching window, so a concurrent GET
# could otherwise observe neither the queued event nor the not-yet-committed
# blob write. Product writes never wait; only the explicit debug read uses this
# condition as a bounded read-your-writes barrier.
_pending_condition = threading.Condition()
_pending_by_uid: dict[str, int] = {}


def _hard_disabled() -> bool:
    """Optional prod kill switch. Explicitly OFF (FEEDLING_V1_FLOW_TRACE=0) force-
    disables everywhere. Unset → the per-user debug-panel toggle is the real gate,
    so nothing extra is needed to use it on test (just flip the switch)."""
    return os.environ.get("FEEDLING_V1_FLOW_TRACE", "").strip().lower() in ("0", "false", "off", "no")


def _deploy_enabled() -> bool:
    """Deploy-level allowed unless hard-disabled."""
    return not _hard_disabled()


def _default_enabled() -> bool:
    """Beta default: record flow traces for all users unless explicitly disabled.

    FEEDLING_V1_FLOW_TRACE_DEFAULT=0 is a softer rollout valve than the hard
    deploy kill switch: it restores old opt-in behavior while leaving explicit
    per-user enables working.
    """
    return os.environ.get("FEEDLING_V1_FLOW_TRACE_DEFAULT", "").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def is_enabled(store) -> bool:
    if _hard_disabled():
        return False
    uid = getattr(store, "user_id", "") or ""
    if not uid:
        return False
    now = time.time()
    cached = _flag_cache.get(uid)
    if cached and cached[1] > now:
        return cached[0]
    flag = db.get_blob(uid, DEBUG_TRACE_FLAG_BLOB)
    if isinstance(flag, dict) and "enabled" in flag:
        enabled = bool(flag.get("enabled"))
    elif flag is None:
        enabled = _default_enabled()
    else:
        enabled = bool(flag)
    _flag_cache[uid] = (enabled, now + _FLAG_CACHE_TTL)
    return enabled


def set_enabled(store, enabled: bool) -> dict:
    uid = getattr(store, "user_id", "") or ""
    doc = {"enabled": bool(enabled), "updated_at": time.time()}
    if uid:
        db.set_blob(uid, DEBUG_TRACE_FLAG_BLOB, doc)
        _flag_cache[uid] = (bool(enabled), time.time() + _FLAG_CACHE_TTL)
    return doc


def verbose_enabled(store) -> bool:
    """Whether to record plaintext content_excerpt. Defaults to is_enabled;
    FEEDLING_DEBUG_VERBOSE=0 force-strips (prod safety valve)."""
    if os.environ.get("FEEDLING_DEBUG_VERBOSE", "").strip().lower() in ("0", "false", "off", "no"):
        return False
    return is_enabled(store)


def _safe_content_excerpt(d: dict[str, Any] | None) -> dict[str, Any]:
    """Metadata-free plaintext excerpt: per-field and per-event byte caps,
    truncation marked. Only str/number fields; drops anything exotic."""
    if not isinstance(d, dict):
        return {}
    out: dict[str, Any] = {}
    budget = _EXCERPT_EVENT_MAX
    for k, v in list(d.items())[:20]:
        if budget <= 0:
            break
        key = str(k)[:40]
        s = v if isinstance(v, str) else str(v)
        field_cap = min(_EXCERPT_FIELD_MAX, budget)
        if len(s.encode("utf-8")) > field_cap:
            s = s.encode("utf-8")[:field_cap].decode("utf-8", "ignore") + _TRUNC_MARK
        out[key] = s
        budget -= len(s.encode("utf-8"))
    return out


def _enabled_fast(store) -> bool:
    """Hot-path gate for trace_event.

    This intentionally avoids DB reads. Per-user toggles are reflected through
    the short in-process flag cache when known; otherwise beta default-on/off is
    decided from env. A stale opt-out may allow an event to enter the queue, but
    the background flusher re-checks the real gate before writing.
    """
    if _hard_disabled():
        return False
    uid = getattr(store, "user_id", "") or ""
    if not uid:
        return False
    now = time.time()
    cached = _flag_cache.get(uid)
    if cached and cached[1] > now:
        return cached[0]
    return _default_enabled()


def _record_drop(uid: str, count: int = 1) -> None:
    if not uid:
        return
    with _dropped_lock:
        _dropped_by_uid[uid] = _dropped_by_uid.get(uid, 0) + max(1, int(count or 1))


def _take_dropped(uid: str) -> int:
    if not uid:
        return 0
    with _dropped_lock:
        return int(_dropped_by_uid.pop(uid, 0) or 0)


def _append_events(uid: str, new_events: list[dict[str, Any]]) -> None:
    if not uid or not new_events:
        return
    try:
        store = type("_TraceStore", (), {"user_id": uid})()
        if not is_enabled(store):
            return
        now = time.time()
        verbose = verbose_enabled(store)
        dropped = _take_dropped(uid)
        if dropped:
            new_events = [{
                "ts": now,
                "subsystem": "debug_trace",
                "type": "debug_trace.dropped",
                "actor": "backend",
                "status": "warn",
                "summary": f"dropped {dropped} debug trace events",
                "explain": "Debug trace queue was full; events were dropped so product flow stayed non-blocking.",
                "trace_id": "",
                "turn_id": "",
                "job_id": "",
                "detail": {"dropped": dropped},
            }] + new_events
        cutoff = now - _TTL_SEC
        cap = _MAX_EVENTS_VERBOSE if verbose else _MAX_EVENTS
        db.append_blob_events_strict(
            uid,
            DEBUG_TRACE_BLOB,
            new_events,
            cutoff_ts=cutoff,
            max_events=cap,
        )
    except Exception:
        _record_drop(uid, len(new_events))


def _flush_batch(batch: list[tuple[str, dict[str, Any]]]) -> None:
    by_uid: dict[str, list[dict[str, Any]]] = {}
    for uid, event in batch:
        by_uid.setdefault(uid, []).append(event)
    for uid, events in by_uid.items():
        _append_events(uid, events)


def _worker_loop() -> None:
    while True:
        batch: list[tuple[str, dict[str, Any]]] = []
        try:
            item = _event_queue.get()
            batch = [item]
            deadline = time.monotonic() + _FLUSH_WAIT_SEC
            while len(batch) < _FLUSH_BATCH_MAX:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout <= 0:
                    break
                try:
                    batch.append(_event_queue.get(timeout=timeout))
                except queue.Empty:
                    break
            try:
                _flush_batch(batch)
            finally:
                # task_done bookkeeping lets _flush_pending_for_user see the
                # worker's in-flight batch via unfinished_tasks — without it a
                # debug read racing this batching window returns stale data.
                for _ in batch:
                    _event_queue.task_done()
        except Exception:
            pass
        finally:
            if batch:
                with _pending_condition:
                    for uid, _event in batch:
                        remaining = _pending_by_uid.get(uid, 0) - 1
                        if remaining > 0:
                            _pending_by_uid[uid] = remaining
                        else:
                            _pending_by_uid.pop(uid, None)
                    _pending_condition.notify_all()


def _ensure_worker_started() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker_loop, name="debug-trace-flush", daemon=True).start()
        _worker_started = True


def _enqueue(uid: str, event: dict[str, Any]) -> None:
    _ensure_worker_started()
    with _pending_condition:
        _pending_by_uid[uid] = _pending_by_uid.get(uid, 0) + 1
    try:
        _event_queue.put_nowait((uid, event))
    except queue.Full:
        with _pending_condition:
            remaining = _pending_by_uid.get(uid, 0) - 1
            if remaining > 0:
                _pending_by_uid[uid] = remaining
            else:
                _pending_by_uid.pop(uid, None)
            _pending_condition.notify_all()
        _record_drop(uid)


def _flush_pending_for_user(uid: str, *, timeout: float = 0.5) -> None:
    """Best-effort read barrier that waits for the sole local queue writer.

    The request path must not drain or persist queue items itself: becoming a
    second writer races a batch already owned by the background thread. The DB
    append is atomic across processes; this condition only supplies bounded
    read-your-writes behavior for events queued in this process. The per-uid
    counter is decremented only AFTER the worker's batch hits the DB, so a zero
    count also honors the worker's in-flight batch (origin/test's intent).
    """
    if not uid:
        return
    # The flush worker is the ONLY DB appender (started by the first
    # _enqueue), and it acknowledges each popped item via task_done only
    # AFTER its batch hit the DB — so unfinished_tasks == 0 is the exact
    # "everything enqueued so far is readable" linearization point. Do not
    # drain or append here: a reader-side drain either acks items before
    # they are written (a concurrent reader then early-returns stale) or
    # races the worker's get_blob→extend→set_blob with a second unlocked
    # RMW and loses events.
    deadline = time.monotonic() + max(0.0, timeout)
    with _pending_condition:
        while _pending_by_uid.get(uid, 0) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _pending_condition.wait(timeout=remaining)


def trace_event(
    store,
    *,
    subsystem: str,
    type: str,
    summary: str = "",
    explain: str = "",
    detail: dict[str, Any] | None = None,
    content_excerpt: dict[str, Any] | None = None,
    actor: str = "backend",
    status: str = "ok",
    trace_id: str = "",
    turn_id: str = "",
    job_id: str = "",
    dur_ms: float | None = None,
) -> None:
    """Append one flow event to the per-user ring buffer — no-op unless enabled.

    Best-effort: never raises (debug must not break the request path)."""
    try:
        if not _enabled_fast(store):
            return
        uid = getattr(store, "user_id", "") or ""
        if not uid:
            return
        now = time.time()
        verbose = os.environ.get("FEEDLING_DEBUG_VERBOSE", "").strip().lower() not in ("0", "false", "off", "no")
        event = {
            "ts": now,
            "subsystem": str(subsystem or "")[:40],
            "type": str(type or "")[:80],
            "actor": str(actor or "backend")[:40],
            "status": str(status or "ok")[:20],
            "summary": str(summary or "")[:300],
            "explain": str(explain or "")[:600],
            "trace_id": str(trace_id or "")[:120],
            "turn_id": str(turn_id or "")[:120],
            "job_id": str(job_id or "")[:120],
            "detail": _safe_detail(detail),
        }
        if dur_ms is not None:
            try:
                event["dur_ms"] = round(float(dur_ms), 1)
            except (TypeError, ValueError):
                pass
        if verbose and content_excerpt:
            event["content_excerpt"] = _safe_content_excerpt(content_excerpt)
        _enqueue(uid, event)
    except Exception:
        pass  # observability must never break the actual flow


def read_trace(store, *, limit: int = 200, subsystem: str = "") -> list[dict]:
    uid = getattr(store, "user_id", "") or ""
    if not uid:
        return []
    _flush_pending_for_user(uid)
    buf = db.get_blob(uid, DEBUG_TRACE_BLOB) or {}
    events = buf.get("events") if isinstance(buf, dict) and isinstance(buf.get("events"), list) else []
    if subsystem:
        events = [e for e in events if str(e.get("subsystem") or "") == subsystem]
    events = sorted(events, key=lambda e: float(e.get("ts") or 0), reverse=True)
    return events[: max(1, min(int(limit or 200), _MAX_EVENTS))]


def clear_trace(store) -> None:
    uid = getattr(store, "user_id", "") or ""
    if uid:
        # Share the append path's row ordering + mirror revision so a delayed
        # pre-clear TEE batch cannot resurrect events after the clear lands.
        db.patch_blob_strict(uid, DEBUG_TRACE_BLOB, {"v": 1, "events": []})


def _safe_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow, size-bounded copy. Detail should already be metadata-only (ids/
    counts/reasons); this just bounds it so a careless caller can't bloat the buf."""
    if not isinstance(detail, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in list(detail.items())[:20]:
        key = str(k)[:40]
        if isinstance(v, (int, float, bool)):
            out[key] = v
        elif isinstance(v, str):
            out[key] = v[:200]
        elif isinstance(v, list):
            out[key] = [str(x)[:80] for x in v[:20]]
        elif isinstance(v, dict):
            out[key] = {str(kk)[:40]: (vv if isinstance(vv, (int, float, bool)) else str(vv)[:80]) for kk, vv in list(v.items())[:20]}
        else:
            out[key] = str(v)[:80]
    return out
