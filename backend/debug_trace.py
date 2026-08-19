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

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import db

DEBUG_TRACE_BLOB = "v1_flow_trace"
DEBUG_TRACE_FLAG_BLOB = "v1_flow_trace_enabled"
# Ring depth. Sized against the 48h TTL below: the point of the ring is to still
# hold yesterday's failure when a tester reports it this morning.
#
# 2026-08-10: raised 500→2500 (verbose 200→1000) together with the bulk-decrypt
# coalescing in core/enclave.py. The cap alone was never the real limit — 182 of
# 200 retained events for an active user were per-row `v2_chat_read` decrypts, so
# a single chat turn evicted the ring and the panel showed a 1-second window.
# Noise removal buys ~10x; the depth increase buys the rest of the 48 hours.
#
# Cost: recording is default-on for every user, and the ring is ONE jsonb doc
# rewritten under a row lock per flush — so depth is paid on every write, by
# everyone. At ~400 bytes/event 2500 events is ~1MB worst case, and only for a
# user who actually generates that much in 48h; a typical user stays far below
# the cap and pays nothing extra. Do not raise this much further without moving
# the ring to an append-only table — the rewrite cost is linear in depth.
_MAX_EVENTS = 2500
_MAX_EVENTS_VERBOSE = 1000
_EXCERPT_FIELD_MAX = 2048
_EXCERPT_EVENT_MAX = 8192
_TRUNC_MARK = "…(truncated)"
_TTL_SEC = 48 * 3600  # beta debug retention: enough to inspect yesterday's reports
_FLAG_CACHE_TTL = 30.0
_QUEUE_MAX = 5000
_FLUSH_BATCH_MAX = 100
_FLUSH_WAIT_SEC = 0.25
_STATS_RETRY_SEC = 1.0
_STATS_HEARTBEAT_SEC = db.TRACE_WRITE_STATS_HEARTBEAT_SEC
_STATS_FAILURE_LOG_INTERVAL_SEC = 60.0
_STATS_ZONE = ZoneInfo("Asia/Shanghai")
log = logging.getLogger("feedling.debug_trace")

# subsystem ∈ memory|genesis|identity|route|voice|perception|proactive|fallback|account
# actor     ∈ host_agent_runtime|vps_resident|backend|ios

_flag_cache: dict[str, tuple[bool, float]] = {}
_event_queue: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue(maxsize=_QUEUE_MAX)
_worker_lock = threading.Lock()
_worker_started = False
_dropped_lock = threading.Lock()
_dropped_by_uid: dict[str, int] = {}
_at_risk_by_uid: dict[str, int] = {}

# T138 block 0 rate ruler.  Values are monotonic absolute totals for one
# process-start writer id.  The database uses GREATEST on conflict, so retrying
# after an ambiguous commit cannot double-count.  A fork/restart gets a new
# writer id and a fresh in-memory map; old database rows remain summable.
_stats_lock = threading.Lock()
_stats_pid = -1
_stats_writer_id = ""
_stats_process_started_at = 0.0
_stats_totals: dict[tuple[str, str, str, str], list[int]] = {}
_stats_flushed: dict[tuple[str, str, str, str], tuple[int, ...]] = {}
_stats_first_seen: dict[tuple[str, str, str, str], float] = {}
_stats_flush_failures_total = 0
_stats_flush_consecutive_failures = 0
_stats_last_flush_failure_at = 0.0
_stats_last_flush_success_at = 0.0
_stats_last_flush_error = ""
_stats_last_failure_warning_at = 0.0
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


def _record_at_risk_marker(uid: str, count: int) -> None:
    if not uid:
        return
    with _dropped_lock:
        _at_risk_by_uid[uid] = (
            _at_risk_by_uid.get(uid, 0) + max(1, int(count or 1))
        )


def _take_at_risk_marker(uid: str) -> int:
    if not uid:
        return 0
    with _dropped_lock:
        return int(_at_risk_by_uid.pop(uid, 0) or 0)


