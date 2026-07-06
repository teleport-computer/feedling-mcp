"""Memory-capture lane job substrate.

Capture jobs reuse the existing proactive job log/wake/claim primitives, but
they are not proactive reach-out wakes. They must never be gated by ambient /
scheduled / delivery controls.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from core import util
from core.store import UserStore

CAPTURE_JOB_KIND_MEMORY = "memory_capture"
CAPTURE_JOB_SOURCE = "memory_capture"
CAPTURE_JOB_ID_PREFIX = "cap"
CAPTURE_JOB_KIND_DREAM = "memory_dream"
DREAM_JOB_SOURCE = "memory_dream"
DREAM_JOB_ID_PREFIX = "dream"
# Legacy-card migration lane (old card → v1). Same substrate as dream: a quiet-window
# maintenance job, never a reach-out wake. The handler picks the batch at run time.
CAPTURE_JOB_KIND_MIGRATE = "memory_migrate"
MIGRATE_JOB_SOURCE = "memory_migrate"
MIGRATE_JOB_ID_PREFIX = "migr"
CAPTURE_ACTIVE_STATUSES = frozenset({"pending", "claimed", "realizing"})
# Same-key terminal states that should NOT block a fresh enqueue: the window was
# not successfully captured, so re-enqueuing the same window is correct (failed =
# error; skipped = abnormal terminal — noop is reported as completed, not skipped).
CAPTURE_RETRYABLE_TERMINAL = frozenset({"failed", "skipped"})


def failure_backoff_sec(streak: int) -> float:
    """连续失败 ``streak`` 次后，同一 maintenance 窗口多久内不得重新入队。

    没有退避时，永远失败的窗口（典型：坏掉的 BYOK key，agent 调用必败）每个
    调度 tick 都会重建 job——min_interval 只看「上次成功完成」，对纯失败流
    不生效。指数退避 base × 2^(streak-1)，封顶 max；三条 lane
    （capture/dream/migrate）共用。"""
    n = int(streak or 0)
    if n <= 0:
        return 0.0
    try:
        base = float(os.environ.get("FEEDLING_MAINTENANCE_FAIL_BACKOFF_BASE_SEC") or 600.0)
    except (TypeError, ValueError):
        base = 600.0
    try:
        cap = float(os.environ.get("FEEDLING_MAINTENANCE_FAIL_BACKOFF_MAX_SEC") or 21600.0)
    except (TypeError, ValueError):
        cap = 21600.0
    base = max(1.0, base)
    cap = max(base, cap)
    return min(base * (2 ** min(n - 1, 16)), cap)


def in_failure_backoff(streak: int, last_failed_at: float, now_ts: float) -> bool:
    streak = int(streak or 0)
    last = float(last_failed_at or 0.0)
    return streak > 0 and last > 0.0 and (now_ts - last) < failure_backoff_sec(streak)


def is_memory_capture_job(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping):
        return False
    return (
        str(job.get("job_kind") or "").strip() == CAPTURE_JOB_KIND_MEMORY
        or str(job.get("source") or "").strip() == CAPTURE_JOB_SOURCE
    )


def is_memory_dream_job(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping):
        return False
    return (
        str(job.get("job_kind") or "").strip() == CAPTURE_JOB_KIND_DREAM
        or str(job.get("source") or "").strip() == DREAM_JOB_SOURCE
    )


def is_memory_migrate_job(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping):
        return False
    return (
        str(job.get("job_kind") or "").strip() == CAPTURE_JOB_KIND_MIGRATE
        or str(job.get("source") or "").strip() == MIGRATE_JOB_SOURCE
    )


def is_memory_maintenance_job(job: Mapping[str, Any] | None) -> bool:
    return is_memory_capture_job(job) or is_memory_dream_job(job) or is_memory_migrate_job(job)


def _safe_window(window: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = window if isinstance(window, Mapping) else {}
    try:
        until_ts = float(raw.get("until_ts") or 0)
    except (TypeError, ValueError):
        until_ts = 0.0
    try:
        message_count = int(raw.get("message_count") or 0)
    except (TypeError, ValueError):
        message_count = 0
    return {
        "after_message_id": str(raw.get("after_message_id") or "")[:160],
        "until_message_id": str(raw.get("until_message_id") or "")[:160],
        "until_ts": until_ts,
        "message_count": max(0, message_count),
    }


def _active_capture_job(job: Mapping[str, Any]) -> bool:
    return is_memory_capture_job(job) and str(job.get("status") or "pending").strip().lower() in CAPTURE_ACTIVE_STATUSES


def _active_dream_job(job: Mapping[str, Any]) -> bool:
    return is_memory_dream_job(job) and str(job.get("status") or "pending").strip().lower() in CAPTURE_ACTIVE_STATUSES


def _find_capture_by_key(store: UserStore, capture_key: str) -> dict | None:
    matches = [
        dict(job)
        for job in store.list_proactive_jobs(since_epoch=0, limit=0)
        if is_memory_capture_job(job) and str(job.get("capture_key") or "") == capture_key
    ]
    if not matches:
        return None
    # Latest wins: after a failed-window retry there can be several same-key jobs;
    # the newest reflects the current state (active retry vs. still-failed) so we
    # don't keep matching a stale failed job and pile up duplicates.
    return max(matches, key=lambda j: float(j.get("ts") or 0))


def _find_dream_by_key(store: UserStore, dream_key: str) -> dict | None:
    matches = [
        dict(job)
        for job in store.list_proactive_jobs(since_epoch=0, limit=0)
        if is_memory_dream_job(job) and str(job.get("dream_key") or "") == dream_key
    ]
    if not matches:
        return None
    # Latest wins (see _find_capture_by_key): a failed-then-retried dream can leave
    # several same-key jobs; the newest reflects the current state.
    return max(matches, key=lambda j: float(j.get("ts") or 0))


def _find_active_capture(store: UserStore) -> dict | None:
    for job in store.list_proactive_jobs(since_epoch=0, limit=0):
        if _active_capture_job(job):
            return dict(job)
    return None


def _find_active_dream(store: UserStore) -> dict | None:
    for job in store.list_proactive_jobs(since_epoch=0, limit=0):
        if _active_dream_job(job):
            return dict(job)
    return None


def _active_migrate_job(job: Mapping[str, Any]) -> bool:
    return is_memory_migrate_job(job) and str(job.get("status") or "pending").strip().lower() in CAPTURE_ACTIVE_STATUSES


def _find_migrate_by_key(store: UserStore, migrate_key: str) -> dict | None:
    matches = [
        dict(job)
        for job in store.list_proactive_jobs(since_epoch=0, limit=0)
        if is_memory_migrate_job(job) and str(job.get("migrate_key") or "") == migrate_key
    ]
    if not matches:
        return None
    return max(matches, key=lambda j: float(j.get("ts") or 0))


def _migrate_same_key_blocks_retry(job: Mapping[str, Any]) -> bool:
    """Whether a same-window migrate job should suppress another enqueue.

    Migration can legitimately need another job in the same quiet window: a prior
    no-op may have raced card seeding, and a finished batch can still leave legacy
    cards. Only active jobs and terminal jobs that settled the window should block.
    """
    status = str(job.get("status") or "pending").strip().lower()
    if status in CAPTURE_ACTIVE_STATUSES:
        return True
    if status in CAPTURE_RETRYABLE_TERMINAL:
        return False
    if status != "completed":
        return True
    reason = str(job.get("status_reason") or "").strip().lower()
    result = job.get("migrate_result") if isinstance(job.get("migrate_result"), Mapping) else {}
    if reason == "migrate_no_legacy" or str(result.get("reason") or "").strip().lower() == "no_legacy":
        return False
    try:
        remaining = int(result.get("remaining"))
    except (TypeError, ValueError):
        remaining = 0
    return remaining <= 0


def _find_active_migrate(store: UserStore) -> dict | None:
    for job in store.list_proactive_jobs(since_epoch=0, limit=0):
        if _active_migrate_job(job):
            return dict(job)
    return None


def make_memory_capture_job(
    *,
    trigger: str,
    capture_key: str,
    window: Mapping[str, Any] | None,
    not_before: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    not_before_ts = now_ts if not_before is None else float(not_before)
    return {
        "job_id": util._new_public_id(CAPTURE_JOB_ID_PREFIX),
        "job_kind": CAPTURE_JOB_KIND_MEMORY,
        "source": CAPTURE_JOB_SOURCE,
        "status": "pending",
        "trigger": str(trigger or "session_break")[:120],
        "capture_key": str(capture_key or "")[:240],
        "window": _safe_window(window),
        "not_before": not_before_ts,
        "ts": now_ts,
        "created_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def make_memory_dream_job(
    *,
    trigger: str,
    dream_key: str,
    dream_until: Mapping[str, Any] | None = None,
    dream_stats: Mapping[str, Any] | None = None,
    not_before: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    not_before_ts = now_ts if not_before is None else float(not_before)
    return {
        "job_id": util._new_public_id(DREAM_JOB_ID_PREFIX),
        "job_kind": CAPTURE_JOB_KIND_DREAM,
        "source": DREAM_JOB_SOURCE,
        "status": "pending",
        "trigger": str(trigger or "nightly_dream")[:120],
        "dream_key": str(dream_key or "")[:240],
        "dream_until": dict(dream_until or {}),
        "dream_stats": dict(dream_stats or {}),
        "not_before": not_before_ts,
        "ts": now_ts,
        "created_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def enqueue_memory_capture_job(
    store: UserStore,
    *,
    trigger: str,
    capture_key: str,
    window: Mapping[str, Any] | None,
    not_before: float | None = None,
    now: float | None = None,
) -> tuple[dict | None, bool, str]:
    """Enqueue one memory-capture job if no equivalent/active job exists.

    Returns (job, enqueued, reason). Existing jobs are returned for idempotency
    or single-flight visibility, but are not appended again.
    """
    key = str(capture_key or "").strip()
    if not key:
        return None, False, "capture_key_required"
    prior = _find_capture_by_key(store, key)
    if prior is not None:
        status = str(prior.get("status") or "pending").strip().lower()
        if status not in CAPTURE_RETRYABLE_TERMINAL:
            # Same key, still in flight (single-flight) OR already completed
            # (window done) → don't enqueue a duplicate.
            return prior, False, "duplicate_capture_key"
        # Same key but failed/skipped: that window was never captured. Fall
        # through to re-enqueue a fresh job for it (subject to single-flight).
    active = _find_active_capture(store)
    if active is not None:
        return active, False, "capture_already_pending"
    job = make_memory_capture_job(
        trigger=trigger,
        capture_key=key,
        window=window,
        not_before=not_before,
        now=now,
    )
    created = store.append_proactive_job(job)
    import debug_trace  # local import avoids load-order cycle

    job_id = str((created or job or {}).get("job_id") or "")
    debug_trace.trace_event(
        store,
        subsystem="memory",
        type="memory.capture.queued",
        actor="backend",
        job_id=job_id,
        summary="memory capture job enqueued",
        explain=f"已排队一次记忆抓取（job {job_id}）",
    )
    return created, True, "enqueued"


def enqueue_memory_dream_job(
    store: UserStore,
    *,
    trigger: str,
    dream_key: str,
    dream_until: Mapping[str, Any] | None = None,
    dream_stats: Mapping[str, Any] | None = None,
    not_before: float | None = None,
    now: float | None = None,
) -> tuple[dict | None, bool, str]:
    """Enqueue one memory-dream job if no equivalent/active dream job exists."""
    key = str(dream_key or "").strip()
    if not key:
        return None, False, "dream_key_required"
    prior = _find_dream_by_key(store, key)
    if prior is not None:
        status = str(prior.get("status") or "pending").strip().lower()
        if status not in CAPTURE_RETRYABLE_TERMINAL:
            # same key still in flight (single-flight) or completed (done) → no dup
            return prior, False, "duplicate_dream_key"
        # same key failed/skipped → that dream never finished; allow a retry
    active = _find_active_dream(store)
    if active is not None:
        return active, False, "dream_already_pending"
    job = make_memory_dream_job(
        trigger=trigger,
        dream_key=key,
        dream_until=dream_until,
        dream_stats=dream_stats,
        not_before=not_before,
        now=now,
    )
    return store.append_proactive_job(job), True, "enqueued"


def make_memory_migrate_job(
    *,
    trigger: str,
    migrate_key: str,
    migrate_stats: Mapping[str, Any] | None = None,
    not_before: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    not_before_ts = now_ts if not_before is None else float(not_before)
    return {
        "job_id": util._new_public_id(MIGRATE_JOB_ID_PREFIX),
        "job_kind": CAPTURE_JOB_KIND_MIGRATE,
        "source": MIGRATE_JOB_SOURCE,
        "status": "pending",
        "trigger": str(trigger or "quiet_window_migrate")[:120],
        "migrate_key": str(migrate_key or "")[:240],
        "migrate_stats": dict(migrate_stats or {}),
        "not_before": not_before_ts,
        "ts": now_ts,
        "created_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def enqueue_memory_migrate_job(
    store: UserStore,
    *,
    trigger: str,
    migrate_key: str,
    migrate_stats: Mapping[str, Any] | None = None,
    not_before: float | None = None,
    now: float | None = None,
) -> tuple[dict | None, bool, str]:
    """Enqueue one legacy→v1 migration batch job if none equivalent/active exists.

    Single-flight per user (one active migrate at a time) so batches run serially
    and never race each other; the handler picks the next batch of legacy cards at
    run time. Idempotent by migrate_key (e.g. the quiet-window day/window id)."""
    from memory import migration as _migration  # local import avoids load-order cycle
    if not _migration.migration_enabled():
        return None, False, "migration_disabled"
    key = str(migrate_key or "").strip()
    if not key:
        return None, False, "migrate_key_required"
    existing_same_key = _find_migrate_by_key(store, key)
    if existing_same_key is not None and _migrate_same_key_blocks_retry(existing_same_key):
        return existing_same_key, False, "duplicate_migrate_key"
    # Plan §2: migration must not run alongside capture/dream (shared memory_lock +
    # overlapping read→derive→write windows). Block at enqueue on ANY active
    # maintenance job, not just another migrate — simplest, single source of truth.
    active = _find_active_capture(store) or _find_active_dream(store) or _find_active_migrate(store)
    if active is not None:
        return active, False, "maintenance_already_pending"
    job = make_memory_migrate_job(
        trigger=trigger,
        migrate_key=key,
        migrate_stats=migrate_stats,
        not_before=not_before,
        now=now,
    )
    return store.append_proactive_job(job), True, "enqueued"
