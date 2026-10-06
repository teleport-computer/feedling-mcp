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
CAPTURE_ACTIVE_STATUSES = frozenset({"pending", "claimed", "realizing"})
# Same-key terminal states that should NOT block a fresh enqueue: the window was
# not successfully captured, so re-enqueuing the same window is correct (failed =
# error; skipped = abnormal terminal — noop is reported as completed, not skipped).
CAPTURE_RETRYABLE_TERMINAL = frozenset({"failed", "skipped"})


def failure_backoff_sec(streak: int) -> float:
    """连续失败 ``streak`` 次后，同一 maintenance 窗口多久内不得重新入队。

    没有退避时，永远失败的窗口（典型：坏掉的 BYOK key，agent 调用必败）每个
    调度 tick 都会重建 job——min_interval 只看「上次成功完成」，对纯失败流
    不生效。指数退避 base × 2^(streak-1)，封顶 max；两条 lane
    （capture/dream）共用。"""
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


_BACKOFF_NOTICE_STREAK = 3   # 前两次退避噪音价值低，第 3 次才打扰用户


def notify_backoff(store, *, lane: str, status: str, streak: int,
                   account_code: str = "", skipped: bool = False) -> None:
    """两条 maintenance lane（capture/dream）共用的退避通知钩子。

    streak>=3 的失败 emit warning（occurrences 天然吸收后续 +1，不刷屏）；
    completed 恢复 resolve（同 lane 精确 dedupe_key，不跨 lane 清）。两支
    互斥（if/elif）——同一次调用绝不会既 emit 又把刚发的 resolve 掉。绝不
    影响原 streak/状态流程（notices.emit/resolve 内部已自吞异常）。"""
    from notices import core as notices
    from notices import catalog
    if status == "completed" or skipped:
        # 跳过一批后游标已经推进、后面继续整理 —— 旧的「受阻/会补记」提示已经不成立，
        # 留着会一直显示「修好后会补记」，而那批其实已经丢了（Codex 第 6 轮）。
        notices.resolve(store, f"memory_backoff:{lane}")
    elif status == "failed" and int(streak or 0) >= _BACKOFF_NOTICE_STREAK:
        # 失败原因是用户自己的账号/服务（余额不足、密钥失效、登录过期…）时，提示里直接说原因：
        # 只写「连续失败 N 次」用户不知道要去充值，记忆就一直停着（2026-09-13 prod：
        # 触发过逃生阀的 42 人里 33 人是账号问题）。文案取统一错误对照表，和聊天报错一致。
        if account_code == "provider_setup":
            # 模型服务还没配好（未配置/未测试/配置无效）：对照表里没有这一条，单独写。
            notices.emit(store, source="memory", error_class="memory_backoff",
                         blame="user_provider", severity="warning",
                         user_text=("记忆整理暂停了：模型服务还没有配置好或没通过测试，"
                                    "请到设置里完成模型配置。配好后会自动继续整理。"),
                         detail=f"lane={lane} streak={streak} cause={account_code}",
                         dedupe_key=f"memory_backoff:{lane}")
            return
        spec = None
        if account_code:
            from notices import error_contract
            spec = error_contract.spec_for(account_code, public_only=False)
        if spec is not None:
            notices.emit(store, source="memory", error_class="memory_backoff",
                         blame=spec.blame, severity="warning",
                         user_text=(f"记忆整理暂停了：{spec.safe_text_zh}"
                                    "修好后会自动继续整理；"
                                    "积压太多或超过 7 天仍未恢复时，较早的聊天可能无法补记。"),
                         detail=f"lane={lane} streak={streak} cause={account_code}",
                         dedupe_key=f"memory_backoff:{lane}")
        else:
            notices.emit(store, source="memory", error_class="memory_backoff",
                         blame=catalog.blame_for("memory_backoff"), severity="warning",
                         user_text=f"记忆整理（{lane}）连续失败 {streak} 次，正在退避重试。",
                         detail=f"lane={lane} streak={streak}",
                         dedupe_key=f"memory_backoff:{lane}")


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


def is_memory_maintenance_job(job: Mapping[str, Any] | None) -> bool:
    return is_memory_capture_job(job) or is_memory_dream_job(job)


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
    window = {
        "after_message_id": str(raw.get("after_message_id") or "")[:160],
        "until_message_id": str(raw.get("until_message_id") or "")[:160],
        "until_ts": until_ts,
        "message_count": max(0, message_count),
    }
    # 🔴 起点 seq 必须跟着任务走，**包括 0**。首次落卡的用户没有 after_message_id，
    # 丢了 after_seq 的话逃生阀只能按「终点」认窗口 —— 新消息一来终点就变，
    # 失败次数永远重数、永远到不了阈值（Codex 第 10 轮）。
    if raw.get("after_seq") is not None and raw.get("after_seq") != "":
        try:
            window["after_seq"] = max(0, int(float(raw.get("after_seq"))))
        except (TypeError, ValueError):
            pass
    # Resident V1 按批次落卡：终点 seq 是这批的精确边界。consumer 靠它按 seq 取批，
    # 完成时游标只推到这里（capture_scheduler._v1_oldest_batch_window）。
    # 老窗口没有这个键，consumer 据此走老的取窗逻辑。
    if raw.get("through_seq") is not None and raw.get("through_seq") != "":
        try:
            through_seq = int(float(raw.get("through_seq")))
        except (TypeError, ValueError):
            through_seq = 0
        if through_seq > 0:
            window["through_seq"] = through_seq
            window["backlog_remaining"] = bool(raw.get("backlog_remaining"))
    return window


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