def _stats_identity_locked() -> str:
    global _stats_pid, _stats_writer_id, _stats_process_started_at
    global _stats_flush_failures_total, _stats_flush_consecutive_failures
    global _stats_last_flush_failure_at, _stats_last_flush_success_at
    global _stats_last_flush_error, _stats_last_failure_warning_at
    pid = os.getpid()
    if _stats_pid != pid:
        _stats_pid = pid
        _stats_writer_id = f"{pid}:{uuid.uuid4().hex}"
        _stats_process_started_at = time.time()
        _stats_totals.clear()
        _stats_flushed.clear()
        _stats_first_seen.clear()
        _stats_flush_failures_total = 0
        _stats_flush_consecutive_failures = 0
        _stats_last_flush_failure_at = 0.0
        _stats_last_flush_success_at = 0.0
        _stats_last_flush_error = ""
        _stats_last_failure_warning_at = 0.0
    return _stats_writer_id


def _stats_event_key(event: dict[str, Any]) -> tuple[str, str, str, str]:
    try:
        ts = float(event.get("ts") or time.time())
        day = datetime.fromtimestamp(ts, _STATS_ZONE).date().isoformat()
    except (OverflowError, OSError, TypeError, ValueError):
        day = datetime.now(_STATS_ZONE).date().isoformat()
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    lane = str(event.get("lane") or detail.get("lane") or "unknown")[:40]
    return (
        day,
        str(event.get("subsystem") or "unknown")[:40],
        str(event.get("type") or "unknown")[:80],
        lane or "unknown",
    )


def _stats_event_bytes(event: dict[str, Any]) -> int:
    """Deterministic UTF-8 JSON payload size, not PostgreSQL row/index bytes."""
    return len(json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


def _record_trace_stats(
    events: list[dict[str, Any]], *, outcome: str,
) -> None:
    """Accumulate one precision class without ever affecting product flow."""
    if outcome not in {"persisted", "known_drop", "at_risk"}:
        return
    try:
        measured = [
            (_stats_event_key(event), _stats_event_bytes(event), float(event.get("ts") or time.time()))
            for event in events if isinstance(event, dict)
        ]
        with _stats_lock:
            _stats_identity_locked()
            for key, size, seen_at in measured:
                totals = _stats_totals.setdefault(key, [0, 0, 0, 0, 0, 0])
                offset = {"persisted": 0, "known_drop": 2, "at_risk": 4}[outcome]
                totals[offset] += 1
                totals[offset + 1] += size
                _stats_first_seen[key] = min(
                    _stats_first_seen.get(key, seen_at), seen_at,
                )
    except Exception:
        pass


def _flush_trace_stats() -> None:
    """Best-effort flush of changed absolute totals; failures remain dirty."""
    global _stats_flush_failures_total, _stats_flush_consecutive_failures
    global _stats_last_flush_failure_at, _stats_last_flush_success_at
    global _stats_last_flush_error, _stats_last_failure_warning_at
    writer_id = "unknown"
    dirty_rows = 0
    try:
        now = time.time()
        with _stats_lock:
            writer_id = _stats_identity_locked()
            snapshots = {
                key: tuple(values)
                for key, values in _stats_totals.items()
                if tuple(values) != _stats_flushed.get(key)
            }
            dirty_rows = len(snapshots)
            rows = [
                (*key[:1], writer_id, *key[1:], *values, _stats_first_seen[key])
                for key, values in snapshots.items()
            ]
            heartbeat_due = (
                bool(rows)
                or _stats_last_flush_success_at <= 0
                or now - _stats_last_flush_success_at >= _STATS_HEARTBEAT_SEC
            )
            if not heartbeat_due:
                return
            writer_health = (
                writer_id,
                _stats_process_started_at,
                _stats_last_flush_failure_at or None,
                _stats_flush_failures_total,
                _stats_flush_consecutive_failures,
                0,
            )
        db.upsert_trace_write_stats(rows, writer_health=writer_health)
        recovered_failures = 0
        recovered_after = 0.0
        now = time.time()
        with _stats_lock:
            if writer_id != _stats_identity_locked():
                return
            for key, values in snapshots.items():
                _stats_flushed[key] = values
            recovered_failures = _stats_flush_consecutive_failures
            if recovered_failures and _stats_last_flush_failure_at:
                recovered_after = max(0.0, now - _stats_last_flush_failure_at)
            _stats_flush_consecutive_failures = 0
            _stats_last_flush_success_at = now
            _stats_last_flush_error = ""
        if recovered_failures:
            log.info(
                "[debug-trace-stats] flush recovered writer=%s "
                "after_failures=%d recovery_sec=%.1f",
                writer_id,
                recovered_failures,
                recovered_after,
            )
    except Exception as exc:
        now = time.time()
        error_code = type(exc).__name__.lower()
        with _stats_lock:
            _stats_flush_failures_total += 1
            _stats_flush_consecutive_failures += 1
            _stats_last_flush_failure_at = now
            _stats_last_flush_error = error_code
            should_warn = (
                _stats_last_failure_warning_at <= 0
                or now - _stats_last_failure_warning_at
                >= _STATS_FAILURE_LOG_INTERVAL_SEC
            )
            if should_warn:
                _stats_last_failure_warning_at = now
            total = _stats_flush_failures_total
            consecutive = _stats_flush_consecutive_failures
        if should_warn:
            log.warning(
                "[debug-trace-stats] persistent counter flush failed "
                "writer=%s total=%d consecutive=%d dirty_rows=%d code=%s; "
                "absolute totals remain dirty for idempotent retry",
                writer_id,
                total,
                consecutive,
                dirty_rows,
                error_code,
            )


def trace_stats_health() -> dict[str, Any]:
    """Process-local detail; the durable report uses expiring success heartbeats."""
    with _stats_lock:
        writer_id = _stats_identity_locked()
        dirty_rows = sum(
            tuple(values) != _stats_flushed.get(key)
            for key, values in _stats_totals.items()
        )
        return {
            "writer_id": writer_id,
            "dirty_rows": dirty_rows,
            "failures_total": _stats_flush_failures_total,
            "consecutive_failures": _stats_flush_consecutive_failures,
            "last_failure_at": _stats_last_flush_failure_at or None,
            "last_success_at": _stats_last_flush_success_at or None,
            "last_error": _stats_last_flush_error,
            "healthy": _stats_flush_consecutive_failures == 0,
        }


def stop_trace_stats_writer() -> None:
    """Best-effort graceful tombstone; a crash deliberately remains stale."""
    with _stats_lock:
        if _stats_pid != os.getpid() or not _stats_writer_id:
            return
    _flush_trace_stats()
    with _stats_lock:
        writer_id = _stats_writer_id
        dirty_rows = sum(
            tuple(values) != _stats_flushed.get(key)
            for key, values in _stats_totals.items()
        )
        consecutive_failures = _stats_flush_consecutive_failures
    # A tombstone is affirmative evidence that this writer drained cleanly.
    # Never publish it after the fail-open final flush left anything dirty;
    # keeping the row open lets last_success_at expire into the honest stale
    # state instead of hiding an unknown shutdown gap as graceful.
    if dirty_rows or consecutive_failures:
        log.warning(
            "[debug-trace-stats] writer stop remains unstopped "
            "writer=%s dirty_rows=%d consecutive_failures=%d",
            writer_id,
            dirty_rows,
            consecutive_failures,
        )
        return
    try:
        db.stop_trace_write_stats_writer(writer_id)
    except Exception as exc:
        log.warning(
            "[debug-trace-stats] graceful writer stop was not persisted "
            "writer=%s code=%s",
            writer_id,
            type(exc).__name__.lower(),
        )


def _append_events(uid: str, new_events: list[dict[str, Any]]) -> None:
    if not uid or not new_events:
        return
    store = type("_TraceStore", (), {"user_id": uid})()
    try:
        if not is_enabled(store):
            return
        now = time.time()
        verbose = verbose_enabled(store)
    except Exception:
        return
    dropped = _take_dropped(uid)
    at_risk = _take_at_risk_marker(uid)
    markers: list[dict[str, Any]] = []
    if dropped:
        markers.append({
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
        })
    if at_risk:
        markers.append({
            "ts": now,
            "subsystem": "debug_trace",
            "type": "debug_trace.at_risk",
            "actor": "backend",
            "status": "warn",
            "summary": f"{at_risk} debug trace events had unknown storage outcome",
            "explain": (
                "A debug trace flush raised after the batch was handed to storage; "
                "the events may or may not have committed."
            ),
            "trace_id": "",
            "turn_id": "",
            "job_id": "",
            "detail": {"at_risk": at_risk},
        })
    original_count = len(new_events)
    new_events = markers + new_events
    cutoff = now - _TTL_SEC
    cap = _MAX_EVENTS_VERBOSE if verbose else _MAX_EVENTS
    try:
        db.append_blob_events_strict(
            uid,
            DEBUG_TRACE_BLOB,
            new_events,
            cutoff_ts=cutoff,
            max_events=cap,
        )
    except Exception:
        # Restore only the legacy queue-full marker backlog.  The failed flush
        # is outcome-unknown, not another known drop.
        if dropped:
            _record_drop(uid, dropped)
        # Keep the prior unknown count plus this attempt's product events and
        # queue-drop marker (if any); do not recursively count the at-risk
        # marker whose sole purpose is to carry that prior count forward.
        _record_at_risk_marker(
            uid,
            at_risk + original_count + (1 if dropped else 0),
        )
        _record_trace_stats(new_events, outcome="at_risk")
    else:
        _record_trace_stats(new_events, outcome="persisted")


def _flush_batch(batch: list[tuple[str, dict[str, Any]]]) -> None:
    by_uid: dict[str, list[dict[str, Any]]] = {}
    for uid, event in batch:
        by_uid.setdefault(uid, []).append(event)
    for uid, events in by_uid.items():
        _append_events(uid, events)


def _worker_loop(
    event_queue: "queue.Queue[tuple[str, dict[str, Any]]]" | None = None,
) -> None:
    # Bind one queue object for this worker's whole lifetime.  Re-reading the
    # module global each iteration can silently turn a test/reset replacement
    # into a second live input queue, violating the sole-writer contract.
    owned_queue = event_queue if event_queue is not None else _event_queue
    while True:
        batch: list[tuple[str, dict[str, Any]]] = []
        try:
            item = owned_queue.get()
            batch = [item]
            deadline = time.monotonic() + _FLUSH_WAIT_SEC
            while len(batch) < _FLUSH_BATCH_MAX:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout <= 0:
                    break
                try:
                    batch.append(owned_queue.get(timeout=timeout))
                except queue.Empty:
                    break
            try:
                _flush_batch(batch)
            finally:
                # task_done bookkeeping lets _flush_pending_for_user see the
                # worker's in-flight batch via unfinished_tasks — without it a
                # debug read racing this batching window returns stale data.
                for _ in batch:
                    owned_queue.task_done()
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


def _stats_worker_loop() -> None:
    """Retry dirty counters independently of the trace queue's sole writer."""
    while True:
        time.sleep(_STATS_RETRY_SEC)
        _flush_trace_stats()


def _ensure_worker_started() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(
            target=_worker_loop,
            args=(_event_queue,),
            name="debug-trace-flush",
            daemon=True,
        ).start()
        threading.Thread(
            target=_stats_worker_loop,
            name="debug-trace-stats-flush",
            daemon=True,
        ).start()
        _worker_started = True


def start_trace_stats_writer() -> None:
    """Register this service process as a ruler writer via its stats loop."""
    _ensure_worker_started()


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
        _record_trace_stats([event], outcome="known_drop")


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
