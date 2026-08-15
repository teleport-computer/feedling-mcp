"""DB access for V2 jobs, status events, summaries, schedules, and metrics.

CONTRIBUTING §2：新表存取逻辑全部收进本模块（jobs_store）。连接走 db.get_pool()
（autocommit）；需要跨语句持行锁的地方（SKIP LOCKED claim / single-flight 选举）
用显式 conn.transaction()。行返回 dict 用 psycopg.rows.dict_row 游标。
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Literal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import db
from core import wake_bus
from model_api_runtime.v2 import usage_reporting
from notices import catalog as notices_catalog
from proactive import capture_daily

log = logging.getLogger("feedling.runtime_v2.jobs_store")

_USAGE_REPORT_GATE = threading.BoundedSemaphore(1)
_USAGE_REPORT_ADVISORY_KEY = 0x4656325553410002
_USAGE_REPORT_POOL_TIMEOUT_SECONDS = 0.5
_USAGE_REPORT_STATEMENT_TIMEOUT_MS = 15_000


class _UsageReportAdmissionBusy(RuntimeError):
    pass


def _usage_snapshot_observer(_event: str, **_fields) -> None:
    """No-op test hook; events never contain user/content data."""


@contextmanager
def _usage_report_admission():
    """Hold process and RDS admission until the whole report is finished."""

    if not _USAGE_REPORT_GATE.acquire(blocking=False):
        raise _UsageReportAdmissionBusy("usage process admission busy")
    try:
        with _usage_pool_connection() as conn:
            if not conn.autocommit:
                conn.autocommit = True
            locked = bool(
                conn.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    (_USAGE_REPORT_ADVISORY_KEY,),
                ).fetchone()[0]
            )
            if not locked:
                raise _UsageReportAdmissionBusy("usage RDS admission busy")
            try:
                yield conn
            finally:
                try:
                    unlocked = bool(
                        conn.execute(
                            "SELECT pg_advisory_unlock(%s)",
                            (_USAGE_REPORT_ADVISORY_KEY,),
                        ).fetchone()[0]
                    )
                    if not unlocked:
                        conn.close()
                except Exception:
                    conn.close()
    finally:
        _USAGE_REPORT_GATE.release()

LANES = {
    "chat",
    "manual_wake",
    "heartbeat",
    "scheduled",
    "capture",
    "maintenance",
    "dream",
    "profile",
    "screen_watch",
    "trajectory_review",
}


def _positive_float_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be finite and > 0") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be finite and > 0")
    return value


# 默认（lane 派生）优先级：预留槽位场景下 chat/manual_wake 必须能在一堆 heartbeat/
# capture 前面被抢到，防止后台唤醒风暴饿死聊天回复。enqueue_job 未显式传 priority
# 时按 lane 落这个值；调用方显式传 priority=<int> 仍原样生效（不被这里覆盖）。
LANE_PRIORITY = {
    "chat": 100,
    "manual_wake": 100,
    "heartbeat": 50,
    "scheduled": 50,
    "screen_watch": 50,
    "capture": 10,
    "maintenance": 10,
    "dream": 10,
    "profile": 10,
    # Offline analysis must never contend with foreground chat/wake or memory
    # maintenance. One generic job drains one encrypted failed-turn review.
    "trajectory_review": 1,
}


@dataclass(frozen=True)
class PreemptedJob:
    job_id: int
    user_id: str
    lane: str
    claimed_by: str | None
    recovery: Literal["terminal", "requeued"]
# Chat admission and execution use separate columns and clocks. Pending rows
# have a short queue deadline so an admitted turn cannot wait forever when the
# fleet dies. Claim starts a distinct owner-fenced execution lease. Workers
# renew only at explicit progress boundaries; a provider call that is itself
# wedged therefore cannot keep a blind heartbeat alive forever.
PENDING_CHAT_TTL_SEC = _positive_float_env("FEEDLING_V2_CHAT_PENDING_TTL_SEC", "120")
RUNNING_TTL_SEC = _positive_float_env("FEEDLING_V2_LEASE_TTL_SEC", "300")

_ACTIVE_STATUSES = ("pending", "claimed", "running")
SCHEDULED_WAKE_STREAM = "proactive_scheduled_wakes_v2"
_TERMINAL_FAILURE_FALLBACK_REPLY = (
    os.environ.get(
        "FALLBACK_REPLY",
        "我这会儿有点慢，刚刚没接上。你稍后再发一次，我会继续接。",
    ).strip()
    or "我这会儿有点慢，刚刚没接上。你稍后再发一次，我会继续接。"
)

# Migration 0041's database trigger rejects pending->claimed transitions from
# pre-0041 workers. This transaction-local protocol marker is deliberately set
# only by the current claim path; it is not authentication or persistent state.
_WORKER_CLAIM_PROTOCOL = "0041"

_TERMINAL_ERROR_CODE_RE = re.compile(r"^[a-z0-9_:-]{1,120}$")


def _terminal_error_code(error: object) -> str:
    """Return the only form allowed to cross the user-visible failure outbox.

    Worker call sites normally pass stable codes already.  This final boundary
    prevents a future/legacy caller from copying an exception message into
    ``last_runtime_error`` through the reconciler.
    """
    value = str(error or "")
    return value if _TERMINAL_ERROR_CODE_RE.fullmatch(value) else "runtime_failed"


def _terminal_error_class(error: object, error_class: object = "") -> str:
    candidate = str(error_class or "").strip()
    if candidate in notices_catalog.ERROR_CLASSES:
        return candidate
    code = _terminal_error_code(error)
    if code == "queue_timeout":
        return "platform_queue_timeout"
    if code in {"slot_watchdog_timeout", "lease_timeout", "runtime_expired"}:
        return "platform_execution_timeout"
    if code in {"provider_timeout", "provider_transport_timeout"}:
        return "provider_timeout"
    if "prompt_frontier_exhausted" in code:
        return "context_overflow"
    if code.endswith(":empty_reply"):
        return "provider_empty_reply"
    return "unknown"


def _pool():
    return db.get_pool()


def _queue_terminal_failure_on_cursor(
    cur,
    job_id,
    user_id: str,
    error: object,
    *,
    error_class: object = "",
) -> bool:
    """Capture one user-visible turn failure in its terminal transaction.

    Foreground chat keeps its parent/frontier so the existing reply sink can
    close that exact turn. A scheduled wake has no user-authored parent; its
    marker deliberately leaves those columns null and the reply sink emits a
    standalone, explicitly labelled reminder-failure message instead.
    """
    cur.execute(
        "INSERT INTO v2_terminal_failure_outbox "
        "(job_id,user_id,error_code,error_class,"
        " target_route_id,target_route_updated_at,"
        " reply_frontier_seq,reply_parent_message_id) "
        "SELECT j.id,j.user_id,%s,%s,r.id,r.updated_at,"
        " CASE WHEN j.lane='chat' THEN input.seq END,"
        " CASE WHEN j.lane='chat' THEN input.msg_id END FROM agent_jobs j "
        "LEFT JOIN LATERAL (SELECT id,updated_at FROM model_api_routes "
        "  WHERE user_id=j.user_id AND is_active LIMIT 1) r ON TRUE "
        "LEFT JOIN LATERAL (SELECT seq,msg_id FROM chat_messages "
        "  WHERE user_id=j.user_id AND doc->>'role' IN ('user','human') "
        "  AND COALESCE(doc->>'source','') "
        "    NOT IN ('verify_ping','resident_maintenance') "
        "  ORDER BY seq DESC LIMIT 1) input ON j.lane='chat' "
        "WHERE j.id=%s AND j.user_id=%s "
        "AND j.lane IN ('chat','scheduled') "
        "ON CONFLICT (job_id) DO NOTHING",
        (
            _terminal_error_code(error),
            _terminal_error_class(error, error_class),
            job_id,
            str(user_id),
        ),
    )
    return cur.rowcount == 1


_TRAJECTORY_REVIEW_LANE = "trajectory_review"
_TRAJECTORY_REVIEW_MAX_ATTEMPTS = 3
_TRAJECTORY_REVIEW_ENABLED_ENV = "FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED"
_TRAJECTORY_REVIEW_MAX_ACTIVE_ENV = "FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE"
_TRAJECTORY_REVIEW_DEFAULT_MAX_ACTIVE = 64
# A database-wide transaction advisory lock makes the active-review count and
# insert/reopen one admission decision across every worker process.  This is a
# namespace-local constant, not a secret or an ownership fence.
_TRAJECTORY_REVIEW_ADMISSION_LOCK = 0x46563254524A0001
_TRAJECTORY_EVENT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TRAJECTORY_IDEMPOTENCY_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,96}$")
_TRAJECTORY_ACCESS_OPERATOR_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._@:-]{2,79}$"
)
_TRAJECTORY_ACCESS_CASE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{2,119}$"
)
_TRAJECTORY_ACCESS_RESULT_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_TRAJECTORY_ACCESS_REASONS = frozenset(
    {"incident", "support", "security", "debug"}
)
_TRAJECTORY_ENVELOPE_REQUIRED = frozenset(
    {
        "v",
        "id",
        "owner_user_id",
        "visibility",
        "body_ct",
        "nonce",
        "K_user",
        "K_enclave",
    }
)
_TRAJECTORY_ENVELOPE_ALLOWED = frozenset(
    {
        "v",
        "id",
        "owner_user_id",
        "visibility",
        "body_ct",
        "nonce",
        "K_user",
        "K_enclave",
        "enclave_pk_fpr",
        "content_pk_fpr",
    }
)


def trajectory_review_admission_cap() -> int:
    """Return the explicit fleet review ceiling, or zero on bad config.

    Review is a BYOK provider call and must fail closed without jeopardizing
    source-job terminalization.  Parsing therefore happens at the decision
    boundary instead of module import: malformed live configuration disables
    review; it never rolls back a user's terminal failure transaction.
    """
    raw = os.environ.get(
        _TRAJECTORY_REVIEW_MAX_ACTIVE_ENV,
        str(_TRAJECTORY_REVIEW_DEFAULT_MAX_ACTIVE),
    )
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return value if 1 <= value <= 10_000 else 0


def trajectory_review_enabled() -> bool:
    """Whether provider-backed failure review is explicitly safe to execute."""
    enabled = os.environ.get(_TRAJECTORY_REVIEW_ENABLED_ENV, "0").strip().lower()
    return enabled in {"1", "true", "yes", "on"} and bool(
        trajectory_review_admission_cap()
    )


def _review_admission_available_on_cursor(cur) -> bool:
    """Serialize and enforce the durable global pending+running ceiling."""
    if not trajectory_review_enabled():
        return False
    cap = trajectory_review_admission_cap()
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (_TRAJECTORY_REVIEW_ADMISSION_LOCK,),
    )
    cur.execute(
        "SELECT COUNT(*)::int AS active_count FROM v2_trajectory_reviews "
        "WHERE status IN ('pending','running')"
    )
    row = cur.fetchone()
    active = int(row["active_count"] if isinstance(row, dict) else row[0])
    return active < cap


def _validate_trajectory_envelope(user_id: str, envelope: object) -> dict:
    if not isinstance(envelope, dict):
        raise ValueError("trajectory payload envelope must be an object")
    if not _TRAJECTORY_ENVELOPE_REQUIRED.issubset(envelope):
        raise ValueError("trajectory payload envelope is incomplete")
    if set(envelope) - _TRAJECTORY_ENVELOPE_ALLOWED:
        raise ValueError("trajectory payload envelope has unsupported fields")
    if type(envelope.get("v")) is not int:
        raise ValueError("trajectory payload envelope version must be an integer")
    if envelope.get("owner_user_id") != str(user_id):
        raise ValueError("trajectory payload envelope owner mismatch")
    if envelope.get("visibility") != "shared":
        raise ValueError("trajectory payload envelope must be shared")
    for field in ("id", "body_ct", "nonce", "K_user", "K_enclave"):
        if not isinstance(envelope.get(field), str) or not envelope[field]:
            raise ValueError(f"trajectory payload envelope {field} required")
    return envelope


def _ensure_review_runner_on_cursor(cur, user_id: str) -> bool:
    """Ensure one low-priority generic runner exists for this user's backlog.

    The source review row carries the source-job identity.  The generic
    ``agent_jobs`` row only gives the existing worker pool a separately
    schedulable lane, so its ordinary per-user/lane single-flight semantics are
    desirable here.
    """
    if not trajectory_review_enabled():
        return False
    cur.execute(
        "INSERT INTO agent_jobs "
        "(user_id,lane,status,reason,priority,expected_runtime_generation) "
        "SELECT %s,%s,'pending','terminal_failure_review',%s,s.runtime_generation "
        "FROM v2_runtime_state s "
        "WHERE s.user_id=%s AND s.hosted_runtime_state='v2' "
        "AND EXISTS (SELECT 1 FROM v2_trajectory_reviews r "
        "            WHERE r.user_id=%s AND r.status='pending') "
        "AND NOT EXISTS (SELECT 1 FROM agent_jobs j "
        "                WHERE j.user_id=%s AND j.lane=%s "
        "                  AND j.status IN ('pending','claimed','running')) "
        "ON CONFLICT DO NOTHING",
        (
            str(user_id),
            _TRAJECTORY_REVIEW_LANE,
            int(LANE_PRIORITY[_TRAJECTORY_REVIEW_LANE]),
            str(user_id),
            str(user_id),
            str(user_id),
            _TRAJECTORY_REVIEW_LANE,
        ),
    )
    return cur.rowcount == 1


def _queue_failure_review_on_cursor(cur, source_job_id: int | str) -> bool:
    """Create a review request in the caller's terminal-state transaction."""
    # Materialize the stream even when capture produced no events. Review
    # completion locks this row as its frontier fence, while later appenders
    # lock the same row before reopening an already-completed review.
    cur.execute(
        "INSERT INTO v2_trajectory_streams (job_id,user_id) "
        "SELECT id,user_id FROM agent_jobs WHERE id=%s "
        "ON CONFLICT (job_id) DO NOTHING",
        (source_job_id,),
    )
    if not _review_admission_available_on_cursor(cur):
        return False
    cur.execute(
        "INSERT INTO v2_trajectory_reviews (source_job_id,user_id,status) "
        "SELECT j.id,j.user_id,'pending' FROM agent_jobs j "
        "JOIN v2_runtime_state s ON s.user_id=j.user_id "
        "WHERE j.id=%s AND j.status IN ('failed','expired') AND j.lane<>%s "
        "AND s.hosted_runtime_state='v2' "
        "ON CONFLICT (source_job_id) DO NOTHING RETURNING user_id",
        (source_job_id, _TRAJECTORY_REVIEW_LANE),
    )
    row = cur.fetchone()
    if row is not None:
        user_id = row["user_id"] if isinstance(row, dict) else row[0]
        _ensure_review_runner_on_cursor(cur, str(user_id))
        return True
    return False


def _recover_review_runner_on_cursor(
    cur, runner_job_id: int | str,
) -> list[tuple[str, str]]:
    """Release a review claim whose generic runner terminalized unexpectedly.

    Returns the ``[(user_id, source_job_id), ...]`` pairs this UPDATE actually
    touched. The caller MUST mark_pending these for TEE requeue itself, and
    MUST NOT do so until its own outer ``with _pool().connection()`` block has
    exited (i.e. the transaction containing this UPDATE has committed).

    Marking from inside this shared cursor, before that commit, would be a
    genuine race, not just a hypothetical one: mirror.mark_pending writes
    through an independent autocommit connection to the TEE shadow DB, so the
    marker can land and be *consumed* before this transaction commits. The
    requeue consumer's fetch is a plain SELECT with no FOR UPDATE, so under
    READ COMMITTED it does not block on this transaction's row lock — it
    simply reads whatever was last committed, i.e. the pre-UPDATE row — and,
    finding a row, deletes the just-created pending marker as "done". When
    this transaction then commits moments later, there is no marker left to
    trigger a re-fetch, so the real state change (running -> pending/failed)
    never reaches TEE. That is silent and permanent, and worse than never
    marking at all: requeue_backlog even looks healthy (the marker was
    "consumed"), with no error anywhere. This is exactly the class of bug
    this task exists to close, reintroduced via a different trigger path.
    """
    cur.execute(
        "UPDATE v2_trajectory_reviews r SET "
        "status=CASE WHEN r.attempt_count<%s THEN 'pending' ELSE 'failed' END, "
        "claimed_by_job_id=NULL, "
        "last_error=CASE WHEN r.attempt_count<%s THEN r.last_error "
        "                ELSE COALESCE(r.last_error,'review_runner_failed') END, "
        "finished_at=CASE WHEN r.attempt_count<%s THEN NULL ELSE now() END "
        "FROM agent_jobs j WHERE j.id=%s AND j.lane=%s "
        "AND r.claimed_by_job_id=j.id AND r.status='running' "
        "RETURNING r.user_id, r.source_job_id",
        (
            _TRAJECTORY_REVIEW_MAX_ATTEMPTS,
            _TRAJECTORY_REVIEW_MAX_ATTEMPTS,
            _TRAJECTORY_REVIEW_MAX_ATTEMPTS,
            runner_job_id,
            _TRAJECTORY_REVIEW_LANE,
        ),
    )
    rows = cur.fetchall()
    recovered: list[tuple[str, str]] = []
    for row in rows:
        user_id = row["user_id"] if isinstance(row, dict) else row[0]
        source_job_id = row["source_job_id"] if isinstance(row, dict) else row[1]
        _ensure_review_runner_on_cursor(cur, str(user_id))
        recovered.append((str(user_id), str(source_job_id)))
    return recovered


def reconcile_failure_review_runners(*, limit: int = 64) -> int:
    """Recreate missing generic runners for durable pending reviews.

    The review enable flag is an operational kill switch. A runner fenced while
    it is off returns its claimed review to ``pending`` but intentionally cannot
    create a successor at that moment. This bounded parent-process sweep closes
    the other half of that contract after re-enable.

    The active-job partial unique index on ``(user_id, lane)`` and
    ``ON CONFLICT DO NOTHING`` make concurrent fleet reconcilers idempotent. A
    tick examines at most ``limit`` users and creates at most one runner per
    pending-review user. Default-off/invalid review configuration returns before
    acquiring a database connection.
    """
    if type(limit) is not int or not 1 <= limit <= 1_000:
        raise ValueError("failure review reconcile limit must be 1..1000")
    if not trajectory_review_enabled():
        return 0
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT r.user_id FROM v2_trajectory_reviews r "
                    "JOIN v2_runtime_state s ON s.user_id=r.user_id "
                    "WHERE r.status='pending' AND r.attempt_count<%s "
                    "AND s.hosted_runtime_state='v2' "
                    "AND NOT EXISTS (SELECT 1 FROM agent_jobs j "
                    "  WHERE j.user_id=r.user_id AND j.lane=%s "
                    "  AND j.status IN ('pending','claimed','running')) "
                    "GROUP BY r.user_id ORDER BY MIN(r.created_at),r.user_id "
                    "LIMIT %s",
                    (_TRAJECTORY_REVIEW_MAX_ATTEMPTS, _TRAJECTORY_REVIEW_LANE, limit),
                )
                users = [str(row["user_id"]) for row in cur.fetchall()]
                return sum(
                    bool(_ensure_review_runner_on_cursor(cur, user_id))
                    for user_id in users
                )


def coalesce_or_insert_on_cursor(
    cur,
    user_id,
    lane,
    *,
    reason=None,
    trace_id=None,
    priority,
    deadline_at=None,
    expected_generation: int | None = None,
) -> tuple[int, bool]:
    """核心 coalesce-or-insert 逻辑，运行在调用方已开的事务/游标上（必须是
    ``dict_row`` 游标——SQL 用 ``existing["stale"]``/``existing["id"]``）。抽成
    模块级函数，好让 db.chat_append_and_enqueue（A7 原子发送+入队）在自己的
    事务里复用同一段逻辑，而不是重开一个连接。语义与原 enqueue_job 内嵌闭包
    完全一致，见该函数 docstring。"""
    cur.execute(
        "SELECT id,status,expected_runtime_generation "
        "FROM agent_jobs "
        "WHERE user_id=%s AND lane=%s "
        "AND status IN ('pending','claimed','running') "
        "ORDER BY id LIMIT 1 FOR UPDATE",
        (user_id, lane),
    )
    existing = cur.fetchone()
    existing_stale = False
    if existing is not None:
        # Check wall time only after the row lock is actually ours.  A sender
        # can wait behind final publication across the deadline; transaction-
        # stable now() would then coalesce its new input into an expired job.
        cur.execute(
            "SELECT CASE WHEN status='pending' THEN "
            "  COALESCE(queue_deadline_at,deadline_at,"
            "    CASE WHEN lane='chat' THEN "
            "      created_at + make_interval(secs => %s) END) "
            "      <= clock_timestamp() "
            "ELSE COALESCE(lease_expires_at,deadline_at) IS NOT NULL "
            "  AND COALESCE(lease_expires_at,deadline_at) "
            "      <= clock_timestamp() END AS stale "
            "FROM agent_jobs WHERE id=%s",
            (float(PENDING_CHAT_TTL_SEC), existing["id"]),
        )
        existing_stale = bool(cur.fetchone()["stale"])
    generation_stale = (
        existing is not None
        and expected_generation is not None
        and (
            existing["expected_runtime_generation"] is None
            or int(existing["expected_runtime_generation"]) != int(expected_generation)
        )
    )
    if existing is not None and not existing_stale and not generation_stale:
        cur.execute(
            "UPDATE agent_jobs SET input_generation=input_generation+1 "
            "WHERE id=%s RETURNING id",
            (existing["id"],),
        )
        return int(cur.fetchone()["id"]), True
    if existing is not None:
        # Reclaim the single-flight key inside the same transaction.  The
        # fresh chat job will re-read every message after the durable cursor,
        # so input attached to the expired row is not lost or stranded.
        if generation_stale:
            cur.execute(
                "UPDATE agent_jobs SET status='superseded',finished_at=now(), "
                "last_error='stale_runtime_generation' WHERE id=%s",
                (existing["id"],),
            )
        else:
            cur.execute(
                "UPDATE agent_jobs SET status='expired',finished_at=now(), "
                "attempt_count=attempt_count+1, "
                "last_error=CASE WHEN status='pending' "
                "THEN 'queue_timeout' ELSE 'lease_timeout' END "
                "WHERE id=%s",
                (existing["id"],),
            )
            # A fresh enqueue can win the timeout race before the independent
            # reaper sees this row.  Queue the same visibility obligation in
            # this caller-owned transaction; generation supersession above is
            # intentional/silent and must not create an error marker.
            _queue_terminal_failure_on_cursor(
                cur,
                existing["id"],
                str(user_id),
                "queue_timeout" if existing["status"] == "pending" else "lease_timeout",
            )
            _queue_failure_review_on_cursor(cur, existing["id"])
    cur.execute(
        "INSERT INTO agent_jobs "
        "(user_id, lane, status, reason, trace_id, priority, queue_deadline_at, "
        "expected_runtime_generation) "
        "VALUES (%s,%s,'pending',%s,%s,%s,"
        "CASE WHEN %s::timestamptz IS NOT NULL THEN %s::timestamptz "
        "     WHEN %s='chat' THEN now() + make_interval(secs => %s) "
        "     ELSE NULL END, %s) RETURNING id",
        (
            user_id,
            lane,
            reason,
            trace_id,
            int(priority),
            deadline_at,
            deadline_at,
            lane,
            float(PENDING_CHAT_TTL_SEC),
            expected_generation,
        ),
    )
    return int(cur.fetchone()["id"]), False


def preempt_active_for_chat_on_cursor(
    cur: psycopg.Cursor,
    *,
    user_id: str,
) -> list[PreemptedJob]:
    """Invalidate same-user non-Chat owners before inserting/coalescing Chat.

    The caller owns the surrounding transaction and has already taken the
    runtime-state/user fence.  Recoverable scheduled and capture executions
    retain their durable Job identity and return to ``pending``; other lanes
    become terminal ``superseded`` rows and may be recreated by their normal
    due/backoff policy.  Each write rechecks the locked row's exact status and
    owner so a stale snapshot can never revoke a newer claim.
    """
    cur.execute(
        "SELECT id,user_id,lane,status,claimed_by FROM agent_jobs "
        "WHERE user_id=%s AND lane<>'chat' "
        "AND status IN ('pending','claimed','running') "
        "ORDER BY id FOR UPDATE",
        (str(user_id),),
    )
    rows = [dict(row) for row in cur.fetchall()]
    results: list[PreemptedJob] = []
    for row in rows:
        lane = str(row["lane"])
        recovery: Literal["terminal", "requeued"]
        if lane in {"scheduled", "capture"}:
            recovery = "requeued"
            cur.execute(
                "UPDATE agent_jobs SET status='pending', "
                "last_error='foreground_chat_preempted', claimed_by=NULL, "
                "claimed_at=NULL, started_at=NULL, finished_at=NULL, "
                "lease_expires_at=NULL, deadline_at=NULL, created_at=now() "
                "WHERE id=%s AND status=%s "
                "AND claimed_by IS NOT DISTINCT FROM %s",
                (row["id"], row["status"], row["claimed_by"]),
            )
        else:
            recovery = "terminal"
            cur.execute(
                "UPDATE agent_jobs SET status='superseded', finished_at=now(), "
                "last_error='foreground_chat_preempted', claimed_by=NULL, "
                "claimed_at=NULL, started_at=NULL, lease_expires_at=NULL, "
                "deadline_at=NULL "
                "WHERE id=%s AND status=%s "
                "AND claimed_by IS NOT DISTINCT FROM %s",
                (row["id"], row["status"], row["claimed_by"]),
            )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"active job ownership changed while preempting job {row['id']}"
            )
        results.append(
            PreemptedJob(
                job_id=int(row["id"]),
                user_id=str(row["user_id"]),
                lane=lane,
                claimed_by=(
                    None if row["claimed_by"] is None else str(row["claimed_by"])
                ),
                recovery=recovery,
            )
        )
    return results


def enqueue_job(
    user_id,
    lane,
    *,
    reason=None,
    trace_id=None,
    priority=None,
    deadline_at=None,
    expected_generation: int | None = None,
) -> tuple[int, bool]:
    """入队一个 job。命中 per-user/lane single-flight（已有 active job）则合并到现有
    pending，返回 (existing_id, True)；否则新建，返回 (new_id, False)。

    实现：事务内先 SELECT ... FOR UPDATE 现有 active job；无则 INSERT。两个并发 enqueue
    可能都读不到现有行而各自 INSERT → 第二个撞 ux_agent_jobs_singleflight 唯一索引抛
    UniqueViolation → 重试一轮即读到赢家并 coalesce。唯一索引是最终防线。

    priority：未显式传（None）时按 LANE_PRIORITY 从 lane 派生（chat/manual_wake=100，
    heartbeat/scheduled=50，capture/maintenance=10）；调用方显式传一个 int 则原样
    使用，不被 lane 派生值覆盖。

    expected_generation：入队方观测到的运行时代数。省略时，如果权威状态当前为
    v2，本函数会在 state->job 的锁顺序下自动钉住当前 generation；resident 状态
    仍保留 None 并会在 claim 时被所有权闸 supersede。若 active 行钉的是另一代，
    不得把新输入 coalesce 进旧代：先 supersede 旧行再建当前代 successor。
    """
    if lane not in LANES:
        raise ValueError(f"unknown lane: {lane!r}")
    if priority is None:
        priority = LANE_PRIORITY.get(lane, 0)

    for _ in range(3):
        try:
            with _pool().connection() as conn:
                with conn.transaction():
                    with conn.cursor(row_factory=dict_row) as cur:
                        effective_generation = expected_generation
                        if effective_generation is None:
                            cur.execute(
                                "SELECT hosted_runtime_state, runtime_generation "
                                "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                                (user_id,),
                            )
                            control = cur.fetchone()
                            if (
                                control is not None
                                and str(control["hosted_runtime_state"]) == "v2"
                            ):
                                effective_generation = int(
                                    control["runtime_generation"]
                                )
                        return coalesce_or_insert_on_cursor(
                            cur,
                            user_id,
                            lane,
                            reason=reason,
                            trace_id=trace_id,
                            priority=priority,
                            deadline_at=deadline_at,
                            expected_generation=effective_generation,
                        )
        except psycopg.errors.UniqueViolation:
            continue  # 并发 racer 抢先建了 active job；重读并 coalesce
    # A very busy terminal/enqueue race can exhaust the optimistic retries.
    # The fallback must still record that new input arrived; merely returning
    # the row id would let finalization miss the follow-up generation.
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                effective_generation = expected_generation
                if effective_generation is None:
                    cur.execute(
                        "SELECT hosted_runtime_state, runtime_generation "
                        "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                        (user_id,),
                    )
                    control = cur.fetchone()
                    if (
                        control is not None
                        and str(control["hosted_runtime_state"]) == "v2"
                    ):
                        effective_generation = int(control["runtime_generation"])
                return coalesce_or_insert_on_cursor(
                    cur,
                    user_id,
                    lane,
                    reason=reason,
                    trace_id=trace_id,
                    priority=priority,
                    deadline_at=deadline_at,
                    expected_generation=effective_generation,
                )


def enqueue_job_with_context_log(
    user_id: str,
    lane: str,
    *,
    reason: str | None,
    trace_id: str | None,
    context_stream: str,
    context_doc: dict,
    context_ts: float,
    priority: int | None = None,
) -> tuple[int, bool]:
    """Atomically enqueue/coalesce one job and attach its input context.

    This closes the enqueue->association crash/race window for externally
    produced wake inputs. The active job row is locked by
    ``coalesce_or_insert_on_cursor`` and the context row commits in the same
    transaction, so finalization can use ``input_generation`` as an exact
    consumed-input fence.
    """
    if lane not in LANES:
        raise ValueError(f"unknown lane: {lane!r}")
    if not str(context_stream).strip():
        raise ValueError("context_stream is required")
    if priority is None:
        priority = LANE_PRIORITY.get(lane, 0)

    inserted: tuple[int, bool, int, dict] | None = None
    for _ in range(4):
        try:
            with _pool().connection() as conn:
                with conn.transaction():
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(
                            "SELECT hosted_runtime_state,runtime_generation "
                            "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                            (str(user_id),),
                        )
                        control = cur.fetchone()
                        expected_generation = (
                            int(control["runtime_generation"])
                            if control is not None
                            and str(control["hosted_runtime_state"]) == "v2"
                            else None
                        )
                        job_id, coalesced = coalesce_or_insert_on_cursor(
                            cur,
                            str(user_id),
                            lane,
                            reason=reason,
                            trace_id=trace_id,
                            priority=int(priority),
                            expected_generation=expected_generation,
                        )
                        payload = dict(context_doc)
                        payload["agent_job_id"] = int(job_id)
                        cur.execute(
                            "INSERT INTO user_logs "
                            "(user_id,stream,ts,item_key,doc) "
                            "VALUES (%s,%s,%s,%s,%s) RETURNING seq",
                            (
                                str(user_id),
                                str(context_stream),
                                float(context_ts),
                                str(int(job_id)),
                                Jsonb(payload),
                            ),
                        )
                        seq = int(cur.fetchone()["seq"])
                        inserted = (int(job_id), bool(coalesced), seq, payload)
            break
        except psycopg.errors.UniqueViolation:
            continue
    if inserted is None:
        raise RuntimeError("could not atomically enqueue job context")

    job_id, coalesced, seq, payload = inserted
    from tee_shadow import mirror

    mirror.execute(
        "INSERT INTO user_logs (user_id,stream,seq,ts,item_key,doc) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (user_id,stream,seq) DO NOTHING",
        (
            str(user_id),
            str(context_stream),
            seq,
            float(context_ts),
            str(job_id),
            Jsonb(payload),
        ),
    )
    return job_id, coalesced


def claim_next_job(worker_id: str, *, lanes: set[str] | None = None) -> dict | None:
    """抢下一个 pending job（priority DESC, created_at）。先乐观选候选，再按
    runtime-state -> job 的统一锁顺序用 FOR UPDATE SKIP LOCKED 完成独占。
    pending → claimed，落 claimed_by/claimed_at。
    返回整行 dict（含 id/user_id/lane/trace_id/expected_runtime_generation/...），
    无活可抢返回 None。

    lanes：可选 lane 白名单（预留槽位场景，如某个 slot 只允许抢 {"chat",
    "manual_wake"}，保证聊天回复不被 heartbeat/capture 之类的后台唤醒风暴饿死）。
    None（默认）＝不限制 lane，行为与改动前完全一致。

    所有权/代过期早退（PR A / spec A3）：只有权威状态仍为 v2 的用户
    可以被 claim。候选行的 expected_runtime_generation 若非空且不等于
    该用户当前 generation（意味着入队之后发生过 cutover），本次
    claim 不把它交给任何 worker 过一轮——同一事务内直接把它判终态 'superseded'，
    然后继续看下一个候选，直到拿到一个非过期的可抢行或彻底抢空。必须在同一个
    claim 事务里做，否则两个并发 worker 可能都读到这个陈旧代的行、一个刚判
    superseded、另一个已经把它当活的 claimed 出去。"""
    # Round-trip budget (the CVM is in Phala, the RDS in AWS — one round trip
    # measured 63.8ms on test, so each saved statement is real user-visible
    # latency). What used to be five statements is now two:
    #   1. `_CANDIDATE_SQL`  set_config + pick the queue head + lock its state row
    #   2. `_CLAIM_SQL`      lock the job + claim-or-supersede it
    # The lock ORDER is unchanged and still load-bearing: statement 1 takes only
    # the v2_runtime_state row, statement 2 only the agent_jobs row. Merging the
    # two into a single SQL text would hand lock acquisition order to the
    # planner and re-open the ABBA deadlock against chat/send (which holds state
    # and wants job) — that is why this stayed two statements, not one.
    lane_clause = "AND j.lane = ANY(%s) " if lanes is not None else ""
    candidate_sql = (
        "WITH cfg AS (SELECT set_config('feedling.v2_worker_protocol',%s,true) AS p), "
        "cand AS ("
        "SELECT j.id, j.user_id FROM agent_jobs j "
        "JOIN users u ON u.user_id=j.user_id "
        "CROSS JOIN cfg "
        "WHERE j.status='pending' "
        "AND j.available_at <= clock_timestamp() "
        "AND (COALESCE(j.queue_deadline_at, j.deadline_at, "
        "CASE WHEN j.lane='chat' THEN "
        "j.created_at + make_interval(secs => %s) END) IS NULL OR "
        "COALESCE(j.queue_deadline_at, j.deadline_at, "
        "CASE WHEN j.lane='chat' THEN "
        "j.created_at + make_interval(secs => %s) END) > now()) "
        + lane_clause +
        "AND NOT EXISTS (SELECT 1 FROM agent_jobs active "
        "WHERE active.user_id=j.user_id "
        "AND active.status IN ('claimed','running')) "
        "ORDER BY j.priority DESC, j.created_at LIMIT 1) "
        # INNER JOIN because FOR UPDATE cannot be applied to the nullable side
        # of an outer join. A candidate whose state row is missing therefore
        # yields no row here and is retired through the orphan path below.
        "SELECT c.id, c.user_id, s.hosted_runtime_state, s.runtime_generation "
        "FROM cand c JOIN v2_runtime_state s ON s.user_id=c.user_id "
        "FOR UPDATE OF s"
    )
    # Same predicate, minus the state join: tells "queue is empty" apart from
    # "queue head has no state row" without costing the hot path a statement.
    orphan_sql = (
        "WITH cfg AS (SELECT set_config('feedling.v2_worker_protocol',%s,true) AS p) "
        "SELECT j.id FROM agent_jobs j "
        "JOIN users u ON u.user_id=j.user_id "
        "CROSS JOIN cfg "
        "WHERE j.status='pending' "
        "AND j.available_at <= clock_timestamp() "
        "AND (COALESCE(j.queue_deadline_at, j.deadline_at, "
        "CASE WHEN j.lane='chat' THEN "
        "j.created_at + make_interval(secs => %s) END) IS NULL OR "
        "COALESCE(j.queue_deadline_at, j.deadline_at, "
        "CASE WHEN j.lane='chat' THEN "
        "j.created_at + make_interval(secs => %s) END) > now()) "
        + lane_clause +
        "AND NOT EXISTS (SELECT 1 FROM agent_jobs active "
        "WHERE active.user_id=j.user_id "
        "AND active.status IN ('claimed','running')) "
        "AND NOT EXISTS (SELECT 1 FROM v2_runtime_state s "
        "WHERE s.user_id=j.user_id) "
        "ORDER BY j.priority DESC, j.created_at LIMIT 1"
    )
    probe_args = (
        (_WORKER_CLAIM_PROTOCOL, float(PENDING_CHAT_TTL_SEC),
         float(PENDING_CHAT_TTL_SEC))
        + ((list(lanes),) if lanes is not None else ())
    )
    # locked -> (claimed | superseded) in ONE statement. `locked` is referenced
    # by both data-modifying CTEs, so it is materialised and evaluated first;
    # the two UPDATEs act on disjoint row sets (generation matches vs not), so
    # no row is ever updated twice. SKIP LOCKED keeps its semantics inside the
    # CTE — verified: a row held by another session returns n_locked=0 in ~66ms
    # instead of blocking.
    claim_sql = (
        "WITH locked AS ("
        "SELECT j.id, j.expected_runtime_generation AS eg FROM agent_jobs j "
        "WHERE j.id=%s AND j.status='pending' "
        "AND j.available_at <= clock_timestamp() "
        "AND (COALESCE(j.queue_deadline_at, j.deadline_at, "
        "CASE WHEN j.lane='chat' THEN "
        "j.created_at + make_interval(secs => %s) END) IS NULL OR "
        "COALESCE(j.queue_deadline_at, j.deadline_at, "
        "CASE WHEN j.lane='chat' THEN "
        "j.created_at + make_interval(secs => %s) END) > clock_timestamp()) "
        "AND NOT EXISTS (SELECT 1 FROM agent_jobs active "
        "WHERE active.user_id=j.user_id "
        "AND active.status IN ('claimed','running')) "
        "FOR UPDATE OF j SKIP LOCKED), "
        "claimed AS ("
        "UPDATE agent_jobs SET status='claimed', claimed_by=%s, "
        "claimed_at=clock_timestamp(), "
        "expected_runtime_generation=COALESCE(expected_runtime_generation,%s), "
        "lease_expires_at = clock_timestamp() + make_interval(secs => %s), "
        "deadline_at = clock_timestamp() + make_interval(secs => %s) "
        "WHERE id IN (SELECT id FROM locked WHERE eg IS NULL OR eg=%s) "
        "RETURNING *), "
        "sup AS ("
        "UPDATE agent_jobs SET status='superseded', finished_at=now(), "
        "last_error='stale_runtime_generation' "
        "WHERE id IN (SELECT id FROM locked WHERE eg IS NOT NULL AND eg<>%s) "
        "RETURNING id) "
        "SELECT l.n_locked, s.n_sup, c.* "
        "FROM (SELECT count(*) AS n_locked FROM locked) l "
        "CROSS JOIN (SELECT count(*) AS n_sup FROM sup) s "
        "LEFT JOIN claimed c ON true"
    )

    # Lock order is runtime-state -> job everywhere. chat/send's atomic
    # append+coalesce and cutover already use that order; claiming the job
    # first would create an ABBA deadlock (send holds state and wants job while
    # claim holds job and wants state). Select a candidate optimistically,
    # then lock its state and re-lock/revalidate the job in one short
    # transaction. A competing worker that won the job simply makes the
    # revalidation return no row and we retry.
    with _pool().connection() as conn:
        while True:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    # 0041's BEFORE UPDATE trigger leaves the shared pending
                    # queue producer-compatible but makes old claim SQL affect
                    # zero rows. The set_config that opts in rides along in the
                    # candidate CTE, scoped (local=true) to exactly this claim
                    # transaction so pooled connections cannot leak authority.
                    # Note it only actually runs when the CROSS JOIN yields a
                    # row — i.e. when there IS a candidate. That is precisely
                    # when a later UPDATE needs it; the empty-queue path below
                    # updates nothing.
                    cur.execute(candidate_sql, probe_args)
                    head = cur.fetchone()
                    if head is None:
                        # Either the queue is empty or its head has no state
                        # row (the INNER JOIN above cannot tell us which).
                        cur.execute(orphan_sql, probe_args)
                        orphan = cur.fetchone()
                        if orphan is None:
                            return None
                        cur.execute(
                            "UPDATE agent_jobs SET status='superseded', finished_at=now(), "
                            "last_error='runtime_state_not_v2' WHERE id=%s",
                            (orphan["id"],),
                        )
                        continue
                    if str(head["hosted_runtime_state"]) != "v2":
                        cur.execute(
                            "UPDATE agent_jobs SET status='superseded', finished_at=now(), "
                            "last_error='runtime_state_not_v2' WHERE id=%s",
                            (head["id"],),
                        )
                        continue
                    current_generation = int(head["runtime_generation"])
                    cur.execute(
                        claim_sql,
                        (
                            head["id"],
                            float(PENDING_CHAT_TTL_SEC),
                            float(PENDING_CHAT_TTL_SEC),
                            worker_id,
                            current_generation,
                            float(RUNNING_TTL_SEC),
                            float(RUNNING_TTL_SEC),
                            current_generation,
                            current_generation,
                        ),
                    )
                    outcome = cur.fetchone()
                    # This statement always returns exactly one row; the job
                    # columns are NULL-extended when nothing was claimed.
                    n_locked = int(outcome.pop("n_locked") or 0)
                    n_superseded = int(outcome.pop("n_sup") or 0)
                    if n_locked == 0:
                        # A competing worker holds it (SKIP LOCKED) or it no
                        # longer satisfies the pending predicate — re-pick.
                        continue
                    if n_superseded:
                        continue
                    if outcome.get("id") is None:
                        # A protocol trigger (current or future) deliberately
                        # skipped the transition. Returning idle is safer than
                        # hot-looping on the same still-pending queue head.
                        log.warning(
                            "claim transition skipped by worker protocol gate "
                            "job=%s protocol=%s",
                            head["id"],
                            _WORKER_CLAIM_PROTOCOL,
                        )
                        return None
                    return outcome


def valid_active_claims(
    claims: list[tuple[int, str]],
) -> set[tuple[int, str]]:
    """Return the exact still-live ``(job_id, claimed_by)`` snapshot pairs.

    The parent supervisor uses this as a periodic backstop for a dropped
    cancellation NOTIFY.  Job id and owner are joined as one composite fence:
    neither a recycled slot generation nor a reassigned job can validate the
    other half of a stale snapshot.
    """
    if not claims:
        return set()
    job_ids = [int(job_id) for job_id, _claimed_by in claims]
    owners = [str(claimed_by) for _job_id, claimed_by in claims]
    with _pool().connection() as conn:
        rows = conn.execute(
            "WITH wanted(job_id, claimed_by) AS ("
            "SELECT * FROM unnest(%s::bigint[], %s::text[])"
            ") SELECT j.id, j.claimed_by FROM wanted w "
            "JOIN agent_jobs j ON j.id=w.job_id AND j.claimed_by=w.claimed_by "
            "WHERE j.status IN ('claimed','running') "
            "AND j.lease_expires_at > clock_timestamp()",
            (job_ids, owners),
        ).fetchall()
    return {(int(row[0]), str(row[1])) for row in rows}


def valid_reconcile_claims(
    claims: list[tuple[int, str]],
) -> set[tuple[int, str]]:
    """Return snapshot pairs that do not justify cancelling their slot.

    Besides a live claimed/running lease, accept ``completed`` and ``failed``
    rows fenced by the same owner.  The worker commits those terminal states
    before its final trajectory/unwind work and parent pipe signal, so treating
    that bounded window as an invalid claim kills a healthy slot after every
    normal turn.  Cancellation states and missing/reassigned rows deliberately
    remain invalid so the reconciler still backs up dropped cancellation
    notifications.
    """
    if not claims:
        return set()
    job_ids = [int(job_id) for job_id, _claimed_by in claims]
    owners = [str(claimed_by) for _job_id, claimed_by in claims]
    with _pool().connection() as conn:
        rows = conn.execute(
            "WITH wanted(job_id, claimed_by) AS ("
            "SELECT * FROM unnest(%s::bigint[], %s::text[])"
            ") SELECT j.id, j.claimed_by FROM wanted w "
            "JOIN agent_jobs j ON j.id=w.job_id AND j.claimed_by=w.claimed_by "
            "WHERE (j.status IN ('claimed','running') "
            "AND j.lease_expires_at > clock_timestamp()) "
            "OR j.status IN ('completed','failed')",
            (job_ids, owners),
        ).fetchall()
    return {(int(row[0]), str(row[1])) for row in rows}


def mark_running(job_id, *, claimed_by: str) -> bool:
    with _pool().connection() as conn:
        with conn.transaction():
            # Discover the user and lock its state row in ONE statement, still
            # without taking a job lock, so the global runtime-state -> job lock
            # order is preserved. Holding the state row through the transition
            # gives turn start a real ownership linearization point instead of a
            # SELECT/UPDATE TOCTOU window.
            #
            # INNER JOIN collapses the two old early-exits into one: a missing
            # job row and a missing state row both returned False before, and
            # both yield no row here. FOR UPDATE OF s locks only the state row —
            # it cannot be applied to an outer join's nullable side anyway.
            control = conn.execute(
                "SELECT s.hosted_runtime_state, s.runtime_generation "
                "FROM agent_jobs j JOIN v2_runtime_state s ON s.user_id=j.user_id "
                "WHERE j.id=%s FOR UPDATE OF s",
                (job_id,),
            ).fetchone()
            if control is None or str(control[0]) != "v2":
                return False
            cur = conn.execute(
                "UPDATE agent_jobs SET status='running', started_at=clock_timestamp(), "
                "lease_expires_at = clock_timestamp() + make_interval(secs => %s), "
                "deadline_at = clock_timestamp() + make_interval(secs => %s) "
                "WHERE id=%s AND status='claimed' "
                "AND (lease_expires_at IS NULL OR lease_expires_at > clock_timestamp()) "
                "AND claimed_by=%s "
                "AND expected_runtime_generation=%s",
                (
                    float(RUNNING_TTL_SEC),
                    float(RUNNING_TTL_SEC),
                    job_id,
                    str(claimed_by),
                    int(control[1]),
                ),
            )
            return cur.rowcount == 1


def renew_job_lease(
    job_id, claimed_by: str, *, ttl_sec: float = RUNNING_TTL_SEC
) -> bool:
    with _pool().connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT user_id FROM agent_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            control = conn.execute(
                "SELECT hosted_runtime_state, runtime_generation "
                "FROM v2_runtime_state WHERE user_id=%s FOR SHARE",
                (row[0],),
            ).fetchone()
            if control is None or str(control[0]) != "v2":
                return False
            cur = conn.execute(
                "UPDATE agent_jobs SET "
                "lease_expires_at=clock_timestamp() + make_interval(secs => %s), "
                "deadline_at=clock_timestamp() + make_interval(secs => %s) "
                "WHERE id=%s AND claimed_by=%s "
                "AND status IN ('claimed','running') "
                "AND lease_expires_at > clock_timestamp() "
                "AND expected_runtime_generation=%s",
                (
                    float(ttl_sec),
                    float(ttl_sec),
                    job_id,
                    str(claimed_by),
                    int(control[1]),
                ),
            )
            return cur.rowcount == 1


_FAIL_BACKOFF_WAKE_LANES = frozenset({"heartbeat", "scheduled"})


def _latest_genuine_user_seq_on_cursor(cur, user_id: str) -> int:
    cur.execute(
        "SELECT COALESCE(MAX(seq),0) FROM chat_messages "
        "WHERE user_id=%s AND doc->>'role' IN ('user','human') "
        "AND COALESCE(doc->>'source','') "
        "NOT IN ('verify_ping','resident_maintenance')",
        (str(user_id),),
    )
    row = cur.fetchone()
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return int(value or 0)


def _clear_wake_backoff_on_cursor(cur, user_id: str) -> None:
    user_seq = _latest_genuine_user_seq_on_cursor(cur, user_id)
    cur.execute(
        "UPDATE v2_wake_schedule SET proactive_fail_streak=0, "
        "proactive_fail_user_seq=%s, proactive_backoff_until=NULL, "
        "updated_at=now() WHERE user_id=%s",
        (user_seq, str(user_id)),
    )


def _arm_wake_backoff_on_cursor(
    cur,
    user_id: str,
    *,
    now: float,
    base_sec: float,
    cap_sec: float,
) -> None:
    base = max(0.0, float(base_sec))
    cap = max(0.0, float(cap_sec))
    if base <= 0 or cap <= 0:
        return
    normalized_user_id = str(user_id)
    cur.execute(
        "INSERT INTO v2_wake_schedule (user_id) VALUES (%s) "
        "ON CONFLICT (user_id) DO NOTHING",
        (normalized_user_id,),
    )
    cur.execute(
        "SELECT proactive_fail_streak,proactive_fail_user_seq "
        "FROM v2_wake_schedule WHERE user_id=%s FOR UPDATE",
        (normalized_user_id,),
    )
    state = cur.fetchone()
    if state is None:
        raise RuntimeError("wake schedule row missing")
    user_seq = _latest_genuine_user_seq_on_cursor(cur, normalized_user_id)
    streak = int(state[0] or 0)
    if user_seq > int(state[1] or 0):
        streak = 0
    streak += 1
    delay = min(base * (2 ** min(streak - 1, 62)), cap)
    cur.execute(
        "UPDATE v2_wake_schedule SET proactive_fail_streak=%s, "
        "proactive_fail_user_seq=%s, proactive_backoff_until=to_timestamp(%s), "
        "updated_at=now() WHERE user_id=%s",
        (streak, user_seq, float(now) + delay, normalized_user_id),
    )


def mark_completed(
    job_id,
    *,
    claimed_by: str,
    clear_wake_backoff: bool = False,
) -> bool:
    with _pool().connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                "UPDATE agent_jobs SET status='completed', finished_at=now() "
                "WHERE id=%s AND status IN ('claimed','running') "
                "AND claimed_by=%s AND lease_expires_at > now() "
                "RETURNING user_id,lane",
                (job_id, str(claimed_by)),
            )
            row = cur.fetchone()
            if row is None:
                return False
            if clear_wake_backoff and str(row[1]) in _FAIL_BACKOFF_WAKE_LANES:
                _clear_wake_backoff_on_cursor(cur, str(row[0]))
            return True


_PERCEPTION_GLANCE_FINGERPRINT_STATE_KEY = (
    "last_completed_perception_glance_fingerprint"
)
_PERCEPTION_GLANCE_SOURCE_JOB_STATE_KEY = (
    "last_completed_perception_glance_source_job_id"
)


def _validate_perception_glance_fingerprint(value: object) -> str:
    if (
        type(value) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise ValueError("completed perception glance fingerprint is invalid")
    return value


def _merge_completed_perception_glance_on_cursor(
    cur,
    *,
    user_id: str,
    source_job_id: int,
    fingerprint: str,
) -> bool:
    """Shallow-merge an ordered completed-glance marker on this transaction.

    The source id prevents an exact-source retry that resumes after a newer
    heartbeat from restoring older state. An absent marker adopts the first
    completion; malformed existing ordering metadata fails closed.
    """
    normalized_fingerprint = _validate_perception_glance_fingerprint(
        fingerprint
    )
    source_id = int(source_job_id)
    if source_id <= 0:
        raise ValueError("completed perception glance source is invalid")
    patch = {
        _PERCEPTION_GLANCE_FINGERPRINT_STATE_KEY: normalized_fingerprint,
        _PERCEPTION_GLANCE_SOURCE_JOB_STATE_KEY: source_id,
    }
    cur.execute(
        "INSERT INTO runtime_state (user_id,state_json,updated_at) "
        "VALUES (%s,%s,now()) "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "state_json=runtime_state.state_json || EXCLUDED.state_json, "
        "updated_at=now() WHERE CASE "
        "WHEN NOT (runtime_state.state_json ? %s) THEN TRUE "
        "WHEN jsonb_typeof(runtime_state.state_json->%s)='number' "
        "AND (runtime_state.state_json->>%s) ~ '^[0-9]+$' "
        "THEN (runtime_state.state_json->>%s)::numeric <= (%s)::numeric "
        "ELSE FALSE END RETURNING state_json",
        (
            str(user_id),
            Jsonb(patch),
            _PERCEPTION_GLANCE_SOURCE_JOB_STATE_KEY,
            _PERCEPTION_GLANCE_SOURCE_JOB_STATE_KEY,
            _PERCEPTION_GLANCE_SOURCE_JOB_STATE_KEY,
            _PERCEPTION_GLANCE_SOURCE_JOB_STATE_KEY,
            source_id,
        ),
    )
    return cur.fetchone() is not None


def finish_wake_job(
    job_id: int,
    *,
    claimed_by: str,
    observed_generation: int,
    context_stream: str,
    consumed_context_seq: int,
    clear_wake_backoff: bool = False,
    completed_perception_glance_fingerprint: str | None = None,
) -> tuple[bool, int | None]:
    """Complete a wake, persist its glance, and hand input to one successor.

    Context producers increment ``input_generation`` and append ``user_logs``
    under the same active-job lock. Rows newer than the worker's consumed
    context cursor are reassigned to the successor in this transaction. Thus
    finalization winning creates a fresh job for a later producer, while the
    producer winning forces this finalizer to preserve its input.

    ``completed_perception_glance_fingerprint`` is supplied only for a proven
    ordinary heartbeat. Its ordered shallow merge shares this transaction with
    completion and successor creation. A final-reply effect may already have
    completed the exact source; the same-source merge remains idempotent.
    """
    completed_fingerprint = (
        _validate_perception_glance_fingerprint(
            completed_perception_glance_fingerprint
        )
        if completed_perception_glance_fingerprint is not None
        else None
    )
    successor_id: int | None = None
    moved_context = False
    user_id = ""
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT user_id FROM agent_jobs WHERE id=%s",
                    (int(job_id),),
                )
                identity = cur.fetchone()
                if identity is None:
                    return False, None
                user_id = str(identity["user_id"])
                cur.execute(
                    "SELECT hosted_runtime_state,runtime_generation "
                    "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                control = cur.fetchone()
                if control is None or str(control["hosted_runtime_state"]) != "v2":
                    return False, None
                runtime_generation = int(control["runtime_generation"])
                cur.execute(
                    "SELECT lane,status,input_generation,priority,"
                    "expected_runtime_generation,lease_expires_at "
                    "FROM agent_jobs WHERE id=%s AND claimed_by=%s FOR UPDATE",
                    (int(job_id), str(claimed_by)),
                )
                row = cur.fetchone()
                if (
                    row is None
                    or str(row["lane"]) != "heartbeat"
                    or row["expected_runtime_generation"] is None
                    or int(row["expected_runtime_generation"]) != runtime_generation
                ):
                    return False, None
                if str(row["status"]) == "completed":
                    if completed_fingerprint is not None:
                        _merge_completed_perception_glance_on_cursor(
                            cur,
                            user_id=user_id,
                            source_job_id=int(job_id),
                            fingerprint=completed_fingerprint,
                        )
                    if clear_wake_backoff:
                        _clear_wake_backoff_on_cursor(cur, user_id)
                    return True, None
                if (
                    str(row["status"]) not in {"claimed", "running"}
                    or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= datetime.now(timezone.utc)
                ):
                    return False, None

                has_late_input = int(row["input_generation"] or 0) > int(
                    observed_generation
                )
                cur.execute(
                    "UPDATE agent_jobs SET status='completed',finished_at=now() "
                    "WHERE id=%s",
                    (int(job_id),),
                )
                if completed_fingerprint is not None:
                    _merge_completed_perception_glance_on_cursor(
                        cur,
                        user_id=user_id,
                        source_job_id=int(job_id),
                        fingerprint=completed_fingerprint,
                    )
                if clear_wake_backoff:
                    _clear_wake_backoff_on_cursor(cur, user_id)
                if has_late_input:
                    cur.execute(
                        "INSERT INTO agent_jobs "
                        "(user_id,lane,status,reason,priority,"
                        "expected_runtime_generation) "
                        "VALUES (%s,'heartbeat','pending',"
                        "'coalesced_perception_followup',%s,%s) RETURNING id",
                        (
                            user_id,
                            int(row["priority"]),
                            runtime_generation,
                        ),
                    )
                    successor_id = int(cur.fetchone()["id"])
                    cur.execute(
                        "UPDATE user_logs SET item_key=%s,"
                        "doc=jsonb_set(doc,'{agent_job_id}',"
                        "to_jsonb(%s::bigint),true) "
                        "WHERE user_id=%s AND stream=%s AND item_key=%s "
                        "AND seq>%s",
                        (
                            str(successor_id),
                            successor_id,
                            user_id,
                            str(context_stream),
                            str(int(job_id)),
                            max(0, int(consumed_context_seq)),
                        ),
                    )
                    moved_context = bool(cur.rowcount)

    if successor_id is not None and moved_context:
        from tee_shadow import mirror

        mirror.execute(
            "UPDATE user_logs SET item_key=%s,"
            "doc=jsonb_set(doc,'{agent_job_id}',to_jsonb(%s::bigint),true) "
            "WHERE user_id=%s AND stream=%s AND item_key=%s AND seq>%s",
            (
                str(successor_id),
                successor_id,
                user_id,
                str(context_stream),
                str(int(job_id)),
                max(0, int(consumed_context_seq)),
            ),
        )
    return True, successor_id


def mark_failed(
    job_id,
    error: str,
    *,
    claimed_by: str,
    error_class: str = "",
    wake_backoff_base_sec: float | None = None,
    wake_backoff_cap_sec: float | None = None,
    wake_backoff_now: float | None = None,
) -> bool:
    """Fail an owned job and transactionally queue required visibility.

    Terminalization, the user-visible outbox obligation, and any trajectory
    review handoff share one explicit transaction, so there is no process-crash
    window between them. Scheduled reminders join chat because firing a timer
    carries a delivery obligation; the other background lanes may still choose
    silence and therefore do not get an outbox row.
    """
    recovered_reviews: list[tuple[str, str]] = []
    with _pool().connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                "UPDATE agent_jobs SET status='failed', finished_at=now(), "
                "last_error=%s, attempt_count=attempt_count+1 "
                "WHERE id=%s AND status IN ('claimed','running') "
                "AND claimed_by=%s AND lease_expires_at > now() "
                "RETURNING id,user_id,lane",
                (str(error)[:500], job_id, str(claimed_by)),
            )
            row = cur.fetchone()
            if row is None:
                return False
            if str(row[2]) in {"chat", "scheduled"}:
                _queue_terminal_failure_on_cursor(
                    cur,
                    row[0],
                    str(row[1]),
                    error,
                    error_class=error_class,
                )
            if (
                str(row[2]) in _FAIL_BACKOFF_WAKE_LANES
                and wake_backoff_base_sec is not None
                and wake_backoff_cap_sec is not None
            ):
                _arm_wake_backoff_on_cursor(
                    cur,
                    str(row[1]),
                    now=(
                        time.time()
                        if wake_backoff_now is None
                        else float(wake_backoff_now)
                    ),
                    base_sec=float(wake_backoff_base_sec),
                    cap_sec=float(wake_backoff_cap_sec),
                )
            recovered_reviews = _recover_review_runner_on_cursor(cur, job_id)
            _queue_failure_review_on_cursor(cur, job_id)
    if recovered_reviews:
        # Must wait until the transaction above has committed — see the
        # docstring on _recover_review_runner_on_cursor for the race this
        # avoids (a requeue consumer reading the pre-commit row and deleting
        # the marker before the real UPDATE lands).
        from tee_shadow import mirror
        for user_id, source_job_id in recovered_reviews:
            mirror.mark_pending(
                user_id, "v2_trajectory_reviews", source_job_id, "requeue")
    return True


def reschedule_owned_job(
    job_id: int,
    *,
    claimed_by: str,
    error: str,
    available_at: float,
) -> bool:
    """Move the exact live claim back to pending at a durable future time."""
    with _pool().connection() as conn:
        with conn.transaction():
            identity = conn.execute(
                "SELECT user_id FROM agent_jobs WHERE id=%s",
                (int(job_id),),
            ).fetchone()
            if identity is None:
                return False
            control = conn.execute(
                "SELECT hosted_runtime_state,runtime_generation "
                "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                (str(identity[0]),),
            ).fetchone()
            if control is None or str(control[0]) != "v2":
                return False
            runtime_generation = int(control[1])
            cur = conn.execute(
                "UPDATE agent_jobs SET status='pending',"
                "available_at=to_timestamp(%s),last_error=%s,"
                "attempt_count=attempt_count+1,claimed_by=NULL,claimed_at=NULL,"
                "started_at=NULL,finished_at=NULL,lease_expires_at=NULL,"
                "deadline_at=NULL "
                "WHERE id=%s AND claimed_by=%s "
                "AND status IN ('claimed','running') "
                "AND lease_expires_at > clock_timestamp() "
                "AND expected_runtime_generation=%s",
                (
                    float(available_at),
                    str(error)[:500],
                    int(job_id),
                    str(claimed_by),
                    runtime_generation,
                ),
            )
            return cur.rowcount == 1


def make_pending_job_ready(user_id: str, *, lane: str = "profile") -> bool:
    """Make one current-generation delayed pending job immediately claimable."""
    with _pool().connection() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs AS job SET available_at=clock_timestamp() "
            "WHERE job.user_id=%s AND job.lane=%s AND job.status='pending' "
            "AND job.available_at > clock_timestamp() "
            "AND EXISTS (SELECT 1 FROM v2_runtime_state AS state "
            "WHERE state.user_id=job.user_id "
            "AND state.hosted_runtime_state='v2' "
            "AND state.runtime_generation=job.expected_runtime_generation)",
            (str(user_id), str(lane)),
        )
        return cur.rowcount == 1


def mark_expired(job_id, error: str = "runtime_expired") -> None:
    recovered_reviews: list[tuple[str, str]] = []
    with _pool().connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                "UPDATE agent_jobs SET status='expired',finished_at=now(),last_error=%s "
                "WHERE id=%s RETURNING id,user_id,lane",
                (str(error)[:500], job_id),
            )
            row = cur.fetchone()
            if row is not None:
                if str(row[2]) in {"chat", "scheduled"}:
                    _queue_terminal_failure_on_cursor(
                        cur, row[0], str(row[1]), error
                    )
                recovered_reviews = _recover_review_runner_on_cursor(cur, job_id)
                _queue_failure_review_on_cursor(cur, job_id)
    if recovered_reviews:
        from tee_shadow import mirror
        for user_id, source_job_id in recovered_reviews:
            mirror.mark_pending(
                user_id, "v2_trajectory_reviews", source_job_id, "requeue")


def recover_killed_job(
    *,
    job_id: int,
    claimed_by: str,
    reason: str = "slot_watchdog_timeout",
) -> dict[str, object] | None:
    """Recover exactly one claim still owned by a killed slot generation."""
    recovered_reviews: list[tuple[str, str]] = []
    result: dict[str, object] | None = None
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id,user_id,lane,status,claimed_by FROM agent_jobs "
                    "WHERE id=%s AND claimed_by=%s "
                    "AND status IN ('claimed','running') FOR UPDATE",
                    (int(job_id), str(claimed_by)),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                lane = str(row["lane"])
                if lane == "chat":
                    recovery = "terminal"
                    cur.execute(
                        "UPDATE agent_jobs SET status='expired',finished_at=now(), "
                        "last_error=%s,attempt_count=attempt_count+1,"
                        "claimed_by=NULL,claimed_at=NULL,started_at=NULL,"
                        "lease_expires_at=NULL,deadline_at=NULL "
                        "WHERE id=%s AND claimed_by=%s "
                        "AND status IN ('claimed','running')",
                        (str(reason)[:500], int(job_id), str(claimed_by)),
                    )
                    if cur.rowcount != 1:
                        return None
                    _queue_terminal_failure_on_cursor(
                        cur, int(job_id), str(row["user_id"]), str(reason)
                    )
                    recovered_reviews = _recover_review_runner_on_cursor(
                        cur, int(job_id)
                    )
                    _queue_failure_review_on_cursor(cur, int(job_id))
                else:
                    recovery = "requeued"
                    cur.execute(
                        "UPDATE agent_jobs SET status='pending',created_at=now(), "
                        "last_error=%s,attempt_count=attempt_count+1,"
                        "claimed_by=NULL,claimed_at=NULL,started_at=NULL,"
                        "finished_at=NULL,lease_expires_at=NULL,deadline_at=NULL "
                        "WHERE id=%s AND claimed_by=%s "
                        "AND status IN ('claimed','running')",
                        (str(reason)[:500], int(job_id), str(claimed_by)),
                    )
                    if cur.rowcount != 1:
                        return None
                result = {
                    "job_id": int(job_id),
                    "user_id": str(row["user_id"]),
                    "lane": lane,
                    "recovery": recovery,
                }
    if recovered_reviews:
        from tee_shadow import mirror

        for user_id, source_job_id in recovered_reviews:
            mirror.mark_pending(
                user_id, "v2_trajectory_reviews", source_job_id, "requeue"
            )
    return result


def reap_stuck_job_rows(now=None) -> list[dict]:
    """Expire overdue pending admissions and claimed/running execution leases.

    The terminal transition releases the single-flight slot. ``now`` is an
    injectable epoch for deterministic tests; ``None`` uses database time.
    Returned rows let the independent watchdog surface chat timeouts.
    """
    ts = float(now) if now is not None else None
    recovered_reviews: list[tuple[str, str]] = []
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "WITH terminal AS ("
                    "  UPDATE agent_jobs SET status='expired', finished_at=now(), "
                    "  attempt_count=attempt_count+1, "
                    "  last_error=CASE WHEN EXISTS ("
                    "    SELECT 1 FROM v2_mcp_mutation_attempts a "
                    "    WHERE a.job_id=agent_jobs.id "
                    "      AND (a.outcome IS NULL OR a.outcome='unknown')"
                    "  ) THEN 'mcp_mutation_outcome_unknown' "
                    "  WHEN status='pending' THEN 'queue_timeout' "
                    "  ELSE 'lease_timeout' END "
                    "  WHERE (status='pending' "
                    "         AND COALESCE(queue_deadline_at, deadline_at, "
                    "             CASE WHEN lane='chat' THEN "
                    "               created_at + make_interval(secs => %s) END) "
                    "             <= COALESCE(to_timestamp(%s), now())) "
                    "     OR (status IN ('claimed','running') "
                    "         AND COALESCE(lease_expires_at, deadline_at) IS NOT NULL "
                    "         AND COALESCE(lease_expires_at, deadline_at) "
                    "             <= COALESCE(to_timestamp(%s), now())) "
                    "  RETURNING id,user_id,lane,last_error,claimed_by"
                    "), mutation_unknown AS ("
                    "  UPDATE v2_mcp_mutation_attempts a "
                    "  SET outcome='unknown',resolved_at=clock_timestamp() "
                    "  FROM terminal t WHERE a.job_id=t.id AND a.outcome IS NULL "
                    "  RETURNING a.job_id"
                    ") SELECT id,user_id,lane,last_error,claimed_by FROM terminal",
                    (float(PENDING_CHAT_TTL_SEC), ts, ts),
                )
                rows = [dict(row) for row in cur.fetchall()]
                for row in rows:
                    if str(row["lane"]) in {"chat", "scheduled"}:
                        _queue_terminal_failure_on_cursor(
                            cur,
                            row["id"],
                            str(row["user_id"]),
                            str(row["last_error"]),
                        )
                    recovered_reviews.extend(
                        _recover_review_runner_on_cursor(cur, row["id"]))
                    _queue_failure_review_on_cursor(cur, row["id"])
                # Prepared Capture journals are retry-only encrypted content.
                # Retire obsolete runtime generations immediately and bound a
                # current-generation crash orphan to 24h. Active turns have a
                # five-minute lease, so this never races healthy execution.
                cur.execute(
                    "DELETE FROM v2_capture_batches WHERE id IN ("
                    " SELECT b.id FROM v2_capture_batches b "
                    " LEFT JOIN v2_runtime_state s ON s.user_id=b.user_id "
                    " WHERE s.user_id IS NULL "
                    "    OR b.runtime_generation<>s.runtime_generation "
                    "    OR b.created_at<clock_timestamp()-interval '24 hours' "
                    " ORDER BY b.created_at LIMIT 100"
                    ")"
                )
    if recovered_reviews:
        from tee_shadow import mirror
        for user_id, source_job_id in recovered_reviews:
            mirror.mark_pending(
                user_id, "v2_trajectory_reviews", source_job_id, "requeue")
    return rows


def ensure_terminal_failure_outbox(
    job_id,
    user_id: str,
    error: str,
    *,
    error_class: str = "",
) -> bool:
    """Idempotently ensure a durable visibility marker exists for ``job_id``.

    ``mark_failed`` and the timeout reaper create this in the terminal
    transaction.  The explicit helper also covers post-completion delivery
    uncertainty, which is user-visible even though the reply job itself stays
    completed.
    """
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # `_surface_terminal_error` is deliberately retriable and can
                # run after a worker lost ownership.  Share the transcript-clear
                # fence and require the source job's pinned V2 generation so a
                # stale callback cannot recreate status/last_runtime_error after
                # clear deleted the old obligation.
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                cur.execute(
                    "SELECT j.id FROM agent_jobs j "
                    "JOIN v2_runtime_state s ON s.user_id=j.user_id "
                    "WHERE j.id=%s AND j.user_id=%s "
                    "AND s.hosted_runtime_state='v2' "
                    "AND j.expected_runtime_generation=s.runtime_generation",
                    (job_id, str(user_id)),
                )
                if cur.fetchone() is None:
                    return False
                return _queue_terminal_failure_on_cursor(
                    cur,
                    job_id,
                    str(user_id),
                    error,
                    error_class=error_class,
                )


_CAPTURE_STATE_KIND = "capture_state"
_CAPTURE_ACTION_TYPES = frozenset({"memory.add", "memory.supersede"})
_CAPTURE_PROVIDER_DB_KEEPALIVE_SEC = 15.0
_CAPTURE_ENVELOPE_FIELDS = frozenset(
    {
        "id",
        "body_ct",
        "nonce",
        "K_user",
        "K_enclave",
        "enclave_pk_fpr",
        "visibility",
        "owner_user_id",
        "occurred_at",
        "type",
        "source",
        "status",
        "importance",
        "pulse",
        "last_referenced_at",
        "anchor_memory_ids",
        "is_sensitive",
        "sensitivity_class",
    }
)


def _capture_provider_db_keepalive(cur) -> None:
    cur.execute("SELECT 1")


def _cancel_and_drain_capture_provider_future(pending_result: Future) -> None:
    """Do not release disclosure locks until the provider Task is terminal."""
    pending_result.cancel()
    try:
        pending_result.result()
    except BaseException:
        # The caller re-raises the original provider/poll/DB failure.  This
        # result call exists solely as a synchronous privacy drain point.
        pass


def _capture_turns_halted_on_cursor(cur) -> bool:
    """Read and lock D4 before any per-user Capture lock.

    ``FOR SHARE`` permits unrelated users' short Capture boundaries to proceed
    concurrently but conflicts with the control plane's halt ``UPDATE``. Thus a
    provider authorization/preparation/commit either finishes before that
    update linearizes, or observes ``turns_halted=true`` after it. Missing
    singleton state fails closed.
    """
    cur.execute(
        "SELECT turns_halted FROM v2_runtime_control WHERE id=1 FOR SHARE"
    )
    row = cur.fetchone()
    if row is None:
        return True
    value = row["turns_halted"] if isinstance(row, dict) else row[0]
    return bool(value)


def _mirror_capture_state_current(user_id: str) -> None:
    """Mirror only the current primary row while holding Chat Clear's fence.

    Capture transactions commit before best-effort TEE I/O. Re-reading under a
    fresh shared fence prevents an old postcommit callback from resurrecting a
    row after Clear, and mirrors a newer state instead of an obsolete snapshot
    when another Capture transition won in between.
    """
    try:
        with _pool().connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                    cur.execute(
                        "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s",
                        (str(user_id), _CAPTURE_STATE_KIND),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        db._mirror_persisted_blob(
                            str(user_id),
                            _CAPTURE_STATE_KIND,
                            dict(row["doc"] or {}),
                        )
    except Exception as exc:  # noqa: BLE001 — primary transition already committed
        log.warning(
            "[v2.jobs] capture_state mirror deferred user=%s code=%s",
            user_id,
            type(exc).__name__.lower(),
        )


def _capture_owned_job_on_cursor(cur, job_id, user_id: str, claimed_by: str):
    """Lock and validate one running Capture job after outer fences.

    Capture disclosure/mutation boundaries take D4 first, then chat, optional
    memory, and consent fences before entering here. Other lifecycle helpers
    take chat first. Within either prefix, runtime row -> job row is invariant;
    renew/claim use that same order and reversing the two can deadlock them.
    """
    cur.execute(
        "SELECT hosted_runtime_state,runtime_generation FROM v2_runtime_state "
        "WHERE user_id=%s FOR UPDATE",
        (str(user_id),),
    )
    runtime = cur.fetchone()
    if runtime is None or str(runtime["hosted_runtime_state"]) != "v2":
        return None
    cur.execute(
        "SELECT id,user_id,lane,status,claimed_by,lease_expires_at,"
        "expected_runtime_generation,"
        "lease_expires_at > clock_timestamp() AS lease_valid "
        "FROM agent_jobs WHERE id=%s FOR UPDATE",
        (job_id,),
    )
    job = cur.fetchone()
    if (
        job is None
        or str(job["user_id"]) != str(user_id)
        or str(job["lane"]) != "capture"
        or str(job["status"]) not in {"claimed", "running"}
        or str(job["claimed_by"] or "") != str(claimed_by)
        or job["lease_expires_at"] is None
        or not bool(job["lease_valid"])
        or int(job["expected_runtime_generation"] or 0)
        != int(runtime["runtime_generation"])
    ):
        return None
    return job


def _validate_capture_actions(user_id: str, actions: list[dict]) -> list[dict]:
    if not isinstance(actions, list) or len(actions) > 20:
        raise ValueError("capture actions must be a list of at most 20 items")
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("capture action must be an object")
        action_type = str(action.get("type") or "")
        if action_type not in _CAPTURE_ACTION_TYPES:
            raise ValueError("unsupported capture action")
        envelope = action.get("envelope")
        if not isinstance(envelope, dict):
            raise ValueError("capture action envelope required")
        memory_id = str(envelope.get("id") or "")
        if (
            not memory_id
            or len(memory_id) > 160
            or memory_id in seen_ids
            or str(envelope.get("owner_user_id") or "") != str(user_id)
        ):
            raise ValueError("invalid capture memory identity")
        seen_ids.add(memory_id)
        for field in (
            "body_ct",
            "nonce",
            "K_user",
            "K_enclave",
            "visibility",
            "occurred_at",
            "type",
        ):
            if not envelope.get(field):
                raise ValueError(f"capture envelope missing {field}")
        if str(envelope["visibility"]) != "shared":
            raise ValueError("capture envelope must be shared")
        if str(envelope["type"]) not in {"moment", "quote", "fact", "event"}:
            raise ValueError("invalid capture memory type")
        if action_type == "memory.supersede":
            raw = action.get("supersedes")
            values = raw if isinstance(raw, list) else [raw]
            targets = [str(value or "") for value in values if str(value or "")]
            if not targets:
                raise ValueError("capture supersede target required")
        # Persist only ciphertext and non-content metadata.  Parser/provider
        # scratch fields (including the plaintext draft) must never enter this
        # retry journal.
        clean_action = {
            "type": action_type,
            "envelope": {
                key: envelope[key]
                for key in _CAPTURE_ENVELOPE_FIELDS
                if key in envelope
            },
        }
        if action_type == "memory.supersede":
            clean_action["supersedes"] = targets
        normalized.append(clean_action)
    return normalized


def _capture_allowed_on_cursor(
    cur, user_id: str, *, lock_row: bool = True
) -> bool:
    """Read Capture consent while the caller holds its advisory lock.

    Mutation boundaries also lock the settings row because they update related
    state in the same transaction.  The long provider-disclosure fence only
    needs the advisory consent lock: keeping the settings row unlocked avoids
    an unnecessary row lock while the network call is in flight.
    """
    cur.execute(
        "SELECT doc FROM user_blobs WHERE user_id=%s "
        "AND kind='proactive_settings'" + (" FOR UPDATE" if lock_row else ""),
        (str(user_id),),
    )
    row = cur.fetchone()
    if row is None:
        return True
    doc = row["doc"]
    if not isinstance(doc, dict):
        return False
    return bool(doc.get("capture_enabled", True))


def _cancel_capture_on_cursor(
    cur,
    *,
    job,
    job_id,
    user_id: str,
    claimed_by: str,
    error: str = "capture_disabled",
) -> dict:
    """Purge prepared content and cancel one already-fenced job without backoff."""
    cur.execute(
        "DELETE FROM v2_capture_batches WHERE user_id=%s "
        "AND runtime_generation=%s",
        (str(user_id), int(job["expected_runtime_generation"])),
    )
    cur.execute(
        "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,%s,'{}') "
        "ON CONFLICT (user_id,kind) DO NOTHING",
        (str(user_id), _CAPTURE_STATE_KIND),
    )
    cur.execute(
        "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s FOR UPDATE",
        (str(user_id), _CAPTURE_STATE_KIND),
    )
    return _capture_fail_on_cursor(
        cur,
        state=dict(cur.fetchone()["doc"] or {}),
        job_id=job_id,
        user_id=str(user_id),
        claimed_by=str(claimed_by),
        error=error,
        increment_backoff=False,
    )


def prepare_capture_batch(
    *,
    job_id,
    user_id: str,
    claimed_by: str,
    window: dict,
    actions: list[dict],
) -> dict | None:
    """Persist encrypted prepared actions once under the Chat Clear fence.

    A retry for the same runtime-generation/frontier returns the first durable
    action list, so provider nondeterminism can never alter a partially retried
    batch.
    """
    normalized = _validate_capture_actions(str(user_id), actions)
    after_seq = int(window.get("after_seq") or 0)
    through_seq = int(window.get("through_seq") or 0)
    if through_seq <= after_seq:
        raise ValueError("capture batch must advance the frontier")
    persisted_state: dict | None = None
    result: dict | None = None
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                halted = _capture_turns_halted_on_cursor(cur)
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                db._lock_capture_consent_on_cursor(cur, str(user_id))
                job = _capture_owned_job_on_cursor(
                    cur, job_id, str(user_id), str(claimed_by)
                )
                if job is None:
                    return None
                if halted:
                    persisted_state = _cancel_capture_on_cursor(
                        cur,
                        job=job,
                        job_id=job_id,
                        user_id=str(user_id),
                        claimed_by=str(claimed_by),
                        error="turns_halted",
                    )
                    result = {
                        "rejected": True,
                        "reason": "turns_halted",
                    }
                elif not _capture_allowed_on_cursor(cur, str(user_id)):
                    persisted_state = _cancel_capture_on_cursor(
                        cur,
                        job=job,
                        job_id=job_id,
                        user_id=str(user_id),
                        claimed_by=str(claimed_by),
                    )
                    result = {
                        "rejected": True,
                        "reason": "capture_disabled",
                    }
                else:
                    generation = int(job["expected_runtime_generation"])
                    cur.execute(
                        "INSERT INTO v2_capture_batches "
                        "(user_id,runtime_generation,after_seq,through_seq,"
                        "after_message_id,until_message_id,until_ts,actions_json,"
                        "action_count,prepared_by_job_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (user_id,runtime_generation,after_seq) DO NOTHING",
                        (
                            str(user_id),
                            generation,
                            after_seq,
                            through_seq,
                            str(window.get("after_message_id") or ""),
                            str(window.get("until_message_id") or ""),
                            float(window.get("until_ts") or 0.0),
                            Jsonb(normalized),
                            len(normalized),
                            job_id,
                        ),
                    )
                    cur.execute(
                        "SELECT * FROM v2_capture_batches WHERE user_id=%s "
                        "AND runtime_generation=%s AND after_seq=%s FOR UPDATE",
                        (str(user_id), generation, after_seq),
                    )
                    row = cur.fetchone()
                    if row is not None and str(row["status"]) == "prepared":
                        result = dict(row)
    if persisted_state is not None:
        _mirror_capture_state_current(str(user_id))
    return result


def get_prepared_capture_batch(
    *,
    job_id,
    user_id: str,
    claimed_by: str,
    after_seq: int,
) -> dict | None:
    """Adopt an earlier crash's encrypted journal before calling provider."""
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                job = _capture_owned_job_on_cursor(
                    cur, job_id, str(user_id), str(claimed_by)
                )
                if job is None:
                    return None
                # At most one Capture job is active per user/lane/generation.
                # A journal at another frontier can only be a stale legacy
                # translation/crash artifact; retaining it would keep encrypted
                # chat-derived content forever with no path that can adopt it.
                cur.execute(
                    "DELETE FROM v2_capture_batches WHERE user_id=%s "
                    "AND (runtime_generation<>%s OR after_seq<>%s)",
                    (
                        str(user_id),
                        int(job["expected_runtime_generation"]),
                        max(0, int(after_seq)),
                    ),
                )
                cur.execute(
                    "SELECT * FROM v2_capture_batches WHERE user_id=%s "
                    "AND runtime_generation=%s AND after_seq=%s "
                    "AND status='prepared' FOR UPDATE",
                    (
                        str(user_id),
                        int(job["expected_runtime_generation"]),
                        max(0, int(after_seq)),
                    ),
                )
                row = cur.fetchone()
                return dict(row) if row is not None else None


def _capture_memory_doc(user_id: str, action: dict) -> dict:
    envelope = dict(action["envelope"])
    occurred_at = str(envelope["occurred_at"])
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    doc = {
        "v": 1,
        "id": str(envelope["id"]),
        "type": str(envelope["type"]),
        "occurred_at": occurred_at,
        "created_at": now_iso,
        "updated_at": now_iso,
        "source": str(envelope.get("source") or "memory_capture"),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "K_enclave": envelope["K_enclave"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": "shared",
        "owner_user_id": str(user_id),
        "status": str(envelope.get("status") or "active"),
        "importance": float(envelope.get("importance") or 0.0),
        "pulse": float(envelope.get("pulse") or 0.0),
        "last_referenced_at": str(
            envelope.get("last_referenced_at") or occurred_at
        ),
    }
    if envelope.get("anchor_memory_ids"):
        doc["anchor_memory_ids"] = list(envelope["anchor_memory_ids"])
    for key in ("is_sensitive", "sensitivity_class"):
        if key in envelope:
            doc[key] = envelope[key]
    if action["type"] == "memory.supersede":
        raw = action.get("supersedes")
        doc["supersedes"] = list(raw if isinstance(raw, list) else [raw])
    return doc


def _capture_same_memory(existing: dict, wanted: dict) -> bool:
    return all(
        existing.get(key) == wanted.get(key)
        for key in (
            "id",
            "type",
            "body_ct",
            "nonce",
            "K_user",
            "K_enclave",
            "visibility",
            "owner_user_id",
            "supersedes",
        )
    )


def _capture_fail_on_cursor(
    cur,
    *,
    state: dict,
    job_id,
    user_id: str,
    claimed_by: str,
    error: str,
    increment_backoff: bool = True,
) -> dict:
    """Fail an already-validated owned Capture job in the caller's txn."""
    failed = dict(state)
    failed.update(
        {
            "pending_capture_key": "",
            "capture_fail_streak": int(failed.get("capture_fail_streak") or 0)
            + (1 if increment_backoff else 0),
            "last_capture_failed_at": (
                time.time()
                if increment_backoff
                else float(failed.get("last_capture_failed_at") or 0.0)
            ),
            "updated_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )
    cur.execute(
        "UPDATE user_blobs SET doc=%s WHERE user_id=%s AND kind=%s",
        (Jsonb(failed), str(user_id), _CAPTURE_STATE_KIND),
    )
    cur.execute(
        "UPDATE agent_jobs SET status='failed',finished_at=now(),"
        "last_error=%s,attempt_count=attempt_count+1 "
        "WHERE id=%s AND status IN ('claimed','running') "
        "AND claimed_by=%s AND lease_expires_at>now()",
        (_terminal_error_code(error), job_id, str(claimed_by)),
    )
    if cur.rowcount != 1:
        raise RuntimeError("capture ownership lost at failure commit")
    return failed


def _capture_owned_job_for_disclosure_on_cursor(
    cur, job_id, user_id: str, claimed_by: str
):
    """Validate a Capture owner without freezing its lease row.

    The caller already owns D4, Chat Clear, and consent transaction fences.  A
    shared runtime-state row lock additionally keeps cutover/generation stable
    for the whole disclosure.  The job row itself remains unlocked so the
    normal keepalive can renew it during a multi-attempt provider call; prepare
    and commit revalidate the exact owner afterwards before any durable write.
    """
    cur.execute(
        "SELECT hosted_runtime_state,runtime_generation FROM v2_runtime_state "
        "WHERE user_id=%s FOR SHARE",
        (str(user_id),),
    )
    runtime = cur.fetchone()
    if runtime is None or str(runtime["hosted_runtime_state"]) != "v2":
        return None
    cur.execute(
        "SELECT id,user_id,lane,status,claimed_by,lease_expires_at,"
        "expected_runtime_generation,"
        "lease_expires_at > clock_timestamp() AS lease_valid "
        "FROM agent_jobs WHERE id=%s",
        (job_id,),
    )
    job = cur.fetchone()
    if (
        job is None
        or str(job["user_id"]) != str(user_id)
        or str(job["lane"]) != "capture"
        or str(job["status"]) not in {"claimed", "running"}
        or str(job["claimed_by"] or "") != str(claimed_by)
        or job["lease_expires_at"] is None
        or not bool(job["lease_valid"])
        or int(job["expected_runtime_generation"] or 0)
        != int(runtime["runtime_generation"])
    ):
        return None
    return job


def authorize_capture_provider_call(
    *,
    job_id,
    user_id: str,
    claimed_by: str,
    provider_call: Callable[[], Any] | None = None,
) -> dict:
    """Run one Capture provider disclosure inside every revocation fence.

    The pooled connection and transaction live entirely on the synchronous
    caller thread.  ``worker`` supplies a callback which bridges the provider
    coroutine back to its owning event loop; the psycopg connection is never
    touched by that loop or another thread.  D4 ``FOR SHARE``, Chat Clear's
    shared advisory lock, Capture consent's exclusive advisory lock, and the
    runtime-generation ``FOR SHARE`` lock remain held until the callback has
    completely returned.  Therefore halt, clear, opt-out, and cutover either
    win before disclosure (the callback is never called) or wait until every
    provider byte has finished.

    ``provider_call=None`` is retained only for narrow compatibility tests that
    inspect the gate result. Production callers must pass the callback and
    require ``provider_call_completed=true`` before accepting a result.
    """
    persisted_state: dict | None = None
    authorized = False
    provider_result: Any = None
    provider_call_completed = False
    reason = "ownership_lost"
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                halted = _capture_turns_halted_on_cursor(cur)
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                db._lock_capture_consent_on_cursor(cur, str(user_id))
                if halted:
                    job = _capture_owned_job_on_cursor(
                        cur, job_id, str(user_id), str(claimed_by)
                    )
                    if job is None:
                        return {"authorized": False, "reason": "ownership_lost"}
                    persisted_state = _cancel_capture_on_cursor(
                        cur,
                        job=job,
                        job_id=job_id,
                        user_id=str(user_id),
                        claimed_by=str(claimed_by),
                        error="turns_halted",
                    )
                    reason = "turns_halted"
                elif not _capture_allowed_on_cursor(
                    cur, str(user_id), lock_row=False
                ):
                    job = _capture_owned_job_on_cursor(
                        cur, job_id, str(user_id), str(claimed_by)
                    )
                    if job is None:
                        return {"authorized": False, "reason": "ownership_lost"}
                    persisted_state = _cancel_capture_on_cursor(
                        cur,
                        job=job,
                        job_id=job_id,
                        user_id=str(user_id),
                        claimed_by=str(claimed_by),
                    )
                    reason = "capture_disabled"
                else:
                    job = _capture_owned_job_for_disclosure_on_cursor(
                        cur, job_id, str(user_id), str(claimed_by)
                    )
                    if job is None:
                        return {"authorized": False, "reason": "ownership_lost"}
                    authorized = True
                    if provider_call is not None:
                        # The callback may persist trajectory events through a
                        # nested pooled connection. Propagate the fact that this
                        # transaction already owns Chat Clear's shared fence so
                        # PostgreSQL's fair lock queue cannot place that nested
                        # shared request behind a waiting exclusive clear and
                        # deadlock outer -> nested -> clear -> outer.
                        with db._chat_user_fence_held_by_outer_transaction(
                            str(user_id)
                        ):
                            pending_result = provider_call()
                            if isinstance(pending_result, Future):
                                # A provider attempt can legitimately span
                                # minutes. Keep the transaction non-idle so a
                                # database idle-in-transaction policy cannot
                                # silently release D4/consent locks while bytes
                                # are still crossing the provider boundary.
                                try:
                                    while True:
                                        try:
                                            provider_result = pending_result.result(
                                                timeout=_CAPTURE_PROVIDER_DB_KEEPALIVE_SEC
                                            )
                                            break
                                        except FutureTimeoutError:
                                            _capture_provider_db_keepalive(cur)
                                except BaseException:
                                    _cancel_and_drain_capture_provider_future(
                                        pending_result
                                    )
                                    raise
                            else:
                                # Narrow synchronous/test callback compatibility.
                                provider_result = pending_result
                        provider_call_completed = True
                        # The row deliberately was not locked during network I/O.
                        # Detect a reaper/owner loss before returning plaintext to
                        # the rest of the Capture pipeline; prepare/commit repeat
                        # this check under their stronger mutation locks.
                        if _capture_owned_job_for_disclosure_on_cursor(
                            cur, job_id, str(user_id), str(claimed_by)
                        ) is None:
                            authorized = False
                            reason = "ownership_lost"
    if persisted_state is not None:
        _mirror_capture_state_current(str(user_id))
    if authorized:
        result = {"authorized": True}
        if provider_call is not None:
            result.update(
                {
                    "provider_call_completed": provider_call_completed,
                    "provider_result": provider_result,
                }
            )
        return result
    if provider_call_completed:
        return {
            "authorized": False,
            "reason": reason,
            "provider_call_completed": True,
            "provider_result": provider_result,
        }
    return {
        "authorized": False,
        "reason": reason,
        "rejected": True,
    }


def commit_capture_batch(
    *,
    job_id,
    user_id: str,
    claimed_by: str,
    batch_id,
) -> dict:
    """Atomically apply every memory effect, advance seq, and finish the job."""
    affected_ids: set[str] = set()
    persisted_state: dict | None = None
    mirrored_logs: list[tuple[int, str, dict, str]] = []
    result: dict = {"committed": False, "reason": "batch_unavailable"}
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                halted = _capture_turns_halted_on_cursor(cur)
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                db._lock_memory_user_mutation_on_cursor(cur, str(user_id))
                db._lock_capture_consent_on_cursor(cur, str(user_id))
                job = _capture_owned_job_on_cursor(
                    cur, job_id, str(user_id), str(claimed_by)
                )
                if job is None:
                    return {"committed": False, "reason": "ownership_lost"}
                cur.execute(
                    "SELECT * FROM v2_capture_batches WHERE id=%s "
                    "AND user_id=%s FOR UPDATE",
                    (batch_id, str(user_id)),
                )
                batch = cur.fetchone()
                batch_unavailable = (
                    batch is None
                    or str(batch["status"]) != "prepared"
                    or int(batch["runtime_generation"])
                    != int(job["expected_runtime_generation"])
                )
                cur.execute(
                    "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,%s,'{}') "
                    "ON CONFLICT (user_id,kind) DO NOTHING",
                    (str(user_id), _CAPTURE_STATE_KIND),
                )
                cur.execute(
                    "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s "
                    "FOR UPDATE",
                    (str(user_id), _CAPTURE_STATE_KIND),
                )
                state = dict(cur.fetchone()["doc"] or {})
                capture_allowed = (
                    False
                    if halted
                    else _capture_allowed_on_cursor(cur, str(user_id))
                )
                raw_seq = state.get("last_captured_until_seq")
                if str(raw_seq or "").isdigit():
                    current_seq = int(raw_seq)
                else:
                    legacy_id = str(
                        state.get("last_captured_until_message_id") or ""
                    )
                    cur.execute(
                        "SELECT seq FROM chat_messages WHERE user_id=%s "
                        "AND msg_id=%s",
                        (str(user_id), legacy_id),
                    )
                    legacy_row = cur.fetchone()
                    current_seq = (
                        int(legacy_row["seq"]) if legacy_row is not None else 0
                    )
                if halted:
                    persisted_state = _cancel_capture_on_cursor(
                        cur,
                        job=job,
                        job_id=job_id,
                        user_id=str(user_id),
                        claimed_by=str(claimed_by),
                        error="turns_halted",
                    )
                    result = {
                        "committed": False,
                        "reason": "turns_halted",
                        "rejected": True,
                    }
                elif not capture_allowed:
                    cur.execute(
                        "DELETE FROM v2_capture_batches WHERE user_id=%s "
                        "AND runtime_generation=%s",
                        (
                            str(user_id),
                            int(job["expected_runtime_generation"]),
                        ),
                    )
                    persisted_state = _capture_fail_on_cursor(
                        cur,
                        state=state,
                        job_id=job_id,
                        user_id=str(user_id),
                        claimed_by=str(claimed_by),
                        error="capture_disabled",
                        increment_backoff=False,
                    )
                    result = {
                        "committed": False,
                        "reason": "capture_disabled",
                        "rejected": True,
                    }
                elif batch_unavailable:
                    # The job is still owned, so terminalize it atomically
                    # instead of masquerading as a stale lease. This can happen
                    # when opt-out erased a journal and opt-in raced back before
                    # the old worker resumed.
                    persisted_state = _capture_fail_on_cursor(
                        cur,
                        state=state,
                        job_id=job_id,
                        user_id=str(user_id),
                        claimed_by=str(claimed_by),
                        error="capture_batch_unavailable",
                    )
                    result = {
                        "committed": False,
                        "reason": "batch_unavailable",
                        "rejected": True,
                    }
                elif current_seq != int(batch["after_seq"]):
                    cur.execute(
                        "DELETE FROM v2_capture_batches WHERE id=%s", (batch_id,)
                    )
                    persisted_state = _capture_fail_on_cursor(
                        cur,
                        state=state,
                        job_id=job_id,
                        user_id=str(user_id),
                        claimed_by=str(claimed_by),
                        error="capture_frontier_changed",
                    )
                    result = {
                        "committed": False,
                        "reason": "frontier_changed",
                        "rejected": True,
                    }
                else:
                    actions = _validate_capture_actions(
                        str(user_id), list(batch["actions_json"] or [])
                    )
                    prepared: list[
                        tuple[dict, dict, dict | None, list[tuple[str, dict]]]
                    ] = []
                    rejection = ""
                    cards_added = 0
                    # Validate and lock the complete effect set before the first
                    # write.  Semantic poison is rejected durably; transient DB
                    # errors still roll back and retain the prepared journal.
                    for action in actions:
                        wanted = _capture_memory_doc(str(user_id), action)
                        memory_id = str(wanted["id"])
                        cur.execute(
                            "SELECT doc FROM memory_moments WHERE user_id=%s "
                            "AND moment_id=%s FOR UPDATE",
                            (str(user_id), memory_id),
                        )
                        existing_row = cur.fetchone()
                        existing = (
                            dict(existing_row["doc"] or {})
                            if existing_row is not None
                            else None
                        )
                        if existing is not None and not _capture_same_memory(
                            existing, wanted
                        ):
                            rejection = "capture_memory_id_conflict"
                            break
                        targets_locked: list[tuple[str, dict]] = []
                        if action["type"] == "memory.supersede":
                            for target_id in action.get("supersedes") or []:
                                target_id = str(target_id)
                                cur.execute(
                                    "SELECT doc FROM memory_moments "
                                    "WHERE user_id=%s AND moment_id=%s FOR UPDATE",
                                    (str(user_id), target_id),
                                )
                                target_row = cur.fetchone()
                                if target_row is None:
                                    rejection = "capture_supersede_target_missing"
                                    break
                                target = dict(target_row["doc"] or {})
                                if str(target.get("owner_user_id") or "") != str(
                                    user_id
                                ):
                                    rejection = "capture_supersede_not_owned"
                                    break
                                targets_locked.append((target_id, target))
                        if rejection:
                            break
                        prepared.append((action, wanted, existing, targets_locked))

                    if rejection:
                        cur.execute(
                            "DELETE FROM v2_capture_batches WHERE id=%s", (batch_id,)
                        )
                        persisted_state = _capture_fail_on_cursor(
                            cur,
                            state=state,
                            job_id=job_id,
                            user_id=str(user_id),
                            claimed_by=str(claimed_by),
                            error=rejection,
                        )
                        result = {
                            "committed": False,
                            "reason": rejection,
                            "rejected": True,
                        }
                    else:
                        now_ts = time.time()
                        now_iso = datetime.fromtimestamp(
                            now_ts, timezone.utc
                        ).isoformat().replace("+00:00", "Z")
                        for ordinal, (
                            action,
                            wanted,
                            existing,
                            targets_locked,
                        ) in enumerate(prepared):
                            memory_id = str(wanted["id"])
                            if existing is None:
                                cur.execute(
                                    "INSERT INTO memory_moments "
                                    "(user_id,moment_id,occurred_at,doc) "
                                    "VALUES (%s,%s,%s,%s)",
                                    (
                                        str(user_id),
                                        memory_id,
                                        str(wanted["occurred_at"]),
                                        Jsonb(wanted),
                                    ),
                                )
                                bootstrap_id = "capboot_" + hashlib.sha256(
                                    f"{batch_id}:{ordinal}".encode("utf-8")
                                ).hexdigest()[:24]
                                bootstrap_doc = {
                                    "user_id": str(user_id),
                                    "event_type": "memory_action_added_envelope_v1",
                                    "success": True,
                                    "error_message": "",
                                    "timestamp": now_iso,
                                }
                                cur.execute(
                                    "INSERT INTO user_logs "
                                    "(user_id,stream,item_key,doc) "
                                    "VALUES (%s,'bootstrap_events',%s,%s) "
                                    "RETURNING seq",
                                    (
                                        str(user_id),
                                        bootstrap_id,
                                        Jsonb(bootstrap_doc),
                                    ),
                                )
                                mirrored_logs.append(
                                    (
                                        int(cur.fetchone()["seq"]),
                                        "bootstrap_events",
                                        bootstrap_doc,
                                        bootstrap_id,
                                    )
                                )
                                if action["type"] == "memory.add":
                                    cards_added += 1
                            affected_ids.add(memory_id)
                            for target_id, target in targets_locked:
                                target.update(
                                    {
                                        "status": "superseded",
                                        "superseded_by": memory_id,
                                        "updated_at": now_iso,
                                        "is_archived": True,
                                        "archived_at": now_iso,
                                        "archive_reason": f"superseded_by:{memory_id}",
                                    }
                                )
                                cur.execute(
                                    "UPDATE memory_moments SET doc=%s "
                                    "WHERE user_id=%s AND moment_id=%s",
                                    (Jsonb(target), str(user_id), target_id),
                                )
                                affected_ids.add(target_id)

                            change_id = "capchg_" + hashlib.sha256(
                                f"{batch_id}:{ordinal}".encode("utf-8")
                            ).hexdigest()[:24]
                            change_doc = {
                                "id": change_id,
                                "ts": now_iso,
                                "action": (
                                    "supersede"
                                    if action["type"] == "memory.supersede"
                                    else "insert"
                                ),
                                "memory_id": memory_id,
                                "type": str(wanted.get("type") or ""),
                                "capture_mode": "memory_capture",
                            }
                            if action["type"] == "memory.supersede":
                                change_doc["supersedes"] = list(
                                    action.get("supersedes") or []
                                )
                            cur.execute(
                                "INSERT INTO user_logs "
                                "(user_id,stream,item_key,doc) "
                                "VALUES (%s,'memory_changes',%s,%s) RETURNING seq",
                                (str(user_id), change_id, Jsonb(change_doc)),
                            )
                            mirrored_logs.append(
                                (
                                    int(cur.fetchone()["seq"]),
                                    "memory_changes",
                                    change_doc,
                                    change_id,
                                )
                            )

                        state.update(
                            {
                                "last_captured_until_message_id": str(
                                    batch["until_message_id"]
                                ),
                                "last_captured_until_ts": float(batch["until_ts"]),
                                "last_captured_until_seq": int(batch["through_seq"]),
                                "capture_seq_initialized": True,
                                "last_capture_completed_at": now_ts,
                                "pending_capture_key": "",
                                "capture_fail_streak": 0,
                                "last_capture_failed_at": 0.0,
                                "updated_at": now_iso,
                            }
                        )
                        state.update(capture_daily.daily_capture_patch(
                            state,
                            cards_added=cards_added,
                            completed_at=now_ts,
                        ))
                        cur.execute(
                            "UPDATE user_blobs SET doc=%s "
                            "WHERE user_id=%s AND kind=%s",
                            (Jsonb(state), str(user_id), _CAPTURE_STATE_KIND),
                        )
                        cur.execute(
                            "UPDATE agent_jobs SET status='completed',finished_at=now() "
                            "WHERE id=%s AND status IN ('claimed','running') "
                            "AND claimed_by=%s AND lease_expires_at>now()",
                            (job_id, str(claimed_by)),
                        )
                        if cur.rowcount != 1:
                            raise RuntimeError("capture ownership lost at commit")
                        cur.execute(
                            "DELETE FROM v2_capture_batches WHERE id=%s", (batch_id,)
                        )
                        persisted_state = state
                        result = {
                            "committed": True,
                            "batch_id": int(batch_id),
                            "cards_added": cards_added,
                            "affected_memory_ids": sorted(affected_ids),
                        }
    if persisted_state is not None:
        _mirror_capture_state_current(str(user_id))
    if affected_ids:
        from tee_shadow import mirror

        for memory_id in sorted(affected_ids):
            mirror.mark_pending(
                str(user_id), "memory_moments", memory_id, "requeue"
            )
        for seq, stream, log_doc, item_key in mirrored_logs:
            mirror.execute(
                "INSERT INTO user_logs "
                "(user_id,stream,seq,item_key,doc) OVERRIDING SYSTEM VALUE "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (str(user_id), stream, seq, item_key, Jsonb(log_doc)),
            )
    return result


def fail_capture_job(
    *,
    job_id,
    user_id: str,
    claimed_by: str,
    error: str,
) -> bool:
    """Fail + arm backoff only while this worker still owns the Capture job."""
    persisted_state: dict | None = None
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                if _capture_owned_job_on_cursor(
                    cur, job_id, str(user_id), str(claimed_by)
                ) is None:
                    return False
                cur.execute(
                    "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,%s,'{}') "
                    "ON CONFLICT (user_id,kind) DO NOTHING",
                    (str(user_id), _CAPTURE_STATE_KIND),
                )
                cur.execute(
                    "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s "
                    "FOR UPDATE",
                    (str(user_id), _CAPTURE_STATE_KIND),
                )
                state = dict(cur.fetchone()["doc"] or {})
                persisted_state = _capture_fail_on_cursor(
                    cur,
                    state=state,
                    job_id=job_id,
                    user_id=str(user_id),
                    claimed_by=str(claimed_by),
                    error=error,
                )
    if persisted_state is not None:
        _mirror_capture_state_current(str(user_id))
    return True


def cancel_capture_job(
    *,
    job_id,
    user_id: str,
    claimed_by: str,
    error: str = "capture_disabled",
) -> bool:
    """Consent/global-off cancellation: purge retry content without backoff."""
    persisted_state: dict | None = None
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                db._lock_capture_consent_on_cursor(cur, str(user_id))
                job = _capture_owned_job_on_cursor(
                    cur, job_id, str(user_id), str(claimed_by)
                )
                if job is None:
                    return False
                cur.execute(
                    "DELETE FROM v2_capture_batches WHERE user_id=%s "
                    "AND runtime_generation=%s",
                    (str(user_id), int(job["expected_runtime_generation"])),
                )
                cur.execute(
                    "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,%s,'{}') "
                    "ON CONFLICT (user_id,kind) DO NOTHING",
                    (str(user_id), _CAPTURE_STATE_KIND),
                )
                cur.execute(
                    "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s "
                    "FOR UPDATE",
                    (str(user_id), _CAPTURE_STATE_KIND),
                )
                persisted_state = _capture_fail_on_cursor(
                    cur,
                    state=dict(cur.fetchone()["doc"] or {}),
                    job_id=job_id,
                    user_id=str(user_id),
                    claimed_by=str(claimed_by),
                    error=error,
                    increment_backoff=False,
                )
    if persisted_state is not None:
        _mirror_capture_state_current(str(user_id))
    return True


def _pending_terminal_failure_rows(
    sink: str,
    *,
    job_id=None,
    limit: int = 100,
    now=None,
) -> list[dict]:
    """List one sink's due markers with unattempted/fair-rotation priority."""
    if sink == "status":
        delivered_column = "status_delivered_at"
        next_column = "status_next_attempt_at"
        last_column = "status_last_attempt_at"
    elif sink == "runtime_error":
        delivered_column = "runtime_error_delivered_at"
        next_column = "runtime_error_next_attempt_at"
        last_column = "runtime_error_last_attempt_at"
    elif sink == "reply":
        delivered_column = "reply_delivered_at"
        next_column = "reply_next_attempt_at"
        last_column = "reply_last_attempt_at"
    else:
        raise ValueError(f"unknown terminal failure sink: {sink!r}")
    bounded = max(1, min(int(limit), 1000))
    where_job = " AND o.job_id=%s" if job_id is not None else ""
    ts = float(now) if now is not None else None
    args: tuple = (ts, job_id, bounded) if job_id is not None else (ts, bounded)
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT o.job_id,o.user_id,o.error_code,o.target_route_id,"
                "o.target_route_updated_at,o.status_delivered_at,"
                "o.runtime_error_delivered_at,o.error_class,"
                "o.reply_frontier_seq,o.reply_parent_message_id,"
                "o.reply_delivered_at,j.lane "
                "FROM v2_terminal_failure_outbox o "
                "JOIN agent_jobs j ON j.id=o.job_id "
                f"WHERE o.{delivered_column} IS NULL "
                f"AND o.{next_column} <= COALESCE(to_timestamp(%s),now())"
                f"{where_job} ORDER BY o.{last_column} NULLS FIRST,"
                f"o.{next_column},o.created_at,o.job_id LIMIT %s",
                args,
            )
            return [dict(row) for row in cur.fetchall()]


def _defer_terminal_failure_sink(job_id, sink: str, *, now=None) -> None:
    """Rotate a poison marker behind unattempted work with bounded backoff."""
    if sink == "status":
        attempt_column = "status_attempt_count"
        last_column = "status_last_attempt_at"
        next_column = "status_next_attempt_at"
        delivered_column = "status_delivered_at"
    elif sink == "runtime_error":
        attempt_column = "runtime_error_attempt_count"
        last_column = "runtime_error_last_attempt_at"
        next_column = "runtime_error_next_attempt_at"
        delivered_column = "runtime_error_delivered_at"
    elif sink == "reply":
        attempt_column = "reply_attempt_count"
        last_column = "reply_last_attempt_at"
        next_column = "reply_next_attempt_at"
        delivered_column = "reply_delivered_at"
    else:
        raise ValueError(f"unknown terminal failure sink: {sink!r}")
    ts = float(now) if now is not None else None
    with _pool().connection() as conn:
        conn.execute(
            f"UPDATE v2_terminal_failure_outbox SET "
            f"{attempt_column}={attempt_column}+1,"
            f"{last_column}=COALESCE(to_timestamp(%s),now()),"
            f"{next_column}=COALESCE(to_timestamp(%s),now()) + "
            f"make_interval(secs => LEAST(300.0,power(2.0,"
            f"LEAST({attempt_column},8)::double precision))),updated_at=now() "
            f"WHERE job_id=%s AND {delivered_column} IS NULL",
            (ts, ts, job_id),
        )


def _deliver_terminal_failure_status(
    job_id,
    *,
    kind: str,
    label: str | None,
    detail: dict | None,
) -> bool:
    """Atomically insert the unique error event and acknowledge that sink.

    A crash can commit both operations or neither.  ``ON CONFLICT`` adopts an
    event emitted by pre-outbox code, and the partial unique index added in
    0037 prevents duplicate error events across concurrent reconcilers.
    """
    delivered = False
    user_id = ""
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT o.user_id,o.status_delivered_at,j.lane "
                    "FROM v2_terminal_failure_outbox o "
                    "JOIN agent_jobs j ON j.id=o.job_id "
                    "WHERE o.job_id=%s FOR UPDATE OF o",
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None or row["status_delivered_at"] is not None:
                    return False
                user_id = str(row["user_id"])
                # A delayed failure from job A must not appear after a newer
                # job B has atomically published a real final reply. Key this
                # to the applied final effect—not merely status=completed,
                # which also covers empty handoffs/maintenance outcomes.
                if str(row["lane"] or "") == "chat":
                    cur.execute(
                        "SELECT 1 FROM v2_effect_outbox e "
                        "WHERE e.user_id=%s AND e.job_id>%s "
                        "AND e.status='applied' "
                        "AND (e.effect_type='reply_final_fenced_v1' "
                        " OR (e.effect_type='reply' "
                        "     AND e.payload ? 'reply_through_seq')) LIMIT 1",
                        (user_id, job_id),
                    )
                    if cur.fetchone() is not None:
                        cur.execute(
                            "UPDATE v2_terminal_failure_outbox "
                            "SET status_delivered_at=now(),updated_at=now() "
                            "WHERE job_id=%s",
                            (job_id,),
                        )
                        return True
                cur.execute(
                    "INSERT INTO agent_status_events "
                    "(job_id,user_id,kind,label,detail_json,seq) "
                    "VALUES (%s,%s,%s,%s,%s,0) "
                    "ON CONFLICT (job_id) "
                    "WHERE kind='error' AND job_id IS NOT NULL DO NOTHING",
                    (job_id, user_id, str(kind), label, Jsonb(dict(detail or {}))),
                )
                cur.execute(
                    "UPDATE v2_terminal_failure_outbox "
                    "SET status_delivered_at=now(),updated_at=now() "
                    "WHERE job_id=%s",
                    (job_id,),
                )
                delivered = True
    if delivered:
        try:
            wake_bus.notify("chat", user_id)
        except Exception:  # noqa: BLE001 — poll timeout remains the fallback
            pass
    return delivered


def _ack_terminal_runtime_error(job_id) -> bool:
    with _pool().connection() as conn:
        cur = conn.execute(
            "UPDATE v2_terminal_failure_outbox "
            "SET runtime_error_delivered_at=now(),updated_at=now() "
            "WHERE job_id=%s AND runtime_error_delivered_at IS NULL",
            (job_id,),
        )
        return cur.rowcount == 1


def _ack_terminal_failure_reply(job_id) -> bool:
    with _pool().connection() as conn:
        cur = conn.execute(
            "UPDATE v2_terminal_failure_outbox "
            "SET reply_delivered_at=now(),updated_at=now() "
            "WHERE job_id=%s AND reply_delivered_at IS NULL",
            (job_id,),
        )
        return cur.rowcount == 1


def _scheduled_failure_notes(user_id: str, job_id) -> list[str]:
    """Read the canonical reminder notes for one fired scheduled job."""
    with _pool().connection() as conn:
        rows = conn.execute(
            "SELECT doc->>'note' FROM user_logs "
            "WHERE user_id=%s AND stream=%s AND doc->>'status'='fired' "
            "AND doc->>'fired_job_id'=%s ORDER BY seq LIMIT 10",
            (str(user_id), SCHEDULED_WAKE_STREAM, str(int(job_id))),
        ).fetchall()
    return [
        str(row[0]).strip()[:1000]
        for row in rows
        if str(row[0] or "").strip()
    ]


def _scheduled_failure_reply_text(
    error_class: str,
    *,
    language: str,
    user_text: str,
    notes: list[str],
) -> str:
    """Explain a missed timer without impersonating a model-authored reply."""
    if str(language or "").strip().lower().startswith("en"):
        reminder = (
            " (" + "; ".join(notes) + ")"
            if notes
            else ""
        )
        if error_class == "provider_empty_reply":
            return (
                "A scheduled reminder" + reminder
                + " was due, but your model service returned "
                "an empty reply, so it could not be delivered. Please set it "
                "again; if this keeps happening, check the model provider or relay."
            )
        return (
            "A scheduled reminder" + reminder
            + " was due, but its reply could not be delivered. "
            "Please set it again."
        )
    reminder = (
        "（" + "；".join(f"“{note}”" for note in notes) + "）"
        if notes
        else ""
    )
    return (
        "刚才的定时任务" + reminder + "已触发，但 TA 的回复没有成功送达。"
        + str(user_text or "连接模型服务时出了问题。").strip()
    )


def _deliver_terminal_failure_reply(row: dict) -> bool:
    """Write one encrypted failure result exactly once.

    Chat failures remain parent-linked and cursor-fenced. Scheduled reminders
    have no current user turn, so they are standalone, explicitly marked
    system failures and never advance or re-parent the chat cursor.
    """
    from core import envelope as core_envelope
    from core import store as core_store

    job_id = row["job_id"]
    user_id = str(row["user_id"])
    # Rows from the durable reader always carry lane. Default to the historical
    # chat behavior for compatibility/test callers that pass the pre-scheduled
    # row shape directly.
    lane = str(row.get("lane") or "chat")
    frontier = int(row.get("reply_frontier_seq") or 0)
    parent_id = str(row.get("reply_parent_message_id") or "").strip()
    if lane != "scheduled" and (frontier <= 0 or not parent_id):
        return _ack_terminal_failure_reply(job_id)

    message_identity = (
        f"v2-scheduled-terminal-failure:{job_id}"
        if lane == "scheduled"
        else f"v2-terminal-failure:{job_id}"
    )
    message_id = hashlib.sha256(message_identity.encode("utf-8")).hexdigest()[:32]
    existing = db.chat_get_strict(user_id, message_id)
    if existing is not None:
        if str(existing.get("terminal_failure_job_id") or "") != str(job_id):
            raise RuntimeError("terminal failure reply id collision")
        return _ack_terminal_failure_reply(job_id)

    # Same-turn supersede gate: 这个 parent 已经被一条真回复(reply_message_id 有值
    # 且没有失败章)回答过了,再投这条迟到的失败气泡只会自相矛盾——按「最终结果
    # 说了算」直接 ack 不投递。严格按本 parent 判定,别的 turn 的真实失败照常投。
    if lane == "chat" and db.v2_turn_failure_supersede_enabled():
        parent = db.chat_get_strict(user_id, parent_id)
        if (
            parent is not None
            and str(parent.get("reply_message_id") or "").strip()
            and not str(parent.get("reply_error_class") or "").strip()
        ):
            return _ack_terminal_failure_reply(job_id)

    failure_identity: dict[str, str] = {}
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT detail_json FROM agent_status_events "
                "WHERE job_id=%s AND kind='error' ORDER BY id DESC LIMIT 1",
                (job_id,),
            )
            identity_row = cur.fetchone()
    detail = (identity_row or {}).get("detail_json")
    if isinstance(detail, dict):
        for source, target, limit in (
            ("failure_model", "turn_failure_model", 96),
            ("failure_provider", "turn_failure_provider", 80),
        ):
            value = re.sub(
                r"[^A-Za-z0-9_./:@+-]+", "_", str(detail.get(source) or "").strip()
            )[:limit]
            if value:
                failure_identity[target] = value

    error_class = _terminal_error_class(
        row.get("error_code"), row.get("error_class")
    )
    blame = notices_catalog.blame_for(error_class)
    try:
        from accounts import registry as accounts_registry

        language = accounts_registry._get_user_archive_language(user_id) or ""
    except Exception:  # noqa: BLE001 — locale lookup must not block failure delivery
        language = ""
    user_text = notices_catalog.user_text_for(
        error_class,
        language=language,
    )
    scheduled_notes = (
        _scheduled_failure_notes(user_id, job_id)
        if lane == "scheduled"
        else []
    )
    reply_text = (
        _scheduled_failure_reply_text(
            error_class,
            language=language,
            user_text=user_text,
            notes=scheduled_notes,
        )
        if lane == "scheduled"
        else (
            user_text
            if error_class
            in {
                "platform_queue_timeout",
                "platform_execution_timeout",
                "provider_timeout",
            }
            or blame == "user_provider"
            else _TERMINAL_FAILURE_FALLBACK_REPLY
        )
    )
    store = core_store.get_store(user_id)
    envelope, error = core_envelope._build_shared_envelope_for_store(
        store,
        reply_text.encode("utf-8"),
        item_id=message_id,
    )
    if envelope is None:
        raise RuntimeError(error or "terminal failure envelope build failed")
    extra = {
        "turn_failure_error_class": error_class,
        "turn_failure_blame": blame,
        "turn_failure_user_text": user_text,
        "terminal_failure_job_id": str(job_id),
        **failure_identity,
    }
    if lane == "scheduled":
        extra.update({
            "wake_kind": "scheduled",
            "notice_kind": "scheduled_wake_failure",
        })
    else:
        extra["reply_to_message_id"] = parent_id
    message = store._build_chat_message(
        "openclaw",
        "model_api",
        envelope,
        extra=extra,
    )
    if lane == "scheduled":
        db.chat_append_strict(
            user_id,
            message_id,
            float(message["ts"]),
            message,
            core_store.MAX_CHAT_MESSAGES,
        )
        persisted = db.chat_get_strict(user_id, message_id)
        if (
            persisted is None
            or str(persisted.get("terminal_failure_job_id") or "")
            != str(job_id)
        ):
            raise RuntimeError("scheduled failure reply delivery was not adopted")
        store.reload()
        store.notify_chat_waiters()
        try:
            wake_bus.notify("chat", user_id)
        except Exception:  # noqa: BLE001 - poll timeout remains the fallback
            pass
        return _ack_terminal_failure_reply(job_id)

    seq, inserted = db.chat_append_effect_with_cursor(
        user_id,
        message_id,
        float(message["ts"]),
        message,
        core_store.MAX_CHAT_MESSAGES,
        frontier,
        require_cursor_advance=True,
    )
    if seq:
        store.reload()
        store.notify_chat_waiters()
        try:
            wake_bus.notify("chat", user_id)
        except Exception:  # noqa: BLE001 - poll timeout remains the fallback
            pass
    if not inserted and seq:
        persisted = db.chat_get_strict(user_id, message_id)
        if (
            persisted is None
            or str(persisted.get("terminal_failure_job_id") or "") != str(job_id)
        ):
            raise RuntimeError("terminal failure reply delivery was not adopted")
    return _ack_terminal_failure_reply(job_id)


def _deliver_terminal_failure_runtime_error(job_id) -> bool:
    """Atomically set the captured active route and acknowledge the marker.

    The route id/version captured with terminalization prevents a delayed
    failure from stamping a newly selected (or reactivated) provider route.
    ``finish_chat_job`` clears the active route and changes ``updated_at`` in
    the same transaction as a later success, so an old marker either wins
    first (and the success clears it) or loses its version predicate and is
    acknowledged without restoring stale UI state.
    """
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT o.user_id,o.error_code,o.error_class,"
                    "o.target_route_id,o.target_route_updated_at,"
                    "o.runtime_error_delivered_at,j.lane "
                    "FROM v2_terminal_failure_outbox o "
                    "JOIN agent_jobs j ON j.id=o.job_id "
                    "WHERE o.job_id=%s FOR UPDATE OF o",
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None or row["runtime_error_delivered_at"] is not None:
                    return False
                if str(row.get("lane") or "") == "scheduled":
                    cur.execute(
                        "UPDATE v2_terminal_failure_outbox "
                        "SET runtime_error_delivered_at=now(),updated_at=now() "
                        "WHERE job_id=%s",
                        (job_id,),
                    )
                    return True
                route_id = row.get("target_route_id")
                if route_id is not None:
                    learns_vision_unsupported = (
                        str(row.get("error_class") or "")
                        == "vision_model_required"
                    )
                    cur.execute(
                        "UPDATE model_api_routes SET last_runtime_error=%s,"
                        "last_runtime_error_class='',"
                        "vision_test_status=CASE WHEN %s THEN 'unsupported' "
                        "  ELSE vision_test_status END,"
                        "last_vision_test_error=CASE WHEN %s "
                        "  THEN 'vision_model_required' "
                        "  ELSE last_vision_test_error END,"
                        "last_vision_test_at=CASE WHEN %s THEN now() "
                        "  ELSE last_vision_test_at END,"
                        "updated_at=now() "
                        "WHERE id=%s AND user_id=%s AND is_active "
                        "AND updated_at IS NOT DISTINCT FROM %s "
                        "AND NOT EXISTS (SELECT 1 FROM agent_jobs newer "
                        "  WHERE newer.user_id=%s AND newer.lane='chat' "
                        "  AND newer.status='completed' AND newer.id>%s)",
                        (
                            str(row["error_code"]),
                            learns_vision_unsupported,
                            learns_vision_unsupported,
                            learns_vision_unsupported,
                            route_id,
                            str(row["user_id"]),
                            row.get("target_route_updated_at"),
                            str(row["user_id"]),
                            job_id,
                        ),
                    )
                cur.execute(
                    "UPDATE v2_terminal_failure_outbox "
                    "SET runtime_error_delivered_at=now(),updated_at=now() "
                    "WHERE job_id=%s",
                    (job_id,),
                )
                return True


def reconcile_terminal_failure_outbox(
    *,
    record_terminal_error=None,
    job_id=None,
    limit: int = 100,
    now=None,
) -> dict[str, int]:
    """Best-effort replay of all user-visible terminal failure sinks.

    Each sink has an independent due queue, so poisoned route delivery cannot
    hide newer status errors.  Production leaves ``record_terminal_error``
    unset and uses the route-version-fenced atomic DB sink above.  The callback
    remains only as a compatibility/test seam for dependency-isolated callers.
    """
    # Local import keeps jobs_store's storage primitives independent of the
    # status vocabulary during module initialization.
    from model_api_runtime.v2 import status_stream

    status_rows = _pending_terminal_failure_rows(
        "status", job_id=job_id, limit=limit, now=now
    )
    runtime_rows = _pending_terminal_failure_rows(
        "runtime_error", job_id=job_id, limit=limit, now=now
    )
    reply_rows = _pending_terminal_failure_rows(
        "reply", job_id=job_id, limit=limit, now=now
    )
    status_count = 0
    runtime_error_count = 0
    reply_count = 0
    event = status_stream.redact_status("error")
    for row in status_rows:
        current_job_id = row["job_id"]
        try:
            if _deliver_terminal_failure_status(
                current_job_id,
                kind=event["kind"],
                label=event["label"],
                detail=event["detail"],
            ):
                status_count += 1
        except Exception as exc:  # noqa: BLE001 — marker remains pending
            try:
                _defer_terminal_failure_sink(current_job_id, "status", now=now)
            except Exception:  # noqa: BLE001 — original DB outage may persist
                pass
            log.warning(
                "terminal status reconciliation failed job=%s code=%s",
                current_job_id,
                type(exc).__name__.lower(),
            )
    for row in runtime_rows:
        current_job_id = row["job_id"]
        try:
            if str(row.get("lane") or "") == "scheduled":
                if _ack_terminal_runtime_error(current_job_id):
                    runtime_error_count += 1
            elif record_terminal_error is not None:
                delivered = record_terminal_error(
                    str(row["user_id"]), str(row["error_code"])
                )
                if delivered is False:
                    raise RuntimeError("runtime-error sink rejected delivery")
                if _ack_terminal_runtime_error(current_job_id):
                    runtime_error_count += 1
            elif _deliver_terminal_failure_runtime_error(current_job_id):
                runtime_error_count += 1
        except Exception as exc:  # noqa: BLE001 — marker remains pending
            try:
                _defer_terminal_failure_sink(current_job_id, "runtime_error", now=now)
            except Exception:  # noqa: BLE001 — original DB outage may persist
                pass
            log.warning(
                "terminal runtime-error reconciliation failed job=%s code=%s",
                current_job_id,
                type(exc).__name__.lower(),
            )
    for row in reply_rows:
        current_job_id = row["job_id"]
        try:
            if _deliver_terminal_failure_reply(row):
                reply_count += 1
        except Exception as exc:  # noqa: BLE001 - marker remains pending
            try:
                _defer_terminal_failure_sink(current_job_id, "reply", now=now)
            except Exception:  # noqa: BLE001 - original outage may persist
                pass
            log.warning(
                "terminal reply reconciliation failed job=%s code=%s",
                current_job_id,
                type(exc).__name__.lower(),
            )
    examined = {row["job_id"] for row in status_rows}
    examined.update(row["job_id"] for row in runtime_rows)
    examined.update(row["job_id"] for row in reply_rows)
    return {
        "examined": len(examined),
        "status_delivered": status_count,
        "runtime_error_delivered": runtime_error_count,
        "reply_delivered": reply_count,
    }


def reap_stuck_jobs(now=None) -> int:
    """Compatibility count wrapper over :func:`reap_stuck_job_rows`."""
    return len(reap_stuck_job_rows(now=now))


def get_input_generation(job_id, *, claimed_by: str) -> int | None:
    """Snapshot chat input generation before reading messages."""
    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT input_generation FROM agent_jobs "
            "WHERE id=%s AND claimed_by=%s AND status IN ('claimed','running') "
            "AND lease_expires_at > now()",
            (job_id, str(claimed_by)),
        ).fetchone()
        return int(row[0]) if row is not None else None


def _mcp_attempt_key(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def start_mcp_mutation_attempt(
    job_id,
    *,
    user_id: str,
    claimed_by: str,
    call_id: str,
    tool_name: str,
    input_frontier_seq: int,
) -> bool:
    """Durably mark remote-mutation intent before any request leaves the worker.

    No arguments or remote content are stored—only one-way identifiers. A
    duplicate call id, lost lease, or wrong owner fails closed before network
    I/O, so crash recovery can never silently repeat an ambiguous mutation.
    """
    if type(input_frontier_seq) is not int or input_frontier_seq < 0:
        raise ValueError("input_frontier_seq must be a non-negative integer")
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT user_id,status,claimed_by,lease_expires_at "
                    "FROM agent_jobs WHERE id=%s FOR UPDATE",
                    (job_id,),
                )
                row = cur.fetchone()
                lease_valid = False
                if row is not None and row["lease_expires_at"] is not None:
                    cur.execute(
                        "SELECT %s::timestamptz > clock_timestamp() AS valid",
                        (row["lease_expires_at"],),
                    )
                    lease_valid = bool(cur.fetchone()["valid"])
                if (
                    row is None
                    or str(row["user_id"]) != str(user_id)
                    or str(row["status"]) != "running"
                    or str(row["claimed_by"] or "") != str(claimed_by)
                    or not lease_valid
                ):
                    return False
                cur.execute(
                    "INSERT INTO v2_mcp_mutation_attempts "
                    "(job_id,user_id,input_frontier_seq,call_key,tool_fingerprint) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING RETURNING 1",
                    (
                        job_id,
                        str(user_id),
                        input_frontier_seq,
                        _mcp_attempt_key(call_id),
                        _mcp_attempt_key(tool_name),
                    ),
                )
                return cur.fetchone() is not None


def finish_mcp_mutation_attempt(
    job_id,
    *,
    call_id: str,
    outcome: str,
) -> bool:
    if outcome not in {"known", "unknown"}:
        raise ValueError("invalid MCP mutation outcome")
    with _pool().connection() as conn:
        cur = conn.execute(
            "UPDATE v2_mcp_mutation_attempts "
            "SET outcome=%s,resolved_at=clock_timestamp() "
            "WHERE job_id=%s AND call_key=%s AND outcome IS NULL",
            (str(outcome), job_id, _mcp_attempt_key(call_id)),
        )
        return cur.rowcount == 1


def has_ambiguous_mcp_mutation(*, job_id=None, user_id: str | None = None) -> bool:
    if job_id is None and user_id is None:
        raise ValueError("job_id or user_id is required")
    clauses = ["(outcome IS NULL OR outcome='unknown')"]
    params: list = []
    if job_id is not None:
        clauses.append("job_id=%s")
        params.append(job_id)
    if user_id is not None:
        clauses.append("user_id=%s")
        params.append(str(user_id))
    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM v2_mcp_mutation_attempts WHERE "
            + " AND ".join(clauses)
            + ")",
            tuple(params),
        ).fetchone()
    return bool(row and row[0])


def get_chat_mutation_recovery_barrier(
    user_id: str,
    *,
    after_seq: int,
    exclude_job_id=None,
) -> dict | None:
    """Return the highest mutation frontier not yet covered by a reply cursor.

    A chat job can commit a platform write or send a remote MCP mutation and
    then die before its final reply advances ``v2_reply_cursor_seq``. Replaying
    that same input with write tools enabled can duplicate the side effect.
    Every MCP request records its frontier before network I/O; every platform
    write stores the same frontier atomically on its outbox row. Outcomes and
    outbox dispositions do not weaken the barrier: a known success is exactly
    the case that must not be repeated.

    The caller excludes its own job because several mutations in one live turn
    are intentional. A later recovery job sees the old job's frontier and runs
    mutation-free until its final reply advances the durable cursor through it.
    Only non-content counters and booleans are returned.
    """
    if type(after_seq) is not int or after_seq < 0:
        raise ValueError("after_seq must be a non-negative integer")
    excluded = None if exclude_job_id is None else int(exclude_job_id)
    with _pool().connection() as conn:
        row = conn.execute(
            "WITH barriers AS ("
            "  SELECT attempt.job_id,attempt.input_frontier_seq,'mcp' AS kind "
            "  FROM v2_mcp_mutation_attempts attempt "
            "  JOIN agent_jobs job ON job.id=attempt.job_id "
            "  WHERE attempt.user_id=%s AND job.lane='chat' "
            "    AND attempt.input_frontier_seq>%s "
            "    AND (%s::bigint IS NULL OR attempt.job_id<>%s) "
            "  UNION ALL "
            "  SELECT effect.job_id,effect.input_frontier_seq,'platform' AS kind "
            "  FROM v2_effect_outbox effect "
            "  JOIN agent_jobs job ON job.id=effect.job_id "
            "  WHERE effect.user_id=%s AND job.lane='chat' "
            "    AND effect.input_frontier_seq>%s "
            "    AND (%s::bigint IS NULL OR effect.job_id<>%s)"
            "    AND NOT ("
            "      effect.effect_type='workspace_batch_encrypted_v1' "
            "      AND effect.status='applied_with_results' "
            "      AND effect.payload->'_applied_result_v1'->>'kind'="
            "          'workspace_batch_v1' "
            "      AND jsonb_typeof("
            "          effect.payload->'_applied_result_v1'->'items'"
            "      )='array' "
            "      AND jsonb_array_length("
            "          effect.payload->'_applied_result_v1'->'items'"
            "      )>0 "
            "      AND NOT EXISTS ("
            "        SELECT 1 FROM jsonb_array_elements("
            "          effect.payload->'_applied_result_v1'->'items'"
            "        ) item WHERE item->>'status'<>'discarded'"
            "      )"
            "    )"
            ") SELECT MAX(input_frontier_seq),"
            "         COALESCE(bool_or(kind='mcp'),false),"
            "         COALESCE(bool_or(kind='platform'),false) "
            "FROM barriers",
            (
                str(user_id),
                after_seq,
                excluded,
                excluded,
                str(user_id),
                after_seq,
                excluded,
                excluded,
            ),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return {
        "through_seq": int(row[0]),
        "has_mcp": bool(row[1]),
        "has_platform": bool(row[2]),
    }


def get_job_status(
    job_id,
    *,
    user_id: str,
    claimed_by: str,
) -> str | None:
    """Return the exact source job's status without weakening its identity.

    Final reply publication can complete a running chat job inside the effect
    outbox transaction.  The producing worker (or a recovery drain) uses this
    read to distinguish that successful terminal state from lost ownership;
    matching only by id would let a malformed/injected job object adopt another
    user's completion.
    """
    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT status FROM agent_jobs "
            "WHERE id=%s AND user_id=%s AND claimed_by=%s",
            (job_id, str(user_id), str(claimed_by)),
        ).fetchone()
    return str(row[0]) if row is not None else None


def get_expected_runtime_generation(
    job_id,
    *,
    claimed_by: str,
) -> int | None:
    """Return the claim-time runtime generation pinned on an owned job."""
    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT expected_runtime_generation FROM agent_jobs "
            "WHERE id=%s AND claimed_by=%s AND status IN ('claimed','running')",
            (job_id, str(claimed_by)),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def finish_chat_job(
    job_id,
    *,
    claimed_by: str,
    observed_generation: int,
    force_successor: bool = False,
) -> tuple[bool, int | None]:
    """Complete an owned chat job and atomically create one late-input successor.

    Sends coalesced after ``observed_generation`` increment the active row under
    the same row lock. If they won the race, this transaction terminates the old
    row and inserts exactly one new pending chat job before releasing the lock.
    If finalization wins first, a concurrent enqueue sees the successor or creates
    a fresh job after the old row is terminal. Either ordering preserves input.

    ``force_successor`` is the bounded-loop handoff path. A final reply can be
    atomically refused by the outbox's input-generation/seq fence on the last
    provider attempt. In that case the durable cursor is deliberately unchanged,
    and this flag creates the successor in the SAME transaction as terminalizing
    the old job, even if a future/broken sender persisted the row without bumping
    ``input_generation``. The successor re-reads every unconsumed row.
    """
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                # Discover the immutable user id without a row lock, then keep
                # the global runtime-state -> job order used by claim, renewal,
                # send admission, and final-effect publication.  Locking the job
                # first here would deadlock against a cutover/send that already
                # owns runtime-state and is waiting for the same active row.
                cur.execute(
                    "SELECT user_id FROM agent_jobs WHERE id=%s",
                    (job_id,),
                )
                identity = cur.fetchone()
                if identity is None:
                    return False, None
                cur.execute(
                    "SELECT hosted_runtime_state,runtime_generation "
                    "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                    (identity["user_id"],),
                )
                control = cur.fetchone()
                if control is None or str(control["hosted_runtime_state"]) != "v2":
                    return False, None
                current_generation = int(control["runtime_generation"])
                cur.execute(
                    "SELECT user_id,lane,input_generation,priority,"
                    "       expected_runtime_generation,lease_expires_at "
                    "FROM agent_jobs "
                    "WHERE id=%s AND claimed_by=%s AND status='running' "
                    "FOR UPDATE",
                    (job_id, str(claimed_by)),
                )
                row = cur.fetchone()
                # PostgreSQL's scan node can evaluate a volatile expression
                # before LockRows sleeps. Check the wall clock only after the
                # row lock has actually been acquired.
                lease_valid = False
                if row is not None and row["lease_expires_at"] is not None:
                    cur.execute(
                        "SELECT %s::timestamptz > clock_timestamp() AS lease_valid",
                        (row["lease_expires_at"],),
                    )
                    lease_valid = bool(cur.fetchone()["lease_valid"])
                if (
                    row is None
                    or str(row["user_id"]) != str(identity["user_id"])
                    or str(row["lane"]) != "chat"
                    or not lease_valid
                    or row["expected_runtime_generation"] is None
                    or int(row["expected_runtime_generation"]) != current_generation
                ):
                    return False, None
                cur.execute(
                    "UPDATE agent_jobs SET status='completed',finished_at=now() WHERE id=%s",
                    (job_id,),
                )
                # The success clear and completed outcome are one transaction.
                # A delayed older failure therefore cannot race in after this:
                # its captured route.updated_at predicate no longer matches.
                if not force_successor:
                    cur.execute(
                        "UPDATE model_api_routes SET last_runtime_error='',"
                        "last_runtime_error_class='',updated_at=now() "
                        "WHERE user_id=%s AND is_active",
                        (row["user_id"],),
                    )
                successor_id = None
                if force_successor or int(row["input_generation"] or 0) > int(
                    observed_generation
                ):
                    cur.execute(
                        "INSERT INTO agent_jobs "
                        "(user_id,lane,status,reason,priority,queue_deadline_at,"
                        " expected_runtime_generation) "
                        "VALUES (%s,'chat','pending','coalesced_followup',%s,"
                        "now() + make_interval(secs => %s),%s) RETURNING id",
                        (
                            row["user_id"],
                            int(row["priority"]),
                            float(PENDING_CHAT_TTL_SEC),
                            current_generation,
                        ),
                    )
                    successor_id = int(cur.fetchone()["id"])
                return True, successor_id


def append_status_event(
    user_id, kind, *, job_id=None, label=None, detail=None, seq=0
) -> int:
    """写一条脱敏 status 事件（非聊天 UX/debug）。detail 只放标签+粗计数，绝无原文。
    返回新事件 id（long-poll 游标用）。

    INSERT 成功后触发一次跨进程唤醒（chat 频道）：V2 worker 与持有 parked chat
    long-poll 的 web 层是不同进程，若不主动 NOTIFY，intermediate status（processing/
    reading_*/writing_reply）只能等到 turn-end 的回复唤醒或 long-poll 自身 ~30s
    超时才被看到——退化成事后补发，违反 §9「渐进可见」。best-effort：wake_bus.notify
    本身已在 db 层兜底，这里再包一层 try/except，绝不让可观测性拖垮已经落库成功的
    status 写入。"""
    # ``job_id=None`` is retained for content-free administrative/test events.
    # Every production turn-derived caller passes a source job (worker._emit_status
    # and the generation-fenced status effect sink). New chat-derived callers
    # must do the same or they will not participate in transcript-clear fencing.
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                if job_id is not None:
                    # Clear-history owns the exclusive form.  If this status
                    # started first, clear waits and deletes it; if clear won,
                    # the source job's pinned generation no longer matches and
                    # the stale worker cannot recreate client-visible state.
                    db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                    cur.execute(
                        "SELECT 1 FROM agent_jobs AS job "
                        "JOIN v2_runtime_state AS state "
                        "ON state.user_id=job.user_id "
                        "WHERE job.id=%s AND job.user_id=%s "
                        "AND job.expected_runtime_generation="
                        "state.runtime_generation",
                        (job_id, str(user_id)),
                    )
                    if cur.fetchone() is None:
                        raise ValueError("status source job generation is stale")
                cur.execute(
                    "INSERT INTO agent_status_events "
                    "(job_id, user_id, kind, label, detail_json, seq) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (
                        job_id,
                        user_id,
                        str(kind),
                        label,
                        Jsonb(dict(detail or {})),
                        int(seq),
                    ),
                )
                event_id = int(cur.fetchone()["id"])
    try:
        wake_bus.notify("chat", user_id)
    except Exception:  # noqa: BLE001 — best-effort; the INSERT already committed
        pass
    return event_id


def chat_turn_activity_rows(user_id: str, turn_id: str) -> tuple[list[dict], list[dict]]:
    """Return V2 jobs and display-safe status rows for one chat message id."""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, status, last_error FROM agent_jobs "
                "WHERE user_id=%s AND lane='chat' AND trace_id=%s ORDER BY id ASC",
                (str(user_id), str(turn_id)),
            )
            jobs = [dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT event.id,event.job_id,event.user_id,event.kind,event.label,"
                "event.detail_json,event.seq,"
                "extract(epoch FROM event.created_at)::float8 AS created_at "
                "FROM agent_status_events AS event "
                "JOIN agent_jobs AS job ON job.id=event.job_id "
                "WHERE event.user_id=%s AND job.user_id=%s AND job.lane='chat' "
                "AND job.trace_id=%s ORDER BY event.id ASC LIMIT 500",
                (str(user_id), str(user_id), str(turn_id)),
            )
            events = [dict(row) for row in cur.fetchall()]
    return jobs, events


def turn_answered_by_real_reply(user_id: str, turn_id: str) -> bool:
    """True iff a real (non-failure-carrier) assistant reply answers this turn.

    Durable reply evidence for the activity projection: job status 单独不可信
    (completed 可能是没发回复的 handoff),这里只认「reply_to_message_id 指回该
    turn、且不带 turn_failure_* 章」的已落库回复。"""
    with _pool().connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM chat_messages WHERE user_id=%s "
            "AND doc->>'reply_to_message_id'=%s "
            "AND doc->>'role' NOT IN ('user','human') "
            "AND COALESCE(doc->>'turn_failure_error_class','')='' LIMIT 1",
            (str(user_id), str(turn_id)),
        )
        return cur.fetchone() is not None


def status_events_for_job(user_id: str, job_id: int) -> list[dict]:
    """Read one V2 job's status stream for final reply projection."""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id,job_id,user_id,kind,label,detail_json,seq,"
                "extract(epoch FROM created_at)::float8 AS created_at "
                "FROM agent_status_events WHERE user_id=%s AND job_id=%s "
                "ORDER BY id ASC LIMIT 500",
                (str(user_id), int(job_id)),
            )
            return [dict(row) for row in cur.fetchall()]


def list_status_events(user_id, *, after_id=0, limit=50) -> list[dict]:
    """按 id 升序返回 user 自 after_id 之后的 status 事件（游标读）。每行含
    id/job_id/user_id/kind/label/detail_json/seq/created_at(epoch float)。委托到
    db.list_agent_status_events —— Plan C 的 chat/poll_core 长轮询走同一原语，
    这里不重复写 SQL（单一读源）。"""
    return db.list_agent_status_events(user_id, after_id=after_id, limit=limit)


def get_runtime_state(user_id) -> dict:
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT state_json FROM runtime_state WHERE user_id=%s", (user_id,)
            )
            row = cur.fetchone()
    return dict(row["state_json"]) if row else {}


def record_worker_heartbeat(
    worker_id: str,
    *,
    pool: str,
    kind: str = "turn",
    capacity: int = 1,
    runtime_state: dict[str, object] | None = None,
) -> None:
    """UPSERT this process's liveness row (turn loops every ~10s via
    serve_worker._heartbeat_loop; the genesis thread every tick with
    kind='genesis').

    ``kind`` is load-bearing, not a label: workers_alive()/live_worker_count()
    read ONLY kind='turn' because they gate chat/send admission. A genesis row
    counted as a turn worker would halve the estimated queue wait.
    """
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats "
            "(worker_id, beat_at, kind, capacity, pool, runtime_state) "
            "VALUES (%s, now(), %s, %s, %s, %s) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = now(), "
            "kind = EXCLUDED.kind, capacity = EXCLUDED.capacity, "
            "pool = EXCLUDED.pool, runtime_state = EXCLUDED.runtime_state",
            (
                str(worker_id),
                str(kind),
                max(0, int(capacity)),
                str(pool),
                Jsonb(dict(runtime_state or {})),
            ),
        )


def workers_alive(*, within_sec: int = 30, pool: str | None = None) -> bool:
    """True iff at least one serve_worker TURN process has recorded a heartbeat
    within the last ``within_sec`` seconds. Used by the chat/send v2 liveness
    guard. Genesis heartbeats are deliberately invisible here — a live genesis
    thread says nothing about whether any turn slot exists to drain the job."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            query = (
                "SELECT EXISTS(SELECT 1 FROM v2_worker_heartbeats "
                "WHERE kind = 'turn' AND capacity > 0 "
                "AND beat_at > now() - make_interval(secs => %s)"
            )
            params: list[object] = [int(within_sec)]
            if pool is not None:
                query += " AND pool = %s"
                params.append(str(pool))
            cur.execute(query + ")", params)
            return bool(cur.fetchone()[0])


def live_worker_count(*, within_sec: int = 30, pool: str | None = None) -> int:
    """窗口内有心跳的 serve_worker TURN 进程数（workers_alive 的计数版，喂 admission
    ceiling）。genesis 心跳不计入——它不占 turn 槽位。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            query = (
                "SELECT count(*) FROM v2_worker_heartbeats "
                "WHERE kind = 'turn' AND capacity > 0 "
                "AND beat_at > now() - make_interval(secs => %s)"
            )
            params: list[object] = [int(within_sec)]
            if pool is not None:
                query += " AND pool = %s"
                params.append(str(pool))
            cur.execute(query, params)
            return int(cur.fetchone()[0])


def live_genesis_worker_ids(*, within_sec: int = 30) -> list[str]:
    """worker_ids with a fresh ``kind='genesis'`` heartbeat. Sibling to
    ``workers_alive``/``live_worker_count`` (which read ``kind='turn'`` only, to
    gate chat admission): this reads the genesis heartbeats to gate the genesis
    orphan reclaim — a ``processing`` job whose claiming worker id is absent here
    was left behind by a dead/replaced worker."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id FROM v2_worker_heartbeats "
                "WHERE kind = 'genesis' "
                "AND beat_at > now() - make_interval(secs => %s)",
                (max(1, int(within_sec)),),
            )
            return [str(r[0]) for r in cur.fetchall()]


def live_worker_capacity(*, within_sec: int = 30, pool: str | None = None) -> int:
    """Sum executable turn slots, not heartbeat processes."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            query = (
                "SELECT COALESCE(sum(capacity),0) FROM v2_worker_heartbeats "
                "WHERE kind='turn' "
                "AND beat_at > now() - make_interval(secs => %s)"
            )
            params: list[object] = [int(within_sec)]
            if pool is not None:
                query += " AND pool = %s"
                params.append(str(pool))
            cur.execute(query, params)
            return int(cur.fetchone()[0])


def genesis_worker_alive(*, within_sec: int = 60) -> bool:
    """True iff the genesis import worker thread has beaten recently.

    Window defaults to 60s (not 30s): a genesis tick holds the thread for the
    whole LLM reduce, and the heartbeat is written once per tick, so the gap
    between beats is the tick interval (default 10s) PLUS the last job's
    duration. Purely observational — nothing gates on this.
    """
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM v2_worker_heartbeats "
                "WHERE kind = 'genesis' AND beat_at > now() - make_interval(secs => %s))",
                (int(within_sec),),
            )
            return bool(cur.fetchone()[0])


def recent_worker_heartbeats(*, within_sec: int = 300, limit: int = 50) -> list[dict]:
    """Recent turn/Genesis identities for authenticated deploy verification.

    Aggregate liveness alone can be satisfied briefly by the old container's
    still-fresh row. The deploy gate uses the build suffix in ``worker_id`` and
    the DB-clock age here to prove the newly published image is beating.
    """
    safe_window = max(1, min(int(within_sec), 3600))
    safe_limit = max(1, min(int(limit), 200))
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT worker_id, kind, capacity, pool, runtime_state, "
                "EXTRACT(EPOCH FROM beat_at) AS beat_at_epoch, "
                "GREATEST(0, EXTRACT(EPOCH FROM (now() - beat_at))) AS age_sec "
                "FROM v2_worker_heartbeats "
                "WHERE kind IN ('turn','genesis') "
                "AND beat_at > now() - make_interval(secs => %s) "
                "ORDER BY beat_at DESC LIMIT %s",
                (safe_window, safe_limit),
            )
            rows = cur.fetchall()
    return [
        {
            "worker_id": str(row["worker_id"]),
            "kind": str(row["kind"]),
            "capacity": int(row["capacity"] or 0),
            "pool": str(row["pool"]),
            "runtime_state": dict(row["runtime_state"] or {}),
            "beat_at_epoch": float(row["beat_at_epoch"]),
            "age_sec": float(row["age_sec"]),
        }
        for row in rows
    ]


def recent_worker_heartbeat_count(*, within_sec: int = 300) -> int:
    """Total rows in the same window as ``recent_worker_heartbeats``.

    The admin endpoint returns a bounded heartbeat list. Deployment gates must
    compare this count with the returned list length so an extra live worker
    cannot be hidden beyond that response cap.
    """
    safe_window = max(1, min(int(within_sec), 3600))
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM v2_worker_heartbeats "
                "WHERE kind IN ('turn','genesis') "
                "AND beat_at > now() - make_interval(secs => %s)",
                (safe_window,),
            )
            return int(cur.fetchone()[0])


def _job_pool_case_sql() -> str:
    return (
        "CASE WHEN lane IN ('chat','manual_wake') THEN 'foreground' "
        "WHEN lane IN ('heartbeat','scheduled','screen_watch') THEN 'wake' "
        "ELSE 'heavy' END"
    )


def pool_queue_metrics() -> dict[str, dict[str, int | float | None]]:
    """Bounded queue/claim aggregates for the three fixed runtime pools."""
    query = (
        "WITH tagged AS (SELECT " + _job_pool_case_sql() + " AS pool, "
        "status,created_at,claimed_at,available_at FROM agent_jobs "
        "WHERE created_at > now() - interval '24 hours' "
        "OR status IN ('pending','claimed','running')) "
        "SELECT pool, count(*) FILTER (WHERE status='pending' "
        "AND available_at <= clock_timestamp()) AS pending_ready, "
        "count(*) FILTER (WHERE status='pending' "
        "AND available_at > clock_timestamp()) AS pending_delayed, "
        "MAX(EXTRACT(EPOCH FROM (now()-created_at))) "
        "FILTER (WHERE status='pending' "
        "AND available_at <= clock_timestamp()) AS oldest_pending_sec, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY "
        "EXTRACT(EPOCH FROM (claimed_at-created_at))*1000) "
        "FILTER (WHERE claimed_at IS NOT NULL) AS claim_p95_ms "
        "FROM tagged GROUP BY pool"
    )
    result = {
        pool: {
            "pending": 0,
            "pending_ready": 0,
            "pending_delayed": 0,
            "oldest_pending_sec": None,
            "claim_p95_ms": None,
        }
        for pool in ("foreground", "wake", "heavy")
    }
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query)
            for row in cur.fetchall():
                pending_ready = int(row["pending_ready"] or 0)
                result[str(row["pool"])] = {
                    "pending": pending_ready,
                    "pending_ready": pending_ready,
                    "pending_delayed": int(row["pending_delayed"] or 0),
                    "oldest_pending_sec": (
                        None
                        if row["oldest_pending_sec"] is None
                        else float(row["oldest_pending_sec"])
                    ),
                    "claim_p95_ms": (
                        None
                        if row["claim_p95_ms"] is None
                        else float(row["claim_p95_ms"])
                    ),
                }
    return result


def job_counts_by_lane() -> dict[str, dict[str, int]]:
    """Current pending and owner-held counts, omitting all-zero lanes."""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT lane, count(*) FILTER (WHERE status='pending' "
                "AND available_at <= clock_timestamp()) AS pending_ready, "
                "count(*) FILTER (WHERE status='pending' "
                "AND available_at > clock_timestamp()) AS pending_delayed, "
                "count(*) FILTER (WHERE status IN ('claimed','running')) AS active "
                "FROM agent_jobs WHERE status IN ('pending','claimed','running') "
                "GROUP BY lane ORDER BY lane"
            )
            rows = cur.fetchall()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        pending_ready = int(row["pending_ready"] or 0)
        result[str(row["lane"])] = {
            "pending": pending_ready,
            "pending_ready": pending_ready,
            "pending_delayed": int(row["pending_delayed"] or 0),
            "active": int(row["active"] or 0),
        }
    return result


def recent_preemption_counts(*, within_hours: int = 24) -> dict[str, int]:
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    with _pool().connection() as conn:
        rows = conn.execute(
            "SELECT lane, count(*) FROM agent_jobs "
            "WHERE last_error='foreground_chat_preempted' "
            "AND COALESCE(finished_at,created_at) > "
            "now()-make_interval(hours => %s) GROUP BY lane ORDER BY lane",
            (safe_hours,),
        ).fetchall()
    return {
        f"{str(lane)}:{'requeued' if str(lane) in {'scheduled', 'capture'} else 'terminal'}": int(count)
        for lane, count in rows
    }


def recent_watchdog_recovery_counts(*, within_hours: int = 24) -> dict[str, int]:
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    with _pool().connection() as conn:
        rows = conn.execute(
            "SELECT lane, count(*) FROM agent_jobs "
            "WHERE last_error='slot_watchdog_timeout' "
            "AND COALESCE(finished_at,created_at) > "
            "now()-make_interval(hours => %s) GROUP BY lane ORDER BY lane",
            (safe_hours,),
        ).fetchall()
    return {
        f"{str(lane)}:{'terminal' if str(lane) == 'chat' else 'requeued'}": int(count)
        for lane, count in rows
    }


def inflight_job_count(*, lanes: set[str] | None = None) -> int:
    """Count pending/claimed/running Jobs, optionally within selected lanes.

    Runtime admission must pass its pool's lane set.  The unfiltered form is
    retained for aggregate Admin observability only.
    """
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            query = (
                "SELECT count(*) FROM agent_jobs "
                "WHERE status IN ('pending','claimed','running')"
            )
            params: list[object] = []
            if lanes:
                query += " AND lane = ANY(%s)"
                params.append(sorted(str(lane) for lane in lanes))
            cur.execute(query, params)
            return int(cur.fetchone()[0])


def recent_mean_service_sec(*, lane: str = "chat", limit: int = 50) -> float | None:
    """最近 limit 条 completed job 的均服务时长（finished_at−started_at，秒）。
    无历史 → None（调用方用默认常量）。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT avg(EXTRACT(EPOCH FROM (finished_at - started_at))) "
                "FROM (SELECT finished_at, started_at FROM agent_jobs "
                "      WHERE status='completed' AND lane=%s "
                "      AND finished_at IS NOT NULL AND started_at IS NOT NULL "
                "      ORDER BY finished_at DESC LIMIT %s) recent",
                (str(lane), int(limit)),
            )
            row = cur.fetchone()
            return None if row is None or row[0] is None else float(row[0])


def record_turn_metric(
    *,
    job_id: int | None,
    user_id: str,
    lane: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    latency_ms: int | None,
) -> None:
    """插一行到 v2_turn_metrics（append-only，账号删除级联清除）。由 turn 调用方在
    一轮结束后调用；provider 未回 usage 时 prompt/completion_tokens 传 None，
    该行仍落地（latency 仍可信），只是不参与 recent_mean_tokens_per_turn 的均值。"""
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics "
            "(job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                job_id,
                str(user_id),
                str(lane),
                prompt_tokens,
                completion_tokens,
                latency_ms,
            ),
        )


def record_whole_turn_metric(
    job_id,
    user_id,
    lane,
    *,
    prompt_tokens,
    completion_tokens,
    latency_ms,
    model_calls,
    retries,
    failed,
    status,
    cache_read_tokens=None,
    cache_write_tokens=None,
    cache_miss_tokens=None,
    usage_reported_calls=0,
    cache_reported_calls=0,
    provider=None,
    model=None,
    cache_route_fingerprint=None,
    effective_tail_turns=None,
    tail_fallback=False,
    prompt_frontier_exhaustion_count=0,
    screen_frames_pushed=0,
    screen_frame_cache_hits=0,
    screen_frame_cache_misses=0,
    visible_reply_count=0,
) -> None:
    """One idempotent whole-turn metric per job (spec B5): upsert on job_id so a
    re-drive (redelivery/retry of the same job) REPLACES rather than appends. Covers
    all model calls, retries, and failed turns. Best-effort: never raises to the turn."""
    try:
        with _pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics (job_id, user_id, lane, prompt_tokens, "
                "completion_tokens, cache_read_tokens, cache_write_tokens, "
                "cache_miss_tokens, usage_reported_calls, cache_reported_calls, "
                "provider, model, cache_route_fingerprint, latency_ms, model_calls, "
                "retries, failed, status, effective_tail_turns, tail_fallback, "
                "prompt_frontier_exhaustion_count, screen_frames_pushed, "
                "screen_frame_cache_hits, screen_frame_cache_misses, "
                "visible_reply_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (job_id) DO UPDATE SET "
                "user_id=EXCLUDED.user_id, lane=EXCLUDED.lane, "
                "prompt_tokens=EXCLUDED.prompt_tokens, completion_tokens=EXCLUDED.completion_tokens, "
                "cache_read_tokens=EXCLUDED.cache_read_tokens, "
                "cache_write_tokens=EXCLUDED.cache_write_tokens, "
                "cache_miss_tokens=EXCLUDED.cache_miss_tokens, "
                "usage_reported_calls=EXCLUDED.usage_reported_calls, "
                "cache_reported_calls=EXCLUDED.cache_reported_calls, "
                "provider=EXCLUDED.provider, model=EXCLUDED.model, "
                "cache_route_fingerprint=EXCLUDED.cache_route_fingerprint, "
                "latency_ms=EXCLUDED.latency_ms, model_calls=EXCLUDED.model_calls, "
                "retries=EXCLUDED.retries, failed=EXCLUDED.failed, status=EXCLUDED.status, "
                "effective_tail_turns=EXCLUDED.effective_tail_turns, "
                "tail_fallback=EXCLUDED.tail_fallback, "
                "prompt_frontier_exhaustion_count="
                "EXCLUDED.prompt_frontier_exhaustion_count, "
                "screen_frames_pushed=EXCLUDED.screen_frames_pushed, "
                "screen_frame_cache_hits=EXCLUDED.screen_frame_cache_hits, "
                "screen_frame_cache_misses=EXCLUDED.screen_frame_cache_misses, "
                "visible_reply_count=EXCLUDED.visible_reply_count, "
                "updated_at=now()",
                (
                    job_id,
                    user_id,
                    lane,
                    prompt_tokens,
                    completion_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cache_miss_tokens,
                    int(usage_reported_calls),
                    int(cache_reported_calls),
                    provider,
                    model,
                    cache_route_fingerprint,
                    latency_ms,
                    model_calls,
                    retries,
                    failed,
                    status,
                    effective_tail_turns,
                    bool(tail_fallback),
                    max(0, int(prompt_frontier_exhaustion_count)),
                    max(0, int(screen_frames_pushed)),
                    max(0, int(screen_frame_cache_hits)),
                    max(0, int(screen_frame_cache_misses)),
                    max(0, int(visible_reply_count)),
                ),
            )
    except Exception as e:  # noqa: BLE001 — best-effort instrumentation, never fail the turn
        log.error("[jobs_store] record_whole_turn_metric(%s) failed: %s", job_id, e)


def recent_tail_window_stats(*, lane: str, limit: int = 1000) -> dict:
    """Content-free adaptive-tail outcomes for one exact Runtime V2 lane."""
    bounded = max(1, min(int(limit), 10_000))
    with _pool().connection() as conn:
        row = conn.execute(
            "WITH recent AS ("
            " SELECT effective_tail_turns,tail_fallback,"
            " prompt_frontier_exhaustion_count,prompt_tokens "
            " FROM v2_turn_metrics WHERE lane=%s "
            " ORDER BY created_at DESC,id DESC LIMIT %s"
            ") SELECT count(*)::int,"
            " count(effective_tail_turns)::int,"
            " min(effective_tail_turns)::int,"
            " avg(effective_tail_turns)::double precision,"
            " max(effective_tail_turns)::int,"
            " count(*) FILTER (WHERE tail_fallback)::int,"
            " coalesce(sum(prompt_frontier_exhaustion_count),0)::bigint,"
            " sum(prompt_tokens)::bigint FROM recent",
            (str(lane), bounded),
        ).fetchone()
    sampled = int(row[0] or 0) if row else 0
    measured = int(row[1] or 0) if row else 0
    fallback_turns = int(row[5] or 0) if row else 0
    return {
        "lane": str(lane),
        "sample_limit": bounded,
        "sampled_turns": sampled,
        "measured_turns": measured,
        "measurement_coverage": (
            float(measured) / float(sampled) if sampled else None
        ),
        "effective_tail_turns_min": (
            int(row[2]) if row and row[2] is not None else None
        ),
        "effective_tail_turns_avg": (
            float(row[3]) if row and row[3] is not None else None
        ),
        "effective_tail_turns_max": (
            int(row[4]) if row and row[4] is not None else None
        ),
        "fallback_turns": fallback_turns,
        "fallback_rate": (
            float(fallback_turns) / float(measured) if measured else None
        ),
        "prompt_frontier_exhaustion_count": int(row[6] or 0) if row else 0,
        "prompt_tokens": int(row[7]) if row and row[7] is not None else None,
    }


def recent_mean_tokens_per_turn(*, lane: str = "chat", limit: int = 50) -> float | None:
    """最近 limit 条该 lane 的 v2_turn_metrics 行中，prompt_tokens+completion_tokens
    的均值——只看两列都非 NULL 的行（provider 未回 usage 的行被自然排除）。
    无这样的历史 → None（调用方用默认常量，同 recent_mean_service_sec 的约定）。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT avg(prompt_tokens + completion_tokens) "
                "FROM (SELECT prompt_tokens, completion_tokens FROM v2_turn_metrics "
                "      WHERE lane=%s AND prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL "
                "      ORDER BY created_at DESC LIMIT %s) recent",
                (str(lane), int(limit)),
            )
            row = cur.fetchone()
            return None if row is None or row[0] is None else float(row[0])


def recent_token_usage_summary(
    *,
    lane: str = "chat",
    within_days: int = 30,
) -> dict:
    """Content-free token roll-up for the operator dashboard.

    Token totals are deliberately nullable: a provider call whose response did
    not include usage must lower ``usage_telemetry_coverage`` instead of being
    presented as a zero-token call. ``prompt_tokens`` is the normalized,
    effective prompt size (including cache reads/writes); cache counters remain
    separate so the same input is never double-counted in ``total_tokens``.
    """
    safe_days = max(1, min(int(within_days), 366))
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int, count(DISTINCT user_id)::int, "
                "coalesce(sum(model_calls), 0)::bigint, "
                "coalesce(sum(usage_reported_calls), 0)::bigint, "
                "coalesce(sum(cache_reported_calls), 0)::bigint, "
                "sum(prompt_tokens)::bigint, sum(completion_tokens)::bigint, "
                "sum(cache_read_tokens)::bigint, "
                "sum(cache_write_tokens)::bigint, "
                "sum(cache_miss_tokens)::bigint "
                "FROM v2_turn_metrics "
                "WHERE lane=%s "
                "AND created_at >= now() - make_interval(days => %s)",
                (str(lane), safe_days),
            )
            row = cur.fetchone()

    sampled_turns = int(row[0] or 0) if row is not None else 0
    users = int(row[1] or 0) if row is not None else 0
    model_calls = int(row[2] or 0) if row is not None else 0
    usage_calls = int(row[3] or 0) if row is not None else 0
    cache_calls = int(row[4] or 0) if row is not None else 0

    def _optional_int(index: int) -> int | None:
        if row is None or row[index] is None:
            return None
        return int(row[index])

    prompt_tokens = _optional_int(5)
    completion_tokens = _optional_int(6)
    return {
        "window_days": safe_days,
        "sampled_turns": sampled_turns,
        "users": users,
        "model_calls": model_calls,
        "usage_reported_calls": usage_calls,
        "cache_reported_calls": cache_calls,
        "usage_telemetry_coverage": (
            float(usage_calls) / float(model_calls) if model_calls else None
        ),
        "cache_telemetry_coverage": (
            float(cache_calls) / float(model_calls) if model_calls else None
        ),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": (
            prompt_tokens + completion_tokens
            if prompt_tokens is not None and completion_tokens is not None
            else None
        ),
        "cache_read_tokens": _optional_int(7),
        "cache_write_tokens": _optional_int(8),
        "cache_miss_tokens": _optional_int(9),
    }


# ``last_error`` may carry a relay's raw error body (quota figures, request
# ids). This is an admin-only surface, but keep every reason bounded so one
# pathological provider string cannot dominate the payload.
_FAILURE_REASON_MAX_CHARS = 400
_FAILURE_REASON_TOP_N = 12
_USER_FAILURE_DETAIL_LIMIT = 20


def _truncated_failure_reason(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= _FAILURE_REASON_MAX_CHARS:
        return text
    return text[:_FAILURE_REASON_MAX_CHARS] + "…"


def _iso_or_empty(value: object) -> str:
    """A timestamp column rendered for admin, or "" when the row has none."""
    if value is None:
        return ""
    try:
        return value.isoformat().replace("+00:00", "Z")  # type: ignore[attr-defined]
    except AttributeError:
        return str(value)


def recent_chat_failures_for_user(
    user_id: str,
    *,
    within_hours: int = 72,
    limit: int = _USER_FAILURE_DETAIL_LIMIT,
) -> dict:
    """Why this one user's V2 turns died, newest first.

    The fleet histogram in ``recent_chat_operational_health`` answers "is V2
    healthy"; support answers a different question — "this person says the AI
    stopped replying". The provider-attempt ledger now shows whether V2 reached
    an upstream model, but only the job row carries the exact terminal code for
    failures before and after that boundary. Reasons only: no prompts, no
    replies, no user content.
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    safe_limit = max(1, min(int(limit), 200))
    uid = str(user_id or "").strip()
    if not uid:
        return {"window_hours": safe_hours, "failures": [], "has_more": False}

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id,status,last_error,attempt_count,"
                "  created_at,finished_at "
                "FROM agent_jobs "
                "WHERE user_id=%s AND lane='chat' "
                "  AND status IN ('failed','expired') "
                "  AND finished_at >= now() - make_interval(hours => %s) "
                "ORDER BY finished_at DESC,id DESC LIMIT %s",
                (uid, safe_hours, safe_limit + 1),
            )
            rows = cur.fetchall()

    has_more = len(rows) > safe_limit
    return {
        "window_hours": safe_hours,
        "has_more": has_more,
        "failures": [
            {
                "job_id": int(row["id"]),
                "status": str(row["status"] or ""),
                "reason": _truncated_failure_reason(row["last_error"]),
                "attempt_count": int(row["attempt_count"] or 0),
                "created_at": _iso_or_empty(row["created_at"]),
                "finished_at": _iso_or_empty(row["finished_at"]),
            }
            for row in rows[:safe_limit]
        ],
    }


WAKE_LANES_FOR_SUPPORT = ("heartbeat", "scheduled", "manual_wake", "screen_watch")


def wake_lane_activity_for_user(user_id: str, *, within_hours: int = 72) -> dict:
    """这一个用户的 V2 **主动唤醒**活动:按 lane 分的 job 计数 + 最近失败原因。

    存在的理由:admin 数据面原本只有 V1 口径的 `proactive_jobs` 日志,而 V2 的唤醒
    job 在 `agent_jobs`。于是一个 V2 用户在数据面上永远显示「心跳 0 次」,看起来
    像故障——2026-08-10 我据此差点误报「心跳十天没跑」。

    出的是计数 + **有界的原始失败原因**(`_truncated_failure_reason`,与
    `recent_chat_failures_for_user` 同一姿态),不出任何 prompt/回复内容。
    ⚠️ 措辞上别说成「只出错误码」:`mark_failed` 收的是任意异常文本,这里给的是它的
    前若干字符,不是受控枚举(codex 复验 2026-08-10 指出)。admin-only,可接受,
    但契约要写准。
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    uid = str(user_id or "").strip()
    empty = {
        "window_hours": safe_hours,
        "by_lane": {},
        "totals": {"jobs": 0, "completed": 0, "failed": 0, "pending": 0},
        "recent_failures": [],
        "last_terminal_at": "",
    }
    if not uid:
        return empty

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT lane, status, count(*) AS n, max(finished_at) AS last_at "
                "FROM agent_jobs "
                "WHERE user_id=%s AND lane = ANY(%s) "
                "  AND created_at >= now() - make_interval(hours => %s) "
                "GROUP BY lane, status",
                (uid, list(WAKE_LANES_FOR_SUPPORT), safe_hours),
            )
            grouped = cur.fetchall()
            cur.execute(
                "SELECT id, lane, status, last_error, created_at, finished_at "
                "FROM agent_jobs "
                "WHERE user_id=%s AND lane = ANY(%s) "
                "  AND status IN ('failed','expired') "
                "  AND finished_at >= now() - make_interval(hours => %s) "
                "ORDER BY finished_at DESC, id DESC LIMIT 20",
                (uid, list(WAKE_LANES_FOR_SUPPORT), safe_hours),
            )
            failures = cur.fetchall()

    by_lane: dict[str, dict] = {}
    totals = {"jobs": 0, "completed": 0, "failed": 0, "pending": 0}
    last_terminal = None
    for row in grouped:
        lane = str(row["lane"] or "")
        status = str(row["status"] or "")
        n = int(row["n"] or 0)
        slot = by_lane.setdefault(lane, {})
        slot[status] = slot.get(status, 0) + n
        totals["jobs"] += n
        if status == "completed":
            totals["completed"] += n
        elif status in ("failed", "expired"):
            totals["failed"] += n
        elif status == "pending":
            totals["pending"] += n
        if row["last_at"] is not None and (last_terminal is None or row["last_at"] > last_terminal):
            last_terminal = row["last_at"]

    return {
        "window_hours": safe_hours,
        "by_lane": by_lane,
        "totals": totals,
        "recent_failures": [
            {
                "job_id": int(r["id"]),
                "lane": str(r["lane"] or ""),
                "status": str(r["status"] or ""),
                "reason": _truncated_failure_reason(r["last_error"]),
                "created_at": _iso_or_empty(r["created_at"]),
                "finished_at": _iso_or_empty(r["finished_at"]),
            }
            for r in failures
        ],
        "last_terminal_at": _iso_or_empty(last_terminal),
    }


def recent_chat_operational_health(
    *,
    within_hours: int = 24,
    limit: int = 1000,
) -> dict:
    """Bounded, content-free health snapshot for foreground V2 turns.

    Job outcomes and trajectory coverage deliberately start from
    ``agent_jobs``.  Starting from metrics or trajectory rows would make a
    completely missing write disappear from the denominator and report a
    falsely healthy fleet.  ``superseded`` is reported separately from real
    outcomes so a runtime-generation cutover cannot dilute the failure/expiry
    rates. Jobs with an explicit lifecycle tombstone remain in outcome health
    but leave the capture denominator; their absent ciphertext is intentional,
    not a capture failure.
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    safe_limit = max(1, min(int(limit), 1000))
    terminal_statuses = ("completed", "failed", "expired", "superseded")

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "WITH recent_outcomes AS ("
                "  SELECT id,status,last_error FROM agent_jobs "
                "  WHERE lane='chat' "
                "    AND status IN ('completed','failed','expired') "
                "    AND finished_at >= now() - make_interval(hours => %s) "
                "  ORDER BY finished_at DESC,id DESC LIMIT %s"
                "), recent_superseded AS ("
                "  SELECT id FROM agent_jobs WHERE lane='chat' "
                "    AND status='superseded' "
                "    AND finished_at >= now() - make_interval(hours => %s) "
                "  ORDER BY finished_at DESC,id DESC LIMIT %s"
                "), outcomes AS ("
                "  SELECT COUNT(*)::int AS sampled_outcome_jobs,"
                "    COUNT(*) FILTER (WHERE status='completed')::int AS completed,"
                "    COUNT(*) FILTER (WHERE status='failed')::int AS failed,"
                "    COUNT(*) FILTER (WHERE status='expired')::int AS expired,"
                "    COUNT(*) FILTER (WHERE status='expired' "
                "      AND last_error='queue_timeout')::int AS queue_expired,"
                "    COUNT(*) FILTER (WHERE status='expired' "
                "      AND last_error='lease_timeout')::int AS lease_expired "
                "  FROM recent_outcomes"
                # Reasons MUST come out of recent_outcomes, not a second scan
                # of agent_jobs. A separate "most recent N failures" query
                # samples a different set than the "most recent N terminal
                # jobs" the counts above are computed from, so once the window
                # holds more than `limit` terminal jobs the histogram stops
                # summing to `failed + expired` and the panel contradicts
                # itself. Sharing the CTE also keeps this to one index scan.
                "), failure_reasons AS ("
                "  SELECT COALESCE(json_agg(json_build_object("
                "    'reason',reason,'count',n) ORDER BY n DESC,reason),"
                "    '[]'::json) AS reasons FROM ("
                "    SELECT COALESCE(NULLIF(last_error,''),'(empty)') AS reason,"
                "      COUNT(*)::int AS n FROM recent_outcomes "
                "    WHERE status IN ('failed','expired') "
                "    GROUP BY 1 ORDER BY n DESC,reason LIMIT %s"
                "  ) ranked"
                "), superseded AS ("
                "  SELECT COUNT(*)::int AS superseded FROM recent_superseded"
                "), pending AS ("
                "  SELECT COUNT(*)::int AS pending,"
                "    EXTRACT(EPOCH FROM "
                "      (clock_timestamp()-MIN(created_at))) AS oldest_pending_age_sec "
                "  FROM agent_jobs WHERE lane='chat' AND status='pending'"
                ") SELECT outcomes.*,superseded.superseded,pending.pending,"
                "pending.oldest_pending_age_sec,failure_reasons.reasons "
                "FROM outcomes CROSS JOIN failure_reasons "
                "CROSS JOIN superseded CROSS JOIN pending",
                (
                    safe_hours,
                    safe_limit,
                    safe_hours,
                    safe_limit,
                    _FAILURE_REASON_TOP_N,
                ),
            )
            job_row = cur.fetchone()

            cur.execute(
                "WITH recent AS ("
                "  SELECT latency_ms FROM v2_turn_metrics "
                "  WHERE lane='chat' AND latency_ms IS NOT NULL AND latency_ms >= 0 "
                "    AND created_at >= now() - make_interval(hours => %s) "
                "  ORDER BY created_at DESC,id DESC LIMIT %s"
                ") SELECT COUNT(*)::int AS sampled_turns,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) "
                "    AS p95_ms FROM recent",
                (safe_hours, safe_limit),
            )
            latency_row = cur.fetchone()

            cur.execute(
                "WITH recent_jobs AS ("
                "  SELECT id,status FROM agent_jobs "
                "  WHERE lane='chat' "
                "    AND created_at >= now() - make_interval(hours => %s) "
                "  ORDER BY id DESC LIMIT %s"
                "), classified AS ("
                "  SELECT job.id,"
                "    EXISTS (SELECT 1 FROM v2_trajectory_events gap "
                "      WHERE gap.job_id=job.id AND gap.event_kind='capture_gap') "
                "      AS has_capture_gap,"
                "    CASE "
                "      WHEN stream.job_id IS NULL "
                "        AND job.status=ANY(%s::text[]) THEN 'missing' "
                "      WHEN stream.job_id IS NULL THEN 'open' "
                "      WHEN EXISTS (SELECT 1 FROM v2_trajectory_events gap "
                "        WHERE gap.job_id=job.id AND gap.event_kind='capture_gap') "
                "        THEN 'partial' "
                "      WHEN EXISTS (SELECT 1 FROM v2_trajectory_events terminal "
                "        WHERE terminal.job_id=job.id "
                "          AND terminal.event_kind='turn_terminal') THEN 'complete' "
                "      WHEN job.status=ANY(%s::text[]) THEN 'partial' "
                "      ELSE 'open' "
                "    END AS capture_status "
                "  FROM recent_jobs job "
                "  LEFT JOIN v2_trajectory_streams stream ON stream.job_id=job.id"
                ") SELECT COUNT(*)::int AS sampled_jobs,"
                "  COUNT(*) FILTER (WHERE capture_status='complete')::int AS complete,"
                "  COUNT(*) FILTER (WHERE capture_status='partial')::int AS partial,"
                "  COUNT(*) FILTER (WHERE capture_status='missing')::int AS missing,"
                "  COUNT(*) FILTER (WHERE capture_status='open')::int AS open,"
                "  COUNT(*) FILTER (WHERE has_capture_gap)::int AS capture_gap "
                "FROM classified",
                (
                    safe_hours,
                    safe_limit,
                    list(terminal_statuses),
                    list(terminal_statuses),
                ),
            )
            trajectory_row = cur.fetchone()

    completed = int(job_row["completed"] or 0)
    failed = int(job_row["failed"] or 0)
    expired = int(job_row["expired"] or 0)
    outcome_jobs = completed + failed + expired
    sampled_trajectories = int(trajectory_row["sampled_jobs"] or 0)
    complete_trajectories = int(trajectory_row["complete"] or 0)
    oldest_pending_age = job_row["oldest_pending_age_sec"]
    p95_ms = latency_row["p95_ms"]
    return {
        "window_hours": safe_hours,
        "sample_limit": safe_limit,
        "jobs": {
            "sampled_terminal_jobs": (
                int(job_row["sampled_outcome_jobs"] or 0)
                + int(job_row["superseded"] or 0)
            ),
            "completed": completed,
            "failed": failed,
            "expired": expired,
            "queue_expired": int(job_row["queue_expired"] or 0),
            "lease_expired": int(job_row["lease_expired"] or 0),
            "superseded": int(job_row["superseded"] or 0),
            "failure_rate": (failed / outcome_jobs) if outcome_jobs else None,
            "expiry_rate": (expired / outcome_jobs) if outcome_jobs else None,
            "error_or_expiry_rate": (
                (failed + expired) / outcome_jobs if outcome_jobs else None
            ),
            "pending": int(job_row["pending"] or 0),
            "oldest_pending_age_sec": (
                max(0.0, float(oldest_pending_age))
                if oldest_pending_age is not None
                else None
            ),
            # Reasons come from the same recent_outcomes sample as the counts
            # above, so this list always reconciles with failed + expired.
            "failure_reasons": [
                {
                    "reason": _truncated_failure_reason(entry.get("reason")),
                    "count": int(entry.get("count") or 0),
                }
                for entry in (job_row["reasons"] or [])
            ],
        },
        "latency": {
            "sampled_turns": int(latency_row["sampled_turns"] or 0),
            "p95_ms": float(p95_ms) if p95_ms is not None else None,
        },
        "trajectory": {
            "sampled_jobs": sampled_trajectories,
            "complete": complete_trajectories,
            "partial": int(trajectory_row["partial"] or 0),
            "missing": int(trajectory_row["missing"] or 0),
            "open": int(trajectory_row["open"] or 0),
            "capture_gap": int(trajectory_row["capture_gap"] or 0),
            "complete_rate": (
                complete_trajectories
                / (
                    complete_trajectories
                    + int(trajectory_row["partial"] or 0)
                    + int(trajectory_row["missing"] or 0)
                )
                if (
                    complete_trajectories
                    + int(trajectory_row["partial"] or 0)
                    + int(trajectory_row["missing"] or 0)
                )
                else None
            ),
        },
    }


def recent_runtime_health(*, within_hours: int = 24) -> dict:
    """全 lane 运行时健康快照（content-free），喂 admin 值班台。

    分母刻意从 ``agent_jobs`` 起算而非从 metrics/trajectory 起算：一次完全漏写
    若同时消失于分子和分母，就会报出虚假健康的机群。``superseded`` 单列、不进
    失败率——运行时代际切换不是故障。延迟分位数只取成功回合（``failed IS NOT
    TRUE``）：失败超时回合会把 p95 拉到与故障同源的高位，让一个故障看起来像两个。

    **窗口内全量，不设采样上界**（2026-07-30 审计打回）：本函数曾对四条子查询各
    加 ``LIMIT 1000``，于是 24h 档写着「24 小时」，实际是「最近 1000 个 job」。
    同页的 ``recent_token_usage_by_lane`` 从一开始就是窗口内全量，两者在 168h /
    720h 档覆盖的时间跨度并不相同——值班时把「失败率」和「token」放在一行读，
    却是两批样本，无法互相对账。采样上界不是性能手段而是正确性缺陷：它让页面
    在长窗口下**静默少报**故障总量。扫描量改由 0071 的三条 ``created_at`` /
    ``finished_at`` 索引承担（不是 ``ix_v2_turn_metrics_lane_created_at``——它
    以 ``lane`` 为前导列，对非前导列的范围谓词只能全索引扫描、给不出范围收窄；
    0071 的注释里有 24h/720h 两档的 EXPLAIN 实测数字）。

    ``capture`` 的第一个桶叫 ``terminal_seen_no_gap`` 而**不叫** ``complete``：
    它只证明「找到了 ``turn_terminal`` 事件、且没有 ``capture_gap``」，不证明
    prompt / provider 往返 / tool call / 最终回复这些 artifact 都齐全。叫
    ``complete`` 会让读者（和下一个改这段代码的人）以为轨迹可以完整回放。
    姊妹函数 ``recent_chat_operational_health`` 仍用 ``complete``/``complete_rate``
    ——那是 /model_api 指标端点的对外契约，改名要单独走版本沟通，不混在本次改动里。
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    terminal_statuses = ("completed", "failed", "expired", "superseded")

    # These jobs intentionally settle as failed so the runtime does not retry or
    # emit an unsafe bubble.  They are still visible outcomes, but they are not
    # interchangeable with a provider/worker/runtime failure on an operations
    # dashboard.  Keep this list content-free and deliberately narrow: an unknown
    # code stays operational until somebody classifies it explicitly.
    control_outcome_codes = frozenset({
        "runtime_mode_changed",
        "turns_halted",
        "capture_disabled",
        "dream_disabled",
        "profile_disabled",
        "maintenance_disabled",
        "heartbeat_disabled",
        "scheduled_disabled",
        "manual_wake_disabled",
    })
    safety_suppression_codes = frozenset({
        "wake_failed:degenerate_reply_suppressed",
        "wake_failed:protocol_fragment_suppressed",
        "wake_failed:malformed_self_thinking_suppressed",
    })

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "WITH recent AS ("
                "  SELECT lane,status,last_error FROM agent_jobs "
                "  WHERE status IN ('completed','failed','expired','superseded') "
                "    AND finished_at >= now() - make_interval(hours => %s)"
                ") SELECT lane,"
                "  COUNT(*) FILTER (WHERE status='completed')::int AS completed,"
                "  COUNT(*) FILTER (WHERE status='failed')::int AS failed,"
                "  COUNT(*) FILTER (WHERE status='expired')::int AS expired,"
                "  COUNT(*) FILTER (WHERE status='superseded')::int AS superseded,"
                "  COUNT(*) FILTER (WHERE status='expired' "
                "    AND last_error='queue_timeout')::int AS queue_expired,"
                "  COUNT(*) FILTER (WHERE status='expired' "
                "    AND last_error='lease_timeout')::int AS lease_expired "
                "FROM recent GROUP BY lane",
                (safe_hours,),
            )
            outcome_rows = cur.fetchall()

            cur.execute(
                "WITH recent AS ("
                "  SELECT lane,latency_ms FROM v2_turn_metrics "
                "  WHERE failed IS NOT TRUE AND latency_ms IS NOT NULL "
                "    AND latency_ms >= 0 "
                "    AND created_at >= now() - make_interval(hours => %s)"
                ") SELECT lane,"
                "  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms "
                "FROM recent GROUP BY lane",
                (safe_hours,),
            )
            latency_rows = cur.fetchall()

            cur.execute(
                "WITH recent_jobs AS ("
                "  SELECT id,lane,status FROM agent_jobs "
                "  WHERE created_at >= now() - make_interval(hours => %s)"
                "), classified AS ("
                "  SELECT job.lane,"
                "    CASE "
                "      WHEN stream.job_id IS NULL "
                "        AND job.status=ANY(%s::text[]) THEN 'missing' "
                "      WHEN stream.job_id IS NULL THEN 'open' "
                "      WHEN EXISTS (SELECT 1 FROM v2_trajectory_events gap "
                "        WHERE gap.job_id=job.id "
                "          AND gap.event_kind='capture_gap') THEN 'partial' "
                "      WHEN EXISTS (SELECT 1 FROM v2_trajectory_events terminal "
                "        WHERE terminal.job_id=job.id "
                "          AND terminal.event_kind='turn_terminal') "
                "        THEN 'terminal_seen_no_gap' "
                "      WHEN job.status=ANY(%s::text[]) THEN 'partial' "
                "      ELSE 'open' "
                "    END AS capture_status "
                "  FROM recent_jobs job "
                "  LEFT JOIN v2_trajectory_streams stream ON stream.job_id=job.id"
                ") SELECT lane,"
                "  COUNT(*) FILTER (WHERE capture_status='terminal_seen_no_gap')::int "
                "    AS terminal_seen_no_gap,"
                "  COUNT(*) FILTER (WHERE capture_status='partial')::int AS partial,"
                "  COUNT(*) FILTER (WHERE capture_status='missing')::int AS missing,"
                "  COUNT(*) FILTER (WHERE capture_status='open')::int AS open "
                "FROM classified GROUP BY lane",
                (
                    safe_hours,
                    list(terminal_statuses),
                    list(terminal_statuses),
                ),
            )
            capture_rows = cur.fetchall()

            cur.execute(
                "WITH recent AS ("
                "  SELECT id,lane,last_error FROM agent_jobs "
                "  WHERE status IN ('failed','expired') AND last_error IS NOT NULL "
                "    AND finished_at >= now() - make_interval(hours => %s)"
                ") SELECT recent.lane,recent.last_error,"
                "  COALESCE(f.error_class,'') AS error_class,"
                "  COUNT(*)::int AS count "
                "FROM recent LEFT JOIN v2_terminal_failure_outbox f "
                "  ON f.job_id=recent.id "
                "GROUP BY recent.lane,recent.last_error,f.error_class "
                "ORDER BY count DESC",
                (safe_hours,),
            )
            failure_rows = cur.fetchall()

            cur.execute(
                "SELECT COUNT(*)::int AS pending,"
                "  EXTRACT(EPOCH FROM "
                "    (clock_timestamp()-MIN(created_at))) AS oldest_pending_age_sec "
                "FROM agent_jobs WHERE status='pending'"
            )
            pending_row = cur.fetchone()

    latency_by_lane = {str(row["lane"] or ""): row for row in latency_rows}
    capture_by_lane = {str(row["lane"] or ""): row for row in capture_rows}
    failures_by_lane: dict[str, list[dict]] = {}
    for row in failure_rows:
        code = str(row["last_error"] or "")
        if code in control_outcome_codes:
            outcome_class = "control"
        elif code in safety_suppression_codes:
            outcome_class = "safety_suppression"
        elif code in {"queue_timeout", "lease_timeout", "runtime_expired"}:
            outcome_class = "timeout"
        else:
            outcome_class = "operational_failure"
        failures_by_lane.setdefault(str(row["lane"] or ""), []).append({
            "code": code,
            "error_class": str(row.get("error_class") or ""),
            "count": int(row["count"] or 0),
            "outcome_class": outcome_class,
        })

    def _optional_ms(row, key):
        if row is None or row.get(key) is None:
            return None
        return float(row[key])

    # 收集所有 lane 的并集：即使全部 job 都未终态的 lane 也要保留，
    # 防止 worker 卡死时该 lane 从健康视图消失。
    all_lanes = set()
    for row in outcome_rows:
        all_lanes.add(str(row["lane"] or ""))
    all_lanes.update(latency_by_lane.keys())
    all_lanes.update(capture_by_lane.keys())
    all_lanes.update(failures_by_lane.keys())

    # 为了排序和记录目的，也建立 outcome 索引
    outcome_by_lane = {
        str(row["lane"] or ""): row for row in outcome_rows
    }

    lanes = []
    for lane in all_lanes:
        outcome = outcome_by_lane.get(lane)
        completed = int((outcome or {}).get("completed") or 0)
        failed = int((outcome or {}).get("failed") or 0)
        expired = int((outcome or {}).get("expired") or 0)
        resolved = completed + failed + expired
        lane_failures = failures_by_lane.get(lane, [])
        control_outcomes = sum(
            int(item.get("count") or 0)
            for item in lane_failures
            if item.get("outcome_class") == "control"
        )
        safety_suppressions = sum(
            int(item.get("count") or 0)
            for item in lane_failures
            if item.get("outcome_class") == "safety_suppression"
        )
        # Expiries are always operational incidents.  Only the two allowlisted
        # failed-job classes above are removed from the hard-failure numerator.
        operational_failures = max(
            0,
            failed + expired - control_outcomes - safety_suppressions,
        )
        capture = capture_by_lane.get(lane)
        lanes.append({
            "lane": lane or "unknown",
            "sampled_jobs": resolved,
            "completed": completed,
            "failed": failed,
            "expired": expired,
            "superseded": int((outcome or {}).get("superseded") or 0),
            "queue_expired": int((outcome or {}).get("queue_expired") or 0),
            "lease_expired": int((outcome or {}).get("lease_expired") or 0),
            "operational_failures": operational_failures,
            "control_outcomes": control_outcomes,
            "safety_suppressions": safety_suppressions,
            "failure_rate": (
                float(failed + expired) / float(resolved) if resolved else None
            ),
            "operational_failure_rate": (
                float(operational_failures) / float(resolved)
                if resolved else None
            ),
            "p50_ok_ms": _optional_ms(latency_by_lane.get(lane), "p50_ms"),
            "p95_ok_ms": _optional_ms(latency_by_lane.get(lane), "p95_ms"),
            "capture": {
                # 只表示「见到 turn_terminal 且无 capture_gap」，不表示 artifact 齐全。
                "terminal_seen_no_gap": int(
                    (capture or {}).get("terminal_seen_no_gap") or 0
                ),
                "partial": int((capture or {}).get("partial") or 0),
                "missing": int((capture or {}).get("missing") or 0),
                "open": int((capture or {}).get("open") or 0),
            },
            "top_failures": lane_failures[:8],
        })

    lanes.sort(key=lambda item: (item["sampled_jobs"], item["lane"]), reverse=True)
    oldest_pending = (
        pending_row.get("oldest_pending_age_sec") if pending_row else None
    )
    return {
        "window_hours": safe_hours,
        "generated_at": time.time(),
        "lanes": lanes,
        "pool": {
            "inflight": inflight_job_count(),
            "pending": int((pending_row or {}).get("pending") or 0),
            "live_workers": live_worker_count(),
            "capacity": live_worker_capacity(),
            "oldest_pending_age_sec": (
                float(oldest_pending) if oldest_pending is not None else None
            ),
        },
    }


def recent_chat_reliability(
    *,
    within_hours: int = 24,
    recent_limit: int = 50,
) -> dict:
    """Hosted V2 chat lifecycle, server-delivery, and latency evidence.

    The cohort begins at ``agent_jobs(lane='chat').created_at``.  A final reply
    effect in ``applied`` / ``applied_with_results`` means the backend finished
    its configured sink transaction; it is *not* a device delivery/read ACK.
    Keeping those two facts separate is the central contract of the Admin chat
    page.

    Provider/model rows come from whole-turn metrics and are diagnostic only.
    They cannot answer duplicate-charge or possibly-billed questions: those
    require the canonical provider-attempt ledger planned for P0-B.
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    safe_limit = max(1, min(int(recent_limit), 200))
    # ``reply`` predates the explicit final/intermediate effect vocabulary.
    # Only legacy rows carrying the consumed-input frontier are final.  A
    # plain ``reply`` may be an acknowledgement or other intermediate bubble
    # and must not inflate final-delivery counts or make the rate exceed 100%.
    explicit_final_effect_types = (
        "reply_final_fenced_v1",
        "reply_terminal_fenced_v1",
    )

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "WITH chat AS ("
                " SELECT * FROM agent_jobs WHERE lane='chat' "
                " AND created_at >= now() - make_interval(hours => %s)"
                ") SELECT "
                " count(*)::int AS admitted,"
                " count(*) FILTER (WHERE claimed_at IS NOT NULL "
                "   OR started_at IS NOT NULL)::int AS started,"
                " count(*) FILTER (WHERE status='completed')::int AS completed,"
                " count(*) FILTER (WHERE status='failed')::int AS failed,"
                " count(*) FILTER (WHERE status='expired')::int AS expired,"
                " count(*) FILTER (WHERE status='superseded')::int AS superseded,"
                " count(*) FILTER (WHERE status IN "
                "   ('pending','claimed','running'))::int AS in_flight,"
                " count(DISTINCT user_id)::int AS users "
                "FROM chat",
                (safe_hours,),
            )
            outcome = cur.fetchone() or {}

            cur.execute(
                "WITH chat AS ("
                " SELECT id,status FROM agent_jobs WHERE lane='chat' "
                " AND created_at >= now() - make_interval(hours => %s)"
                "), effect_by_job AS ("
                " SELECT e.job_id, count(*)::int AS effect_rows,"
                "  bool_or(e.status IN ('applied','applied_with_results')) AS applied,"
                "  bool_or(e.status IN ('pending','pending_fenced_v1')) AS pending,"
                "  bool_or(e.status='needs_reconciliation') AS needs_reconciliation,"
                "  bool_or(e.status='discarded') AS discarded "
                " FROM v2_effect_outbox e JOIN chat ON chat.id=e.job_id "
                " WHERE (e.effect_type=ANY(%s::text[]) "
                " OR (e.effect_type='reply' "
                "     AND e.payload ? 'reply_through_seq')) "
                " AND e.created_at >= now() - make_interval(hours => %s) "
                " GROUP BY e.job_id"
                ") SELECT "
                " count(*) FILTER (WHERE effect_rows IS NOT NULL)::int "
                "   AS final_effect_jobs,"
                " coalesce(sum(effect_rows),0)::int AS final_effect_rows,"
                " count(*) FILTER (WHERE applied)::int AS final_applied_jobs,"
                " count(*) FILTER (WHERE pending)::int AS final_pending_jobs,"
                " count(*) FILTER (WHERE needs_reconciliation)::int "
                "   AS final_reconciliation_jobs,"
                " count(*) FILTER (WHERE discarded)::int AS final_discarded_jobs,"
                " count(*) FILTER (WHERE effect_rows > 1)::int "
                "   AS duplicate_final_effect_jobs,"
                " count(*) FILTER (WHERE chat.status='completed' "
                "   AND coalesce(applied,false) IS NOT TRUE)::int "
                "   AS completed_without_final_applied "
                "FROM chat LEFT JOIN effect_by_job e ON e.job_id=chat.id",
                (safe_hours, list(explicit_final_effect_types), safe_hours),
            )
            effects = cur.fetchone() or {}

            cur.execute(
                "WITH chat AS ("
                " SELECT id FROM agent_jobs WHERE lane='chat' "
                " AND created_at >= now() - make_interval(hours => %s)"
                ") SELECT "
                " count(*)::int AS failure_rows,"
                " count(*) FILTER (WHERE f.reply_delivered_at IS NOT NULL)::int "
                "   AS fallback_reply_delivered,"
                " count(*) FILTER (WHERE f.reply_delivered_at IS NULL)::int "
                "   AS fallback_reply_pending,"
                " count(*) FILTER (WHERE f.status_delivered_at IS NOT NULL)::int "
                "   AS error_status_delivered,"
                " count(*) FILTER (WHERE f.runtime_error_delivered_at IS NOT NULL)::int "
                "   AS runtime_error_delivered "
                "FROM chat JOIN v2_terminal_failure_outbox f ON f.job_id=chat.id",
                (safe_hours,),
            )
            failure_delivery = cur.fetchone() or {}

            cur.execute(
                "WITH chat AS ("
                " SELECT * FROM agent_jobs WHERE lane='chat' "
                " AND created_at >= now() - make_interval(hours => %s)"
                "), final_applied AS ("
                " SELECT e.job_id,min(e.applied_at) AS applied_at "
                " FROM v2_effect_outbox e JOIN chat ON chat.id=e.job_id "
                " WHERE (e.effect_type=ANY(%s::text[]) "
                " OR (e.effect_type='reply' "
                "     AND e.payload ? 'reply_through_seq')) "
                " AND e.created_at >= now() - make_interval(hours => %s) "
                " AND e.status IN ('applied','applied_with_results') "
                " AND e.applied_at IS NOT NULL GROUP BY e.job_id"
                ") SELECT "
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (coalesce(started_at,claimed_at)-created_at))) "
                "   FILTER (WHERE coalesce(started_at,claimed_at) >= created_at) "
                "     AS queue_p50_sec,"
                " percentile_cont(0.95) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (coalesce(started_at,claimed_at)-created_at))) "
                "   FILTER (WHERE coalesce(started_at,claimed_at) >= created_at) "
                "     AS queue_p95_sec,"
                " percentile_cont(0.99) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (coalesce(started_at,claimed_at)-created_at))) "
                "   FILTER (WHERE coalesce(started_at,claimed_at) >= created_at) "
                "     AS queue_p99_sec,"
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (finished_at-coalesce(started_at,claimed_at)))) "
                "   FILTER (WHERE finished_at >= coalesce(started_at,claimed_at)) "
                "     AS processing_p50_sec,"
                " percentile_cont(0.95) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (finished_at-coalesce(started_at,claimed_at)))) "
                "   FILTER (WHERE finished_at >= coalesce(started_at,claimed_at)) "
                "     AS processing_p95_sec,"
                " percentile_cont(0.99) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (finished_at-coalesce(started_at,claimed_at)))) "
                "   FILTER (WHERE finished_at >= coalesce(started_at,claimed_at)) "
                "     AS processing_p99_sec,"
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (finished_at-created_at))) "
                "   FILTER (WHERE finished_at >= created_at) AS turn_p50_sec,"
                " percentile_cont(0.95) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (finished_at-created_at))) "
                "   FILTER (WHERE finished_at >= created_at) AS turn_p95_sec,"
                " percentile_cont(0.99) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (finished_at-created_at))) "
                "   FILTER (WHERE finished_at >= created_at) AS turn_p99_sec,"
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (final_applied.applied_at-chat.created_at))) "
                "   FILTER (WHERE final_applied.applied_at >= chat.created_at) "
                "     AS server_applied_p50_sec,"
                " percentile_cont(0.95) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (final_applied.applied_at-chat.created_at))) "
                "   FILTER (WHERE final_applied.applied_at >= chat.created_at) "
                "     AS server_applied_p95_sec,"
                " percentile_cont(0.99) WITHIN GROUP (ORDER BY "
                "   extract(epoch FROM (final_applied.applied_at-chat.created_at))) "
                "   FILTER (WHERE final_applied.applied_at >= chat.created_at) "
                "     AS server_applied_p99_sec "
                "FROM chat LEFT JOIN final_applied ON final_applied.job_id=chat.id",
                (safe_hours, list(explicit_final_effect_types), safe_hours),
            )
            latency = cur.fetchone() or {}

            cur.execute(
                "SELECT coalesce(provider,'unknown') AS provider,"
                " coalesce(model,'unknown') AS model,"
                " count(*)::int AS turns,"
                " coalesce(sum(model_calls),0)::int AS model_calls,"
                " coalesce(sum(retries),0)::int AS retries,"
                " count(*) FILTER (WHERE failed IS TRUE)::int AS failed_turns,"
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) "
                "   FILTER (WHERE latency_ms >= 0) AS p50_ms,"
                " percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) "
                "   FILTER (WHERE latency_ms >= 0) AS p95_ms,"
                " percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) "
                "   FILTER (WHERE latency_ms >= 0) AS p99_ms "
                "FROM v2_turn_metrics WHERE lane='chat' "
                " AND created_at >= now() - make_interval(hours => %s) "
                "GROUP BY provider,model ORDER BY turns DESC,provider,model",
                (safe_hours,),
            )
            model_breakdown = [dict(row) for row in cur.fetchall()]

            cur.execute(
                "SELECT last_error,count(*)::int AS count FROM agent_jobs "
                "WHERE lane='chat' AND status IN ('failed','expired') "
                " AND created_at >= now() - make_interval(hours => %s) "
                "GROUP BY last_error ORDER BY count DESC",
                (safe_hours,),
            )
            merged_failures: dict[str, int] = {}
            for row in cur.fetchall():
                code = _terminal_error_code(row.get("last_error"))
                merged_failures[code] = (
                    merged_failures.get(code, 0) + int(row.get("count") or 0)
                )

            cur.execute(
                "WITH chat AS ("
                " SELECT * FROM agent_jobs WHERE lane='chat' "
                " AND created_at >= now() - make_interval(hours => %s) "
                " ORDER BY created_at DESC,id DESC LIMIT %s"
                "), final_effect AS ("
                " SELECT e.job_id,"
                "  CASE "
                "   WHEN bool_or(e.status IN ('applied','applied_with_results')) "
                "     THEN 'server_applied' "
                "   WHEN bool_or(e.status='needs_reconciliation') "
                "     THEN 'needs_reconciliation' "
                "   WHEN bool_or(e.status IN ('pending','pending_fenced_v1')) "
                "     THEN 'pending' "
                "   WHEN bool_or(e.status='discarded') THEN 'discarded' "
                "   ELSE 'unknown' END AS final_effect_status "
                " FROM v2_effect_outbox e JOIN chat ON chat.id=e.job_id "
                " WHERE (e.effect_type=ANY(%s::text[]) "
                " OR (e.effect_type='reply' "
                "     AND e.payload ? 'reply_through_seq')) "
                " AND e.created_at >= now() - make_interval(hours => %s) "
                " GROUP BY e.job_id"
                "), metric AS ("
                " SELECT DISTINCT ON (m.job_id) m.job_id,m.provider,m.model,"
                "   m.model_calls,m.retries,m.latency_ms "
                " FROM v2_turn_metrics m JOIN chat ON chat.id=m.job_id "
                " ORDER BY m.job_id,m.created_at DESC,m.id DESC"
                ") SELECT chat.id AS job_id,chat.user_id,chat.status,"
                " chat.last_error,chat.created_at,chat.started_at,chat.finished_at,"
                " coalesce(final_effect.final_effect_status,'missing') "
                "   AS final_effect_status,"
                " metric.provider,metric.model,metric.model_calls,metric.retries,"
                " metric.latency_ms "
                "FROM chat LEFT JOIN final_effect ON final_effect.job_id=chat.id "
                "LEFT JOIN metric ON metric.job_id=chat.id "
                "ORDER BY chat.created_at DESC,chat.id DESC",
                (
                    safe_hours,
                    safe_limit,
                    list(explicit_final_effect_types),
                    safe_hours,
                ),
            )
            recent_jobs = []
            for row in cur.fetchall():
                item = dict(row)
                item["last_error"] = (
                    _terminal_error_code(item.get("last_error"))
                    if item.get("last_error") else ""
                )
                recent_jobs.append(item)

    def as_int(row: dict, key: str) -> int:
        return int(row.get(key) or 0)

    def optional_float(row: dict, key: str) -> float | None:
        value = row.get(key)
        return None if value is None else float(value)

    completed = as_int(outcome, "completed")
    failed = as_int(outcome, "failed")
    expired = as_int(outcome, "expired")
    admitted = as_int(outcome, "admitted")
    settled = completed + failed + expired
    applied = as_int(effects, "final_applied_jobs")
    return {
        "window_hours": safe_hours,
        "outcomes": {
            key: as_int(outcome, key)
            for key in (
                "admitted", "started", "completed", "failed", "expired",
                "superseded", "in_flight", "users",
            )
        },
        "reply_delivery": {
            key: as_int(effects, key)
            for key in (
                "final_effect_jobs", "final_effect_rows", "final_applied_jobs",
                "final_pending_jobs", "final_reconciliation_jobs",
                "final_discarded_jobs", "duplicate_final_effect_jobs",
                "completed_without_final_applied",
            )
        },
        "failure_delivery": {
            key: as_int(failure_delivery, key)
            for key in (
                "failure_rows", "fallback_reply_delivered",
                "fallback_reply_pending", "error_status_delivered",
                "runtime_error_delivered",
            )
        },
        "settled_jobs": settled,
        "terminal_completion_rate": (
            float(completed) / float(settled) if settled else None
        ),
        "server_final_reply_applied_rate": (
            float(applied) / float(admitted) if admitted else None
        ),
        "latency": {
            key: optional_float(latency, key)
            for key in (
                "queue_p50_sec", "queue_p95_sec", "queue_p99_sec",
                "processing_p50_sec", "processing_p95_sec",
                "processing_p99_sec", "turn_p50_sec", "turn_p95_sec",
                "turn_p99_sec", "server_applied_p50_sec",
                "server_applied_p95_sec", "server_applied_p99_sec",
            )
        },
        "model_breakdown": model_breakdown,
        "failure_reasons": [
            {"code": code, "count": count}
            for code, count in sorted(
                merged_failures.items(), key=lambda item: (-item[1], item[0])
            )[:12]
        ],
        "recent_jobs": recent_jobs,
        "client_delivery_ack": None,
        "provider_attempt_accounting": None,
    }


def recent_token_usage_by_lane(
    *, within_hours: int = 24, offset_hours: int = 0
) -> dict:
    """按 lane 的 token 开销汇总（content-free），喂 admin 值班台。

    ``offset_hours`` 把窗口整体后移成 [now-(offset+within)h, now-offset h)，
    供环比对照列使用；0 时刻意不加上界——保持既有行为（含查询计划）逐字节
    不变。返回结构不区分两种情况。

    与 ``recent_runtime_health`` 的延迟分位数口径**相反**：那里只算成功回合
    （失败超时会把 p95 拉到与故障同源的高位），这里算全部回合——失败回合照样
    烧 token，provider 已经算过钱了。

    刻意不加 ``LIMIT``：sum 聚合加采样上界会静默少报总量（"最新 N 条的 token
    和"不是任何人想要的数字）——这个决策本身仍然正确，但下面这句话曾经写的
    依据是错的，且方向反了：本查询**没有** ``WHERE lane = ...`` 等值谓词，
    只有 ``created_at >= ...`` 加 ``GROUP BY lane``。
    ``ix_v2_turn_metrics_lane_created_at`` 是 ``(lane, created_at DESC)``，
    ``lane`` 是前缀这件事恰恰意味着它**给不出范围收窄**——PG 16 没有 B-tree
    skip scan（PG 18 才有），前导列缺等值谓词时它最多退化成全索引扫描（720h
    档实测确实会被选中，但扫满 21k+ buffer，见 0071 注释）。本地 PG 16
    （50 万行、5 个 lane）实测：本查询走 Parallel Seq Scan（
    ``Rows Removed by Filter`` 随窗口内行数线性增长，`shared hit` 六千+
    buffer）；而有 ``lane = 'chat'`` 等值谓词的既有
    ``recent_token_usage_summary`` 才真正吃到该索引的 Bitmap Index Scan
    （`shared read=35`）。``v2_turn_metrics`` 是 append-only 表，本查询的扫描
    量因此随表增长单调变大；该 follow-up 已由迁移 0071 落地
    （``ix_v2_turn_metrics_created_at``，单列 ``created_at DESC``）——
    ``recent_runtime_health`` 同期去掉采样上界后，两条查询共用它。

    token 为空一律 ``None`` 而非 ``0``：provider 未回 usage 的调用应当降低
    ``usage_coverage``，而不是被记成零 token 混进总量假装正常。
    """
    safe_hours = max(1, min(int(within_hours), 24 * 366))
    safe_offset = max(0, min(int(offset_hours), 24 * 366))
    if safe_offset:
        window_sql = (
            "WHERE created_at >= now() - make_interval(hours => %s) "
            "AND created_at < now() - make_interval(hours => %s) "
        )
        window_params = (safe_offset + safe_hours, safe_offset)
    else:
        window_sql = "WHERE created_at >= now() - make_interval(hours => %s) "
        window_params = (safe_hours,)

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT lane,GROUPING(lane)::int AS is_total,"
                "  count(*)::bigint AS turns,"
                "  count(DISTINCT user_id) FILTER (WHERE model_calls > 0)::bigint"
                "    AS model_active_users,"
                "  count(DISTINCT (user_id,timezone('Asia/Shanghai',created_at)::date))"
                "    FILTER (WHERE model_calls > 0)::bigint AS active_user_days,"
                "  coalesce(sum(model_calls), 0)::bigint AS model_calls,"
                "  coalesce(sum(retries), 0)::bigint AS retries,"
                "  count(*) FILTER (WHERE failed IS TRUE)::bigint AS failed_turns,"
                "  coalesce(sum(usage_reported_calls), 0)::bigint"
                "    AS usage_reported_calls,"
                "  coalesce(sum(cache_reported_calls), 0)::bigint"
                "    AS cache_reported_calls,"
                "  coalesce(sum(screen_frames_pushed), 0)::bigint"
                "    AS screen_frames_pushed,"
                "  count(*) FILTER (WHERE visible_reply_count > 0)::bigint"
                "    AS visible_reply_turns,"
                "  coalesce(sum(visible_reply_count), 0)::bigint"
                "    AS visible_reply_count,"
                "  sum(prompt_tokens)::bigint AS prompt_tokens,"
                "  sum(completion_tokens)::bigint AS completion_tokens,"
                "  sum(cache_read_tokens)::bigint AS cache_read_tokens,"
                "  sum(cache_miss_tokens)::bigint AS cache_miss_tokens "
                "FROM v2_turn_metrics "
                + window_sql +
                "GROUP BY GROUPING SETS ((lane), ())",
                window_params,
            )
            rows = cur.fetchall()

    def _optional_int(row, key):
        value = row.get(key)
        return int(value) if value is not None else None

    lanes: dict[str, dict] = {}
    total: dict | None = None
    for row in rows:
        model_calls = int(row["model_calls"] or 0)
        usage_calls = int(row["usage_reported_calls"] or 0)
        cache_calls = int(row["cache_reported_calls"] or 0)
        prompt_tokens = _optional_int(row, "prompt_tokens")
        completion_tokens = _optional_int(row, "completion_tokens")
        cache_read = _optional_int(row, "cache_read_tokens")
        cache_miss = _optional_int(row, "cache_miss_tokens")
        # 任一为 None 时分母按 0 算 → ratio 为 None（"不知道"），不是 0.0 或
        # 1.0（"完美命中"/"零命中"）。对齐 users 页既有的 admin/data_track.py
        # 「运营 Telemetry」区块同名指标的算法——Anthropic 只有 cache write、
        # 无 cache read 的回合会产出 cache_read=None, cache_miss=500 这种真实
        # 组合（provider_client.py 已核实）；若用 `or 0` 兜底，这种情况会显示
        # 成 "0.0%"，反过来 cache_read=500, cache_miss=None 会显示成 "100.0%"
        # （假装缓存完美命中，而真相是 miss 根本没上报）。两页必须可对账，
        # 否则页顶"窗口不同"的免责声明会被拿来误导运维把真实的算法差异当成
        # 窗口差异。
        cache_denominator = (
            cache_read + cache_miss
            if cache_read is not None and cache_miss is not None
            else 0
        )
        rendered = {
            "turns": int(row.get("turns") or 0),
            "model_active_users": int(row.get("model_active_users") or 0),
            "active_user_days": int(row.get("active_user_days") or 0),
            "model_calls": model_calls,
            "retries": int(row.get("retries") or 0),
            "failed_turns": int(row.get("failed_turns") or 0),
            "usage_reported_calls": usage_calls,
            # cache coverage 与 usage coverage 是两个不同的东西：前者是"有多少次
            # 调用报了缓存指标"，后者是"有多少次调用报了 token usage"。页面此前把
            # cache_hit_ratio 与 usage_coverage 挤在一列、标签写「缓存命中 · 上报」，
            # 读者会把那个"上报"当成 cache 上报（2026-07-30 审计指出）。
            "cache_reported_calls": cache_calls,
            "screen_frames_pushed": int(row.get("screen_frames_pushed") or 0),
            "visible_reply_turns": int(row.get("visible_reply_turns") or 0),
            "visible_reply_count": int(row.get("visible_reply_count") or 0),
            "cache_coverage": (
                float(cache_calls) / float(model_calls) if model_calls else None
            ),
            "usage_coverage": (
                float(usage_calls) / float(model_calls) if model_calls else None
            ),
            "prompt_tokens": prompt_tokens,
            # Admin calls this column "token input". Keep the normalized
            # provider name too, but expose the explicit alias so downstream
            # lane telemetry does not have to guess that prompt == input.
            "input_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": (
                prompt_tokens + completion_tokens
                if prompt_tokens is not None and completion_tokens is not None
                else None
            ),
            "cache_read_tokens": cache_read,
            "cache_miss_tokens": cache_miss,
            "cache_hit_ratio": (
                float(cache_read or 0) / float(cache_denominator)
                if cache_denominator
                else None
            ),
        }
        rendered["tokens_per_active_user_day"] = (
            float(rendered["total_tokens"]) / float(rendered["active_user_days"])
            if rendered["total_tokens"] is not None
            and rendered["active_user_days"]
            else None
        )
        rendered["visible_reply_rate"] = (
            float(rendered["visible_reply_turns"]) / float(rendered["turns"])
            if rendered["turns"]
            else None
        )

        if int(row.get("is_total") or 0) == 1:
            total = rendered
        else:
            lanes[str(row["lane"] or "unknown")] = rendered

    return {
        "window_hours": safe_hours,
        "active_user_day_timezone": "Asia/Shanghai",
        "lanes": lanes,
        "total": total,
    }


_RUNTIME_USER_EFFECT_WINDOW_SQL = """
WITH cutoff AS (
  SELECT now() - make_interval(hours => %s) AS ts
)
SELECT
  e.user_id,
  count(*) FILTER (
    WHERE e.status IN ('applied', 'applied_with_results')
  )::int AS all_applied_in_window,
  count(*) FILTER (
    WHERE e.status = 'discarded'
  )::int AS all_discarded_in_window,
  count(*) FILTER (
    WHERE e.effect_type IN (
      'reply',
      'reply_final_fenced_v1',
      'reply_terminal_fenced_v1',
      'reply_intermediate_fenced_v1'
    )
      AND e.status IN ('applied', 'applied_with_results')
  )::int AS reply_applied_in_window,
  count(*) FILTER (
    WHERE e.effect_type = 'status'
      AND e.status IN ('applied', 'applied_with_results')
  )::int AS status_applied_in_window
FROM v2_effect_outbox e
CROSS JOIN cutoff
WHERE e.created_at >= cutoff.ts
  AND e.status IN ('applied', 'applied_with_results', 'discarded')
GROUP BY e.user_id
"""


_RUNTIME_USER_EFFECT_BACKLOG_SQL = """
SELECT
  e.user_id,
  count(*) FILTER (
    WHERE e.status IN ('pending', 'pending_fenced_v1')
  )::int AS all_pending,
  count(*) FILTER (
    WHERE e.status = 'needs_reconciliation'
  )::int AS all_needs_reconciliation,
  count(*) FILTER (
    WHERE e.effect_type IN (
      'reply',
      'reply_final_fenced_v1',
      'reply_terminal_fenced_v1',
      'reply_intermediate_fenced_v1'
    )
      AND e.status IN ('pending', 'pending_fenced_v1')
  )::int AS reply_pending,
  count(*) FILTER (
    WHERE e.effect_type IN (
      'reply',
      'reply_final_fenced_v1',
      'reply_terminal_fenced_v1',
      'reply_intermediate_fenced_v1'
    )
      AND e.status = 'needs_reconciliation'
  )::int AS reply_needs_reconciliation,
  count(*) FILTER (
    WHERE e.effect_type = 'status' AND e.status = 'pending'
  )::int AS status_pending,
  count(*) FILTER (
    WHERE e.effect_type = 'status'
      AND e.status = 'needs_reconciliation'
  )::int AS status_needs_reconciliation,
  extract(epoch FROM (clock_timestamp() - min(e.created_at)))
    AS oldest_unfinished_age_sec
FROM v2_effect_outbox e
WHERE e.status IN (
  'pending',
  'pending_fenced_v1',
  'needs_reconciliation'
)
GROUP BY e.user_id
"""


_RUNTIME_USER_TERMINAL_WINDOW_SQL = """
WITH cutoff AS (
  SELECT now() - make_interval(hours => %s) AS ts
)
SELECT
  f.user_id,
  count(*) FILTER (
    WHERE f.reply_delivered_at IS NOT NULL
  )::int AS reply_delivered_in_window,
  count(*) FILTER (
    WHERE f.status_delivered_at IS NOT NULL
  )::int AS status_delivered_in_window,
  count(*) FILTER (
    WHERE f.runtime_error_delivered_at IS NOT NULL
  )::int AS runtime_error_delivered_in_window
FROM v2_terminal_failure_outbox f
CROSS JOIN cutoff
WHERE f.created_at >= cutoff.ts
  AND (
    f.reply_delivered_at IS NOT NULL
    OR f.status_delivered_at IS NOT NULL
    OR f.runtime_error_delivered_at IS NOT NULL
  )
GROUP BY f.user_id
"""


_RUNTIME_USER_TERMINAL_BACKLOG_SQL = """
SELECT
  f.user_id,
  count(*) FILTER (
    WHERE f.reply_delivered_at IS NULL
  )::int AS reply_undelivered,
  count(*) FILTER (
    WHERE f.status_delivered_at IS NULL
  )::int AS status_undelivered,
  count(*) FILTER (
    WHERE f.runtime_error_delivered_at IS NULL
  )::int AS runtime_error_undelivered,
  extract(epoch FROM (clock_timestamp() - min(f.created_at)))
    AS oldest_unfinished_age_sec
FROM v2_terminal_failure_outbox f
WHERE f.reply_delivered_at IS NULL
   OR f.status_delivered_at IS NULL
   OR f.runtime_error_delivered_at IS NULL
GROUP BY f.user_id
"""


def _usage_optional_int(row, key: str) -> int | None:
    value = row.get(key)
    return None if value is None else int(value)


def _usage_optional_float(row, key: str) -> float | None:
    value = row.get(key)
    return None if value is None else float(value)


def _usage_rate(numerator: int, denominator: int) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


def _usage_optional_breakdown(
    conn,
    cur,
    name: str,
    statement: str,
    params: tuple[object, ...],
    *,
    fetch: str,
):
    """Run one optional report section behind a savepoint in the outer snapshot."""
    try:
        # psycopg nests transaction contexts as SAVEPOINTs.  The caller already
        # owns the single REPEATABLE READ, READ ONLY transaction, so this keeps
        # one snapshot while allowing one broken breakdown to roll back locally.
        with conn.transaction():
            cur.execute(statement, params)
            return cur.fetchone() if fetch == "one" else cur.fetchall()
    except Exception:
        log.exception("usage report %s breakdown unavailable", name)
        return None


def _usage_pool_connection():
    """Short checkout for optional Admin analytics, with proxy compatibility."""

    pool = _pool()
    try:
        return pool.connection(timeout=_USAGE_REPORT_POOL_TIMEOUT_SECONDS)
    except TypeError:
        # Lightweight test doubles and older pool facades may not expose the
        # timeout keyword; production psycopg_pool does.
        return pool.connection()


_USAGE_FACT_TOKENS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_miss_tokens",
)


def _usage_rollup_day_predicate(days) -> tuple[str, list[object]]:
    """Compress exact local days into indexable half-open contiguous ranges."""

    ordered = sorted(set(days))
    if not ordered:
        return "FALSE", []
    ranges = []
    start = previous = ordered[0]
    for current in ordered[1:]:
        if current != previous + timedelta(days=1):
            ranges.append((start, previous + timedelta(days=1)))
            start = current
        previous = current
    ranges.append((start, previous + timedelta(days=1)))
    clauses = ["(local_day >= %s AND local_day < %s)" for _ in ranges]
    params = [bound for pair in ranges for bound in pair]
    return "(" + " OR ".join(clauses) + ")", params


def _usage_fact_query(
    query,
    partition: usage_reporting.RollupPartition,
    *,
    dimensions: bool,
    prefix: str | None = None,
    include_dimension_filters: bool = True,
    include_latency: bool = True,
) -> tuple[str, tuple[object, ...]]:
    """Build a disjoint rollup/full-day + raw/edge fact relation."""

    selected = prefix or query.completeness
    if selected not in {"all", "metered", "unknown"}:
        raise ValueError("unsupported usage completeness")
    table = (
        "v2_usage_daily_dimensions"
        if dimensions
        else "v2_usage_daily_users"
    )
    identity_columns = "lane,provider,model," if dimensions else ""
    identity_group = (
        ",coalesce(nullif(m.lane,''),'unknown'),"
        "coalesce(nullif(m.provider,''),'unknown'),"
        "coalesce(nullif(m.model,''),'unknown')"
        if dimensions
        else ""
    )
    identity_select = (
        "coalesce(nullif(m.lane,''),'unknown') AS lane,"
        "coalesce(nullif(m.provider,''),'unknown') AS provider,"
        "coalesce(nullif(m.model,''),'unknown') AS model,"
        if dimensions
        else ""
    )
    selected_condition = {
        "all": "TRUE",
        "metered": "m.usage_reported_calls > 0",
        "unknown": "m.usage_reported_calls < m.model_calls",
    }[selected]
    metered_prefix = "metered" if selected in {"all", "metered"} else None
    metric_columns = (
        "turns",
        "model_calls",
        "retries",
        "failed_turns",
        "usage_reported_calls",
        "cache_reported_calls",
        "unknown_usage_calls",
    )
    rollup_metrics = ",".join(
        f"{selected}_{field} AS {field}" for field in metric_columns
    )
    rollup_tokens = ",".join(
        f"{selected}_{field}_sum AS {field}_sum,"
        f"{selected}_{field}_known_count AS {field}_known_count"
        for field in _USAGE_FACT_TOKENS
    )
    if metered_prefix is None:
        rollup_metered = ",".join(
            f"NULL::bigint AS metered_{field}"
            for field in ("turns", "prompt_tokens_sum", "prompt_tokens_known_count", "completion_tokens_sum", "completion_tokens_known_count")
        )
    else:
        rollup_metered = ",".join(
            f"{metered_prefix}_{field} AS metered_{field}"
            for field in ("turns", "prompt_tokens_sum", "prompt_tokens_known_count", "completion_tokens_sum", "completion_tokens_known_count")
        )
    rollup_latency = (
        f",{selected}_latency_samples AS latency_samples" if dimensions else ""
    )
    if not include_latency:
        rollup_latency = ""
    user_clauses: list[str] = []
    rollup_day_where, rollup_params = _usage_rollup_day_predicate(
        partition.rollup_days
    )
    raw_range_clauses: list[str] = []
    raw_params: list[object] = []
    for raw_day in partition.raw_days:
        day_start = datetime.combine(
            raw_day, datetime.min.time(), tzinfo=usage_reporting.SHANGHAI
        ).astimezone(timezone.utc)
        day_end = datetime.combine(
            raw_day + timedelta(days=1),
            datetime.min.time(),
            tzinfo=usage_reporting.SHANGHAI,
        ).astimezone(timezone.utc)
        raw_range_clauses.append(
            "(m.created_at >= %s AND m.created_at < %s)"
        )
        raw_params.extend(
            (
                max(day_start, query.start_at_utc),
                min(day_end, query.end_at_utc),
            )
        )
    if query.user_id:
        user_clauses.append("coalesce(user_id,'unknown')=%s")
        rollup_params.append(query.user_id)
    rollup_dimension_clauses: list[str] = []
    if dimensions and include_dimension_filters:
        for field in ("lane", "provider", "model"):
            value = getattr(query, field)
            if value:
                rollup_dimension_clauses.append(f"{field}=%s")
                rollup_params.append(value)
    rollup_where = [rollup_day_where, *user_clauses, *rollup_dimension_clauses]
    if selected != "all":
        rollup_where.append(f"{selected}_turns > 0")
    rollup_statement = f"""
SELECT local_day,coalesce(user_id,'unknown') AS user_id,{identity_columns}
  first_metric_at,last_metric_at,last_model_call_at,
  {rollup_metrics},{rollup_tokens},{rollup_metered}{rollup_latency}
FROM {table}
WHERE {' AND '.join(rollup_where)}
"""
    if not partition.raw_days:
        # Empty parameter arrays do not guarantee that PostgreSQL avoids the
        # other side of the UNION: filtered plans have chosen a hash join that
        # scanned all of v2_turn_metrics before observing zero raw ranges.
        # A complete rollup window has no authoritative-raw contribution, so
        # omit that relation structurally rather than relying on join order.
        return rollup_statement, tuple(rollup_params)

    raw_where = [
        selected_condition,
        "(" + " OR ".join(raw_range_clauses) + ")",
    ]
    if query.user_id:
        raw_where.append("coalesce(m.user_id,'unknown')=%s")
        raw_params.append(query.user_id)
    if dimensions and include_dimension_filters:
        for field in ("lane", "provider", "model"):
            value = getattr(query, field)
            if value:
                raw_where.append(
                    f"coalesce(nullif(m.{field},''),'unknown')=%s"
                )
                raw_params.append(value)

    raw_metrics = (
        "count(*)::bigint AS turns,"
        "coalesce(sum(m.model_calls),0)::bigint AS model_calls,"
        "coalesce(sum(m.retries),0)::bigint AS retries,"
        "count(*) FILTER (WHERE m.failed)::bigint AS failed_turns,"
        "coalesce(sum(m.usage_reported_calls),0)::bigint AS usage_reported_calls,"
        "coalesce(sum(m.cache_reported_calls),0)::bigint AS cache_reported_calls,"
        "coalesce(sum(greatest(m.model_calls-m.usage_reported_calls,0)),0)::bigint AS unknown_usage_calls"
    )
    raw_tokens = ",".join(
        f"coalesce(sum(m.{field}),0)::bigint AS {field}_sum,"
        f"count(m.{field})::bigint AS {field}_known_count"
        for field in _USAGE_FACT_TOKENS
    )
    raw_metered = (
        "count(*) FILTER (WHERE m.usage_reported_calls>0)::bigint AS metered_turns,"
        "coalesce(sum(m.prompt_tokens) FILTER (WHERE m.usage_reported_calls>0),0)::bigint AS metered_prompt_tokens_sum,"
        "count(m.prompt_tokens) FILTER (WHERE m.usage_reported_calls>0)::bigint AS metered_prompt_tokens_known_count,"
        "coalesce(sum(m.completion_tokens) FILTER (WHERE m.usage_reported_calls>0),0)::bigint AS metered_completion_tokens_sum,"
        "count(m.completion_tokens) FILTER (WHERE m.usage_reported_calls>0)::bigint AS metered_completion_tokens_known_count"
    )
    raw_latency = (
        ",coalesce(array_agg(m.latency_ms ORDER BY m.latency_ms,m.id) "
        "FILTER (WHERE m.latency_ms IS NOT NULL),'{}'::integer[]) AS latency_samples"
        if dimensions and include_latency
        else ""
    )
    statement = f"""
WITH facts AS (
{rollup_statement}
UNION ALL
SELECT (m.created_at AT TIME ZONE 'Asia/Shanghai')::date AS local_day,
  coalesce(m.user_id,'unknown') AS user_id,{identity_select}
  min(m.created_at) AS first_metric_at,max(m.created_at) AS last_metric_at,
  max(m.created_at) FILTER (WHERE m.model_calls>0) AS last_model_call_at,
  {raw_metrics},{raw_tokens},{raw_metered}{raw_latency}
FROM v2_turn_metrics m
WHERE {' AND '.join(raw_where)}
GROUP BY (m.created_at AT TIME ZONE 'Asia/Shanghai')::date,m.user_id{identity_group}
)
SELECT * FROM facts
"""
    return statement, tuple(rollup_params + raw_params)


def _usage_known_sum(field: str, alias: str = "s") -> str:
    if field not in _USAGE_FACT_TOKENS:
        raise ValueError("unsupported usage token field")
    return (
        f"CASE WHEN sum({alias}.{field}_known_count)>0 "
        f"THEN sum({alias}.{field}_sum)::bigint END"
    )


def _usage_fact_aggregate_columns(alias: str = "s") -> str:
    tokens = ",".join(
        f"{_usage_known_sum(field, alias)} AS {field}"
        for field in _USAGE_FACT_TOKENS
    )
    return (
        f"coalesce(sum({alias}.turns),0)::bigint AS turns,"
        f"coalesce(sum({alias}.model_calls),0)::bigint AS model_calls,"
        f"coalesce(sum({alias}.retries),0)::bigint AS retries,"
        f"coalesce(sum({alias}.failed_turns),0)::bigint AS failed_turns,"
        f"coalesce(sum({alias}.usage_reported_calls),0)::bigint AS usage_reported_calls,"
        f"coalesce(sum({alias}.cache_reported_calls),0)::bigint AS cache_reported_calls,"
        f"coalesce(sum({alias}.unknown_usage_calls),0)::bigint AS unknown_usage_calls,"
        f"coalesce(sum({alias}.metered_turns),0)::bigint AS metered_turns,"
        f"CASE WHEN sum({alias}.metered_prompt_tokens_known_count)>0 "
        f"THEN sum({alias}.metered_prompt_tokens_sum)::bigint END AS metered_prompt_tokens,"
        f"CASE WHEN sum({alias}.metered_completion_tokens_known_count)>0 "
        f"THEN sum({alias}.metered_completion_tokens_sum)::bigint END AS metered_completion_tokens,"
        f"{tokens}"
    )


def _usage_fact_reduce_columns(alias: str = "s") -> str:
    counters = (
        "turns", "model_calls", "retries", "failed_turns",
        "usage_reported_calls", "cache_reported_calls", "unknown_usage_calls",
        "metered_turns", "metered_prompt_tokens_sum",
        "metered_prompt_tokens_known_count", "metered_completion_tokens_sum",
        "metered_completion_tokens_known_count",
    )
    values = [f"coalesce(sum({alias}.{field}),0)::bigint AS {field}" for field in counters]
    for field in _USAGE_FACT_TOKENS:
        values.extend(
            (
                f"coalesce(sum({alias}.{field}_sum),0)::bigint AS {field}_sum",
                f"coalesce(sum({alias}.{field}_known_count),0)::bigint AS {field}_known_count",
            )
        )
    return ",".join(values)


def _usage_rendered_aggregate_columns(alias: str = "u") -> str:
    counters = (
        "turns", "model_calls", "retries", "failed_turns",
        "usage_reported_calls", "cache_reported_calls", "unknown_usage_calls",
        "metered_turns",
    )
    values = [f"coalesce(sum({alias}.{field}),0)::bigint AS {field}" for field in counters]
    for field in (*_USAGE_FACT_TOKENS, "metered_prompt_tokens", "metered_completion_tokens"):
        values.append(
            f"CASE WHEN count({alias}.{field})>0 THEN sum({alias}.{field})::bigint END AS {field}"
        )
    return ",".join(values)


def _usage_known_total_order(alias: str = "s") -> str:
    """Match raw ``sum(prompt)+sum(completion) NULLS LAST`` semantics."""

    return (
        f"CASE WHEN sum({alias}.prompt_tokens_known_count)>0 "
        f"AND sum({alias}.completion_tokens_known_count)>0 "
        f"THEN sum({alias}.prompt_tokens_sum)+sum({alias}.completion_tokens_sum) END"
    )


def _usage_optional_pg_section(cur, name: str, reader):
    """Rollback one optional aggregate without poisoning the shared snapshot."""

    try:
        with cur.connection.transaction():
            return reader()
    except Exception:
        log.exception("usage %s section unavailable", name)
        return None


def _usage_parallel_core_rows_separate(
    cur, query, partition, *, unknown_auxiliary=True
) -> dict:
    """Aggregate user/day facts inside PostgreSQL; never fetch canonical rows."""

    dimensions = usage_reporting.has_dimension_filter(query)
    source_sql, params = _usage_fact_query(
        query, partition, dimensions=dimensions
    )
    prefix = f"WITH source AS ({source_sql})"
    aggregates = _usage_fact_aggregate_columns()
    _usage_snapshot_observer("read", role="exporter", section="core")
    cur.execute(
        prefix
        + f""",
token_user_totals AS (
 SELECT user_id,
  (sum(prompt_tokens_sum)+sum(completion_tokens_sum))::bigint
    AS total_tokens
 FROM source
 GROUP BY user_id
 HAVING sum(prompt_tokens_known_count)>0
    AND sum(completion_tokens_known_count)>0
)
SELECT {aggregates},
 count(DISTINCT user_id) FILTER (WHERE model_calls>0)::int AS model_active_users,
 (SELECT count(*)::int FROM token_user_totals) AS token_users,
 (SELECT coalesce(sum(total_tokens),0)::bigint FROM token_user_totals)
   AS token_user_tokens,
 count(DISTINCT user_id) FILTER (WHERE usage_reported_calls>0)::int AS metered_users,
 count(DISTINCT (user_id,local_day)) FILTER (WHERE model_calls>0)::int AS active_user_days
FROM source s
""",
        params,
    )
    totals = cur.fetchone()
    distribution_sql = prefix + f""",
user_days AS (
 SELECT user_id,local_day,{_usage_known_sum('prompt_tokens')} AS prompt_tokens,
 {_usage_known_sum('completion_tokens')} AS completion_tokens
 FROM source s WHERE model_calls>0 GROUP BY user_id,local_day
), known AS (
 SELECT (prompt_tokens+completion_tokens)::numeric AS total_tokens FROM user_days
 WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
)
SELECT percentile_cont(.5) WITHIN GROUP (ORDER BY total_tokens) AS p50,
 percentile_cont(.75) WITHIN GROUP (ORDER BY total_tokens) AS p75,
 percentile_cont(.9) WITHIN GROUP (ORDER BY total_tokens) AS p90,
 percentile_cont(.95) WITHIN GROUP (ORDER BY total_tokens) AS p95,
 max(total_tokens) AS max FROM known
"""
    distribution = _usage_optional_pg_section(
        cur,
        "distribution",
        lambda: (cur.execute(distribution_sql, params), cur.fetchone())[1],
    )
    if dimensions:
        token_user_days = f""",
token_user_days AS (
 SELECT local_day,user_id,
  (sum(prompt_tokens_sum)+sum(completion_tokens_sum))::bigint
    AS total_tokens
 FROM source
 GROUP BY local_day,user_id
 HAVING sum(prompt_tokens_known_count)>0
    AND sum(completion_tokens_known_count)>0
), token_daily AS (
 SELECT local_day,count(*)::int AS token_users,
  coalesce(sum(total_tokens),0)::bigint AS token_user_tokens
 FROM token_user_days GROUP BY local_day
), daily_rows AS (
 SELECT local_day,{aggregates},
  count(DISTINCT user_id) FILTER (WHERE model_calls>0)::int
    AS model_active_users
 FROM source s GROUP BY local_day
)"""
        daily_sql = prefix + token_user_days + """
SELECT d.*,coalesce(t.token_users,0)::int AS token_users,
 coalesce(t.token_user_tokens,0)::bigint AS token_user_tokens
FROM daily_rows d LEFT JOIN token_daily t USING (local_day)
ORDER BY d.local_day
"""
    else:
        # The non-dimensional source is already one row per user/day.  Avoid
        # materializing and rescanning it only to reconstruct the same grain.
        daily_token_columns = """
 count(*) FILTER (
  WHERE prompt_tokens_known_count>0 AND completion_tokens_known_count>0
 )::int AS token_users,
 coalesce(sum(prompt_tokens_sum+completion_tokens_sum) FILTER (
  WHERE prompt_tokens_known_count>0 AND completion_tokens_known_count>0
 ),0)::bigint AS token_user_tokens"""
        daily_sql = prefix + f"""
SELECT local_day,{aggregates},
 count(DISTINCT user_id) FILTER (WHERE model_calls>0)::int AS model_active_users,
{daily_token_columns}
FROM source s GROUP BY local_day ORDER BY local_day
"""
    daily = _usage_optional_pg_section(
        cur,
        "daily",
        lambda: (cur.execute(daily_sql, params), cur.fetchall())[1],
    )
    users_sql = prefix + f""",
user_days AS (
 SELECT user_id,local_day,{_usage_known_sum('prompt_tokens')} AS prompt_tokens,
 {_usage_known_sum('completion_tokens')} AS completion_tokens
 FROM source s WHERE model_calls>0 GROUP BY user_id,local_day
), dist AS (
 SELECT user_id,
  percentile_cont(.5) WITHIN GROUP (ORDER BY prompt_tokens+completion_tokens)
    FILTER (WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL) AS daily_p50,
  percentile_cont(.95) WITHIN GROUP (ORDER BY prompt_tokens+completion_tokens)
    FILTER (WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL) AS daily_p95
 FROM user_days GROUP BY user_id
)
SELECT s.user_id,{aggregates},
 count(DISTINCT s.local_day) FILTER (WHERE s.model_calls>0)::int AS active_days,
 max(s.last_model_call_at) AS last_model_call_at,
 max(dist.daily_p50) AS daily_p50,max(dist.daily_p95) AS daily_p95
FROM source s LEFT JOIN dist USING (user_id) GROUP BY s.user_id
ORDER BY {_usage_known_total_order()} DESC NULLS LAST,
 sum(s.model_calls) DESC,s.user_id
"""
    users = _usage_optional_pg_section(
        cur,
        "users",
        lambda: (cur.execute(users_sql, params), cur.fetchall())[1],
    )
    result = {
        "totals": totals,
        "distribution": distribution,
        "daily": daily,
        "users": users,
    }
    if query.completeness == "unknown" and unknown_auxiliary:
        all_days = tuple(sorted(partition.rollup_days + partition.raw_days))
        auxiliary = _usage_parallel_core_rows_separate(
            cur,
            query,
            usage_reporting.RollupPartition(rollup_days=(), raw_days=all_days),
            unknown_auxiliary=False,
        )
        fields = (
            "metered_turns",
            "metered_prompt_tokens",
            "metered_completion_tokens",
        )
        for field in fields:
            result["totals"][field] = auxiliary["totals"][field]
        for section, keys in (
            ("daily", ("local_day",)),
            ("users", ("user_id",)),
        ):
            if result[section] is None or auxiliary[section] is None:
                continue
            by_key = {
                tuple(row[key] for key in keys): row
                for row in auxiliary[section]
            }
            for row in result[section]:
                source = by_key.get(tuple(row[key] for key in keys)) or {}
                for field in fields:
                    row[field] = source.get(field)
    return result


def _usage_parallel_core_rows(cur, query, partition, *, unknown_auxiliary=True) -> dict:
    """Read the common core from one materialized fact scan, with safe fallback."""

    if query.completeness == "unknown":
        return _usage_parallel_core_rows_separate(
            cur, query, partition, unknown_auxiliary=unknown_auxiliary
        )
    dimensions = usage_reporting.has_dimension_filter(query)
    source_sql, params = _usage_fact_query(query, partition, dimensions=dimensions)
    aggregates = _usage_fact_aggregate_columns("d")
    reduced = _usage_fact_reduce_columns()
    rendered_aggregates = _usage_rendered_aggregate_columns()
    try:
        with cur.connection.transaction():
            _usage_snapshot_observer("read", role="exporter", section="core_bundle")
            cur.execute(
                f"""
WITH source AS ({source_sql}),
user_days AS MATERIALIZED (
 SELECT user_id,local_day,min(first_metric_at) AS first_metric_at,
  max(last_metric_at) AS last_metric_at,max(last_model_call_at) AS last_model_call_at,
  {reduced}
 FROM source s GROUP BY user_id,local_day
),
user_rows AS MATERIALIZED (
 SELECT d.user_id,{aggregates},
  count(DISTINCT d.local_day) FILTER (WHERE d.model_calls>0)::int AS active_days,
  max(d.last_model_call_at) AS last_model_call_at,
  percentile_cont(.5) WITHIN GROUP (ORDER BY d.prompt_tokens_sum+d.completion_tokens_sum)
   FILTER (WHERE d.model_calls>0 AND d.prompt_tokens_known_count>0 AND d.completion_tokens_known_count>0) AS daily_p50,
  percentile_cont(.95) WITHIN GROUP (ORDER BY d.prompt_tokens_sum+d.completion_tokens_sum)
   FILTER (WHERE d.model_calls>0 AND d.prompt_tokens_known_count>0 AND d.completion_tokens_known_count>0) AS daily_p95,
  {_usage_known_total_order('d')} AS _order_total
 FROM user_days d GROUP BY d.user_id
), totals_row AS (
 SELECT {rendered_aggregates},
 count(*) FILTER (WHERE model_calls>0)::int AS model_active_users,
  count(*) FILTER (
    WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
  )::int AS token_users,
  coalesce(sum(prompt_tokens+completion_tokens) FILTER (
    WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
  ),0)::bigint AS token_user_tokens,
  count(*) FILTER (WHERE usage_reported_calls>0)::int AS metered_users,
  coalesce(sum(active_days),0)::int AS active_user_days
 FROM user_rows u
)
SELECT (SELECT to_jsonb(t) FROM totals_row t) AS totals,
 NULL::jsonb AS distribution,
 NULL::jsonb AS daily,
 (SELECT coalesce(jsonb_agg(to_jsonb(u)-'_order_total'
   ORDER BY u._order_total DESC NULLS LAST,u.model_calls DESC,u.user_id),'[]')
  FROM user_rows u) AS users
""",
                params,
            )
            bundle = cur.fetchone()
        daily = bundle["daily"]
        users = bundle["users"]
        for row in daily or []:
            if isinstance(row.get("local_day"), str):
                row["local_day"] = datetime.fromisoformat(row["local_day"]).date()
        for row in users:
            value = row.get("last_model_call_at")
            if isinstance(value, str):
                row["last_model_call_at"] = datetime.fromisoformat(value)
        return {
            "totals": bundle["totals"],
            "distribution": bundle["distribution"],
            "daily": daily,
            "users": users,
        }
    except psycopg.errors.QueryCanceled:
        # The shared report deadline is exhausted.  Four more isolated reads
        # would multiply timeout load and delay admission release.
        raise
    except Exception:
        log.exception("usage core bundle unavailable; trying isolated sections")
        return _usage_parallel_core_rows_separate(
            cur, query, partition, unknown_auxiliary=unknown_auxiliary
        )


def _usage_parallel_grouped_dimension_rows(cur, query, partition, fields: str):
    source_sql, params = _usage_fact_query(query, partition, dimensions=True)
    prefix = f"WITH source AS ({source_sql})"
    aggregates = _usage_fact_aggregate_columns()
    cur.execute(
        prefix
        + f"""
SELECT {fields},{aggregates},count(DISTINCT user_id)::int AS users
FROM source s GROUP BY {fields}
ORDER BY {_usage_known_total_order()} DESC NULLS LAST,
 sum(s.model_calls) DESC,{fields}
""",
        params,
    )
    return cur.fetchall()


def _usage_parallel_model_rows(cur, query, partition):
    return _usage_parallel_grouped_dimension_rows(
        cur, query, partition, "provider,model"
    )


def _usage_parallel_lane_rows(cur, query, partition):
    return _usage_parallel_grouped_dimension_rows(cur, query, partition, "lane")


def _usage_parallel_primary_rows(cur, query, partition):
    source_sql, params = _usage_fact_query(query, partition, dimensions=True)
    prefix = f"WITH source AS ({source_sql})"
    cur.execute(
        prefix
        + """,
ranked AS (
 SELECT user_id,provider,model,
 row_number() OVER (PARTITION BY user_id
 ORDER BY sum(model_calls) DESC,provider,model) AS rank
 FROM source GROUP BY user_id,provider,model
)
SELECT user_id,provider,model FROM ranked WHERE rank=1
""",
        params,
    )
    return cur.fetchall()


def _usage_parallel_dimension_rows(
    cur,
    query,
    partition,
    *,
    unknown_auxiliary=True,
    auxiliary_only=False,
) -> dict:
    """Run fixed-bin A, plus only the raw patches needed by unknown."""

    _usage_snapshot_observer("read", role="dimension", section="breakdowns")

    models = _usage_optional_pg_section(
        cur, "models", lambda: _usage_parallel_model_rows(cur, query, partition)
    )
    lanes = (
        _usage_optional_pg_section(
            cur, "lanes", lambda: _usage_parallel_lane_rows(cur, query, partition)
        )
        if query.completeness == "unknown" or auxiliary_only
        else None
    )
    primary = (
        None
        if auxiliary_only
        else _usage_optional_pg_section(
            cur,
            "primary",
            lambda: _usage_parallel_primary_rows(cur, query, partition),
        )
    )
    filters = (
        None
        if auxiliary_only
        else _usage_optional_pg_section(
            cur,
            "filters",
            lambda: _usage_parallel_option_rows(cur, query, partition),
        )
    )
    distribution = (
        _usage_optional_pg_section(
            cur,
            "distribution",
            lambda: _usage_parallel_distribution_row(cur, query, partition),
        )
        if query.completeness != "unknown" and not auxiliary_only
        else None
    )
    result = {
        "models": models,
        "lanes": lanes,
        "primary": primary,
        "filters": filters,
        "distribution": distribution,
    }
    if query.completeness == "unknown" and unknown_auxiliary:
        all_days = tuple(sorted(partition.rollup_days + partition.raw_days))
        auxiliary = _usage_parallel_dimension_rows(
            cur,
            query,
            usage_reporting.RollupPartition(rollup_days=(), raw_days=all_days),
            unknown_auxiliary=False,
            auxiliary_only=True,
        )
        fields = (
            "metered_turns",
            "metered_prompt_tokens",
            "metered_completion_tokens",
        )
        for section, keys in (
            ("models", ("provider", "model")),
            ("lanes", ("lane",)),
        ):
            if result[section] is None or auxiliary[section] is None:
                continue
            by_key = {
                tuple(row[key] for key in keys): row
                for row in auxiliary[section]
            }
            for row in result[section]:
                source = by_key.get(tuple(row[key] for key in keys)) or {}
                for field in fields:
                    row[field] = source.get(field)
    return result


def _usage_parallel_option_rows(cur, query, partition) -> dict:
    source_sql, params = _usage_fact_query(
        query,
        partition,
        dimensions=True,
        prefix="all",
        include_dimension_filters=False,
    )
    _usage_snapshot_observer("read", role="latency", section="filters")
    cur.execute(
        f"WITH source AS ({source_sql}) "
        "SELECT array_agg(DISTINCT lane ORDER BY lane) AS lanes,"
        "array_agg(DISTINCT provider ORDER BY provider) AS providers,"
        "array_agg(DISTINCT model ORDER BY model) AS models FROM source",
        params,
    )
    return cur.fetchone()


def _usage_parallel_grouped_latency_rows(cur, query, partition, fields: str):
    source_sql, params = _usage_fact_query(query, partition, dimensions=True)
    prefix = f"WITH source AS ({source_sql}), samples AS ("
    prefix += (
        "SELECT s.provider,s.model,s.lane,value::double precision AS latency_ms "
        "FROM source s CROSS JOIN LATERAL unnest(s.latency_samples) value)"
    )
    cur.execute(
        prefix
        + f" SELECT {fields},"
        "percentile_cont(.5) WITHIN GROUP (ORDER BY latency_ms) AS latency_ms_p50,"
        "percentile_cont(.95) WITHIN GROUP (ORDER BY latency_ms) AS latency_ms_p95 "
        f"FROM samples GROUP BY {fields}",
        params,
    )
    return cur.fetchall()


def _usage_parallel_latency_model_rows(cur, query, partition):
    return _usage_parallel_grouped_latency_rows(
        cur, query, partition, "provider,model"
    )


def _usage_parallel_latency_lane_rows(cur, query, partition):
    return _usage_parallel_grouped_latency_rows(cur, query, partition, "lane")


def _usage_parallel_latency_rows(cur, query, partition) -> dict:
    """Compute exact p50/p95 in PostgreSQL; return only grouped percentiles."""

    _usage_snapshot_observer("read", role="latency", section="latency")
    return {
        "models": _usage_parallel_latency_model_rows(cur, query, partition),
        "lanes": _usage_parallel_latency_lane_rows(cur, query, partition),
    }


def _usage_parallel_distribution_row(cur, query, partition) -> dict:
    dimensions = usage_reporting.has_dimension_filter(query)
    source_sql, params = _usage_fact_query(
        query,
        partition,
        dimensions=dimensions,
        include_latency=False,
    )
    cur.execute(
        f"""
WITH source AS ({source_sql}), user_days AS (
 SELECT user_id,local_day,{_usage_known_sum('prompt_tokens')} AS prompt_tokens,
  {_usage_known_sum('completion_tokens')} AS completion_tokens
 FROM source s WHERE model_calls>0 GROUP BY user_id,local_day
)
SELECT percentile_cont(.5) WITHIN GROUP (ORDER BY prompt_tokens+completion_tokens) AS p50,
 percentile_cont(.75) WITHIN GROUP (ORDER BY prompt_tokens+completion_tokens) AS p75,
 percentile_cont(.9) WITHIN GROUP (ORDER BY prompt_tokens+completion_tokens) AS p90,
 percentile_cont(.95) WITHIN GROUP (ORDER BY prompt_tokens+completion_tokens) AS p95,
 max(prompt_tokens+completion_tokens) AS max
FROM user_days WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
""",
        params,
    )
    return cur.fetchone()


def _usage_parallel_daily_rows(cur, query, partition) -> list[dict]:
    dimensions = usage_reporting.has_dimension_filter(query)
    source_sql, params = _usage_fact_query(
        query,
        partition,
        dimensions=dimensions,
        include_latency=False,
    )
    aggregates = _usage_fact_aggregate_columns()
    if dimensions:
        token_user_days = f""", token_user_days AS (
 SELECT local_day,user_id,
  (sum(prompt_tokens_sum)+sum(completion_tokens_sum))::bigint
    AS total_tokens
 FROM source
 GROUP BY local_day,user_id
 HAVING sum(prompt_tokens_known_count)>0
    AND sum(completion_tokens_known_count)>0
), token_daily AS (
 SELECT local_day,count(*)::int AS token_users,
  coalesce(sum(total_tokens),0)::bigint AS token_user_tokens
 FROM token_user_days GROUP BY local_day
), daily_rows AS (
 SELECT local_day,{aggregates},
  count(DISTINCT user_id) FILTER (WHERE model_calls>0)::int
    AS model_active_users
 FROM source s GROUP BY local_day
)"""
        statement = f"""
WITH source AS ({source_sql}){token_user_days}
SELECT d.*,coalesce(t.token_users,0)::int AS token_users,
 coalesce(t.token_user_tokens,0)::bigint AS token_user_tokens
FROM daily_rows d LEFT JOIN token_daily t USING (local_day)
ORDER BY d.local_day
"""
    else:
        # The non-dimensional fact query already emits one row per user/day.
        daily_token_columns = """
 count(*) FILTER (
  WHERE prompt_tokens_known_count>0 AND completion_tokens_known_count>0
 )::int AS token_users,
 coalesce(sum(prompt_tokens_sum+completion_tokens_sum) FILTER (
  WHERE prompt_tokens_known_count>0 AND completion_tokens_known_count>0
 ),0)::bigint AS token_user_tokens"""
        statement = f"""
WITH source AS ({source_sql})
SELECT local_day,{aggregates},
 count(DISTINCT user_id) FILTER (WHERE model_calls>0)::int AS model_active_users,
{daily_token_columns}
FROM source s GROUP BY local_day ORDER BY local_day
"""
    cur.execute(
        statement,
        params,
    )
    return cur.fetchall()


def _usage_parallel_latency_bundle(cur, query, partition) -> dict:
    """Run fixed-bin B; every task rolls back and degrades independently."""

    _usage_snapshot_observer("read", role="latency", section="task_b")
    return {
        "daily": (
            _usage_optional_pg_section(
                cur,
                "daily",
                lambda: _usage_parallel_daily_rows(cur, query, partition),
            )
            if query.completeness != "unknown"
            else None
        ),
        "lanes": (
            _usage_optional_pg_section(
                cur,
                "lanes",
                lambda: _usage_parallel_lane_rows(cur, query, partition),
            )
            if query.completeness != "unknown"
            else None
        ),
        "latency_models": _usage_optional_pg_section(
            cur,
            "latency_models",
            lambda: _usage_parallel_latency_model_rows(cur, query, partition),
        ),
    }


class _UsageImporterControl:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None

    def attach(self, conn) -> None:
        with self._lock:
            self._conn = conn

    def detach(self, conn) -> None:
        with self._lock:
            if self._conn is conn:
                self._conn = None

    def cancel(self, *, timeout: float = 0.25) -> None:
        # Keep ownership through cancellation.  ``detach`` runs before the pool
        # context returns this physical session, and must not race ahead while
        # cancel is targeting it.
        with self._lock:
            conn = self._conn
            if conn is not None:
                _usage_snapshot_observer("cancel", role="importer")
                conn.cancel_safe(timeout=timeout)

    def close(self) -> None:
        with self._lock:
            conn = self._conn
            if conn is not None:
                conn.close()


def _usage_remaining_timeout_ms(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("usage report deadline exceeded")
    return max(1, int(remaining * 1000))


class _UsageDeadlineCursor:
    def __init__(self, cursor, deadline: float):
        self._cursor = cursor
        self._deadline = deadline

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, statement, params=None):
        timeout_ms = _usage_remaining_timeout_ms(self._deadline)
        self._cursor.execute(
            "SELECT set_config('statement_timeout',%s,true)",
            (str(timeout_ms),),
        )
        if params is None:
            return self._cursor.execute(statement)
        return self._cursor.execute(statement, params)


class _UsageImporterUnsettled(RuntimeError):
    pass


def _usage_cancel_and_settle(future, control: _UsageImporterControl) -> None:
    future.cancel()
    try:
        control.cancel(timeout=0.25)
    except Exception:
        pass
    try:
        future.result(timeout=1.0)
    except FutureTimeoutError:
        if future.done():
            return
        control.close()
        try:
            future.result(timeout=0.5)
        except FutureTimeoutError as exc:
            if future.done():
                return
            raise _UsageImporterUnsettled(
                "usage importer did not settle after bounded cancel/close"
            ) from exc
        except Exception:
            pass
    except Exception:
        pass


def _usage_importer_result(future, control, deadline: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _usage_cancel_and_settle(future, control)
        raise TimeoutError("usage importer total deadline exceeded")
    try:
        return future.result(timeout=remaining)
    except FutureTimeoutError:
        if future.done():
            return future.result()
        _usage_cancel_and_settle(future, control)
        raise TimeoutError("usage importer total deadline exceeded")


class _UsageImporterExecutor:
    """Never perform an unbounded ``shutdown(wait=True)`` on stuck importers."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="usage-report"
        )
        self._owned = []

    def __enter__(self):
        return self

    def submit(self, control, fn, *args):
        future = self._executor.submit(fn, *args)
        self._owned.append((future, control))
        return future

    def __exit__(self, exc_type, _exc, _tb):
        unsettled = None
        for future, control in self._owned:
            if future.done():
                continue
            try:
                _usage_cancel_and_settle(future, control)
            except _UsageImporterUnsettled as error:
                unsettled = unsettled or error
        self._executor.shutdown(
            wait=unsettled is None,
            cancel_futures=True,
        )
        if unsettled is not None and exc_type is None:
            raise unsettled
        return False


def _usage_import_snapshot(
    snapshot_id: str,
    control: _UsageImporterControl,
    deadline: float,
    reader,
    *args,
):
    """Import before every data read and isolate rollback/connection failure."""

    pool = _pool()
    with pool.connection(timeout=_USAGE_REPORT_POOL_TIMEOUT_SECONDS) as conn:
        control.attach(conn)
        try:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    cur.execute(
                        sql.SQL("SET TRANSACTION SNAPSHOT {}").format(
                            sql.Literal(snapshot_id)
                        )
                    )
                    _usage_snapshot_observer(
                        "imported", role=getattr(reader, "__name__", "reader")
                    )
                    return reader(_UsageDeadlineCursor(cur, deadline), *args)
        finally:
            control.detach(conn)


def _usage_payload_from_parallel_rows(
    query, cohort, core, dimensions, latency_bundle, state, partition
) -> dict:
    totals = core["totals"]
    duration_days = (query.end_at_utc - query.start_at_utc).total_seconds() / 86400

    def render(row) -> dict:
        prompt = _usage_optional_int(row, "prompt_tokens")
        completion = _usage_optional_int(row, "completion_tokens")
        total = prompt + completion if prompt is not None and completion is not None else None
        calls = int(row.get("model_calls") or 0)
        usage_calls = int(row.get("usage_reported_calls") or 0)
        cache_calls = int(row.get("cache_reported_calls") or 0)
        cache_read = _usage_optional_int(row, "cache_read_tokens")
        cache_miss = _usage_optional_int(row, "cache_miss_tokens")
        cache_denominator = cache_read + cache_miss if cache_read is not None and cache_miss is not None else 0
        return {
            "turns": int(row.get("turns") or 0), "model_calls": calls,
            "retries": int(row.get("retries") or 0),
            "failed_turns": int(row.get("failed_turns") or 0),
            "metered_turns": int(row.get("metered_turns") or 0),
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total, "cache_read_tokens": cache_read,
            "cache_write_tokens": _usage_optional_int(row, "cache_write_tokens"),
            "cache_miss_tokens": cache_miss,
            "unknown_usage_calls": int(row.get("unknown_usage_calls") or 0),
            "usage_reported_calls": usage_calls,
            "cache_reported_calls": cache_calls,
            "usage_coverage": _usage_rate(usage_calls, calls),
            "cache_coverage": _usage_rate(cache_calls, calls),
            "cache_hit_ratio": float(cache_read) / cache_denominator if cache_denominator else None,
        }

    rendered_totals = render(totals)
    activated = int(cohort["activated_users"] or 0)
    current_app_users = int(cohort["current_app_users"] or 0)
    current_hosted_v2_users = int(
        cohort["current_hosted_v2_users"] or 0
    )
    token_user_tokens = int(totals.get("token_user_tokens") or 0)
    total_tokens = rendered_totals["total_tokens"]
    metered_prompt = _usage_optional_int(totals, "metered_prompt_tokens")
    metered_completion = _usage_optional_int(totals, "metered_completion_tokens")
    metered_total = metered_prompt + metered_completion if metered_prompt is not None and metered_completion is not None else None
    overview = {
        "registered_accounts": int(cohort["registered_accounts"] or 0),
        "activated_users": activated,
        "current_app_users": current_app_users,
        "current_hosted_v2_users": current_hosted_v2_users,
        "current_hosted_v2_coverage": _usage_rate(
            current_hosted_v2_users, current_app_users
        ),
        "model_active_users": int(totals["model_active_users"] or 0),
        "token_users": int(totals["token_users"] or 0),
        "token_user_tokens": token_user_tokens,
        "metered_users": int(totals["metered_users"] or 0),
        "active_user_days": int(totals["active_user_days"] or 0),
        **{key: rendered_totals[key] for key in (
            "turns", "model_calls", "retries", "failed_turns", "metered_turns",
            "prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens",
            "cache_write_tokens", "cache_miss_tokens", "unknown_usage_calls",
        )},
    }
    distribution = core.get("distribution")
    averages = {
        "tokens_per_calendar_day": float(total_tokens) / duration_days if total_tokens is not None and duration_days > 0 else None,
        "tokens_per_active_user_day": float(total_tokens) / overview["active_user_days"] if total_tokens is not None and overview["active_user_days"] else None,
        "tokens_per_token_user": float(token_user_tokens) / overview["token_users"] if overview["token_users"] else None,
        "tokens_per_activated_user_day": (
            float(total_tokens) / (activated * duration_days)
            if total_tokens is not None and activated and duration_days > 0
            and not usage_reporting.has_dimension_filter(query) else None
        ),
        "tokens_per_metered_turn": float(metered_total) / overview["metered_turns"] if metered_total is not None and overview["metered_turns"] else None,
        "user_day_tokens": ({key: _usage_optional_float(distribution, key) for key in ("p50", "p75", "p90", "p95", "max")} if distribution is not None else None),
        "model_calls_per_turn": _usage_rate(overview["model_calls"], overview["turns"]),
        "retries_per_turn": _usage_rate(overview["retries"], overview["turns"]),
    }

    daily = None
    if core.get("daily") is not None:
        daily_by_day = {row["local_day"]: row for row in core["daily"]}
        daily = []
        day = query.start_at_utc.astimezone(usage_reporting.SHANGHAI).date()
        last_day = (query.end_at_utc - timedelta(microseconds=1)).astimezone(usage_reporting.SHANGHAI).date()
        while day <= last_day:
            row = daily_by_day.get(day)
            if row is None:
                item = {
                    "turns": 0, "model_calls": 0, "retries": 0, "failed_turns": 0,
                    "metered_turns": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "cache_miss_tokens": 0, "unknown_usage_calls": 0,
                    "usage_reported_calls": 0, "cache_reported_calls": 0,
                    "usage_coverage": None, "cache_coverage": None, "cache_hit_ratio": None,
                    "token_user_tokens": 0,
                }
                active_users = 0
                token_users = 0
                day_metered_total = None
            else:
                item = render(row)
                active_users = int(row["model_active_users"] or 0)
                token_users = int(row["token_users"] or 0)
                item["token_user_tokens"] = int(
                    row.get("token_user_tokens") or 0
                )
                mp = _usage_optional_int(row, "metered_prompt_tokens")
                mc = _usage_optional_int(row, "metered_completion_tokens")
                day_metered_total = mp + mc if mp is not None and mc is not None else None
            item.update({
                "local_day": day.isoformat(), "model_active_users": active_users,
                "token_users": token_users,
                "tokens_per_active_user_day": float(item["total_tokens"]) / active_users if item["total_tokens"] is not None and active_users else None,
                "tokens_per_token_user": float(item["token_user_tokens"]) / token_users if token_users else None,
                "tokens_per_metered_turn": float(day_metered_total) / item["metered_turns"] if day_metered_total is not None and item["metered_turns"] else None,
            })
            daily.append(item)
            day += timedelta(days=1)

    primary_rows = (dimensions or {}).get("primary")
    primary = {
        str(row["user_id"]): (str(row["provider"]), str(row["model"]))
        for row in (primary_rows or [])
    }
    users = None if core.get("users") is None else []
    for row in core.get("users") or []:
        item = render(row)
        provider, model = primary.get(
            str(row["user_id"]),
            ("unavailable", "unavailable")
            if dimensions is None or primary_rows is None
            else ("unknown", "unknown"),
        )
        mp = _usage_optional_int(row, "metered_prompt_tokens")
        mc = _usage_optional_int(row, "metered_completion_tokens")
        mt = mp + mc if mp is not None and mc is not None else None
        active_days = int(row["active_days"] or 0)
        item.update({
            "user_id": str(row["user_id"]), "active_days": active_days,
            "last_model_call_at": row["last_model_call_at"],
            "primary_provider": provider, "primary_model": model,
            "daily_p50": _usage_optional_float(row, "daily_p50"),
            "daily_p95": _usage_optional_float(row, "daily_p95"),
            "tokens_per_calendar_day": float(item["total_tokens"]) / duration_days if item["total_tokens"] is not None and duration_days > 0 else None,
            "tokens_per_active_day": float(item["total_tokens"]) / active_days if item["total_tokens"] is not None and active_days else None,
            "tokens_per_metered_turn": float(mt) / item["metered_turns"] if mt is not None and item["metered_turns"] else None,
            "known_token_share": float(item["total_tokens"]) / total_tokens if item["total_tokens"] is not None and total_tokens else None,
        })
        users.append(item)

    def identity_rows(name: str, fields: tuple[str, ...]):
        if dimensions is None or dimensions.get(name) is None:
            return None
        latency_rows = (
            ((latency_bundle or {}).get("latency") or {}).get(name) or []
        )
        latency = {tuple(row[field] for field in fields): row for row in latency_rows}
        items = []
        for row in dimensions[name]:
            item = render(row)
            if fields == ("provider", "model"):
                item["metered_turns"] = 0
            item.update({field: str(row[field]) for field in fields})
            values = latency.get(tuple(row[field] for field in fields)) or {}
            item.update({
                "users": int(row["users"] or 0),
                "tokens_per_call": float(item["total_tokens"]) / item["model_calls"] if item["total_tokens"] is not None and item["model_calls"] else None,
                "latency_ms_p50": _usage_optional_float(values, "latency_ms_p50"),
                "latency_ms_p95": _usage_optional_float(values, "latency_ms_p95"),
                "failure_rate": _usage_rate(item["failed_turns"], item["turns"]),
                "retry_rate": _usage_rate(item["retries"], item["model_calls"]),
            })
            items.append(item)
        return items

    filter_row = (latency_bundle or {}).get("filters")
    rendered_filters = ({
        "lanes": list(filter_row["lanes"] or []),
        "providers": list(filter_row["providers"] or []),
        "models": list(filter_row["models"] or []),
    } if filter_row is not None else None)
    cache_read = rendered_totals["cache_read_tokens"]
    cache_miss = rendered_totals["cache_miss_tokens"]
    cache_denominator = cache_read + cache_miss if cache_read is not None and cache_miss is not None else 0
    return {
        "overview": overview, "averages": averages, "daily": daily, "users": users,
        "models": identity_rows("models", ("provider", "model")),
        "lanes": identity_rows("lanes", ("lane",)), "filters": rendered_filters,
        "coverage": {
            "usage_reported_calls": rendered_totals["usage_reported_calls"],
            "model_calls": rendered_totals["model_calls"],
            "usage_coverage": rendered_totals["usage_coverage"],
            "cache_reported_calls": rendered_totals["cache_reported_calls"],
            "cache_coverage": rendered_totals["cache_coverage"],
            "cache_hit_ratio": float(cache_read) / cache_denominator if cache_denominator else None,
            "reference_cohort": {
                "basis": "parseable_utc_write_timestamps_at_end_at",
                "unparseable_registered_rows": int(cohort["unparseable_registered_rows"] or 0),
                "legacy_memory_rows_without_valid_created_at": int(cohort["legacy_memory_rows_without_valid_created_at"] or 0),
                "limitation": "legacy users.created_at and memory doc.created_at values that are missing or invalid are excluded from historical registered and activated reference cohorts",
            },
            "rollup": {
                "mode": "hybrid-parallel", "refreshed_at": state["refreshed_at"],
                "last_success_at": state["last_success_at"],
                "processed_updated_at": state["source_updated_at"], "processed_id": int(state["source_id"]),
                "source_observed_updated_at": state["source_observed_updated_at"],
                "source_lag_seconds": state["source_lag_seconds"],
                "last_error_at": state["last_error_at"], "last_error": state["last_error"],
                "raw_days": [value.isoformat() for value in partition.raw_days],
                "rollup_days": [value.isoformat() for value in partition.rollup_days],
            },
        },
    }


def _usage_report_snapshot_raw(query, *, exporter_conn=None) -> dict:
    """Return one coherent, content-free Hosted V2 Usage analytics snapshot."""

    where_sql, where_params = usage_reporting.metric_filter_sql(query)
    filter_where_sql, filter_where_params = usage_reporting.metric_filter_sql(
        query, include_dimensions=False
    )
    duration_days = (
        query.end_at_utc - query.start_at_utc
    ).total_seconds() / 86400.0
    dimension_filtered = usage_reporting.has_dimension_filter(query)
    user_cohort_sql = ""
    user_cohort_params: tuple[object, ...] = ()
    if query.user_id:
        user_cohort_sql = " AND u.user_id=%s"
        user_cohort_params = (query.user_id,)

    def utc_text_timestamp(expression: str) -> str:
        """Parse explicit-offset ISO text directly and legacy naive text as UTC."""

        return f"""
CASE
  WHEN ({expression}) ~ '(Z|[+-]\\d{{2}}:?\\d{{2}})$'
    AND pg_input_is_valid(({expression}), 'timestamptz')
    THEN ({expression})::timestamptz
  WHEN pg_input_is_valid(({expression}), 'timestamp')
    THEN ({expression})::timestamp AT TIME ZONE 'UTC'
  ELSE NULL
END
"""

    registered_at_sql = utc_text_timestamp("u.created_at")
    memory_created_at_sql = utc_text_timestamp("mm.doc->>'created_at'")

    base_cte = f"""
WITH base AS (
  SELECT
    COALESCE(m.user_id, 'unknown') AS user_id,
    COALESCE(NULLIF(m.lane, ''), 'unknown') AS lane,
    COALESCE(NULLIF(m.provider, ''), 'unknown') AS provider,
    COALESCE(NULLIF(m.model, ''), 'unknown') AS model,
    timezone(%s, m.created_at)::date AS local_day,
    m.created_at, m.model_calls, m.retries, m.failed, m.latency_ms,
    m.usage_reported_calls, m.cache_reported_calls,
    m.prompt_tokens, m.completion_tokens,
    m.cache_read_tokens, m.cache_write_tokens, m.cache_miss_tokens
  FROM v2_turn_metrics m
  WHERE {where_sql}
)
"""
    base_params = (query.timezone, *where_params)

    connection_context = (
        nullcontext(exporter_conn)
        if exporter_conn is not None
        else _usage_pool_connection()
    )
    with connection_context as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                cur.execute(
                    "SELECT set_config('statement_timeout',%s,true)",
                    (str(_USAGE_REPORT_STATEMENT_TIMEOUT_MS),),
                )
                cur.execute(
                    f"""
WITH user_times AS (
  SELECT u.user_id, {registered_at_sql} AS registered_at
  FROM users u
  WHERE true {user_cohort_sql}
), registered AS (
  SELECT user_id, registered_at < %s AS existed_at_end,
         registered_at IS NULL AS unparseable
  FROM user_times
), activated AS (
  SELECT r.user_id
  FROM registered r
  WHERE r.existed_at_end AND (
    EXISTS (
      SELECT 1 FROM chat_messages c
      WHERE c.user_id=r.user_id
        AND c.doc->>'role' IN ('user', 'human')
        AND COALESCE(c.doc->>'source', '') NOT IN
          ('verify_ping', 'resident_maintenance')
        AND to_timestamp(c.ts) < %s
    ) OR EXISTS (
      SELECT 1 FROM memory_moments mm
      WHERE mm.user_id=r.user_id
        AND ({memory_created_at_sql}) < %s
    )
  )
), current_population AS (
  SELECT
    count(*)::int AS current_app_users,
    count(*) FILTER (WHERE s.hosted_runtime_state='v2')::int
      AS current_hosted_v2_users
  FROM users u
  LEFT JOIN v2_runtime_state s ON s.user_id=u.user_id
)
SELECT
  count(*) FILTER (WHERE existed_at_end)::int AS registered_accounts,
  (SELECT count(*)::int FROM activated) AS activated_users,
  (SELECT current_app_users FROM current_population) AS current_app_users,
  (SELECT current_hosted_v2_users FROM current_population)
    AS current_hosted_v2_users,
  count(*) FILTER (WHERE unparseable)::int AS unparseable_registered_rows,
  (SELECT count(*)::int FROM memory_moments mm
   JOIN registered r ON r.user_id=mm.user_id
   WHERE ({memory_created_at_sql}) IS NULL)
    AS legacy_memory_rows_without_valid_created_at
FROM registered
""",
                    (
                        *user_cohort_params,
                        query.end_at_utc,
                        query.end_at_utc,
                        query.end_at_utc,
                    ),
                )
                cohort = cur.fetchone()

                cur.execute(
                    base_cte
                    + """,
token_user_totals AS (
  SELECT user_id,
         (sum(prompt_tokens) + sum(completion_tokens))::bigint AS total_tokens
  FROM base
  GROUP BY user_id
  HAVING count(prompt_tokens)>0 AND count(completion_tokens)>0
)
SELECT
  count(*)::int AS turns,
  count(DISTINCT user_id) FILTER (WHERE model_calls > 0)::int
    AS model_active_users,
  (SELECT count(*)::int FROM token_user_totals) AS token_users,
  (SELECT coalesce(sum(total_tokens),0)::bigint FROM token_user_totals)
    AS token_user_tokens,
  count(DISTINCT user_id) FILTER (WHERE usage_reported_calls > 0)::int
    AS metered_users,
  count(DISTINCT (user_id, local_day)) FILTER (WHERE model_calls > 0)::int
    AS active_user_days,
  count(*) FILTER (WHERE usage_reported_calls > 0)::int AS metered_turns,
  coalesce(sum(model_calls), 0)::bigint AS model_calls,
  coalesce(sum(retries), 0)::bigint AS retries,
  count(*) FILTER (WHERE failed)::int AS failed_turns,
  sum(prompt_tokens)::bigint AS prompt_tokens,
  sum(completion_tokens)::bigint AS completion_tokens,
  sum(prompt_tokens) FILTER (WHERE usage_reported_calls > 0)::bigint
    AS metered_prompt_tokens,
  sum(completion_tokens) FILTER (WHERE usage_reported_calls > 0)::bigint
    AS metered_completion_tokens,
  sum(cache_read_tokens)::bigint AS cache_read_tokens,
  sum(cache_write_tokens)::bigint AS cache_write_tokens,
  sum(cache_miss_tokens)::bigint AS cache_miss_tokens,
  coalesce(sum(GREATEST(model_calls - usage_reported_calls, 0)), 0)::bigint
    AS unknown_usage_calls,
  coalesce(sum(usage_reported_calls), 0)::bigint AS usage_reported_calls,
  coalesce(sum(cache_reported_calls), 0)::bigint AS cache_reported_calls
FROM base
""",
                    base_params,
                )
                totals = cur.fetchone()

                distribution = _usage_optional_breakdown(
                    conn,
                    cur,
                    "distribution",
                    base_cte
                    + """,
user_days AS (
  SELECT user_id, local_day,
         sum(prompt_tokens)::bigint AS prompt_tokens,
         sum(completion_tokens)::bigint AS completion_tokens
  FROM base
  WHERE model_calls > 0
  GROUP BY user_id, local_day
), known_user_days AS (
  SELECT (prompt_tokens + completion_tokens)::numeric AS total_tokens
  FROM user_days
  WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
)
SELECT
  percentile_cont(0.50) WITHIN GROUP (ORDER BY total_tokens) AS p50,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY total_tokens) AS p75,
  percentile_cont(0.90) WITHIN GROUP (ORDER BY total_tokens) AS p90,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY total_tokens) AS p95,
  max(total_tokens) AS max
FROM known_user_days
""",
                    base_params,
                    fetch="one",
                )

                daily_rows = _usage_optional_breakdown(
                    conn,
                    cur,
                    "daily",
                    base_cte
                    + """,
days AS (
  SELECT generate_series(
    timezone(%s, %s::timestamptz)::date,
    timezone(%s, (%s::timestamptz - interval '1 microsecond'))::date,
    interval '1 day'
  )::date AS local_day
), daily AS (
  SELECT local_day, count(*)::int AS turns,
         count(DISTINCT user_id) FILTER (WHERE model_calls > 0)::int
           AS model_active_users,
         count(*) FILTER (WHERE usage_reported_calls > 0)::int AS metered_turns,
         coalesce(sum(model_calls), 0)::bigint AS model_calls,
         coalesce(sum(retries), 0)::bigint AS retries,
         count(*) FILTER (WHERE failed)::int AS failed_turns,
         sum(prompt_tokens)::bigint AS prompt_tokens,
         sum(completion_tokens)::bigint AS completion_tokens,
         sum(prompt_tokens) FILTER (WHERE usage_reported_calls > 0)::bigint
           AS metered_prompt_tokens,
         sum(completion_tokens) FILTER (WHERE usage_reported_calls > 0)::bigint
           AS metered_completion_tokens,
         sum(cache_read_tokens)::bigint AS cache_read_tokens,
         sum(cache_write_tokens)::bigint AS cache_write_tokens,
         sum(cache_miss_tokens)::bigint AS cache_miss_tokens,
         coalesce(sum(GREATEST(model_calls-usage_reported_calls, 0)), 0)::bigint
           AS unknown_usage_calls,
         coalesce(sum(usage_reported_calls), 0)::bigint AS usage_reported_calls,
         coalesce(sum(cache_reported_calls), 0)::bigint AS cache_reported_calls
  FROM base GROUP BY local_day
), daily_token_users AS (
  SELECT local_day, count(*)::int AS token_users,
         coalesce(sum(total_tokens),0)::bigint AS token_user_tokens
  FROM (
    SELECT local_day, user_id,
           (sum(prompt_tokens) + sum(completion_tokens))::bigint
             AS total_tokens
    FROM base
    GROUP BY local_day, user_id
    HAVING count(prompt_tokens)>0 AND count(completion_tokens)>0
  ) known_user_days
  GROUP BY local_day
)
SELECT days.local_day, coalesce(d.turns, 0)::int AS turns,
       coalesce(d.model_active_users, 0)::int AS model_active_users,
       coalesce(t.token_users, 0)::int AS token_users,
       coalesce(t.token_user_tokens, 0)::bigint AS token_user_tokens,
       coalesce(d.metered_turns, 0)::int AS metered_turns,
       coalesce(d.model_calls, 0)::bigint AS model_calls,
       coalesce(d.retries, 0)::bigint AS retries,
       coalesce(d.failed_turns, 0)::int AS failed_turns,
       CASE WHEN d.local_day IS NULL THEN 0 ELSE d.prompt_tokens END
         AS prompt_tokens,
       CASE WHEN d.local_day IS NULL THEN 0 ELSE d.completion_tokens END
         AS completion_tokens,
       d.metered_prompt_tokens, d.metered_completion_tokens,
       CASE WHEN d.local_day IS NULL THEN 0 ELSE d.cache_read_tokens END
         AS cache_read_tokens,
       CASE WHEN d.local_day IS NULL THEN 0 ELSE d.cache_write_tokens END
         AS cache_write_tokens,
       CASE WHEN d.local_day IS NULL THEN 0 ELSE d.cache_miss_tokens END
         AS cache_miss_tokens,
       coalesce(d.unknown_usage_calls, 0)::bigint AS unknown_usage_calls,
       coalesce(d.usage_reported_calls, 0)::bigint AS usage_reported_calls,
       coalesce(d.cache_reported_calls, 0)::bigint AS cache_reported_calls
FROM days
LEFT JOIN daily d USING (local_day)
LEFT JOIN daily_token_users t USING (local_day)
ORDER BY days.local_day
""",
                    (
                        *base_params,
                        query.timezone,
                        query.start_at_utc,
                        query.timezone,
                        query.end_at_utc,
                    ),
                    fetch="all",
                )

                user_rows = _usage_optional_breakdown(
                    conn,
                    cur,
                    "users",
                    base_cte
                    + """,
user_days AS (
  SELECT user_id,local_day,sum(prompt_tokens)::bigint AS prompt_tokens,
         sum(completion_tokens)::bigint AS completion_tokens
  FROM base WHERE model_calls > 0 GROUP BY user_id,local_day
), user_distribution AS (
  SELECT user_id,
    percentile_cont(0.50) WITHIN GROUP
      (ORDER BY prompt_tokens+completion_tokens) AS daily_p50,
    percentile_cont(0.95) WITHIN GROUP
      (ORDER BY prompt_tokens+completion_tokens) AS daily_p95
  FROM user_days
  WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
  GROUP BY user_id
), provider_model_rank AS (
  SELECT user_id,provider,model,
    row_number() OVER (
      PARTITION BY user_id ORDER BY sum(model_calls) DESC, provider, model
    ) AS rank
  FROM base GROUP BY user_id,provider,model
)
SELECT b.user_id, count(*)::int AS turns,
  count(DISTINCT b.local_day) FILTER (WHERE b.model_calls > 0)::int AS active_days,
  max(b.created_at) FILTER (WHERE b.model_calls > 0) AS last_model_call_at,
  coalesce(sum(b.model_calls), 0)::bigint AS model_calls,
  coalesce(sum(b.retries), 0)::bigint AS retries,
  count(*) FILTER (WHERE b.failed)::int AS failed_turns,
  count(*) FILTER (WHERE b.usage_reported_calls > 0)::int AS metered_turns,
  sum(b.prompt_tokens)::bigint AS prompt_tokens,
  sum(b.completion_tokens)::bigint AS completion_tokens,
  sum(b.prompt_tokens) FILTER (WHERE b.usage_reported_calls > 0)::bigint
    AS metered_prompt_tokens,
  sum(b.completion_tokens) FILTER (WHERE b.usage_reported_calls > 0)::bigint
    AS metered_completion_tokens,
  sum(b.cache_read_tokens)::bigint AS cache_read_tokens,
  sum(b.cache_write_tokens)::bigint AS cache_write_tokens,
  sum(b.cache_miss_tokens)::bigint AS cache_miss_tokens,
  coalesce(sum(GREATEST(b.model_calls-b.usage_reported_calls, 0)), 0)::bigint
    AS unknown_usage_calls,
  coalesce(sum(b.usage_reported_calls), 0)::bigint AS usage_reported_calls,
  coalesce(sum(b.cache_reported_calls), 0)::bigint AS cache_reported_calls,
  max(pr.provider) AS primary_provider, max(pr.model) AS primary_model,
  max(ud.daily_p50) AS daily_p50, max(ud.daily_p95) AS daily_p95
FROM base b
LEFT JOIN provider_model_rank pr ON pr.user_id=b.user_id AND pr.rank=1
LEFT JOIN user_distribution ud ON ud.user_id=b.user_id
GROUP BY b.user_id
ORDER BY (sum(b.prompt_tokens)+sum(b.completion_tokens)) DESC NULLS LAST,
         sum(b.model_calls) DESC, b.user_id
""",
                    base_params,
                    fetch="all",
                )

                model_rows = _usage_optional_breakdown(
                    conn,
                    cur,
                    "models",
                    base_cte
                    + """
SELECT provider,model,count(DISTINCT user_id)::int AS users,
  count(*)::int AS turns,
  coalesce(sum(model_calls), 0)::bigint AS model_calls,
  coalesce(sum(retries), 0)::bigint AS retries,
  count(*) FILTER (WHERE failed)::int AS failed_turns,
  sum(prompt_tokens)::bigint AS prompt_tokens,
  sum(completion_tokens)::bigint AS completion_tokens,
  sum(cache_read_tokens)::bigint AS cache_read_tokens,
  sum(cache_write_tokens)::bigint AS cache_write_tokens,
  sum(cache_miss_tokens)::bigint AS cache_miss_tokens,
  coalesce(sum(GREATEST(model_calls-usage_reported_calls, 0)), 0)::bigint
    AS unknown_usage_calls,
  coalesce(sum(usage_reported_calls), 0)::bigint AS usage_reported_calls,
  coalesce(sum(cache_reported_calls), 0)::bigint AS cache_reported_calls,
  percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms)
    FILTER (WHERE latency_ms IS NOT NULL) AS latency_ms_p50,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
    FILTER (WHERE latency_ms IS NOT NULL) AS latency_ms_p95
FROM base GROUP BY provider,model
ORDER BY (sum(prompt_tokens)+sum(completion_tokens)) DESC NULLS LAST,
         sum(model_calls) DESC,provider,model
""",
                    base_params,
                    fetch="all",
                )

                lane_rows = _usage_optional_breakdown(
                    conn,
                    cur,
                    "lanes",
                    base_cte
                    + """
SELECT lane,count(DISTINCT user_id)::int AS users,
  count(*)::int AS turns,
  coalesce(sum(model_calls), 0)::bigint AS model_calls,
  coalesce(sum(retries), 0)::bigint AS retries,
  count(*) FILTER (WHERE failed)::int AS failed_turns,
  count(*) FILTER (WHERE usage_reported_calls > 0)::int AS metered_turns,
  sum(prompt_tokens)::bigint AS prompt_tokens,
  sum(completion_tokens)::bigint AS completion_tokens,
  sum(cache_read_tokens)::bigint AS cache_read_tokens,
  sum(cache_write_tokens)::bigint AS cache_write_tokens,
  sum(cache_miss_tokens)::bigint AS cache_miss_tokens,
  coalesce(sum(GREATEST(model_calls-usage_reported_calls, 0)), 0)::bigint
    AS unknown_usage_calls,
  coalesce(sum(usage_reported_calls), 0)::bigint AS usage_reported_calls,
  coalesce(sum(cache_reported_calls), 0)::bigint AS cache_reported_calls,
  percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms)
    FILTER (WHERE latency_ms IS NOT NULL) AS latency_ms_p50,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
    FILTER (WHERE latency_ms IS NOT NULL) AS latency_ms_p95
FROM base GROUP BY lane
ORDER BY (sum(prompt_tokens)+sum(completion_tokens)) DESC NULLS LAST,
         sum(model_calls) DESC,lane
""",
                    base_params,
                    fetch="all",
                )

                filter_options = _usage_optional_breakdown(
                    conn,
                    cur,
                    "filters",
                    f"""
SELECT
  array_agg(DISTINCT COALESCE(NULLIF(m.lane, ''), 'unknown')
    ORDER BY COALESCE(NULLIF(m.lane, ''), 'unknown')) AS lanes,
  array_agg(DISTINCT COALESCE(NULLIF(m.provider, ''), 'unknown')
    ORDER BY COALESCE(NULLIF(m.provider, ''), 'unknown')) AS providers,
  array_agg(DISTINCT COALESCE(NULLIF(m.model, ''), 'unknown')
    ORDER BY COALESCE(NULLIF(m.model, ''), 'unknown')) AS models
FROM v2_turn_metrics m WHERE {filter_where_sql}
""",
                    filter_where_params,
                    fetch="one",
                )

    prompt_tokens = _usage_optional_int(totals, "prompt_tokens")
    completion_tokens = _usage_optional_int(totals, "completion_tokens")
    total_tokens = (
        prompt_tokens + completion_tokens
        if prompt_tokens is not None and completion_tokens is not None
        else None
    )
    metered_prompt_tokens = _usage_optional_int(
        totals, "metered_prompt_tokens"
    )
    metered_completion_tokens = _usage_optional_int(
        totals, "metered_completion_tokens"
    )
    metered_total_tokens = (
        metered_prompt_tokens + metered_completion_tokens
        if metered_prompt_tokens is not None
        and metered_completion_tokens is not None
        else None
    )
    turns = int(totals["turns"] or 0)
    model_calls = int(totals["model_calls"] or 0)
    retries = int(totals["retries"] or 0)
    active_user_days = int(totals["active_user_days"] or 0)
    metered_turns = int(totals["metered_turns"] or 0)
    activated_users = int(cohort["activated_users"] or 0)
    current_app_users = int(cohort["current_app_users"] or 0)
    current_hosted_v2_users = int(
        cohort["current_hosted_v2_users"] or 0
    )
    token_user_tokens = int(totals.get("token_user_tokens") or 0)
    usage_calls = int(totals["usage_reported_calls"] or 0)
    cache_calls = int(totals["cache_reported_calls"] or 0)
    cache_read = _usage_optional_int(totals, "cache_read_tokens")
    cache_miss = _usage_optional_int(totals, "cache_miss_tokens")
    cache_denominator = (
        cache_read + cache_miss
        if cache_read is not None and cache_miss is not None
        else 0
    )

    overview = {
        "registered_accounts": int(cohort["registered_accounts"] or 0),
        "activated_users": activated_users,
        "current_app_users": current_app_users,
        "current_hosted_v2_users": current_hosted_v2_users,
        "current_hosted_v2_coverage": _usage_rate(
            current_hosted_v2_users, current_app_users
        ),
        "model_active_users": int(totals["model_active_users"] or 0),
        "token_users": int(totals["token_users"] or 0),
        "token_user_tokens": token_user_tokens,
        "metered_users": int(totals["metered_users"] or 0),
        "active_user_days": active_user_days,
        "turns": turns,
        "model_calls": model_calls,
        "retries": retries,
        "failed_turns": int(totals["failed_turns"] or 0),
        "metered_turns": metered_turns,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": _usage_optional_int(totals, "cache_write_tokens"),
        "cache_miss_tokens": cache_miss,
        "unknown_usage_calls": int(totals["unknown_usage_calls"] or 0),
    }
    averages = {
        "tokens_per_calendar_day": (
            float(total_tokens) / duration_days
            if total_tokens is not None and duration_days > 0
            else None
        ),
        "tokens_per_active_user_day": (
            float(total_tokens) / active_user_days
            if total_tokens is not None and active_user_days
            else None
        ),
        "tokens_per_token_user": (
            float(token_user_tokens) / overview["token_users"]
            if overview["token_users"]
            else None
        ),
        "tokens_per_activated_user_day": (
            float(total_tokens) / (activated_users * duration_days)
            if total_tokens is not None
            and activated_users
            and duration_days > 0
            and not dimension_filtered
            else None
        ),
        "tokens_per_metered_turn": (
            float(metered_total_tokens) / metered_turns
            if metered_total_tokens is not None and metered_turns
            else None
        ),
        "user_day_tokens": (
            {
                key: _usage_optional_float(distribution, key)
                for key in ("p50", "p75", "p90", "p95", "max")
            }
            if distribution is not None
            else None
        ),
        "model_calls_per_turn": _usage_rate(model_calls, turns),
        "retries_per_turn": _usage_rate(retries, turns),
    }

    def aggregate_row(row, *, include_day: bool = False) -> dict:
        row_prompt = _usage_optional_int(row, "prompt_tokens")
        row_completion = _usage_optional_int(row, "completion_tokens")
        row_total = (
            row_prompt + row_completion
            if row_prompt is not None and row_completion is not None
            else None
        )
        row_calls = int(row["model_calls"] or 0)
        row_usage_calls = int(row["usage_reported_calls"] or 0)
        row_cache_calls = int(row["cache_reported_calls"] or 0)
        row_cache_read = _usage_optional_int(row, "cache_read_tokens")
        row_cache_miss = _usage_optional_int(row, "cache_miss_tokens")
        row_cache_denominator = (
            row_cache_read + row_cache_miss
            if row_cache_read is not None and row_cache_miss is not None
            else 0
        )
        result = {
            "turns": int(row["turns"] or 0),
            "model_calls": row_calls,
            "retries": int(row["retries"] or 0),
            "failed_turns": int(row["failed_turns"] or 0),
            "metered_turns": int(row.get("metered_turns") or 0),
            "prompt_tokens": row_prompt,
            "completion_tokens": row_completion,
            "total_tokens": row_total,
            "cache_read_tokens": row_cache_read,
            "cache_write_tokens": _usage_optional_int(row, "cache_write_tokens"),
            "cache_miss_tokens": row_cache_miss,
            "unknown_usage_calls": int(row["unknown_usage_calls"] or 0),
            "usage_reported_calls": row_usage_calls,
            "cache_reported_calls": row_cache_calls,
            "usage_coverage": _usage_rate(row_usage_calls, row_calls),
            "cache_coverage": _usage_rate(row_cache_calls, row_calls),
            "cache_hit_ratio": (
                float(row_cache_read) / row_cache_denominator
                if row_cache_denominator
                else None
            ),
        }
        if include_day:
            result["local_day"] = row["local_day"].isoformat()
            active_users = int(row["model_active_users"] or 0)
            token_users = int(row["token_users"] or 0)
            result["model_active_users"] = active_users
            result["token_users"] = token_users
            result["token_user_tokens"] = int(
                row.get("token_user_tokens") or 0
            )
            result["tokens_per_active_user_day"] = (
                float(row_total) / active_users
                if row_total is not None and active_users
                else None
            )
            result["tokens_per_token_user"] = (
                float(result["token_user_tokens"]) / token_users
                if token_users
                else None
            )
            day_metered_prompt = _usage_optional_int(
                row, "metered_prompt_tokens"
            )
            day_metered_completion = _usage_optional_int(
                row, "metered_completion_tokens"
            )
            day_metered_total = (
                day_metered_prompt + day_metered_completion
                if day_metered_prompt is not None
                and day_metered_completion is not None
                else None
            )
            result["tokens_per_metered_turn"] = (
                float(day_metered_total) / result["metered_turns"]
                if day_metered_total is not None and result["metered_turns"]
                else None
            )
        return result

    daily = (
        [aggregate_row(row, include_day=True) for row in daily_rows]
        if daily_rows is not None
        else None
    )
    users = None
    if user_rows is not None:
        users = []
        for row in user_rows:
            item = aggregate_row(row)
            user_metered_prompt = _usage_optional_int(
                row, "metered_prompt_tokens"
            )
            user_metered_completion = _usage_optional_int(
                row, "metered_completion_tokens"
            )
            user_metered_total = (
                user_metered_prompt + user_metered_completion
                if user_metered_prompt is not None
                and user_metered_completion is not None
                else None
            )
            item.update(
                {
                    "user_id": str(row["user_id"]),
                    "active_days": int(row["active_days"] or 0),
                    "last_model_call_at": row["last_model_call_at"],
                    "primary_provider": str(
                        row["primary_provider"] or "unknown"
                    ),
                    "primary_model": str(row["primary_model"] or "unknown"),
                    "daily_p50": _usage_optional_float(row, "daily_p50"),
                    "daily_p95": _usage_optional_float(row, "daily_p95"),
                    "tokens_per_calendar_day": (
                        float(item["total_tokens"]) / duration_days
                        if item["total_tokens"] is not None
                        and duration_days > 0
                        else None
                    ),
                    "tokens_per_active_day": (
                        float(item["total_tokens"]) / int(row["active_days"])
                        if item["total_tokens"] is not None
                        and row["active_days"]
                        else None
                    ),
                    "tokens_per_metered_turn": (
                        float(user_metered_total) / item["metered_turns"]
                        if user_metered_total is not None
                        and item["metered_turns"]
                        else None
                    ),
                    "known_token_share": (
                        float(item["total_tokens"]) / total_tokens
                        if item["total_tokens"] is not None and total_tokens
                        else None
                    ),
                }
            )
            users.append(item)

    def identity_breakdown(rows, *identity_fields):
        if rows is None:
            return None
        items = []
        for row in rows:
            item = aggregate_row(row)
            item.update(
                {
                    field: str(row[field])
                    for field in identity_fields
                }
            )
            item.update(
                {
                    "users": int(row["users"] or 0),
                    "tokens_per_call": (
                        float(item["total_tokens"]) / item["model_calls"]
                        if item["total_tokens"] is not None
                        and item["model_calls"]
                        else None
                    ),
                    "latency_ms_p50": _usage_optional_float(
                        row, "latency_ms_p50"
                    ),
                    "latency_ms_p95": _usage_optional_float(
                        row, "latency_ms_p95"
                    ),
                    "failure_rate": _usage_rate(
                        item["failed_turns"], item["turns"]
                    ),
                    "retry_rate": _usage_rate(
                        item["retries"], item["model_calls"]
                    ),
                }
            )
            items.append(item)
        return items

    models = identity_breakdown(model_rows, "provider", "model")
    lanes = identity_breakdown(lane_rows, "lane")
    rendered_filters = (
        {
            "lanes": list(filter_options["lanes"] or []),
            "providers": list(filter_options["providers"] or []),
            "models": list(filter_options["models"] or []),
        }
        if filter_options is not None
        else None
    )

    return {
        "overview": overview,
        "averages": averages,
        "daily": daily,
        "users": users,
        "models": models,
        "lanes": lanes,
        "filters": rendered_filters,
        "coverage": {
            "usage_reported_calls": usage_calls,
            "model_calls": model_calls,
            "usage_coverage": _usage_rate(usage_calls, model_calls),
            "cache_reported_calls": cache_calls,
            "cache_coverage": _usage_rate(cache_calls, model_calls),
            "cache_hit_ratio": (
                float(cache_read) / cache_denominator
                if cache_denominator
                else None
            ),
            "reference_cohort": {
                "basis": "parseable_utc_write_timestamps_at_end_at",
                "unparseable_registered_rows": int(
                    cohort["unparseable_registered_rows"] or 0
                ),
                "legacy_memory_rows_without_valid_created_at": int(
                    cohort["legacy_memory_rows_without_valid_created_at"] or 0
                ),
                "limitation": (
                    "legacy users.created_at and memory doc.created_at values "
                    "that are missing or invalid are excluded from historical "
                    "registered and activated reference cohorts"
                ),
            },
        },
    }


def _usage_percentile(values: list[int], fraction: float) -> float | None:
    """PostgreSQL percentile_cont-compatible interpolation."""

    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    return float(ordered[lower]) + (ordered[upper] - ordered[lower]) * (
        position - lower
    )


def _usage_sum_facts(rows: list[dict]) -> dict:
    result = {
        key: sum(int(row.get(key) or 0) for row in rows)
        for key in (
            "turns",
            "model_calls",
            "retries",
            "failed_turns",
            "usage_reported_calls",
            "cache_reported_calls",
            "unknown_usage_calls",
            "metered_turns",
        )
    }
    for field in _USAGE_FACT_TOKENS:
        known = sum(int(row.get(f"{field}_known_count") or 0) for row in rows)
        result[f"{field}_known_count"] = known
        result[field] = (
            sum(int(row.get(f"{field}_sum") or 0) for row in rows)
            if known
            else None
        )
    for field in ("prompt_tokens", "completion_tokens"):
        known = sum(
            int(row.get(f"metered_{field}_known_count") or 0) for row in rows
        )
        result[f"metered_{field}_known_count"] = known
        result[f"metered_{field}"] = (
            sum(int(row.get(f"metered_{field}_sum") or 0) for row in rows)
            if known
            else None
        )
    result["last_model_call_at"] = max(
        (row["last_model_call_at"] for row in rows if row.get("last_model_call_at")),
        default=None,
    )
    result["latency_samples"] = [
        int(value) for row in rows for value in (row.get("latency_samples") or [])
    ]
    return result


def _usage_group_facts(rows: list[dict], fields: tuple[str, ...]) -> list[dict]:
    grouped: dict[tuple[object, ...], list[dict]] = {}
    for row in rows:
        grouped.setdefault(tuple(row[field] for field in fields), []).append(row)
    return [
        {**dict(zip(fields, key, strict=True)), **_usage_sum_facts(items)}
        for key, items in grouped.items()
    ]


def _usage_cohort_on_cursor(cur, query) -> dict:
    user_filter = " AND u.user_id=%s" if query.user_id else ""
    params: list[object] = [query.user_id] if query.user_id else []
    # Explicit offsets are honored; legacy naive values remain UTC regardless
    # of the database session timezone.
    registered = """
CASE
  WHEN (u.created_at) ~ '(Z|[+-]\\d{2}:?\\d{2})$'
    AND pg_input_is_valid((u.created_at), 'timestamptz')
    THEN (u.created_at)::timestamptz
  WHEN pg_input_is_valid((u.created_at), 'timestamp')
    THEN (u.created_at)::timestamp AT TIME ZONE 'UTC'
  ELSE NULL
END
"""
    memory = """
CASE
  WHEN (mm.doc->>'created_at') ~ '(Z|[+-]\\d{2}:?\\d{2})$'
    AND pg_input_is_valid((mm.doc->>'created_at'), 'timestamptz')
    THEN (mm.doc->>'created_at')::timestamptz
  WHEN pg_input_is_valid((mm.doc->>'created_at'), 'timestamp')
    THEN (mm.doc->>'created_at')::timestamp AT TIME ZONE 'UTC'
  ELSE NULL
END
"""
    cur.execute(
        f"""
WITH registered AS (
 SELECT u.user_id,{registered} AS registered_at FROM users u WHERE true{user_filter}
), at_end AS (
 SELECT user_id,registered_at < %s AS existed_at_end,
        registered_at IS NULL AS unparseable FROM registered
), activated AS (
 SELECT r.user_id FROM at_end r WHERE r.existed_at_end AND (
  EXISTS (SELECT 1 FROM chat_messages c WHERE c.user_id=r.user_id
    AND c.doc->>'role' IN ('user','human')
    AND coalesce(c.doc->>'source','') NOT IN ('verify_ping','resident_maintenance')
    AND to_timestamp(c.ts) < %s)
  OR EXISTS (SELECT 1 FROM memory_moments mm WHERE mm.user_id=r.user_id
    AND ({memory}) < %s)
 )), current_population AS (
 SELECT count(*)::int AS current_app_users,
  count(*) FILTER (WHERE s.hosted_runtime_state='v2')::int
    AS current_hosted_v2_users
 FROM users u
 LEFT JOIN v2_runtime_state s ON s.user_id=u.user_id
 )
SELECT count(*) FILTER (WHERE existed_at_end)::int AS registered_accounts,
 (SELECT count(*)::int FROM activated) AS activated_users,
 (SELECT current_app_users FROM current_population) AS current_app_users,
 (SELECT current_hosted_v2_users FROM current_population)
   AS current_hosted_v2_users,
 count(*) FILTER (WHERE unparseable)::int AS unparseable_registered_rows,
 (SELECT count(*)::int FROM memory_moments mm JOIN at_end r ON r.user_id=mm.user_id
  WHERE ({memory}) IS NULL) AS legacy_memory_rows_without_valid_created_at
FROM at_end
""",
        (*params, query.end_at_utc, query.end_at_utc, query.end_at_utc),
    )
    return cur.fetchone()


def _usage_report_snapshot_hybrid_serial(query) -> dict | None:
    """Read exact daily facts and raw edges from one bounded RR/RO snapshot."""

    with _pool().connection(timeout=0.5) as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cur.execute("SELECT set_config('statement_timeout','15000',true)")
                cur.execute(
                    "SELECT * FROM v2_usage_rollup_watermarks "
                    "WHERE rollup_name='hosted_v2_usage'"
                )
                state = cur.fetchone()
                if state is None or not state["bootstrap_complete"]:
                    return None
                partition = usage_reporting.rollup_partition(
                    query,
                    dirty_from_day=state["dirty_from_day"],
                    dirty_through_day=state["dirty_through_day"],
                )
                if partition is None:
                    return None
                core_dimensions = usage_reporting.has_dimension_filter(query)
                core_sql, core_params = _usage_fact_query(
                    query, partition, dimensions=core_dimensions
                )
                cur.execute(core_sql, core_params)
                core_facts = cur.fetchall()
                dim_sql, dim_params = _usage_fact_query(
                    query, partition, dimensions=True
                )
                cur.execute(dim_sql, dim_params)
                dimension_facts = cur.fetchall()
                if query.completeness == "unknown":
                    # The three canonical prefixes deliberately overlap, but
                    # unknown-and-metered turn count/token sums are an
                    # intersection not encoded as a fourth prefix.  Read only
                    # that auxiliary from authoritative raw rows in this same
                    # snapshot; never approximate it from two daily totals.
                    touched_days = tuple(
                        sorted(partition.rollup_days + partition.raw_days)
                    )
                    auxiliary_partition = usage_reporting.RollupPartition(
                        rollup_days=(), raw_days=touched_days
                    )
                    auxiliary_sql, auxiliary_params = _usage_fact_query(
                        query, auxiliary_partition, dimensions=True
                    )
                    cur.execute(auxiliary_sql, auxiliary_params)
                    auxiliary_dimensions = cur.fetchall()
                    auxiliary_by_dimension = {
                        (
                            row["local_day"], row["user_id"], row["lane"],
                            row["provider"], row["model"],
                        ): row
                        for row in auxiliary_dimensions
                    }
                    metered_fields = (
                        "metered_turns",
                        "metered_prompt_tokens_sum",
                        "metered_prompt_tokens_known_count",
                        "metered_completion_tokens_sum",
                        "metered_completion_tokens_known_count",
                    )
                    for row in dimension_facts:
                        auxiliary = auxiliary_by_dimension.get(
                            (
                                row["local_day"], row["user_id"], row["lane"],
                                row["provider"], row["model"],
                            )
                        )
                        for field in metered_fields:
                            row[field] = int(auxiliary.get(field) or 0) if auxiliary else 0
                    if core_dimensions:
                        core_facts = dimension_facts
                    else:
                        auxiliary_users = {
                            (row["local_day"], row["user_id"]): row
                            for row in _usage_group_facts(
                                auxiliary_dimensions, ("local_day", "user_id")
                            )
                        }
                        for row in core_facts:
                            auxiliary = auxiliary_users.get(
                                (row["local_day"], row["user_id"])
                            )
                            for field in metered_fields:
                                row[field] = int(auxiliary.get(field) or 0) if auxiliary else 0
                option_sql, option_params = _usage_fact_query(
                    query,
                    partition,
                    dimensions=True,
                    prefix="all",
                    include_dimension_filters=False,
                )
                cur.execute(option_sql, option_params)
                option_facts = cur.fetchall()
                cohort = _usage_cohort_on_cursor(cur, query)

    totals = _usage_sum_facts(core_facts)
    duration_days = (query.end_at_utc - query.start_at_utc).total_seconds() / 86400
    active_user_days = len(
        {(row["user_id"], row["local_day"]) for row in core_facts if row["model_calls"] > 0}
    )
    model_active_users = len(
        {row["user_id"] for row in core_facts if row["model_calls"] > 0}
    )
    metered_users = len(
        {row["user_id"] for row in core_facts if row["usage_reported_calls"] > 0}
    )
    user_groups = _usage_group_facts(core_facts, ("user_id",))
    known_token_users = [
        row
        for row in user_groups
        if row["prompt_tokens"] is not None
        and row["completion_tokens"] is not None
    ]
    token_users = len(known_token_users)
    token_user_tokens = sum(
        int(row["prompt_tokens"]) + int(row["completion_tokens"])
        for row in known_token_users
    )
    activated_users = int(cohort["activated_users"] or 0)
    current_app_users = int(cohort["current_app_users"] or 0)
    current_hosted_v2_users = int(
        cohort["current_hosted_v2_users"] or 0
    )
    prompt = totals["prompt_tokens"]
    completion = totals["completion_tokens"]
    total_tokens = prompt + completion if prompt is not None and completion is not None else None
    metered_prompt = totals["metered_prompt_tokens"]
    metered_completion = totals["metered_completion_tokens"]
    metered_total = (
        metered_prompt + metered_completion
        if metered_prompt is not None and metered_completion is not None
        else None
    )

    def render_aggregate(row: dict) -> dict:
        row_prompt = row["prompt_tokens"]
        row_completion = row["completion_tokens"]
        row_total = (
            row_prompt + row_completion
            if row_prompt is not None and row_completion is not None
            else None
        )
        calls = int(row["model_calls"])
        cache_read = row["cache_read_tokens"]
        cache_miss = row["cache_miss_tokens"]
        cache_denominator = (
            cache_read + cache_miss
            if cache_read is not None and cache_miss is not None
            else 0
        )
        return {
            "turns": int(row["turns"]),
            "model_calls": calls,
            "retries": int(row["retries"]),
            "failed_turns": int(row["failed_turns"]),
            "metered_turns": int(row["metered_turns"]),
            "prompt_tokens": row_prompt,
            "completion_tokens": row_completion,
            "total_tokens": row_total,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": row["cache_write_tokens"],
            "cache_miss_tokens": cache_miss,
            "unknown_usage_calls": int(row["unknown_usage_calls"]),
            "usage_reported_calls": int(row["usage_reported_calls"]),
            "cache_reported_calls": int(row["cache_reported_calls"]),
            "usage_coverage": _usage_rate(int(row["usage_reported_calls"]), calls),
            "cache_coverage": _usage_rate(int(row["cache_reported_calls"]), calls),
            "cache_hit_ratio": (
                float(cache_read) / cache_denominator if cache_denominator else None
            ),
        }

    overview = {
        "registered_accounts": int(cohort["registered_accounts"] or 0),
        "activated_users": activated_users,
        "current_app_users": current_app_users,
        "current_hosted_v2_users": current_hosted_v2_users,
        "current_hosted_v2_coverage": _usage_rate(
            current_hosted_v2_users, current_app_users
        ),
        "model_active_users": model_active_users,
        "token_users": token_users,
        "token_user_tokens": token_user_tokens,
        "metered_users": metered_users,
        "active_user_days": active_user_days,
        **{key: render_aggregate(totals)[key] for key in (
            "turns", "model_calls", "retries", "failed_turns", "metered_turns",
            "prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens",
            "cache_write_tokens", "cache_miss_tokens", "unknown_usage_calls",
        )},
    }
    user_day_groups = _usage_group_facts(core_facts, ("user_id", "local_day"))
    known_user_day_tokens = [
        row["prompt_tokens"] + row["completion_tokens"]
        for row in user_day_groups
        if row["model_calls"] > 0
        and row["prompt_tokens"] is not None
        and row["completion_tokens"] is not None
    ]
    averages = {
        "tokens_per_calendar_day": float(total_tokens) / duration_days if total_tokens is not None and duration_days > 0 else None,
        "tokens_per_active_user_day": float(total_tokens) / active_user_days if total_tokens is not None and active_user_days else None,
        "tokens_per_token_user": float(token_user_tokens) / token_users if token_users else None,
        "tokens_per_activated_user_day": (
            float(total_tokens) / (activated_users * duration_days)
            if total_tokens is not None and activated_users and duration_days > 0
            and not usage_reporting.has_dimension_filter(query) else None
        ),
        "tokens_per_metered_turn": (
            float(metered_total) / totals["metered_turns"]
            if metered_total is not None and totals["metered_turns"] else None
        ),
        "user_day_tokens": {
            "p50": _usage_percentile(known_user_day_tokens, 0.5),
            "p75": _usage_percentile(known_user_day_tokens, 0.75),
            "p90": _usage_percentile(known_user_day_tokens, 0.9),
            "p95": _usage_percentile(known_user_day_tokens, 0.95),
            "max": float(max(known_user_day_tokens)) if known_user_day_tokens else None,
        },
        "model_calls_per_turn": _usage_rate(totals["model_calls"], totals["turns"]),
        "retries_per_turn": _usage_rate(totals["retries"], totals["turns"]),
    }

    by_day = {row["local_day"]: row for row in _usage_group_facts(core_facts, ("local_day",))}
    daily = []
    day = query.start_at_utc.astimezone(usage_reporting.SHANGHAI).date()
    final_day = (query.end_at_utc - timedelta(microseconds=1)).astimezone(usage_reporting.SHANGHAI).date()
    while day <= final_day:
        row = by_day.get(day)
        if row is None:
            row = _usage_sum_facts([])
            for field in _USAGE_FACT_TOKENS:
                row[field] = 0
        item = render_aggregate(row)
        day_users = {fact["user_id"] for fact in core_facts if fact["local_day"] == day and fact["model_calls"] > 0}
        day_token_users = [
            fact
            for fact in user_day_groups
            if fact["local_day"] == day
            and fact["prompt_tokens"] is not None
            and fact["completion_tokens"] is not None
        ]
        day_token_user_tokens = sum(
            int(fact["prompt_tokens"]) + int(fact["completion_tokens"])
            for fact in day_token_users
        )
        item.update({
            "local_day": day.isoformat(),
            "model_active_users": len(day_users),
            "token_users": len(day_token_users),
            "token_user_tokens": day_token_user_tokens,
            "tokens_per_active_user_day": float(item["total_tokens"]) / len(day_users) if item["total_tokens"] is not None and day_users else None,
            "tokens_per_token_user": float(day_token_user_tokens) / len(day_token_users) if day_token_users else None,
            "tokens_per_metered_turn": (
                float(row["metered_prompt_tokens"] + row["metered_completion_tokens"]) / row["metered_turns"]
                if row["metered_prompt_tokens"] is not None and row["metered_completion_tokens"] is not None and row["metered_turns"] else None
            ),
        })
        daily.append(item)
        day += timedelta(days=1)

    primary_groups = _usage_group_facts(dimension_facts, ("user_id", "provider", "model"))
    primary_by_user: dict[str, tuple[str, str]] = {}
    for row in sorted(primary_groups, key=lambda row: (row["user_id"], -row["model_calls"], row["provider"], row["model"])):
        primary_by_user.setdefault(str(row["user_id"]), (str(row["provider"]), str(row["model"])))
    users = []
    for row in user_groups:
        item = render_aggregate(row)
        user_days = [day_row for day_row in user_day_groups if day_row["user_id"] == row["user_id"] and day_row["model_calls"] > 0]
        known = [d["prompt_tokens"] + d["completion_tokens"] for d in user_days if d["prompt_tokens"] is not None and d["completion_tokens"] is not None]
        provider, model = primary_by_user.get(str(row["user_id"]), ("unknown", "unknown"))
        user_metered_total = row["metered_prompt_tokens"] + row["metered_completion_tokens"] if row["metered_prompt_tokens"] is not None and row["metered_completion_tokens"] is not None else None
        item.update({
            "user_id": str(row["user_id"]), "active_days": len(user_days),
            "last_model_call_at": row["last_model_call_at"], "primary_provider": provider,
            "primary_model": model, "daily_p50": _usage_percentile(known, 0.5),
            "daily_p95": _usage_percentile(known, 0.95),
            "tokens_per_calendar_day": float(item["total_tokens"]) / duration_days if item["total_tokens"] is not None and duration_days > 0 else None,
            "tokens_per_active_day": float(item["total_tokens"]) / len(user_days) if item["total_tokens"] is not None and user_days else None,
            "tokens_per_metered_turn": float(user_metered_total) / row["metered_turns"] if user_metered_total is not None and row["metered_turns"] else None,
            "known_token_share": float(item["total_tokens"]) / total_tokens if item["total_tokens"] is not None and total_tokens else None,
        })
        users.append(item)
    users.sort(key=lambda item: (item["total_tokens"] is None, -(item["total_tokens"] or 0), -item["model_calls"], item["user_id"]))

    def breakdown(fields: tuple[str, ...]) -> list[dict]:
        result = []
        for row in _usage_group_facts(dimension_facts, fields):
            item = render_aggregate(row)
            # Preserve the existing Provider / Model payload contract.  Its raw
            # query never exposed a metered-turn count (lanes do).
            if fields == ("provider", "model"):
                item["metered_turns"] = 0
            item.update({field: str(row[field]) for field in fields})
            item.update({
                "users": len({f["user_id"] for f in dimension_facts if all(f[field] == row[field] for field in fields)}),
                "tokens_per_call": float(item["total_tokens"]) / item["model_calls"] if item["total_tokens"] is not None and item["model_calls"] else None,
                "latency_ms_p50": _usage_percentile(row["latency_samples"], 0.5),
                "latency_ms_p95": _usage_percentile(row["latency_samples"], 0.95),
                "failure_rate": _usage_rate(item["failed_turns"], item["turns"]),
                "retry_rate": _usage_rate(item["retries"], item["model_calls"]),
            })
            result.append(item)
        result.sort(key=lambda item: (item["total_tokens"] is None, -(item["total_tokens"] or 0), -item["model_calls"], *(item[field] for field in fields)))
        return result

    rendered_totals = render_aggregate(totals)
    cache_read = rendered_totals["cache_read_tokens"]
    cache_miss = rendered_totals["cache_miss_tokens"]
    cache_denominator = cache_read + cache_miss if cache_read is not None and cache_miss is not None else 0
    return {
        "overview": overview, "averages": averages, "daily": daily, "users": users,
        "models": breakdown(("provider", "model")), "lanes": breakdown(("lane",)),
        "filters": {
            "lanes": sorted({str(row["lane"]) for row in option_facts}),
            "providers": sorted({str(row["provider"]) for row in option_facts}),
            "models": sorted({str(row["model"]) for row in option_facts}),
        },
        "coverage": {
            "usage_reported_calls": totals["usage_reported_calls"], "model_calls": totals["model_calls"],
            "usage_coverage": _usage_rate(totals["usage_reported_calls"], totals["model_calls"]),
            "cache_reported_calls": totals["cache_reported_calls"],
            "cache_coverage": _usage_rate(totals["cache_reported_calls"], totals["model_calls"]),
            "cache_hit_ratio": float(cache_read) / cache_denominator if cache_denominator else None,
            "reference_cohort": {
                "basis": "parseable_utc_write_timestamps_at_end_at",
                "unparseable_registered_rows": int(cohort["unparseable_registered_rows"] or 0),
                "legacy_memory_rows_without_valid_created_at": int(cohort["legacy_memory_rows_without_valid_created_at"] or 0),
                "limitation": "legacy users.created_at and memory doc.created_at values that are missing or invalid are excluded from historical registered and activated reference cohorts",
            },
            "rollup": {
                "mode": "hybrid", "refreshed_at": state["refreshed_at"],
                "last_success_at": state["last_success_at"],
                "processed_updated_at": state["source_updated_at"],
                "processed_id": int(state["source_id"]),
                "source_observed_updated_at": state["source_observed_updated_at"],
                "source_lag_seconds": state["source_lag_seconds"],
                "last_error_at": state["last_error_at"], "last_error": state["last_error"],
                "raw_days": [d.isoformat() for d in partition.raw_days],
                "rollup_days": [d.isoformat() for d in partition.rollup_days],
            },
        },
    }


def _usage_merge_parallel_task_results(
    core: dict,
    task_a: dict | None,
    task_b: dict | None,
    latency_lanes: list[dict] | None,
) -> tuple[dict, dict, dict]:
    """Reassemble fixed three-bin reads into the existing report payload."""

    task_a = task_a or {}
    task_b = task_b or {}
    merged_core = dict(core)
    if merged_core.get("distribution") is None:
        merged_core["distribution"] = task_a.get("distribution")
    if merged_core.get("daily") is None:
        merged_core["daily"] = task_b.get("daily")
    dimensions = {
        "models": task_a.get("models"),
        "lanes": (
            task_a["lanes"]
            if task_a.get("lanes") is not None
            else task_b.get("lanes")
        ),
        "primary": task_a.get("primary"),
    }
    latency_bundle = {
        "filters": task_a.get("filters"),
        "latency": {
            "models": task_b.get("latency_models"),
            "lanes": latency_lanes,
        },
    }
    return merged_core, dimensions, latency_bundle


def _usage_report_snapshot_hybrid_parallel(
    query, *, exporter_conn=None
) -> dict | None:
    """Read three balanced aggregate bins from one exported snapshot."""

    if exporter_conn is None:
        with _usage_report_admission() as admitted_conn:
            return _usage_report_snapshot_hybrid_parallel(
                query, exporter_conn=admitted_conn
            )
    conn = exporter_conn
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            cur.execute(
                "SELECT set_config('statement_timeout',%s,true)",
                (str(_USAGE_REPORT_STATEMENT_TIMEOUT_MS),),
            )
            cur.execute(
                "SELECT * FROM v2_usage_rollup_watermarks "
                "WHERE rollup_name='hosted_v2_usage'"
            )
            state = cur.fetchone()
            if state is None or not state["bootstrap_complete"]:
                return None
            partition = usage_reporting.rollup_partition(
                query,
                dirty_from_day=state["dirty_from_day"],
                dirty_through_day=state["dirty_through_day"],
            )
            if partition is None:
                return None
            cur.execute("SELECT pg_export_snapshot()")
            snapshot_id = str(cur.fetchone()["pg_export_snapshot"])
            _usage_snapshot_observer("exported", snapshot_id=snapshot_id)
            importer_deadline = (
                time.monotonic() + _USAGE_REPORT_STATEMENT_TIMEOUT_MS / 1000
            )
            task_a_control = _UsageImporterControl()
            task_b_control = _UsageImporterControl()
            with _UsageImporterExecutor() as executor:
                task_a_future = executor.submit(
                    task_a_control,
                    _usage_import_snapshot,
                    snapshot_id,
                    task_a_control,
                    importer_deadline,
                    _usage_parallel_dimension_rows,
                    query,
                    partition,
                )
                task_b_future = executor.submit(
                    task_b_control,
                    _usage_import_snapshot,
                    snapshot_id,
                    task_b_control,
                    importer_deadline,
                    _usage_parallel_latency_bundle,
                    query,
                    partition,
                )
                core = _usage_parallel_core_rows(cur, query, partition)
                latency_lanes = _usage_optional_pg_section(
                    cur,
                    "latency_lanes",
                    lambda: _usage_parallel_latency_lane_rows(
                        cur, query, partition
                    ),
                )
                cohort = _usage_cohort_on_cursor(cur, query)
                try:
                    dimensions = _usage_importer_result(
                        task_a_future,
                        task_a_control,
                        importer_deadline,
                    )
                except _UsageImporterUnsettled:
                    raise
                except Exception:
                    task_a_future.cancel()
                    log.exception(
                        "usage task A importer unavailable; trying serial"
                    )
                    try:
                        cur.execute(
                            "SELECT set_config('statement_timeout',%s,true)",
                            (str(_usage_remaining_timeout_ms(importer_deadline)),),
                        )
                        with conn.transaction():
                            dimensions = _usage_parallel_dimension_rows(
                                cur, query, partition
                            )
                    except Exception:
                        log.exception(
                            "usage task A serial fallback unavailable"
                        )
                        dimensions = None
                try:
                    latency_bundle = _usage_importer_result(
                        task_b_future,
                        task_b_control,
                        importer_deadline,
                    )
                except _UsageImporterUnsettled:
                    raise
                except Exception:
                    task_b_future.cancel()
                    log.exception(
                        "usage task B importer unavailable; trying serial"
                    )
                    try:
                        cur.execute(
                            "SELECT set_config('statement_timeout',%s,true)",
                            (str(_usage_remaining_timeout_ms(importer_deadline)),),
                        )
                        with conn.transaction():
                            latency_bundle = _usage_parallel_latency_bundle(
                                cur, query, partition
                            )
                    except Exception:
                        log.exception(
                            "usage task B serial fallback unavailable"
                        )
                        latency_bundle = None
            core, dimensions, latency_bundle = _usage_merge_parallel_task_results(
                core, dimensions, latency_bundle, latency_lanes
            )
            return _usage_payload_from_parallel_rows(
                query,
                cohort,
                core,
                dimensions,
                latency_bundle,
                state,
                partition,
            )


def usage_report_snapshot(query) -> dict:
    """Select the exact rollup/raw report path without changing its payload."""

    with _usage_report_admission() as exporter_conn:
        try:
            report = None
            if query.timezone == "Asia/Shanghai":
                report = _usage_report_snapshot_hybrid_parallel(
                    query, exporter_conn=exporter_conn
                )
            if report is None:
                report = _usage_report_snapshot_raw(
                    query, exporter_conn=exporter_conn
                )
                report["coverage"]["rollup"] = _usage_raw_freshness()
            return report
        except _UsageReportAdmissionBusy:
            raise
        except Exception:
            log.exception("usage report unavailable")
            raise


def _usage_raw_freshness() -> dict:
    return {
        "mode": "raw",
        "refreshed_at": None,
        "last_success_at": None,
        "processed_updated_at": None,
        "processed_id": None,
        "source_observed_updated_at": None,
        "source_lag_seconds": None,
        "last_error_at": None,
        "last_error": None,
        "raw_days": None,
        "rollup_days": [],
    }


def _empty_runtime_user_delivery() -> dict:
    return {
        "reply_effects": {
            "applied_in_window": 0,
            "pending": 0,
            "needs_reconciliation": 0,
        },
        "status_effects": {
            "applied_in_window": 0,
            "pending": 0,
            "needs_reconciliation": 0,
        },
        "all_effects": {
            "applied_in_window": 0,
            "discarded_in_window": 0,
            "pending": 0,
            "needs_reconciliation": 0,
        },
        "terminal_failure": {
            "reply_delivered_in_window": 0,
            "reply_undelivered": 0,
            "status_delivered_in_window": 0,
            "status_undelivered": 0,
            "runtime_error_delivered_in_window": 0,
            "runtime_error_undelivered": 0,
        },
        "oldest_unfinished_age_sec": None,
    }


def _merge_runtime_user_delivery(
    ensure_user: Callable[[str], dict],
    effect_window_rows,
    effect_backlog_rows,
    terminal_window_rows,
    terminal_backlog_rows,
) -> None:
    def set_oldest(delivery: dict, age) -> None:
        if age is None:
            return
        current = delivery["oldest_unfinished_age_sec"]
        delivery["oldest_unfinished_age_sec"] = max(
            current if current is not None else 0,
            float(age),
        )

    for row in effect_window_rows:
        delivery = ensure_user(str(row["user_id"]))["delivery"]
        delivery["reply_effects"]["applied_in_window"] = int(
            row["reply_applied_in_window"] or 0
        )
        delivery["status_effects"]["applied_in_window"] = int(
            row["status_applied_in_window"] or 0
        )
        delivery["all_effects"]["applied_in_window"] = int(
            row["all_applied_in_window"] or 0
        )
        delivery["all_effects"]["discarded_in_window"] = int(
            row["all_discarded_in_window"] or 0
        )

    for row in effect_backlog_rows:
        delivery = ensure_user(str(row["user_id"]))["delivery"]
        delivery["reply_effects"]["pending"] = int(row["reply_pending"] or 0)
        delivery["reply_effects"]["needs_reconciliation"] = int(
            row["reply_needs_reconciliation"] or 0
        )
        delivery["status_effects"]["pending"] = int(
            row["status_pending"] or 0
        )
        delivery["status_effects"]["needs_reconciliation"] = int(
            row["status_needs_reconciliation"] or 0
        )
        delivery["all_effects"]["pending"] = int(row["all_pending"] or 0)
        delivery["all_effects"]["needs_reconciliation"] = int(
            row["all_needs_reconciliation"] or 0
        )
        set_oldest(delivery, row["oldest_unfinished_age_sec"])

    for row in terminal_window_rows:
        terminal = ensure_user(str(row["user_id"]))["delivery"][
            "terminal_failure"
        ]
        terminal["reply_delivered_in_window"] = int(
            row["reply_delivered_in_window"] or 0
        )
        terminal["status_delivered_in_window"] = int(
            row["status_delivered_in_window"] or 0
        )
        terminal["runtime_error_delivered_in_window"] = int(
            row["runtime_error_delivered_in_window"] or 0
        )

    for row in terminal_backlog_rows:
        delivery = ensure_user(str(row["user_id"]))["delivery"]
        terminal = delivery["terminal_failure"]
        terminal["reply_undelivered"] = int(row["reply_undelivered"] or 0)
        terminal["status_undelivered"] = int(row["status_undelivered"] or 0)
        terminal["runtime_error_undelivered"] = int(
            row["runtime_error_undelivered"] or 0
        )
        set_oldest(delivery, row["oldest_unfinished_age_sec"])


def _read_runtime_user_delivery_rows(cur, safe_hours: int):
    cur.execute(_RUNTIME_USER_EFFECT_WINDOW_SQL, (safe_hours,))
    effect_window_rows = cur.fetchall()
    cur.execute(_RUNTIME_USER_EFFECT_BACKLOG_SQL)
    effect_backlog_rows = cur.fetchall()
    cur.execute(_RUNTIME_USER_TERMINAL_WINDOW_SQL, (safe_hours,))
    terminal_window_rows = cur.fetchall()
    cur.execute(_RUNTIME_USER_TERMINAL_BACKLOG_SQL)
    terminal_backlog_rows = cur.fetchall()
    return (
        effect_window_rows,
        effect_backlog_rows,
        terminal_window_rows,
        terminal_backlog_rows,
    )


def recent_runtime_user_delivery_report(*, within_hours: int = 24) -> dict:
    """Read only per-user delivery reliability; never scan turn metrics."""
    safe_hours = max(1, min(int(within_hours), 24 * 366))

    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                delivery_rows = _read_runtime_user_delivery_rows(
                    cur, safe_hours
                )

    users: dict[str, dict] = {}

    def ensure_user(user_id: str) -> dict:
        return users.setdefault(
            user_id,
            {
                "user_id": user_id,
                "delivery": _empty_runtime_user_delivery(),
            },
        )

    _merge_runtime_user_delivery(ensure_user, *delivery_rows)
    return {
        "window_hours": safe_hours,
        "users": sorted(users.values(), key=lambda row: row["user_id"]),
    }


def recent_runtime_user_report(*, within_hours: int = 24) -> dict:
    """Legacy mixed token/model + delivery report kept for compatibility."""
    safe_hours = max(1, min(int(within_hours), 24 * 366))

    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                cur.execute(
                    "SELECT COALESCE(user_id, 'unknown') AS user_id,"
                    "  COALESCE(NULLIF(provider, ''), 'unknown') AS provider,"
                    "  COALESCE(NULLIF(model, ''), 'unknown') AS model,"
                    "  COALESCE(NULLIF(cache_route_fingerprint, ''), 'unknown') AS route,"
                    "  array_agg("
                    "    DISTINCT COALESCE(NULLIF(lane, ''), 'unknown')"
                    "    ORDER BY COALESCE(NULLIF(lane, ''), 'unknown')"
                    "  ) AS lanes,"
                    "  count(*)::int AS turns,"
                    "  coalesce(sum(model_calls), 0)::bigint AS model_calls,"
                    "  coalesce(sum(retries), 0)::bigint AS retries,"
                    "  coalesce(sum(usage_reported_calls), 0)::bigint"
                    "    AS usage_reported_calls,"
                    "  coalesce(sum(cache_reported_calls), 0)::bigint"
                    "    AS cache_reported_calls,"
                    "  sum(prompt_tokens)::bigint AS prompt_tokens,"
                    "  sum(completion_tokens)::bigint AS completion_tokens,"
                    "  sum(cache_read_tokens)::bigint AS cache_read_tokens,"
                    "  sum(cache_write_tokens)::bigint AS cache_write_tokens,"
                    "  sum(cache_miss_tokens)::bigint AS cache_miss_tokens "
                    "FROM v2_turn_metrics "
                    "WHERE created_at >= now() - make_interval(hours => %s) "
                    "GROUP BY COALESCE(user_id, 'unknown'), provider, model, "
                    "cache_route_fingerprint",
                    (safe_hours,),
                )
                rows = cur.fetchall()
                delivery_rows = _read_runtime_user_delivery_rows(
                    cur, safe_hours
                )

    def _optional_int(row, key):
        value = row.get(key)
        return int(value) if value is not None else None

    def _known_total_sort_key(total, calls, identity):
        return (
            total is None,
            -(int(total) if total is not None else 0),
            -int(calls or 0),
            identity,
        )

    users: dict[str, dict] = {}

    def _delivery_user(user_id: str) -> dict:
        return users.setdefault(
            user_id,
            {
                "user_id": user_id,
                "known_total_tokens": None,
                "model_calls": 0,
                "models": [],
                "delivery": _empty_runtime_user_delivery(),
            },
        )

    for row in rows:
        model_calls = int(row["model_calls"] or 0)
        usage_calls = int(row["usage_reported_calls"] or 0)
        cache_calls = int(row["cache_reported_calls"] or 0)
        prompt_tokens = _optional_int(row, "prompt_tokens")
        completion_tokens = _optional_int(row, "completion_tokens")
        cache_read = _optional_int(row, "cache_read_tokens")
        cache_write = _optional_int(row, "cache_write_tokens")
        cache_miss = _optional_int(row, "cache_miss_tokens")
        total_tokens = (
            prompt_tokens + completion_tokens
            if prompt_tokens is not None and completion_tokens is not None
            else None
        )
        cache_denominator = (
            cache_read + cache_miss
            if cache_read is not None and cache_miss is not None
            else 0
        )
        user_id = str(row["user_id"])
        user = _delivery_user(user_id)
        user["models"].append({
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "route": str(row["route"]),
            "lanes": list(row["lanes"]),
            "turns": int(row["turns"] or 0),
            "model_calls": model_calls,
            "retries": int(row["retries"] or 0),
            "usage_reported_calls": usage_calls,
            "cache_reported_calls": cache_calls,
            "usage_coverage": (
                float(usage_calls) / float(model_calls) if model_calls else None
            ),
            "cache_coverage": (
                float(cache_calls) / float(model_calls) if model_calls else None
            ),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_miss_tokens": cache_miss,
            "cache_hit_ratio": (
                float(cache_read) / float(cache_denominator)
                if cache_denominator
                else None
            ),
        })
        user["model_calls"] += model_calls
        if total_tokens is not None:
            user["known_total_tokens"] = (
                (user["known_total_tokens"] or 0) + total_tokens
            )

    _merge_runtime_user_delivery(_delivery_user, *delivery_rows)

    users_list = list(users.values())
    for user in users_list:
        user["models"].sort(
            key=lambda model: _known_total_sort_key(
                model["total_tokens"],
                model["model_calls"],
                (model["provider"], model["model"], model["route"]),
            )
        )
    users_list.sort(
        key=lambda user: _known_total_sort_key(
            user["known_total_tokens"], user["model_calls"], user["user_id"]
        )
    )
    return {"window_hours": safe_hours, "users": users_list}


def recent_delivery_health(*, within_hours: int = 24) -> dict:
    """端到端交付健康（content-free），喂 admin 值班台。

    补的是 2026-07-30 审计里最实的一个盲区：``agent_jobs`` 判 ``completed`` 只
    证明**回合跑完了**，不证明它的产物到达了用户。副作用走 ``v2_effect_outbox``
    落库后异步 apply，用户可见的终态失败走 ``v2_terminal_failure_outbox`` 投递；
    这两条队列堵住时，job 结局层面一切正常——页面此前照样报绿。

    三块的窗口语义**刻意不同**，别当成一套：

    * ``effect_outbox`` / ``terminal_failure_outbox`` 是**当前积压状态量**，不受
      ``within_hours`` 约束（和 ``pool.pending`` 同类）。一条三天前就该 apply 的
      effect 如果还堵着，那是现在的故障，不该因为窗口切到 24h 就消失。
    * ``mcp_mutation`` 是**窗口内计数**：远端 mutation 的结果未知是一次性事件，
      过去某天出过一次不该永久点亮值班台。

    ``unknown`` 与 ``unresolved`` 分开：前者是**已判定**结果不可知（远端可能已经
    改了数据，我们不知道），后者是 ``resolved_at IS NULL`` 的悬空记录（进程死在
    判定之前）。两者都需要人看，但含义不同，合并计数会掩盖后者。
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT COUNT(*)::int AS pending,"
                "  EXTRACT(EPOCH FROM (clock_timestamp()-MIN(created_at))) "
                "    AS oldest_pending_age_sec "
                "FROM v2_effect_outbox WHERE status='pending'"
            )
            effect_row = cur.fetchone() or {}

            # 两种投递义务各自独立退避、各自独立标记完成，所以分别计数；年龄取
            # 两者中最老的未完成行——值班只需要一个「堵了多久」。
            cur.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE status_delivered_at IS NULL)::int "
                "    AS status_undelivered,"
                "  COUNT(*) FILTER (WHERE runtime_error_delivered_at IS NULL)::int "
                "    AS runtime_error_undelivered,"
                "  EXTRACT(EPOCH FROM (clock_timestamp()-MIN(created_at) FILTER ("
                "    WHERE status_delivered_at IS NULL "
                "      OR runtime_error_delivered_at IS NULL))) "
                "    AS oldest_undelivered_age_sec "
                "FROM v2_terminal_failure_outbox"
            )
            failure_row = cur.fetchone() or {}

            cur.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE outcome='unknown')::int AS unknown,"
                "  COUNT(*) FILTER (WHERE outcome IS NULL "
                "    AND resolved_at IS NULL)::int AS unresolved "
                "FROM v2_mcp_mutation_attempts "
                "WHERE started_at >= now() - make_interval(hours => %s)",
                (safe_hours,),
            )
            mutation_row = cur.fetchone() or {}

    def _age(row, key):
        value = row.get(key)
        return float(value) if value is not None else None

    return {
        "window_hours": safe_hours,
        "effect_outbox": {
            "pending": int(effect_row.get("pending") or 0),
            "oldest_pending_age_sec": _age(effect_row, "oldest_pending_age_sec"),
        },
        "terminal_failure_outbox": {
            "status_undelivered": int(failure_row.get("status_undelivered") or 0),
            "runtime_error_undelivered": int(
                failure_row.get("runtime_error_undelivered") or 0
            ),
            "oldest_undelivered_age_sec": _age(
                failure_row, "oldest_undelivered_age_sec"
            ),
        },
        "mcp_mutation": {
            "unknown": int(mutation_row.get("unknown") or 0),
            "unresolved": int(mutation_row.get("unresolved") or 0),
        },
    }


def recent_prompt_cache_stats(
    *,
    lane: str = "chat",
    limit: int = 100,
    provider: str | None = None,
    model: str | None = None,
    cache_route_fingerprint: str | None = None,
    user_id: str | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
    include_turns: bool = False,
) -> dict:
    """Aggregate honest prompt-cache telemetry over the most recent turns.

    Token totals remain ``None`` when no sampled provider call reported that
    field.  Coverage ratios make partial provider telemetry explicit instead
    of converting unknown cache behavior into a false miss or hit.
    """
    provider_filter = str(provider or "").strip().lower() or None
    model_filter = str(model or "").strip() or None
    route_filter = str(cache_route_fingerprint or "").strip() or None
    user_filter = str(user_id or "").strip() or None
    where = ["lane=%s"]
    params: list = [str(lane)]
    if provider_filter is not None:
        where.append("provider=%s")
        params.append(provider_filter)
    if model_filter is not None:
        where.append("model=%s")
        params.append(model_filter)
    if route_filter is not None:
        where.append("cache_route_fingerprint=%s")
        params.append(route_filter)
    if user_filter is not None:
        where.append("user_id=%s")
        params.append(user_filter)
    if since_ts is not None:
        where.append("created_at >= to_timestamp(%s)")
        params.append(float(since_ts))
    if until_ts is not None:
        where.append("created_at <= to_timestamp(%s)")
        params.append(float(until_ts))
    params.append(max(1, min(int(limit), 1000)))

    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "WITH recent AS (SELECT id, job_id, created_at, model_calls, retries, "
                "usage_reported_calls, cache_reported_calls, prompt_tokens, "
                "cache_read_tokens, cache_write_tokens, cache_miss_tokens, "
                "cache_route_fingerprint, provider, model, failed, status "
                "FROM v2_turn_metrics "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY created_at DESC, id DESC LIMIT %s), "
                "aggregate AS (SELECT count(*), coalesce(sum(model_calls), 0), "
                "coalesce(sum(usage_reported_calls), 0), "
                "coalesce(sum(cache_reported_calls), 0), "
                "sum(prompt_tokens), sum(cache_read_tokens), "
                "sum(cache_write_tokens), sum(cache_miss_tokens), "
                "count(cache_route_fingerprint), "
                "count(DISTINCT cache_route_fingerprint), "
                "CASE WHEN count(DISTINCT cache_route_fingerprint)=1 "
                "THEN min(cache_route_fingerprint) ELSE NULL END FROM recent) "
                "SELECT aggregate.*, COALESCE((SELECT jsonb_agg(jsonb_build_object("
                "'job_id', job_id, "
                "'created_at_ts', EXTRACT(EPOCH FROM created_at)::double precision, "
                "'model_calls', model_calls, "
                "'retries', retries, "
                "'usage_reported_calls', usage_reported_calls, "
                "'cache_reported_calls', cache_reported_calls, "
                "'prompt_tokens', prompt_tokens, "
                "'cache_read_tokens', cache_read_tokens, "
                "'cache_write_tokens', cache_write_tokens, "
                "'cache_miss_tokens', cache_miss_tokens, "
                "'provider', provider, 'model', model, "
                "'cache_route_fingerprint', cache_route_fingerprint, "
                "'failed', failed, 'status', status) "
                "ORDER BY created_at ASC, id ASC) FROM recent), '[]'::jsonb) "
                "FROM aggregate",
                tuple(params),
            )
            row = cur.fetchone()

    sampled_turns = int(row[0] or 0) if row is not None else 0
    model_calls = int(row[1] or 0) if row is not None else 0
    usage_calls = int(row[2] or 0) if row is not None else 0
    cache_calls = int(row[3] or 0) if row is not None else 0

    def _optional_int(index: int) -> int | None:
        if row is None or row[index] is None:
            return None
        return int(row[index])

    prompt_tokens = _optional_int(4)
    cache_read = _optional_int(5)
    cache_write = _optional_int(6)
    cache_miss = _optional_int(7)
    route_identified_turns = int(row[8] or 0) if row is not None else 0
    route_fingerprint_count = int(row[9] or 0) if row is not None else 0
    sole_route_fingerprint = (
        str(row[10]) if row is not None and row[10] is not None else None
    )
    effective_input = None
    if cache_calls and cache_read is not None and cache_miss is not None:
        # Writes are a subset of the miss side for providers with explicit
        # cache creation (not an additional input category).  Adding writes
        # here would double-count Anthropic/OpenAI cache creation tokens.
        effective_input = cache_read + cache_miss
    hit_ratio = None
    if effective_input:
        hit_ratio = float(cache_read or 0) / float(effective_input)
    result = {
        "sampled_turns": sampled_turns,
        "model_calls": model_calls,
        "usage_reported_calls": usage_calls,
        "cache_reported_calls": cache_calls,
        "usage_telemetry_coverage": (
            float(usage_calls) / float(model_calls) if model_calls else None
        ),
        "cache_telemetry_coverage": (
            float(cache_calls) / float(model_calls) if model_calls else None
        ),
        "route_identity_coverage": (
            float(route_identified_turns) / float(sampled_turns)
            if sampled_turns
            else None
        ),
        "route_fingerprint_count": route_fingerprint_count,
        "route_fingerprint": sole_route_fingerprint,
        "prompt_tokens": prompt_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cache_miss_tokens": cache_miss,
        "effective_input_tokens": effective_input,
        "hit_ratio": hit_ratio,
    }
    if include_turns:
        turns = row[11] if row is not None and len(row) > 11 else []
        result["turns"] = list(turns) if isinstance(turns, list) else []
    if include_turns or any(
        value is not None
        for value in (
            provider_filter,
            model_filter,
            route_filter,
            user_filter,
            since_ts,
            until_ts,
        )
    ):
        result["filter"] = {
            "provider": provider_filter,
            "model": model_filter,
            "cache_route_fingerprint": route_filter,
            "user_id": user_filter,
            "since_ts": float(since_ts) if since_ts is not None else None,
            "until_ts": float(until_ts) if until_ts is not None else None,
            "include_turns": bool(include_turns),
        }
    return result


WAKE_LANES = ("heartbeat", "scheduled", "manual_wake")


_WAKE_SHADOW_LANES = frozenset(
    {"heartbeat", "scheduled", "manual_wake", "screen_watch"}
)
_WAKE_SHADOW_RETENTION_DAYS = 90


def record_wake_shadow_decision(
    *,
    job_id: int,
    local_day: date | str,
    local_hour: int,
    local_minute: int,
    lane: str,
    decision_allowed: bool,
    apns_alert_sent: bool,
    decided_at: datetime | float,
) -> bool:
    """Persist one immutable, content-free A′ wake observation.

    The source job id is the idempotency key, so replays cannot inflate Seven's
    counts. The row deliberately survives source-job cleanup and owns a bounded
    90-day retention window. This write happens only after the wake decision;
    no return value is allowed to feed back into runtime policy.
    """
    normalized_lane = str(lane or "").strip()
    if normalized_lane not in _WAKE_SHADOW_LANES:
        raise ValueError("invalid wake shadow lane")
    if not isinstance(decision_allowed, bool) or not isinstance(
        apns_alert_sent, bool
    ):
        raise ValueError("wake shadow outcomes must be booleans")
    if apns_alert_sent and not decision_allowed:
        raise ValueError("a suppressed wake cannot send an APNs alert")
    hour = int(local_hour)
    minute = int(local_minute)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid wake shadow local time")
    day = (
        local_day.date()
        if isinstance(local_day, datetime)
        else (
            local_day
            if isinstance(local_day, date)
            else date.fromisoformat(str(local_day))
        )
    )
    observed_at = (
        decided_at
        if isinstance(decided_at, datetime)
        else datetime.fromtimestamp(float(decided_at), tz=timezone.utc)
    )
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    with db.get_pool().connection() as conn:
        # This observation owns its retention. It deliberately does not depend
        # on agent_jobs lifetime, so a 90-day report remains meaningful even if
        # queue rows gain a shorter GC policy later.
        conn.execute(
            "DELETE FROM v2_wake_shadow_decisions "
            "WHERE recorded_at < now() - (%s * interval '1 day')",
            (_WAKE_SHADOW_RETENTION_DAYS,),
        )
        row = conn.execute(
            "INSERT INTO v2_wake_shadow_decisions "
            "(job_id,local_day,local_hour,local_minute,lane,"
            "decision_allowed,apns_alert_sent,decided_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (job_id) DO NOTHING RETURNING job_id",
            (
                int(job_id),
                day,
                hour,
                minute,
                normalized_lane,
                decision_allowed,
                apns_alert_sent,
                observed_at.astimezone(timezone.utc),
            ),
        ).fetchone()
    return row is not None


def wake_shadow_report(
    *,
    days: int,
    bucket_start_hour: int,
    bucket_end_hour: int,
    through_day: date | str | None = None,
) -> dict:
    """Count A′ observations in a caller-defined local-hour bucket.

    The bucket is report input, not a runtime sleep-window constant.  A range
    with start > end crosses midnight (for example 23→7); equal endpoints are
    rejected instead of silently defining either zero or twenty-four hours.
    """
    bounded_days = int(days)
    start_hour = int(bucket_start_hour)
    end_hour = int(bucket_end_hour)
    if not 1 <= bounded_days <= 90:
        raise ValueError("days must be between 1 and 90")
    if not 0 <= start_hour <= 23 or not 0 <= end_hour <= 23:
        raise ValueError("bucket hours must be between 0 and 23")
    if start_hour == end_hour:
        raise ValueError("bucket hours must differ")
    end_day = (
        date.today()
        if through_day is None
        else (
            through_day
            if isinstance(through_day, date)
            else date.fromisoformat(str(through_day))
        )
    )
    start_day = end_day - timedelta(days=bounded_days - 1)
    if start_hour < end_hour:
        bucket_sql = "(local_hour >= %s AND local_hour < %s)"
    else:
        bucket_sql = "(local_hour >= %s OR local_hour < %s)"
    base_sql = (
        "WITH selected AS ("
        " SELECT lane,decision_allowed,apns_alert_sent," + bucket_sql + " AS in_bucket"
        " FROM v2_wake_shadow_decisions WHERE local_day BETWEEN %s AND %s"
        ") "
    )
    count_sql = (
        "count(*)::bigint AS total_decisions,"
        "count(*) FILTER (WHERE decision_allowed)::bigint AS allowed,"
        "count(*) FILTER (WHERE NOT decision_allowed)::bigint AS suppressed,"
        "count(*) FILTER (WHERE apns_alert_sent)::bigint AS apns_alert_sent,"
        "count(*) FILTER (WHERE decision_allowed AND in_bucket)::bigint "
        "AS bucket_allowed,"
        "count(*) FILTER "
        "(WHERE decision_allowed AND in_bucket AND apns_alert_sent)::bigint "
        "AS bucket_allowed_apns_alert_sent"
    )
    params = (start_hour, end_hour, start_day, end_day)
    with db.get_pool().connection() as conn:
        total = conn.execute(
            base_sql + "SELECT " + count_sql + " FROM selected",
            params,
        ).fetchone()
        lane_rows = conn.execute(
            base_sql
            + "SELECT lane,"
            + count_sql
            + " FROM selected GROUP BY lane ORDER BY lane",
            params,
        ).fetchall()

    def render(row, *, offset: int = 0) -> dict:
        return {
            "total_decisions": int(row[offset] or 0),
            "allowed": int(row[offset + 1] or 0),
            "suppressed": int(row[offset + 2] or 0),
            "apns_alert_sent": int(row[offset + 3] or 0),
            "bucket_allowed": int(row[offset + 4] or 0),
            "bucket_allowed_apns_alert_sent": int(row[offset + 5] or 0),
        }

    return {
        "days": bounded_days,
        "start_day": start_day.isoformat(),
        "end_day": end_day.isoformat(),
        "bucket": {
            "start_hour_inclusive": start_hour,
            "end_hour_exclusive": end_hour,
            "crosses_midnight": start_hour > end_hour,
            "purpose": "observation_only_not_product_policy",
        },
        **render(total),
        "by_lane": {
            str(row[0]): render(row, offset=1)
            for row in lane_rows
        },
    }


def wake_success_stats(*, within_hours: int = 24) -> dict:
    """V2 唤醒（proactive wake）成功率——独立于 legacy `proactive_jobs` 流。V2 的
    heartbeat/scheduled/manual_wake 唤醒全走 agent_jobs，从不写那张旧表，所以这是
    一条全新的、只读 agent_jobs 的口径，不是去修 legacy daily-report 那条查询。

    地雷1（legacy daily-report 曾踩过的坑，此处照抄同一原则）：wake job 的
    `completed` 终态本身就是成功——即使这一轮唤醒判断"这次不用发消息"（silence 是
    D3 设计里的合法结果），也照样落 status='completed'，必须计入成功，不能因为没发
    消息就当失败去拉低成功率。只有 `failed`（真错误：provider 异常/校验失败）和
    `expired`（reaper 判定卡死回收）计入失败侧的分母。

    返回 {"completed": int, "failed": int, "expired": int,
          "success_rate": float in [0,1] | None（三者之和为 0 时，无历史可算）,
          "by_lane": {lane: {status: count}}}（by_lane 只含窗口内出现过的 lane/status
    组合，无活动的 lane 不会出现空条目）。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lane, status, count(*) FROM agent_jobs "
                "WHERE lane IN ('heartbeat','scheduled','manual_wake') "
                "AND finished_at IS NOT NULL "
                "AND finished_at > now() - make_interval(hours => %s) "
                "GROUP BY lane, status",
                (int(within_hours),),
            )
            rows = cur.fetchall()
    completed = failed = expired = 0
    by_lane: dict[str, dict[str, int]] = {}
    for lane, status, count in rows:
        count = int(count)
        by_lane.setdefault(lane, {})[status] = count
        if status == "completed":
            completed += count
        elif status == "failed":
            failed += count
        elif status == "expired":
            expired += count
    denom = completed + failed + expired
    return {
        "completed": completed,
        "failed": failed,
        "expired": expired,
        "success_rate": (completed / denom) if denom else None,
        "by_lane": by_lane,
    }


def memory_lane_health(*, within_hours: int = 24) -> dict:
    """记忆车道（capture 落卡 / dream 整理）的舰队级健康度。

    为什么单独一条而不是塞进 ``wake_success_stats`` 的 lane 列表：这两条车道不是
    唤醒。混进去会让"做梦大面积失败"表现为"唤醒成功率下降"，把排查的人引去查唤醒
    ——同一个数字承载两件事，就是 TESTING §2-N 那条口径漂移。

    补这条的由来（2026-07-31）：用户报"切到 V2 后晚上不整理记忆了"，而当时
    ``/v1/admin/v2-metrics`` 里**没有任何记忆车道的数字**，只能靠逐个用户比对
    ``bootstrap_events`` 里最后一次写卡时间才看出 4 个 V2 用户里 3 个自切换起再没
    写过卡。那个方法只在"已经知道该怀疑谁"时管用；**没有用户报障的话，记忆整理
    全线停摆我们也发现不了**——它坏掉的样子就是"什么都没发生"，和"今天没什么可记
    的"长得一模一样。

    与 wake 同一条判据（照抄那边的地雷1）：``completed`` 就是成功，**即使这一轮
    一张卡都没写**。capture 跑完发现没什么值得记是合法结果（noop），不能当失败去
    拉低成功率；只有 ``failed``（解析/provider 真错误）和 ``expired``（reaper 判定
    卡死回收）计入失败侧。

    返回形状与 ``wake_success_stats`` 一致，便于并排读。

    ``failed_reasons``（2026-08-05 dream 阀门重构）：失败侧按 ``last_error`` 首段
    细分。dream 的出口闸从「按提案静默丢」改成了「明显不对就让整个 job 失败」
    （``dream_blast_radius_exceeded`` / ``invalid_card_content*``），不细分的话
    「保险丝在熔断」和「provider 在挂」在成功率上长得一模一样——阀门必须有刻度。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lane, status, count(*) FROM agent_jobs "
                "WHERE lane IN ('capture','dream') "
                "AND finished_at IS NOT NULL "
                "AND finished_at > now() - make_interval(hours => %s) "
                "GROUP BY lane, status",
                (int(within_hours),),
            )
            rows = cur.fetchall()
            cur.execute(
                # last_error 本身就是脱敏短码(extraction_failed:xxx),整串聚合
                # 才能把 dream_blast_radius_exceeded 和 provider 挂掉分开看。
                "SELECT lane, "
                "COALESCE(NULLIF(left(last_error, 120), ''), 'unknown'), "
                "count(*) FROM agent_jobs "
                "WHERE lane IN ('capture','dream') AND status='failed' "
                "AND finished_at IS NOT NULL "
                "AND finished_at > now() - make_interval(hours => %s) "
                "GROUP BY 1, 2",
                (int(within_hours),),
            )
            reason_rows = cur.fetchall()
    completed = failed = expired = 0
    by_lane: dict[str, dict[str, int]] = {}
    for lane, status, count in rows:
        count = int(count)
        by_lane.setdefault(lane, {})[status] = count
        if status == "completed":
            completed += count
        elif status == "failed":
            failed += count
        elif status == "expired":
            expired += count
    failed_reasons: dict[str, dict[str, int]] = {}
    for lane, reason, count in reason_rows:
        failed_reasons.setdefault(lane, {})[str(reason)] = int(count)
    denom = completed + failed + expired
    return {
        "completed": completed,
        "failed": failed,
        "expired": expired,
        "success_rate": (completed / denom) if denom else None,
        "by_lane": by_lane,
        "failed_reasons": failed_reasons,
    }


def pending_job_count() -> int:
    """Count pending jobs that are ready for a worker to claim now."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM agent_jobs WHERE status='pending' "
                "AND available_at <= clock_timestamp()"
            )
            return int(cur.fetchone()[0])


def get_summary_row(user_id) -> dict | None:
    """读取该用户当前的会话摘要行（若存在）。返回
    {"summary_envelope": dict|None, "watermark_ts": float, "version": int,
    "watermark_seq": int}，无行返回 None（该用户从未压缩过）。

    ``watermark_seq``（D5/Task 9，migration 0031）与 ``watermark_ts`` 同一行
    并存：新压缩写入的行两者都是真值（见 ``upsert_summary_row_cas``）；但
    0031 之前就存在的行只有 ``watermark_ts``、``watermark_seq`` 落着迁移
    默认值 0。这里做一次性懒翻译——``watermark_seq==0`` 但
    ``watermark_ts>0`` 时，用 ``db.seq_for_watermark_ts``（保守、
    strictly-less）现算一个替身返回，不回写库（下一次真正压缩发生时才会
    落一个精确值）。选在读侧做而不是把翻译推给每个调用方，是因为
    ``get_summary_row`` 只有一个实现、调用方（当前的 ``_read_summary``、
    未来 Task 10 的 prompt-invariant 边界读取）都自动拿到一致语义，不用人人
    记得先查有没有 watermark_seq 再翻译一次。"""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT summary_envelope, watermark_ts, version, watermark_seq,"
                "materialized_segment_ids "
                "FROM v2_conversation_summary WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    watermark_ts = float(row["watermark_ts"])
    watermark_seq = int(row["watermark_seq"] or 0)
    materialized_segment_ids = tuple(
        int(value) for value in (row["materialized_segment_ids"] or [])
    )
    if watermark_seq == 0 and watermark_ts > 0 and not materialized_segment_ids:
        watermark_seq = db.seq_for_watermark_ts(user_id, watermark_ts)
    return {
        "summary_envelope": dict(row["summary_envelope"])
        if row["summary_envelope"] is not None
        else None,
        "watermark_ts": watermark_ts,
        "version": int(row["version"]),
        "watermark_seq": watermark_seq,
        "materialized_segment_ids": materialized_segment_ids,
    }


def upsert_summary_row_cas(
    user_id,
    *,
    summary_envelope: dict,
    watermark_ts: float,
    expected_version: int,
    watermark_seq: int | None = None,
    require_source_row: bool = False,
) -> bool:
    """compare-and-swap 写入该用户的会话摘要行。expected_version==0 走首建
    （INSERT ... ON CONFLICT DO NOTHING，若行已存在说明输了竞态，返回 False）；
    否则走 UPDATE ... WHERE version=expected_version（不匹配说明摘要在别处已被
    推进，本次写入是过期/丢失的 CAS，返回 False）。成功返回 True。

    ``watermark_seq``（D5/Task 9）与 ``watermark_ts`` 在同一次 CAS 写入里
    原子推进——同一行、同一个 UPDATE 语句，不是两次写。``None``（默认，
    向后兼容旧调用方/旧测试 fixture 未传这个参数的场景）在 UPDATE 分支用
    ``COALESCE`` 保留该行原有的 watermark_seq（不清零、不误伤未真正推进
    seq 的调用方）；INSERT 分支没有旧值可留，落 0（与迁移 0031 的列默认值
    一致）。"""
    is_cas_update = int(expected_version) != 0
    success = False
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                if require_source_row and (
                    watermark_seq is None or int(watermark_seq) <= 0
                ):
                    return False
                if require_source_row:
                    # The exact high-watermark row is the source-validity
                    # witness.  Clear removes it under the exclusive fence, so
                    # a paused pre-clear compactor cannot publish its encrypted
                    # summary over a new empty/post-clear conversation.
                    cur.execute(
                        "SELECT 1 FROM chat_messages "
                        "WHERE user_id=%s AND seq=%s",
                        (user_id, int(watermark_seq)),
                    )
                    if cur.fetchone() is None:
                        return False
                if int(expected_version) == 0:
                    cur.execute(
                        "INSERT INTO v2_conversation_summary "
                        "(user_id, summary_envelope, watermark_ts, version, watermark_seq) "
                        "VALUES (%s, %s, %s, 1, %s) "
                        "ON CONFLICT (user_id) DO NOTHING",
                        (
                            user_id,
                            Jsonb(dict(summary_envelope or {})),
                            float(watermark_ts),
                            int(watermark_seq or 0),
                        ),
                    )
                else:
                    cur.execute(
                        "UPDATE v2_conversation_summary "
                        "SET summary_envelope=%s, watermark_ts=%s, version=version+1, "
                        "watermark_seq=COALESCE(%s, watermark_seq), updated_at=now() "
                        "WHERE user_id=%s AND version=%s",
                        (
                            Jsonb(dict(summary_envelope or {})),
                            float(watermark_ts),
                            int(watermark_seq) if watermark_seq is not None else None,
                            user_id,
                            int(expected_version),
                        ),
                    )
                success = cur.rowcount == 1
    if success and is_cas_update:
        # Same-PK in-place rewrite (the row already existed, this CAS just
        # advanced its version) — the append-only replicator cursor never
        # revisits it. The INSERT branch above (expected_version==0, first
        # creation) needs no requeue: it is a brand-new PK the forward cursor
        # scan will pick up normally.
        from tee_shadow import mirror
        mirror.mark_pending(str(user_id), "v2_conversation_summary", str(user_id), "requeue")
    return success


def get_summary_frontier_state(user_id) -> dict | None:
    """Return the immutable canonical summary cover plus exact DB witnesses.

    Checkpoints coexist with their retained children.  A child is absent from
    the canonical cover only when a higher-level segment wholly contains its
    range.  Rows beyond the committed head watermark are never considered,
    which keeps a read that raced a later leaf append self-consistent.

    The returned envelopes are still encrypted.  ``first_source_seq`` and
    ``covered_source_count`` are content-free witnesses computed from retained
    ``chat_messages``; the assembly layer validates them after decrypting the
    canonical nodes and fails closed on any mismatch.
    """
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                # One repeatable-read snapshot binds the head/version/materialized
                # IDs to the canonical rows and source witnesses. A checkpoint
                # committing between separate reads must not manufacture a false
                # integrity failure on an otherwise healthy live turn.
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                cur.execute(
                    "SELECT summary_envelope,watermark_ts,version,watermark_seq,"
                    "materialized_segment_ids,EXISTS(SELECT 1 FROM "
                    "v2_conversation_summary_segments s WHERE s.user_id=%s) "
                    "AS has_segment_rows FROM v2_conversation_summary "
                    "WHERE user_id=%s",
                    (user_id, user_id),
                )
                raw_head = cur.fetchone()
                if raw_head is None:
                    return None
                watermark_ts = float(raw_head["watermark_ts"])
                watermark_seq = int(raw_head["watermark_seq"] or 0)
                raw_materialized_ids = tuple(
                    int(value)
                    for value in (raw_head["materialized_segment_ids"] or [])
                )
                if (
                    watermark_seq == 0
                    and watermark_ts > 0
                    and not raw_materialized_ids
                ):
                    cur.execute(
                        "SELECT COALESCE(MAX(seq),0) AS seq FROM chat_messages "
                        "WHERE user_id=%s AND ts<%s",
                        (user_id, watermark_ts),
                    )
                    watermark_seq = int(cur.fetchone()["seq"] or 0)
                head = {
                    "summary_envelope": dict(raw_head["summary_envelope"])
                    if raw_head["summary_envelope"] is not None
                    else None,
                    "watermark_ts": watermark_ts,
                    "version": int(raw_head["version"]),
                    "watermark_seq": watermark_seq,
                    "materialized_segment_ids": raw_materialized_ids,
                    "has_segment_rows": bool(raw_head["has_segment_rows"]),
                }
                cur.execute(
                    "WITH eligible AS ("
                    " SELECT s.* FROM v2_conversation_summary_segments s "
                    " WHERE s.user_id=%s AND s.end_seq<=%s"
                    "), canonical AS ("
                    " SELECT child.* FROM eligible child "
                    " WHERE NOT EXISTS ("
                    "   SELECT 1 FROM eligible parent "
                    "   WHERE parent.level>child.level "
                    "     AND parent.start_seq<=child.start_seq "
                    "     AND parent.end_seq>=child.end_seq"
                    " )"
                    ") SELECT segment_id,format_version,coverage_kind,level,"
                    "start_seq,end_seq,source_message_count,"
                    "legacy_opaque_through_seq,child_segment_ids,summary_envelope "
                    "FROM canonical ORDER BY start_seq,end_seq,level DESC",
                    (user_id, watermark_seq),
                )
                segment_rows = [dict(row) for row in cur.fetchall()]
                opaque_through = max(
                    (
                        int(row.get("legacy_opaque_through_seq") or 0)
                        for row in segment_rows
                        if row.get("coverage_kind") == "legacy_opaque"
                    ),
                    default=0,
                )
                if segment_rows:
                    cur.execute(
                        "SELECT COALESCE(MIN(seq),0) AS first_seq,"
                        "COUNT(*) AS source_count FROM chat_messages "
                        "WHERE user_id=%s AND seq>%s AND seq<=%s "
                        # GC-able synthetic rows (verify_ping/resident_maintenance,
                        # see db.chat_messages_after_seq) never enter the fold, so
                        # they must be excluded from the canonical witness too —
                        # otherwise deleting one leaves the leaf's frozen source
                        # count above this live count and every later turn fails
                        # validate_canonical_frontier.
                        "AND COALESCE(doc->>'source','') "
                        "NOT IN ('verify_ping','resident_maintenance')",
                        (user_id, opaque_through, watermark_seq),
                    )
                    witness = cur.fetchone()
                else:
                    # Pre-segmentation heads are intentionally accepted as
                    # opaque until lazy seeding. Their old watermark cannot
                    # supply an exact source-count proof, and a full-history
                    # COUNT on every ordinary rollout turn would be pure waste.
                    witness = {"first_seq": 0, "source_count": 0}
    return {
        **head,
        "segments": segment_rows,
        "first_source_seq": int(witness["first_seq"]),
        "covered_source_count": int(witness["source_count"]),
    }


def append_summary_leaf_cas(
    user_id,
    *,
    summary_envelope: dict,
    head_summary_envelope: dict | None = None,
    start_seq: int,
    end_seq: int,
    source_message_count: int,
    watermark_ts: float,
    expected_version: int,
    previous_watermark_seq: int,
) -> bool:
    """Append one immutable level-0 segment and advance the head atomically.

    The previous single-blob row remains the CAS/watermark head.  On the first
    segmented write, any existing legacy encrypted blob is copied verbatim into
    one retained level-0 segment covering its already-committed source range.
    Subsequent segment rows are never rewritten.  The head envelope is a
    bounded, rollback-compatible materialized prompt view bound atomically to
    the exact canonical segment IDs; normal turns decrypt it once, while
    checkpoint work opens the immutable canonical nodes themselves.
    """
    start = int(start_seq)
    end = int(end_seq)
    count = int(source_message_count)
    expected = int(expected_version)
    previous = int(previous_watermark_seq)
    compatibility_envelope = dict(head_summary_envelope or summary_envelope or {})
    if start <= 0 or end < start or count <= 0 or expected < 0 or previous < 0:
        raise ValueError("invalid summary leaf metadata")
    if previous and start <= previous:
        raise ValueError("summary leaf does not advance the watermark")
    is_head_update = False
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                cur.execute(
                    "SELECT summary_envelope,watermark_ts,version,watermark_seq "
                    "FROM v2_conversation_summary WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                head = cur.fetchone()
                if head is None:
                    if expected != 0 or previous != 0:
                        return False
                else:
                    if int(head["version"]) != expected:
                        return False
                    stored_previous = int(head["watermark_seq"] or 0)
                    if stored_previous not in {0, previous}:
                        return False

                # Exact retained-row witness: a seq range may contain other
                # users' global identities, so count only this user's rows.
                # GC-able synthetic rows (verify_ping/resident_maintenance) are
                # excluded here for the same reason the fold reader and the
                # read-time witness exclude them: a leaf must never claim
                # coverage of a row a later verify_loop deletes (permanent
                # v2_summary_frontier_integrity_error). This write-time witness
                # must count the SAME set the fold saw, or a correct
                # synthetic-excluded leaf would be refused here.
                cur.execute(
                    "SELECT COALESCE(MIN(seq),0) AS first_seq,"
                    "COALESCE(MAX(seq),0) AS last_seq,COUNT(*) AS n "
                    "FROM chat_messages WHERE user_id=%s AND seq>%s AND seq<=%s "
                    "AND COALESCE(doc->>'source','') "
                    "NOT IN ('verify_ping','resident_maintenance')",
                    (user_id, previous, end),
                )
                source = cur.fetchone()
                if (
                    int(source["first_seq"]) != start
                    or int(source["last_seq"]) != end
                    or int(source["n"]) != count
                ):
                    return False

                cur.execute(
                    "SELECT COUNT(*) AS n FROM v2_conversation_summary_segments "
                    "WHERE user_id=%s",
                    (user_id,),
                )
                segment_count = int(cur.fetchone()["n"])
                if segment_count == 0 and head is not None:
                    legacy_envelope = head["summary_envelope"]
                    if not legacy_envelope:
                        return False
                    legacy_end = previous if previous > 0 else max(0, start - 1)
                    cur.execute(
                        "INSERT INTO v2_conversation_summary_segments "
                        "(user_id,format_version,coverage_kind,level,start_seq,end_seq,"
                        " source_message_count,legacy_opaque_through_seq,"
                        " child_segment_ids,summary_envelope) "
                        "VALUES (%s,1,'legacy_opaque',0,0,%s,0,%s,"
                        "'{}'::BIGINT[],%s)",
                        (
                            user_id,
                            legacy_end,
                            legacy_end,
                            Jsonb(dict(legacy_envelope)),
                        ),
                    )

                cur.execute(
                    "INSERT INTO v2_conversation_summary_segments "
                    "(user_id,format_version,coverage_kind,level,start_seq,end_seq,"
                    " source_message_count,legacy_opaque_through_seq,"
                    " child_segment_ids,summary_envelope) "
                    "VALUES (%s,1,'exact',0,%s,%s,%s,0,'{}'::BIGINT[],%s) "
                    "RETURNING segment_id",
                    (
                        user_id,
                        start,
                        end,
                        count,
                        Jsonb(dict(summary_envelope or {})),
                    ),
                )
                leaf_id = int(cur.fetchone()["segment_id"])
                cur.execute(
                    "WITH eligible AS ("
                    " SELECT s.* FROM v2_conversation_summary_segments s "
                    " WHERE s.user_id=%s AND s.end_seq<=%s"
                    "), canonical AS ("
                    " SELECT child.* FROM eligible child WHERE NOT EXISTS ("
                    "   SELECT 1 FROM eligible parent "
                    "   WHERE parent.level>child.level "
                    "     AND parent.start_seq<=child.start_seq "
                    "     AND parent.end_seq>=child.end_seq"
                    " )"
                    ") SELECT segment_id FROM canonical "
                    "ORDER BY start_seq,end_seq,level DESC",
                    (user_id, end),
                )
                materialized_ids = [
                    int(row["segment_id"]) for row in cur.fetchall()
                ]
                if not materialized_ids or materialized_ids[-1] != leaf_id:
                    raise RuntimeError("new summary leaf is not canonical")
                if head is None:
                    cur.execute(
                        "INSERT INTO v2_conversation_summary "
                        "(user_id,summary_envelope,watermark_ts,version,watermark_seq,"
                        " materialized_segment_ids) VALUES (%s,%s,%s,1,%s,%s)",
                        (
                            user_id,
                            Jsonb(compatibility_envelope),
                            float(watermark_ts),
                            end,
                            materialized_ids,
                        ),
                    )
                else:
                    is_head_update = True
                    cur.execute(
                        "UPDATE v2_conversation_summary SET summary_envelope=%s,"
                        "watermark_ts=%s,watermark_seq=%s,version=version+1,"
                        "materialized_segment_ids=%s,updated_at=now() "
                        "WHERE user_id=%s AND version=%s",
                        (
                            Jsonb(compatibility_envelope),
                            float(watermark_ts),
                            end,
                            materialized_ids,
                            user_id,
                            expected,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("summary head lock lost")
    if is_head_update:
        # Same-PK in-place rewrite of the existing summary head row — the
        # append-only replicator cursor never revisits it. The head-is-None
        # branch above is a brand-new PK the forward cursor scan picks up
        # normally, no requeue needed.
        from tee_shadow import mirror
        mirror.mark_pending(str(user_id), "v2_conversation_summary", str(user_id), "requeue")
    return True


def seed_legacy_summary_segment(
    user_id,
    *,
    expected_version: int,
    translated_watermark_seq: int,
) -> bool:
    """Atomically bind a pre-segmentation head as one opaque immutable leaf.

    This is the no-new-message migration path for an oversized legacy summary.
    It copies the already-encrypted envelope; no plaintext enters the DB layer.
    A concurrent leaf/checkpoint/clear either wins before the head lock or loses
    its version/generation fence, so the materialized binding is never partial.
    """
    expected = int(expected_version)
    translated = int(translated_watermark_seq)
    if expected <= 0 or translated < 0:
        raise ValueError("invalid legacy summary seed metadata")
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                cur.execute(
                    "SELECT summary_envelope,version,watermark_seq,"
                    "materialized_segment_ids FROM v2_conversation_summary "
                    "WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                head = cur.fetchone()
                if head is None:
                    return False
                cur.execute(
                    "SELECT COUNT(*) AS n FROM v2_conversation_summary_segments "
                    "WHERE user_id=%s",
                    (user_id,),
                )
                if int(cur.fetchone()["n"]) > 0:
                    # A legitimate concurrent seeder commits the segment and
                    # its head-ID binding in one transaction, so it must also
                    # have advanced the version we originally observed.  Do
                    # not bless an orphan/corrupt segment set as a successful
                    # lazy migration.
                    return bool(tuple(head["materialized_segment_ids"] or ())) and (
                        int(head["version"]) > expected
                    )
                if (
                    int(head["version"]) != expected
                    or tuple(head["materialized_segment_ids"] or ())
                    or not head["summary_envelope"]
                ):
                    return False
                stored = int(head["watermark_seq"] or 0)
                if stored not in {0, translated}:
                    return False
                opaque_end = max(stored, translated)
                cur.execute(
                    "INSERT INTO v2_conversation_summary_segments "
                    "(user_id,format_version,coverage_kind,level,start_seq,end_seq,"
                    " source_message_count,legacy_opaque_through_seq,"
                    " child_segment_ids,summary_envelope) "
                    "VALUES (%s,1,'legacy_opaque',0,0,%s,0,%s,"
                    "'{}'::BIGINT[],%s) RETURNING segment_id",
                    (
                        user_id,
                        opaque_end,
                        opaque_end,
                        Jsonb(dict(head["summary_envelope"])),
                    ),
                )
                segment_id = int(cur.fetchone()["segment_id"])
                cur.execute(
                    "UPDATE v2_conversation_summary SET watermark_seq=%s,"
                    "materialized_segment_ids=%s,version=version+1,updated_at=now() "
                    "WHERE user_id=%s AND version=%s",
                    (opaque_end, [segment_id], user_id, expected),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("legacy summary seed CAS lost")
    # Same-PK in-place rewrite of the existing summary head row (this function
    # only ever reaches here when `head` already existed) — the append-only
    # replicator cursor never revisits it.
    from tee_shadow import mirror
    mirror.mark_pending(str(user_id), "v2_conversation_summary", str(user_id), "requeue")
    return True


def insert_summary_checkpoint(
    user_id,
    *,
    summary_envelope: dict,
    head_summary_envelope: dict,
    level: int,
    start_seq: int,
    end_seq: int,
    source_message_count: int,
    child_segment_ids: list[int] | tuple[int, ...],
    expected_version: int,
    expected_watermark_seq: int,
    coverage_kind: str = "exact",
    legacy_opaque_through_seq: int = 0,
) -> bool:
    """Insert one immutable parent over an exact current canonical run.

    Locking the summary head serializes competing checkpoint writers without
    mutating the head.  The chat-user fence prevents an in-flight pre-clear
    provider result from recreating a checkpoint after explicit history clear.
    """
    child_ids = tuple(int(value) for value in child_segment_ids)
    parent_level = int(level)
    start = int(start_seq)
    end = int(end_seq)
    count = int(source_message_count)
    coverage = str(coverage_kind)
    opaque_through = int(legacy_opaque_through_seq)
    expected = int(expected_version)
    expected_watermark = int(expected_watermark_seq)
    if (
        parent_level <= 0
        or (coverage == "exact" and start <= 0)
        or (coverage == "legacy_opaque" and start != 0)
        or end < start
        or count < 0
        or not child_ids
        or len(child_ids) != len(set(child_ids))
        or coverage not in {"exact", "legacy_opaque"}
        or (coverage == "exact" and (count <= 0 or opaque_through != 0))
        or (
            coverage == "legacy_opaque"
            and (opaque_through < 0 or opaque_through > end)
        )
        or expected <= 0
        or expected_watermark < 0
    ):
        raise ValueError("invalid summary checkpoint metadata")
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                cur.execute(
                    "SELECT watermark_seq,version,materialized_segment_ids "
                    "FROM v2_conversation_summary "
                    "WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                head = cur.fetchone()
                if (
                    head is None
                    or int(head["watermark_seq"] or 0) != expected_watermark
                    or int(head["version"] or 0) != expected
                    or expected_watermark < end
                ):
                    return False
                cur.execute(
                    "WITH eligible AS ("
                    " SELECT s.* FROM v2_conversation_summary_segments s "
                    " WHERE s.user_id=%s AND s.end_seq<=%s"
                    "), canonical AS ("
                    " SELECT child.* FROM eligible child WHERE NOT EXISTS ("
                    "   SELECT 1 FROM eligible parent "
                    "   WHERE parent.level>child.level "
                    "     AND parent.start_seq<=child.start_seq "
                    "     AND parent.end_seq>=child.end_seq"
                    " )"
                    ") SELECT segment_id FROM canonical "
                    "ORDER BY start_seq,end_seq,level DESC",
                    (user_id, expected_watermark),
                )
                canonical_before = tuple(
                    int(row["segment_id"]) for row in cur.fetchall()
                )
                if canonical_before != tuple(
                    int(value) for value in head["materialized_segment_ids"]
                ):
                    return False
                cur.execute(
                    "SELECT segment_id,coverage_kind,level,start_seq,end_seq,"
                    "source_message_count,legacy_opaque_through_seq,child_segment_ids "
                    "FROM v2_conversation_summary_segments "
                    "WHERE user_id=%s AND segment_id=ANY(%s) "
                    "ORDER BY start_seq,end_seq,level DESC",
                    (user_id, list(child_ids)),
                )
                children = [dict(row) for row in cur.fetchall()]
                if len(children) != len(child_ids):
                    return False
                ordered_ids = tuple(int(row["segment_id"]) for row in children)
                if ordered_ids != child_ids:
                    return False
                if (
                    int(children[0]["start_seq"]) != start
                    or int(children[-1]["end_seq"]) != end
                    or sum(int(row["source_message_count"]) for row in children)
                    != count
                    or parent_level <= max(int(row["level"]) for row in children)
                    or (
                        coverage == "legacy_opaque"
                        and opaque_through
                        != max(int(row["legacy_opaque_through_seq"] or 0)
                               for row in children)
                    )
                    or (
                        coverage == "exact"
                        and any(row["coverage_kind"] != "exact" for row in children)
                    )
                ):
                    return False
                for left, right in zip(children, children[1:]):
                    if int(left["end_seq"]) >= int(right["start_seq"]):
                        return False

                # The supplied children must be the entire current canonical
                # cover inside [start,end].  This rejects partial/crossing
                # parents and makes containment-based projection unambiguous.
                cur.execute(
                    "WITH eligible AS ("
                    " SELECT s.* FROM v2_conversation_summary_segments s "
                    " WHERE s.user_id=%s AND s.end_seq<=("
                    "   SELECT watermark_seq FROM v2_conversation_summary "
                    "   WHERE user_id=%s"
                    " )"
                    "), canonical AS ("
                    " SELECT child.* FROM eligible child WHERE NOT EXISTS ("
                    "   SELECT 1 FROM eligible parent "
                    "   WHERE parent.level>child.level "
                    "     AND parent.start_seq<=child.start_seq "
                    "     AND parent.end_seq>=child.end_seq"
                    " )"
                    ") SELECT segment_id FROM canonical "
                    "WHERE start_seq>=%s AND end_seq<=%s "
                    "ORDER BY start_seq,end_seq,level DESC",
                    (user_id, user_id, start, end),
                )
                canonical_ids = tuple(int(row["segment_id"]) for row in cur.fetchall())
                if canonical_ids != child_ids:
                    return False
                cur.execute(
                    "INSERT INTO v2_conversation_summary_segments "
                    "(user_id,format_version,coverage_kind,level,start_seq,end_seq,"
                    " source_message_count,legacy_opaque_through_seq,"
                    " child_segment_ids,summary_envelope) "
                    "VALUES (%s,1,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (user_id,level,start_seq,end_seq) DO NOTHING "
                    "RETURNING segment_id",
                    (
                        user_id,
                        coverage,
                        parent_level,
                        start,
                        end,
                        count,
                        opaque_through,
                        list(child_ids),
                        Jsonb(dict(summary_envelope or {})),
                    ),
                )
                inserted = cur.fetchone()
                if inserted is None:
                    return False
                parent_id = int(inserted["segment_id"])
                cur.execute(
                    "WITH eligible AS ("
                    " SELECT s.* FROM v2_conversation_summary_segments s "
                    " WHERE s.user_id=%s AND s.end_seq<=%s"
                    "), canonical AS ("
                    " SELECT child.* FROM eligible child WHERE NOT EXISTS ("
                    "   SELECT 1 FROM eligible parent "
                    "   WHERE parent.level>child.level "
                    "     AND parent.start_seq<=child.start_seq "
                    "     AND parent.end_seq>=child.end_seq"
                    " )"
                    ") SELECT segment_id FROM canonical "
                    "ORDER BY start_seq,end_seq,level DESC",
                    (user_id, expected_watermark),
                )
                materialized_ids = [
                    int(row["segment_id"]) for row in cur.fetchall()
                ]
                if parent_id not in materialized_ids:
                    raise RuntimeError("new summary checkpoint is not canonical")
                cur.execute(
                    "UPDATE v2_conversation_summary SET summary_envelope=%s,"
                    "materialized_segment_ids=%s,version=version+1,updated_at=now() "
                    "WHERE user_id=%s AND version=%s AND watermark_seq=%s",
                    (
                        Jsonb(dict(head_summary_envelope or {})),
                        materialized_ids,
                        user_id,
                        expected,
                        expected_watermark,
                    ),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("summary head checkpoint CAS lost")
    # Same-PK in-place rewrite of the existing summary head row (head must
    # already exist for this function to reach here) — the append-only
    # replicator cursor never revisits it.
    from tee_shadow import mirror
    mirror.mark_pending(str(user_id), "v2_conversation_summary", str(user_id), "requeue")
    return True


# ---------------------------------------------------------------------------
# Encrypted virtual workspace (migration 0042).
#
# Contents are v1 shared envelopes. Paths/kinds/revisions are deliberately
# plaintext routing metadata; callers must never place user document content in
# those columns. Optimistic revision checks are authoritative in PostgreSQL so
# disjoint future file writes may execute concurrently without blind last-write
# wins on the same path.
# ---------------------------------------------------------------------------


def get_workspace_entry(user_id: str, path: str) -> dict | None:
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT path,kind,content_envelope,mime_type,source_ref,revision,"
                "created_at,updated_at FROM v2_workspace_entries "
                "WHERE user_id=%s AND path=%s",
                (user_id, path),
            )
            row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["content_envelope"] = dict(out["content_envelope"])
    return out


def list_workspace_entries(
    user_id: str,
    *,
    prefix: str = "/",
    recursive: bool = False,
    limit: int = 100,
) -> list[dict]:
    maximum = max(1, min(int(limit), 500))
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if prefix == "/":
                if recursive:
                    cur.execute(
                        "SELECT path,kind,mime_type,source_ref,revision,created_at,updated_at "
                        "FROM v2_workspace_entries WHERE user_id=%s "
                        "ORDER BY path LIMIT %s",
                        (user_id, maximum),
                    )
                else:
                    cur.execute(
                        "SELECT path,kind,mime_type,source_ref,revision,created_at,updated_at "
                        "FROM v2_workspace_entries WHERE user_id=%s "
                        "AND position('/' IN substring(path FROM 2))=0 "
                        "ORDER BY path LIMIT %s",
                        (user_id, maximum),
                    )
            else:
                escaped = (
                    prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                if recursive:
                    cur.execute(
                        "SELECT path,kind,mime_type,source_ref,revision,created_at,updated_at "
                        "FROM v2_workspace_entries WHERE user_id=%s "
                        "AND (path=%s OR path LIKE %s ESCAPE '\\') "
                        "ORDER BY path LIMIT %s",
                        (user_id, prefix, escaped + "/%", maximum),
                    )
                else:
                    # Filter direct children in SQL. A fixed 500-row prefetch
                    # followed by Python filtering can otherwise return an
                    # empty/incomplete directory when many nested paths sort
                    # before its direct children.
                    cur.execute(
                        "SELECT path,kind,mime_type,source_ref,revision,created_at,updated_at "
                        "FROM v2_workspace_entries WHERE user_id=%s "
                        "AND (path=%s OR (path LIKE %s ESCAPE '\\' "
                        "AND position('/' IN substring(path FROM %s))=0)) "
                        "ORDER BY path LIMIT %s",
                        (
                            user_id,
                            prefix,
                            escaped + "/%",
                            len(prefix) + 2,
                            maximum,
                        ),
                    )
            return [dict(row) for row in cur.fetchall()]


def put_workspace_entry_cas(
    user_id: str,
    path: str,
    *,
    kind: str,
    content_envelope: dict,
    mime_type: str,
    source_ref: str,
    expected_revision: int,
) -> dict | None:
    """Create at revision 0 or replace exactly ``expected_revision``.

    Returns the new metadata row. A missing/stale revision returns ``None``;
    database failures still raise so a write effect is retried rather than
    acknowledged without durable state.
    """
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    is_rewrite = False
    result: dict | None = None
    try:
        with _pool().connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    if str(kind) == "artifact":
                        db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                        cur.execute(
                            "SELECT 1 FROM chat_messages "
                            "WHERE user_id=%s AND msg_id=%s",
                            (str(user_id), str(source_ref)),
                        )
                        if cur.fetchone() is None:
                            return None
                    cur.execute(
                        "SELECT revision FROM v2_workspace_entries "
                        "WHERE user_id=%s AND path=%s FOR UPDATE",
                        (user_id, path),
                    )
                    current = cur.fetchone()
                    actual = int(current["revision"]) if current is not None else 0
                    if actual != expected_revision:
                        return None
                    if current is None:
                        cur.execute(
                            "INSERT INTO v2_workspace_entries "
                            "(user_id,path,kind,content_envelope,mime_type,source_ref,revision) "
                            "VALUES (%s,%s,%s,%s,%s,%s,1) "
                            "RETURNING path,kind,mime_type,source_ref,revision,created_at,updated_at",
                            (
                                user_id,
                                path,
                                kind,
                                Jsonb(dict(content_envelope)),
                                mime_type,
                                source_ref,
                            ),
                        )
                    else:
                        is_rewrite = True
                        cur.execute(
                            "UPDATE v2_workspace_entries SET kind=%s,content_envelope=%s,"
                            "mime_type=%s,source_ref=%s,revision=revision+1,updated_at=now() "
                            "WHERE user_id=%s AND path=%s AND revision=%s "
                            "RETURNING path,kind,mime_type,source_ref,revision,created_at,updated_at",
                            (
                                kind,
                                Jsonb(dict(content_envelope)),
                                mime_type,
                                source_ref,
                                user_id,
                                path,
                                expected_revision,
                            ),
                        )
                    row = cur.fetchone()
                    result = dict(row) if row is not None else None
    except psycopg.errors.UniqueViolation:
        # Concurrent revision-0 creators: one wins; the loser observes a clean
        # conflict rather than surfacing a database exception to the model.
        return None
    if result is not None and is_rewrite:
        # Same-PK (user_id,path) in-place rewrite — the append-only replicator
        # cursor never revisits it. The INSERT branch (current is None, first
        # write at revision 0) needs no requeue: brand-new PK, forward cursor
        # scan picks it up normally.
        from tee_shadow import mirror
        mirror.mark_pending(str(user_id), "v2_workspace_entries", str(path), "requeue")
    return result


def delete_workspace_entry_cas(
    user_id: str,
    path: str,
    *,
    expected_revision: int,
) -> bool:
    if type(expected_revision) is not int or expected_revision <= 0:
        return False
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM v2_workspace_entries "
                "WHERE user_id=%s AND path=%s AND revision=%s",
                (user_id, path, expected_revision),
            )
            return cur.rowcount == 1


def record_sandbox_acquisition(
    user_id: str,
    *,
    provider: str,
    purpose: str,
) -> int:
    """Append one content-free provider acquisition event for billing/usage."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v2_sandbox_usage_events (user_id,provider,purpose) "
                "VALUES (%s,%s,%s) RETURNING id",
                (user_id, str(provider)[:80], str(purpose)[:80]),
            )
            return int(cur.fetchone()[0])


def finish_sandbox_acquisition(
    usage_id: int,
    user_id: str,
    *,
    duration_ms: int,
    outcome: str,
) -> bool:
    """Finalize one billable sandbox lifetime exactly once.

    An acquired row left open after a worker crash is intentionally visible to
    reconciliation; this update never guesses a duration or overwrites a prior
    close event.
    """
    bounded_duration = max(0, int(duration_ms))
    bounded_outcome = str(outcome or "closed")[:40]
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v2_sandbox_usage_events "
                "SET released_at=now(),duration_ms=%s,outcome=%s "
                "WHERE id=%s AND user_id=%s AND released_at IS NULL",
                (bounded_duration, bounded_outcome, int(usage_id), user_id),
            )
            return cur.rowcount == 1


def append_trajectory_events_batch(
    job_id: int | str,
    user_id: str,
    *,
    events: list[dict],
) -> list[int]:
    """Atomically append encrypted event rows and return their immutable indices.

    Stream-row locking serializes concurrent callbacks without locking the
    source ``agent_jobs`` row. Repeating idempotency keys returns the existing
    indices and never rewrites ciphertext. The batch form lets one oversized
    logical trajectory event store every encrypted chunk under one transaction
    and one stream-frontier advance.
    """
    if not isinstance(events, list) or not 1 <= len(events) <= 4096:
        raise ValueError("invalid trajectory event batch")
    reopened_review = False
    normalized: list[dict] = []
    seen_keys: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid trajectory event")
        event_kind = str(event.get("event_kind") or "")
        idempotency_key = str(event.get("idempotency_key") or "")
        payload_bytes = event.get("payload_bytes")
        if not _TRAJECTORY_EVENT_KIND_RE.fullmatch(event_kind):
            raise ValueError("invalid trajectory event kind")
        if not _TRAJECTORY_IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise ValueError("invalid trajectory idempotency key")
        if idempotency_key in seen_keys:
            raise ValueError("duplicate trajectory idempotency key in batch")
        if type(payload_bytes) is not int or not 1 <= payload_bytes <= 1024 * 1024:
            raise ValueError("invalid trajectory payload size")
        seen_keys.add(idempotency_key)
        normalized.append(
            {
                "event_kind": event_kind,
                "idempotency_key": idempotency_key,
                "payload_envelope": _validate_trajectory_envelope(
                    str(user_id), event.get("payload_envelope")
                ),
                "payload_bytes": payload_bytes,
                "truncated": bool(event.get("truncated", False)),
            }
        )
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                # Capture is retained across an explicit chat clear, but only
                # events that linearize before that clear may be appended. The
                # shared fence lets clear wait for an already-started append;
                # the generation witness rejects a worker resuming afterwards.
                db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                cur.execute(
                    "SELECT 1 FROM agent_jobs AS job "
                    "JOIN v2_runtime_state AS state "
                    "ON state.user_id=job.user_id "
                    "WHERE job.id=%s AND job.user_id=%s "
                    "AND state.hosted_runtime_state='v2' "
                    "AND job.expected_runtime_generation="
                    "state.runtime_generation",
                    (job_id, str(user_id)),
                )
                if cur.fetchone() is None:
                    raise ValueError("trajectory source job generation is stale")
                cur.execute(
                    "INSERT INTO v2_trajectory_streams (job_id,user_id) "
                    "SELECT id,user_id FROM agent_jobs WHERE id=%s AND user_id=%s "
                    "ON CONFLICT (job_id) DO NOTHING",
                    (job_id, str(user_id)),
                )
                cur.execute(
                    "SELECT next_event_index FROM v2_trajectory_streams "
                    "WHERE job_id=%s AND user_id=%s FOR UPDATE",
                    (job_id, str(user_id)),
                )
                stream = cur.fetchone()
                if stream is None:
                    raise ValueError("trajectory source job not found")
                keys = [event["idempotency_key"] for event in normalized]
                cur.execute(
                    "SELECT idempotency_key,event_index FROM v2_trajectory_events "
                    "WHERE job_id=%s AND idempotency_key=ANY(%s::text[])",
                    (job_id, keys),
                )
                existing = {
                    str(row["idempotency_key"]): int(row["event_index"])
                    for row in cur.fetchall()
                }
                next_event_index = int(stream["next_event_index"])
                event_indices: list[int] = []
                rows_to_insert: list[tuple] = []
                for event in normalized:
                    prior = existing.get(event["idempotency_key"])
                    if prior is not None:
                        event_indices.append(prior)
                        continue
                    event_index = next_event_index
                    next_event_index += 1
                    rows_to_insert.append(
                        (
                            job_id,
                            str(user_id),
                            event_index,
                            event["event_kind"],
                            event["idempotency_key"],
                            Jsonb(dict(event["payload_envelope"])),
                            event["payload_bytes"],
                            event["truncated"],
                        )
                    )
                    event_indices.append(event_index)
                inserted = bool(rows_to_insert)
                if inserted:
                    # All physical chunks for one logical event land in one SQL
                    # statement. This avoids one client/server round trip per
                    # chunk while the stream frontier lock is held.
                    values_sql = sql.SQL(",").join(
                        sql.SQL("(%s,%s,%s,%s,%s,%s,%s,%s)")
                        for _row in rows_to_insert
                    )
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO v2_trajectory_events "
                            "(job_id,user_id,event_index,event_kind,idempotency_key,"
                            "payload_envelope,payload_bytes,truncated) VALUES {}"
                        ).format(values_sql),
                        [value for row in rows_to_insert for value in row],
                    )
                    cur.execute(
                        "UPDATE v2_trajectory_streams SET next_event_index=%s "
                        "WHERE job_id=%s AND user_id=%s",
                        (next_event_index, job_id, str(user_id)),
                    )
                # A terminal event can land just after an offline review
                # completed. Reopen it while holding the same stream frontier
                # lock so the completed analysis can never remain authoritative
                # over an older prefix. If review is disabled or globally
                # capped, invalidate the stale analysis without scheduling a
                # provider call; trajectory capture itself remains intact.
                cur.execute(
                    "SELECT 1 FROM v2_trajectory_reviews "
                    "WHERE source_job_id=%s AND user_id=%s AND status='completed'",
                    (job_id, str(user_id)),
                )
                if inserted and cur.fetchone() is not None:
                    admitted = _review_admission_available_on_cursor(cur)
                    cur.execute(
                        "UPDATE v2_trajectory_reviews SET status=%s,"
                        "attempt_count=0,claimed_by_job_id=NULL,review_envelope=NULL,"
                        "last_error=%s,finished_at="
                        "CASE WHEN %s THEN NULL ELSE clock_timestamp() END "
                        "WHERE source_job_id=%s AND user_id=%s AND status='completed'",
                        (
                            "pending" if admitted else "failed",
                            "trajectory_frontier_advanced"
                            if admitted
                            else "trajectory_review_disabled_or_capped",
                            admitted,
                            job_id,
                            str(user_id),
                        ),
                    )
                    reopened_review = bool(cur.rowcount)
                    if admitted and reopened_review:
                        _ensure_review_runner_on_cursor(cur, str(user_id))
    if reopened_review:
        # Same-PK in-place rewrite of the completed review row (reopened back
        # to pending/failed) — the append-only replicator cursor never
        # revisits it.
        from tee_shadow import mirror
        mirror.mark_pending(str(user_id), "v2_trajectory_reviews", str(job_id), "requeue")
    return event_indices


def append_trajectory_event(
    job_id: int | str,
    user_id: str,
    *,
    event_kind: str,
    idempotency_key: str,
    payload_envelope: dict,
    payload_bytes: int,
    truncated: bool = False,
) -> int:
    """Backward-compatible single-event wrapper around the atomic batch path."""
    return append_trajectory_events_batch(
        job_id,
        user_id,
        events=[
            {
                "event_kind": event_kind,
                "idempotency_key": idempotency_key,
                "payload_envelope": payload_envelope,
                "payload_bytes": payload_bytes,
                "truncated": truncated,
            }
        ],
    )[0]


def list_trajectory_events(
    job_id: int | str,
    user_id: str,
    *,
    after_index: int = -1,
    limit: int = 256,
) -> list[dict]:
    if type(after_index) is not int or after_index < -1:
        raise ValueError("invalid trajectory event cursor")
    if type(limit) is not int or not 1 <= limit <= 1024:
        raise ValueError("invalid trajectory event limit")
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT event_index,event_kind,payload_envelope,payload_bytes,"
                "truncated,created_at FROM v2_trajectory_events "
                "WHERE job_id=%s AND user_id=%s AND event_index>%s "
                "ORDER BY event_index LIMIT %s",
                (job_id, str(user_id), after_index, limit),
            )
            return [dict(row) for row in cur.fetchall()]


def get_trajectory_source_job(job_id: int | str, user_id: str) -> dict | None:
    """Return content-free source metadata only for the exact user/job pair."""
    try:
        source_job_id = int(job_id)
    except (TypeError, ValueError):
        return None
    if source_job_id <= 0:
        return None
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id,lane,status FROM agent_jobs "
                "WHERE id=%s AND user_id=%s",
                (source_job_id, str(user_id)),
            )
            row = cur.fetchone()
            return dict(row) if row is not None else None


def _trajectory_access_values(
    *,
    access_id: str,
    phase: str,
    user_id: str,
    job_id: int | str,
    operator_id: str,
    reason_code: str,
    case_ref: str,
    event_count: int | None,
    result_code: str,
) -> tuple:
    """Validate and normalize one content-free trajectory-access phase."""
    try:
        stable_access_id = str(uuid.UUID(str(access_id)))
        source_job_id = int(job_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid trajectory access identity") from exc
    stable_phase = str(phase or "")
    stable_operator = str(operator_id or "")
    stable_reason = str(reason_code or "")
    stable_case = str(case_ref or "")
    stable_result = str(result_code or "")
    if stable_phase not in {"requested", "succeeded", "failed"}:
        raise ValueError("invalid trajectory access phase")
    if source_job_id <= 0 or not str(user_id):
        raise ValueError("invalid trajectory access target")
    if _TRAJECTORY_ACCESS_OPERATOR_RE.fullmatch(stable_operator) is None:
        raise ValueError("invalid trajectory access operator")
    if stable_reason not in _TRAJECTORY_ACCESS_REASONS:
        raise ValueError("invalid trajectory access reason")
    if _TRAJECTORY_ACCESS_CASE_RE.fullmatch(stable_case) is None:
        raise ValueError("invalid trajectory access case")
    if _TRAJECTORY_ACCESS_RESULT_RE.fullmatch(stable_result) is None:
        raise ValueError("invalid trajectory access result")
    if event_count is not None and (
        type(event_count) is not int or not 1 <= event_count <= 100_000
    ):
        raise ValueError("invalid trajectory access event count")
    expected_shape = (
        (stable_phase == "requested" and event_count is None and stable_result == "pending")
        or (stable_phase == "succeeded" and event_count is not None and stable_result == "ok")
        or (stable_phase == "failed" and event_count is None and stable_result != "pending")
    )
    if not expected_shape:
        raise ValueError("invalid trajectory access phase shape")
    return (
        stable_access_id,
        stable_phase,
        str(user_id),
        source_job_id,
        stable_operator,
        stable_reason,
        stable_case,
        event_count,
        stable_result,
    )


def append_trajectory_access_audit(
    *,
    access_id: str,
    phase: str,
    user_id: str,
    job_id: int | str,
    operator_id: str,
    reason_code: str,
    case_ref: str,
    event_count: int | None,
    result_code: str,
) -> None:
    """Append one content-free access phase; plaintext is never accepted."""
    values = _trajectory_access_values(
        access_id=access_id,
        phase=phase,
        user_id=user_id,
        job_id=job_id,
        operator_id=operator_id,
        reason_code=reason_code,
        case_ref=case_ref,
        event_count=event_count,
        result_code=result_code,
    )
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_trajectory_access_audit "
            "(access_id,phase,user_id,job_id,operator_id,reason_code,case_ref,"
            "event_count,result_code) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            values,
        )


def authorize_trajectory_inspection_success(
    *,
    access_id: str,
    user_id: str,
    job_id: int | str,
    operator_id: str,
    reason_code: str,
    case_ref: str,
    event_count: int,
    expected_next_event_index: int,
) -> bool:
    """Linearize a successful inspection against concurrent stream appends.

    The caller may decrypt before this point, but plaintext must not be returned
    unless this transaction proves the exact source/frontier is still live and
    commits the matching success phase. The chat-user fence and stream lock
    serialize with late event appends. A false result is an authorization loss,
    never a soft audit failure.
    """
    if type(expected_next_event_index) is not int or expected_next_event_index < 1:
        raise ValueError("invalid trajectory inspection frontier")
    values = _trajectory_access_values(
        access_id=access_id,
        phase="succeeded",
        user_id=user_id,
        job_id=job_id,
        operator_id=operator_id,
        reason_code=reason_code,
        case_ref=case_ref,
        event_count=event_count,
        result_code="ok",
    )
    stable_access_id, _phase, stable_user_id, source_job_id = values[:4]
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                db._lock_chat_user_fence_on_cursor(cur, stable_user_id)
                cur.execute(
                    "SELECT stream.next_event_index FROM agent_jobs AS job "
                    "JOIN v2_trajectory_streams AS stream ON stream.job_id=job.id "
                    "WHERE job.id=%s AND job.user_id=%s "
                    "AND stream.user_id=%s FOR SHARE OF stream",
                    (source_job_id, stable_user_id, stable_user_id),
                )
                frontier = cur.fetchone()
                if (
                    frontier is None
                    or int(frontier["next_event_index"]) != expected_next_event_index
                ):
                    return False
                cur.execute(
                    "SELECT COUNT(*)::int AS event_count "
                    "FROM v2_trajectory_events WHERE job_id=%s AND user_id=%s",
                    (source_job_id, stable_user_id),
                )
                if int(cur.fetchone()["event_count"]) != event_count:
                    return False
                cur.execute(
                    "SELECT 1 FROM v2_trajectory_access_audit "
                    "WHERE access_id=%s AND phase='requested' AND user_id=%s "
                    "AND job_id=%s AND operator_id=%s AND reason_code=%s "
                    "AND case_ref=%s AND result_code='pending' FOR SHARE",
                    (
                        stable_access_id,
                        stable_user_id,
                        source_job_id,
                        values[4],
                        values[5],
                        values[6],
                    ),
                )
                if cur.fetchone() is None:
                    return False
                cur.execute(
                    "INSERT INTO v2_trajectory_access_audit "
                    "(access_id,phase,user_id,job_id,operator_id,reason_code,case_ref,"
                    "event_count,result_code) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )
                return True


def get_trajectory_capture_state(job_id: int | str, user_id: str) -> dict:
    """Content-free capture completeness frontier for diagnostics/review."""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT s.next_event_index,COALESCE(MAX(e.event_index),-1) AS last_event_index,"
                "COUNT(e.event_index)::int AS event_count,BOOL_OR(e.truncated) AS any_truncated,"
                "COALESCE(BOOL_OR(e.event_kind='capture_gap'),false) AS has_capture_gap,"
                "COALESCE(MAX(e.event_index) FILTER "
                "(WHERE e.event_kind='turn_terminal'),-1) AS terminal_event_index,"
                "j.status AS source_job_status,CASE "
                "WHEN COALESCE(BOOL_OR(e.event_kind='capture_gap'),false) THEN 'partial' "
                "WHEN BOOL_OR(e.event_kind='turn_terminal') THEN 'complete' "
                "WHEN j.status IN ('completed','failed','expired','superseded') THEN 'partial' "
                "ELSE 'open' END AS capture_status "
                "FROM v2_trajectory_streams s JOIN agent_jobs j ON j.id=s.job_id "
                "LEFT JOIN v2_trajectory_events e "
                "ON e.job_id=s.job_id WHERE s.job_id=%s AND s.user_id=%s "
                "GROUP BY s.next_event_index,j.status",
                (job_id, str(user_id)),
            )
            row = cur.fetchone()
            return (
                dict(row)
                if row is not None
                else {
                    "next_event_index": 0,
                    "last_event_index": -1,
                    "event_count": 0,
                    "any_truncated": False,
                    "has_capture_gap": False,
                    "terminal_event_index": -1,
                    "source_job_status": None,
                    "capture_status": "missing",
                }
            )


def claim_failure_review(
    user_id: str,
    *,
    runner_job_id: int | str,
    claimed_by: str,
) -> dict | None:
    """Claim the oldest pending source for one owned review-lane job."""
    if not trajectory_review_enabled():
        return None
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT 1 FROM agent_jobs WHERE id=%s AND user_id=%s "
                    "AND lane=%s AND status IN ('claimed','running') "
                    "AND claimed_by=%s AND lease_expires_at>now() FOR UPDATE",
                    (
                        runner_job_id,
                        str(user_id),
                        _TRAJECTORY_REVIEW_LANE,
                        str(claimed_by),
                    ),
                )
                if cur.fetchone() is None:
                    return None
                cur.execute(
                    "SELECT source_job_id FROM v2_trajectory_reviews "
                    "WHERE user_id=%s AND status='pending' "
                    "AND attempt_count<%s ORDER BY created_at,source_job_id "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (str(user_id), _TRAJECTORY_REVIEW_MAX_ATTEMPTS),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                source_job_id = int(row["source_job_id"])
                cur.execute(
                    "UPDATE v2_trajectory_reviews SET status='running',"
                    "attempt_count=attempt_count+1,claimed_by_job_id=%s,"
                    "started_at=clock_timestamp(),finished_at=NULL,last_error=NULL "
                    "WHERE source_job_id=%s RETURNING *",
                    (runner_job_id, source_job_id),
                )
                claimed = dict(cur.fetchone())
    # Same-PK in-place rewrite (pending -> running claim) — the append-only
    # replicator cursor never revisits it.
    from tee_shadow import mirror
    mirror.mark_pending(str(user_id), "v2_trajectory_reviews", str(source_job_id), "requeue")
    return claimed


def finish_failure_review(
    *,
    runner_job_id: int | str,
    source_job_id: int | str,
    user_id: str,
    claimed_by: str,
    review_envelope: dict | None = None,
    error_code: str | None = None,
    captured_next_event_index: int | None = None,
) -> dict:
    """Atomically settle one review runner and schedule any remaining backlog."""
    if (review_envelope is None) == (error_code is None):
        raise ValueError("provide exactly one review outcome")
    envelope = None
    if review_envelope is not None:
        envelope = _validate_trajectory_envelope(str(user_id), review_envelope)
        if type(captured_next_event_index) is not int or captured_next_event_index < 0:
            raise ValueError("captured trajectory frontier required")
    safe_error = _terminal_error_code(error_code) if error_code is not None else None
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                # Global lifecycle lock order is agent job -> trajectory stream
                # -> review row. The
                # timeout reaper terminalizes the runner before releasing its
                # review claim, so taking these in the opposite order here
                # would create an ABBA deadlock at lease expiry.
                cur.execute(
                    "SELECT 1 FROM agent_jobs j JOIN v2_runtime_state s "
                    "ON s.user_id=j.user_id WHERE j.id=%s AND j.user_id=%s "
                    "AND j.lane=%s AND j.status IN ('claimed','running') "
                    "AND j.claimed_by=%s AND j.lease_expires_at>now() "
                    "AND s.hosted_runtime_state='v2' "
                    "AND j.expected_runtime_generation=s.runtime_generation "
                    "FOR UPDATE OF j",
                    (
                        runner_job_id,
                        str(user_id),
                        _TRAJECTORY_REVIEW_LANE,
                        str(claimed_by),
                    ),
                )
                if cur.fetchone() is None:
                    return {"settled": False, "review_status": "lost"}
                cur.execute(
                    "SELECT next_event_index FROM v2_trajectory_streams "
                    "WHERE job_id=%s AND user_id=%s FOR UPDATE",
                    (source_job_id, str(user_id)),
                )
                stream = cur.fetchone()
                if stream is None:
                    return {"settled": False, "review_status": "lost"}
                cur.execute(
                    "SELECT attempt_count FROM v2_trajectory_reviews "
                    "WHERE source_job_id=%s AND user_id=%s AND status='running' "
                    "AND claimed_by_job_id=%s FOR UPDATE",
                    (source_job_id, str(user_id), runner_job_id),
                )
                review = cur.fetchone()
                if review is None:
                    return {"settled": False, "review_status": "lost"}
                frontier_advanced = envelope is not None and int(
                    stream["next_event_index"]
                ) != int(captured_next_event_index)
                if frontier_advanced:
                    review_status = "pending"
                    stored_envelope = None
                    stored_error = "trajectory_frontier_advanced"
                elif envelope is not None:
                    review_status = "completed"
                    stored_envelope = envelope
                    stored_error = None
                elif int(review["attempt_count"]) < _TRAJECTORY_REVIEW_MAX_ATTEMPTS:
                    review_status = "pending"
                    stored_envelope = None
                    stored_error = safe_error
                else:
                    review_status = "failed"
                    stored_envelope = None
                    stored_error = safe_error
                cur.execute(
                    "UPDATE v2_trajectory_reviews SET status=%s,"
                    "claimed_by_job_id=NULL,review_envelope=%s,last_error=%s,"
                    "attempt_count=CASE WHEN %s THEN "
                    "GREATEST(attempt_count-1,0) ELSE attempt_count END,"
                    "finished_at=CASE WHEN %s='pending' THEN NULL ELSE now() END "
                    "WHERE source_job_id=%s",
                    (
                        review_status,
                        Jsonb(dict(stored_envelope))
                        if stored_envelope is not None
                        else None,
                        stored_error,
                        frontier_advanced,
                        review_status,
                        source_job_id,
                    ),
                )
                cur.execute(
                    "UPDATE agent_jobs SET status='completed',finished_at=now() "
                    "WHERE id=%s AND user_id=%s AND lane=%s "
                    "AND status IN ('claimed','running') AND claimed_by=%s "
                    "AND lease_expires_at>now()",
                    (
                        runner_job_id,
                        str(user_id),
                        _TRAJECTORY_REVIEW_LANE,
                        str(claimed_by),
                    ),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("trajectory review runner ownership lost")
                _ensure_review_runner_on_cursor(cur, str(user_id))
    # Same-PK in-place rewrite (running -> completed/pending/failed settle) —
    # the append-only replicator cursor never revisits it.
    from tee_shadow import mirror
    mirror.mark_pending(str(user_id), "v2_trajectory_reviews", str(source_job_id), "requeue")
    return {
        "settled": True,
        "review_status": review_status,
        "frontier_advanced": frontier_advanced,
    }


def finish_empty_failure_review_runner(
    *,
    runner_job_id: int | str,
    user_id: str,
    claimed_by: str,
) -> bool:
    """Complete a stale generic runner that found no pending source row."""
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "UPDATE agent_jobs SET status='completed',finished_at=now() "
                    "WHERE id=%s AND user_id=%s AND lane=%s "
                    "AND status IN ('claimed','running') AND claimed_by=%s "
                    "AND lease_expires_at>now()",
                    (
                        runner_job_id,
                        str(user_id),
                        _TRAJECTORY_REVIEW_LANE,
                        str(claimed_by),
                    ),
                )
                completed = cur.rowcount == 1
                if completed:
                    _ensure_review_runner_on_cursor(cur, str(user_id))
                return completed


def get_failure_review(source_job_id: int | str, user_id: str) -> dict | None:
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT source_job_id,user_id,status,attempt_count,review_envelope,"
                "last_error,created_at,started_at,finished_at "
                "FROM v2_trajectory_reviews WHERE source_job_id=%s AND user_id=%s",
                (source_job_id, str(user_id)),
            )
            row = cur.fetchone()
            return dict(row) if row is not None else None


def get_wake_schedule(user_id) -> dict | None:
    """读取该用户的 v2_wake_schedule 行（proactive 唤醒调度：下次心跳/采集/屏幕监看到期
    时间 + BYOK 支付冷却截止 + screen_watch 与 foreground screen-chat 的跨回合
    frame cursor），无行返回 None（该用户尚未被调度器接管过）。

    **四个时间列一律以 epoch 浮点数返回**（`EXTRACT(EPOCH FROM ...)`），与
    `upsert_wake_schedule` 收的 epoch float 对称，调用方可以直接做算术比较。
    曾经只有 `next_screen_watch_at` 是 float、其余三列是 `datetime` —— 同一个 dict 里四个
    同类字段两种类型是个地雷（谁会记得只有第四个能做减法？）。`updated_at` 保持
    datetime：它是审计字段，没人拿它做算术。"""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT user_id, "
                "EXTRACT(EPOCH FROM next_heartbeat_at) AS next_heartbeat_at, "
                "EXTRACT(EPOCH FROM next_capture_at) AS next_capture_at, "
                "EXTRACT(EPOCH FROM payment_cooldown_until) AS payment_cooldown_until, "
                "EXTRACT(EPOCH FROM next_screen_watch_at) AS next_screen_watch_at, "
                "EXTRACT(EPOCH FROM proactive_backoff_until) "
                "AS proactive_backoff_until, "
                "last_screen_watch_frame_id, last_screen_chat_frame_id, self_wake_streak, "
                "self_wake_user_seq, self_wake_last_effect_id, "
                "self_wake_last_effect_accepted, proactive_fail_streak, "
                "proactive_fail_user_seq, updated_at "
                "FROM v2_wake_schedule WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    result = dict(row)
    if result["next_screen_watch_at"] is not None:
        result["next_screen_watch_at"] = float(result["next_screen_watch_at"])
    if result["proactive_backoff_until"] is not None:
        result["proactive_backoff_until"] = float(
            result["proactive_backoff_until"]
        )
    return result


def reserve_self_wake(
    user_id: str,
    *,
    effect_id: str,
    max_consecutive: int,
) -> dict[str, Any]:
    """Atomically admit one AI-authored self-schedule attempt.

    Only Runtime V2 wake turns call this helper. Heartbeat/event wake
    realization never touches the counter. A newer genuine user message lazily
    resets the streak at the next admission decision. Production calls this
    from the effect sink while the outbox owns the chat-user advisory fence, so
    the reset is ordered against a concurrent Send. The last effect result
    makes an outbox replay return the same decision without incrementing twice.
    """
    normalized_user_id = str(user_id)
    normalized_effect_id = str(effect_id or "").strip()
    if not normalized_effect_id:
        raise ValueError("self-wake effect_id required")
    limit = max(0, int(max_consecutive))
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "INSERT INTO v2_wake_schedule (user_id) VALUES (%s) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (normalized_user_id,),
                )
                cur.execute(
                    "SELECT self_wake_streak,self_wake_user_seq,"
                    "self_wake_last_effect_id,self_wake_last_effect_accepted "
                    "FROM v2_wake_schedule WHERE user_id=%s FOR UPDATE",
                    (normalized_user_id,),
                )
                state = cur.fetchone()
                if state is None:
                    raise RuntimeError("self-wake schedule row missing")
                if str(state["self_wake_last_effect_id"] or "") == normalized_effect_id:
                    accepted = bool(state["self_wake_last_effect_accepted"])
                    return {
                        "accepted": accepted,
                        "streak": int(state["self_wake_streak"] or 0),
                        "reason": "" if accepted else "self_wake_loop_guard",
                        "replayed": True,
                    }
                cur.execute(
                    "SELECT COALESCE(MAX(seq),0) AS user_seq FROM chat_messages "
                    "WHERE user_id=%s AND doc->>'role' IN ('user','human') "
                    "AND COALESCE(doc->>'source','') "
                    "NOT IN ('verify_ping','resident_maintenance')",
                    (normalized_user_id,),
                )
                user_seq = int(cur.fetchone()["user_seq"] or 0)
                previous_user_seq = int(state["self_wake_user_seq"] or 0)
                streak = int(state["self_wake_streak"] or 0)
                if user_seq > previous_user_seq:
                    streak = 0
                accepted = limit <= 0 or streak < limit
                next_streak = streak + 1 if accepted else streak
                cur.execute(
                    "UPDATE v2_wake_schedule SET self_wake_streak=%s,"
                    "self_wake_user_seq=%s,self_wake_last_effect_id=%s,"
                    "self_wake_last_effect_accepted=%s,updated_at=now() "
                    "WHERE user_id=%s",
                    (
                        next_streak,
                        user_seq,
                        normalized_effect_id,
                        accepted,
                        normalized_user_id,
                    ),
                )
                return {
                    "accepted": accepted,
                    "streak": next_streak,
                    "reason": "" if accepted else "self_wake_loop_guard",
                    "replayed": False,
                }


def upsert_wake_schedule(
    user_id,
    *,
    next_heartbeat_at: float | None = None,
    next_capture_at: float | None = None,
    payment_cooldown_until: float | None = None,
    next_screen_watch_at: float | None = None,
    last_screen_watch_frame_id: str | None = None,
    last_screen_chat_frame_id: str | None = None,
) -> None:
    """UPSERT 该用户的唤醒调度行。时间列各自是可选的 epoch 浮点数；传 None 表示
    「本次不动这一列」（COALESCE(EXCLUDED.col, 现有值) 保留旧值），不会把已有到期时间
    清空——这些列只会被推进到未来的时间戳，从不被置空，调用方总是「只更新我刚算出来的
    那一列」。updated_at 每次调用都刷新。

    last_screen_watch_frame_id 同一套「None=不动」语义：resident 把这个值放进程内存，
    V2 没有 per-user 常驻进程，不落库每个 scheduler tick 都会把同一帧当成新内容，变成
    唤醒风暴——所以推进 next_screen_watch_at 的调用（不带 frame id）绝不能把已记的
    frame id 冲掉。"""
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_wake_schedule "
            "(user_id, next_heartbeat_at, next_capture_at, payment_cooldown_until, "
            "next_screen_watch_at, last_screen_watch_frame_id, "
            "last_screen_chat_frame_id, updated_at) "
            "VALUES (%s, to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), "
            "to_timestamp(%s), %s, %s, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "next_heartbeat_at = COALESCE(EXCLUDED.next_heartbeat_at, v2_wake_schedule.next_heartbeat_at), "
            "next_capture_at = COALESCE(EXCLUDED.next_capture_at, v2_wake_schedule.next_capture_at), "
            "payment_cooldown_until = COALESCE(EXCLUDED.payment_cooldown_until, v2_wake_schedule.payment_cooldown_until), "
            "next_screen_watch_at = COALESCE(EXCLUDED.next_screen_watch_at, v2_wake_schedule.next_screen_watch_at), "
            "last_screen_watch_frame_id = COALESCE(EXCLUDED.last_screen_watch_frame_id, v2_wake_schedule.last_screen_watch_frame_id), "
            "last_screen_chat_frame_id = COALESCE(EXCLUDED.last_screen_chat_frame_id, v2_wake_schedule.last_screen_chat_frame_id), "
            "updated_at = now()",
            (
                user_id,
                float(next_heartbeat_at) if next_heartbeat_at is not None else None,
                float(next_capture_at) if next_capture_at is not None else None,
                float(payment_cooldown_until)
                if payment_cooldown_until is not None
                else None,
                float(next_screen_watch_at)
                if next_screen_watch_at is not None
                else None,
                str(last_screen_watch_frame_id)
                if last_screen_watch_frame_id is not None
                else None,
                str(last_screen_chat_frame_id)
                if last_screen_chat_frame_id is not None
                else None,
            ),
        )


def seed_missing_wake_clocks(
    user_id: str,
    *,
    due_at: float | None = None,
) -> bool:
    """Atomically arm every NULL-backed wake lane for one V2 user.

    ``due_heartbeat_users`` and ``due_screen_watch_users`` both deliberately
    exclude NULL timestamps. Therefore row existence is not proof that either
    lane is armed: self-wake, payment cooldown, or the other lane can create the
    row while leaving one clock NULL. Fill only those two missing clocks and
    preserve every already-advanced timestamp. ``next_capture_at`` is not
    included because capture/dream eligibility does not use it as a due-list
    predicate.

    Returns True when a row was inserted or at least one NULL clock was repaired.
    """
    timestamp = time.time() if due_at is None else float(due_at)
    with _pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO v2_wake_schedule "
            "(user_id,next_heartbeat_at,next_screen_watch_at,updated_at) "
            "VALUES (%s,to_timestamp(%s),to_timestamp(%s),now()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "next_heartbeat_at=COALESCE(v2_wake_schedule.next_heartbeat_at,"
            "EXCLUDED.next_heartbeat_at),"
            "next_screen_watch_at=COALESCE(v2_wake_schedule.next_screen_watch_at,"
            "EXCLUDED.next_screen_watch_at),updated_at=now() "
            "WHERE v2_wake_schedule.next_heartbeat_at IS NULL "
            "OR v2_wake_schedule.next_screen_watch_at IS NULL "
            "RETURNING user_id",
            (str(user_id), timestamp, timestamp),
        ).fetchone()
    return row is not None


_LATEST_GENUINE_USER_SEQ_SQL = (
    "(SELECT COALESCE(MAX(message.seq),0) FROM chat_messages AS message "
    "WHERE message.user_id=schedule.user_id "
    "AND message.doc->>'role' IN ('user','human') "
    "AND COALESCE(message.doc->>'source','') "
    "NOT IN ('verify_ping','resident_maintenance'))"
)


def heartbeat_due_diagnosis(user_id: str, *, now: float | None = None) -> dict:
    """单用户版的「心跳现在到期了吗?不到期是因为哪一条?」

    **判据必须与 `due_heartbeat_users` 逐条同源**——support 面板给出的结论如果和
    调度器实际用的规则不一致,那就是又一个「看起来在测量、其实没有」的信号
    (2026-08-10 我正是被这类信号骗过)。所以这里复用同一个
    `_LATEST_GENUINE_USER_SEQ_SQL`、同一套 DND EXISTS 子句,而不是在 Python 侧
    重写一遍。

    尤其是 **proactive_backoff**:那条不是「退避窗没过就挡」,还有一条逃生口——
    用户在失败之后又真人发过言(`proactive_fail_user_seq < 最新真人 seq`)就照样
    到期。少算这一条就只能给出模棱两可的 maybe,support 依旧判断不了。

    无行返回 `{"present": False}`。
    """
    ts = float(now) if now is not None else None
    uid = str(user_id or "").strip()
    if not uid:
        return {"present": False}
    sql = (
        "SELECT "
        "  (schedule.next_heartbeat_at IS NULL) AS unarmed, "
        "  (schedule.next_heartbeat_at IS NOT NULL AND schedule.next_heartbeat_at "
        "     > COALESCE(to_timestamp(%s), now())) AS not_due_yet, "
        "  (schedule.payment_cooldown_until IS NOT NULL AND schedule.payment_cooldown_until "
        "     > COALESCE(to_timestamp(%s), now())) AS payment_cooldown, "
        "  EXISTS (SELECT 1 FROM user_blobs AS settings "
        "     WHERE settings.user_id=schedule.user_id AND settings.kind='proactive_settings' "
        "     AND settings.doc @> '{\"dnd\": true}'::jsonb) AS dnd, "
        "  (schedule.proactive_backoff_until IS NOT NULL "
        "     AND schedule.proactive_backoff_until > COALESCE(to_timestamp(%s), now()) "
        "     AND schedule.proactive_fail_user_seq >= "
        + _LATEST_GENUINE_USER_SEQ_SQL
        + ") AS proactive_backoff "
        "FROM v2_wake_schedule AS schedule WHERE schedule.user_id=%s"
    )
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (ts, ts, ts, uid))
            row = cur.fetchone()
    if row is None:
        return {"present": False}
    blockers = [name for name in
                ("unarmed", "not_due_yet", "payment_cooldown", "dnd", "proactive_backoff")
                if bool(row.get(name))]
    return {"present": True, "blocked_by": blockers}


def due_heartbeat_users(*, now: float | None = None, limit: int = 500) -> list[str]:
    """到期需要心跳唤醒的 user_id 列表（next_heartbeat_at 已到、未开启 DND 且不在 BYOK 支付冷却
    窗口内），按 next_heartbeat_at 升序（最该醒的排前面），供 D3 调度器 poll 后逐个
    enqueue_job(..., 'heartbeat')。now 可注入 epoch 浮点数用于确定性测试；
    None → 用 DB now()（镜像 reap_stuck_jobs 的 to_timestamp(%s) 约定）。"""
    ts = float(now) if now is not None else None
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schedule.user_id FROM v2_wake_schedule AS schedule "
                "WHERE schedule.next_heartbeat_at IS NOT NULL "
                "AND schedule.next_heartbeat_at "
                "<= COALESCE(to_timestamp(%s), now()) "
                "AND (schedule.payment_cooldown_until IS NULL "
                "     OR schedule.payment_cooldown_until "
                "        <= COALESCE(to_timestamp(%s), now())) "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM user_blobs AS settings "
                "    WHERE settings.user_id=schedule.user_id "
                "      AND settings.kind='proactive_settings' "
                "      AND settings.doc @> '{\"dnd\": true}'::jsonb"
                ") "
                "AND (schedule.proactive_backoff_until IS NULL "
                "     OR schedule.proactive_backoff_until "
                "        <= COALESCE(to_timestamp(%s), now()) "
                "     OR schedule.proactive_fail_user_seq < "
                + _LATEST_GENUINE_USER_SEQ_SQL
                + ") ORDER BY schedule.next_heartbeat_at LIMIT %s",
                (ts, ts, ts, int(limit)),
            )
            return [row[0] for row in cur.fetchall()]


def due_screen_watch_users(*, now: float | None = None, limit: int = 500) -> list[str]:
    """到期需要屏幕监看唤醒的 user_id 列表（next_screen_watch_at 已到、未开启 DND 且不在 BYOK 支付
    冷却窗口内），按 next_screen_watch_at 升序，供 D3 调度器 poll 后逐个
    enqueue_job(..., 'screen_watch')。镜像 due_heartbeat_users 的每一处语义（NULL 不
    算到期；now 可注入 epoch 浮点数用于确定性测试；payment_cooldown_until 排除——
    一个已死的 BYOK key 不该被屏幕轮询器持续锤）。"""
    ts = float(now) if now is not None else None
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schedule.user_id FROM v2_wake_schedule AS schedule "
                "WHERE schedule.next_screen_watch_at IS NOT NULL "
                "AND schedule.next_screen_watch_at <= COALESCE(to_timestamp(%s), now()) "
                "AND (schedule.payment_cooldown_until IS NULL "
                "     OR schedule.payment_cooldown_until <= COALESCE(to_timestamp(%s), now())) "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM user_blobs AS settings "
                "    WHERE settings.user_id=schedule.user_id "
                "      AND settings.kind='proactive_settings' "
                "      AND settings.doc @> '{\"dnd\": true}'::jsonb"
                ") "
                "ORDER BY schedule.next_screen_watch_at LIMIT %s",
                (ts, ts, int(limit)),
            )
            return [row[0] for row in cur.fetchall()]


def due_compaction_users(
    *, min_backlog: int, limit: int = 200
) -> list[tuple[str, int]]:
    """V2 用户里积压过大、且当前没人在折的 ``[(user_id, backlog)]``，积压降序。

    这是唯一不需要用户开口的 maintenance 入队来源。其余四个入队点都挂在 turn
    上（回复成功后、coverage 降级、自链 catchup、CAS 重试），所以一个不再说话的
    用户就此停止折叠，而刚切到 V2 的大积压用户根本没人踢第一脚。

    三条口径必须和折叠本身一致，否则扫出来的用户会空转：

    * **从 ``v2_runtime_state`` 出发**（不是从 summary），因为刚切过来的用户还
      没有 summary 行，``COALESCE(watermark_seq, 0)`` 让他们的积压等于全部消息
      ——正是最需要扫的那批。同时天然排除已回滚到 resident 的用户：给 V1 用户
      入队 maintenance 会把已经停掉的 V2 工作重新拉起来。
    * **排除 GC-able 合成行**，与 ``db.count_messages_after_seq`` /
      ``chat_messages_after_seq`` 和两个 frontier 见证同一集合。把 verify_ping
      当积压会安排一次针对「马上要被 verify_loop 删掉的行」的折叠，那是永久性的
      frontier 损坏。
    * **跳过已有在途 maintenance job 的用户**。``enqueue_job`` 自己有 per-user
      单飞会 coalesce，所以重复入队本身无害；这一条只是不让扫描器每个 tick 都
      对同一批用户做无用功。

    每个用户的计数走 LATERAL 子查询并 ``LIMIT min_backlog``：只需要判定「够不够
    阈值」，不需要精确总数，所以一个积压 5 万条的用户也只扫 ``min_backlog`` 行。
    返回的 backlog 因此是**下界**（等于阈值即「至少这么多」），够排序用。
    """
    threshold = max(1, int(min_backlog))
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rs.user_id, backlog.n FROM v2_runtime_state AS rs "
                "LEFT JOIN v2_conversation_summary AS s ON s.user_id = rs.user_id "
                "JOIN LATERAL ("
                "  SELECT count(*) AS n FROM ("
                "    SELECT 1 FROM chat_messages AS m "
                "    WHERE m.user_id = rs.user_id "
                "      AND m.seq > COALESCE(s.watermark_seq, 0) "
                "      AND COALESCE(m.doc->>'source','') "
                "          NOT IN ('verify_ping','resident_maintenance') "
                "    LIMIT %s"
                "  ) AS capped"
                ") AS backlog ON TRUE "
                "WHERE rs.hosted_runtime_state = 'v2' "
                "  AND backlog.n >= %s "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM agent_jobs AS j "
                "    WHERE j.user_id = rs.user_id AND j.lane = 'maintenance' "
                "      AND j.status IN ('pending','claimed','running')"
                "  ) "
                "ORDER BY backlog.n DESC, rs.user_id LIMIT %s",
                (threshold, threshold, int(limit)),
            )
            return [(row[0], int(row[1])) for row in cur.fetchall()]


def due_scheduled_users(*, now: float | None = None, limit: int = 500) -> list[str]:
    """有到期 self-wake timer 的用户（跨用户）。

    `user_logs` 是 append-only：一个 timer 会有 created→claimed→fired 多行。必须用
    `DISTINCT ON (user_id, item_key) ... ORDER BY seq DESC` 只取每个 timer 的**最新一版**，
    否则早已 fire 的 timer 会被当成 pending 反复唤醒。

    到期 = pending，或 claimed 但 claim 租约已过期（持有者死了）。触发的原子性由
    `ScheduledWakeServiceV2.fire_due_timers` 内部的 claim_due CAS 保证——本函数只是个
    廉价的候选人筛子，允许假阳性（多个 scheduler 抢同一个 timer 是安全的）。"""
    ts = time.time() if now is None else float(now)
    if limit <= 0:
        return []
    sql = (
        "SELECT DISTINCT latest.user_id FROM ("
        "  SELECT DISTINCT ON (user_id, item_key) user_id, doc"
        "  FROM user_logs WHERE stream = %s"
        "  ORDER BY user_id, item_key, seq DESC"
        ") latest "
        "LEFT JOIN v2_wake_schedule AS schedule "
        "ON schedule.user_id=latest.user_id "
        "WHERE COALESCE(NULLIF(doc->>'due_at','')::float8, 0) <= %s "
        "  AND (doc->>'status' = 'pending' OR (doc->>'status' = 'claimed' "
        "       AND COALESCE(NULLIF(doc->>'claim_expires_at','')::float8, 0) <= %s)) "
        "  AND (schedule.proactive_backoff_until IS NULL "
        "       OR schedule.proactive_backoff_until <= to_timestamp(%s) "
        "       OR schedule.proactive_fail_user_seq < "
        + _LATEST_GENUINE_USER_SEQ_SQL
        + ") "
        "LIMIT %s"
    )
    with _pool().connection() as conn:
        rows = conn.execute(
            sql,
            (SCHEDULED_WAKE_STREAM, ts, ts, ts, limit),
        ).fetchall()
    return [str(r[0]) for r in rows]


def upsert_runtime_state(
    user_id,
    patch: dict,
    *,
    source_job_id: int | str | None = None,
) -> dict | None:
    """Shallow-merge content-free turn state, optionally source-fenced.

    Production chat turns pass ``source_job_id``.  That form shares the
    clear-history advisory fence and checks the job's pinned runtime generation
    before recreating ``runtime_state``.  A stale post-clear worker therefore
    returns ``None`` instead of resurrecting ``last_replied_ts``/action digest.
    The optional form preserves the small storage primitive used by tests and
    non-job maintenance callers.
    """
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                if source_job_id is not None:
                    db._lock_chat_user_fence_on_cursor(cur, str(user_id))
                    cur.execute(
                        "SELECT 1 FROM agent_jobs AS job "
                        "JOIN v2_runtime_state AS state "
                        "ON state.user_id=job.user_id "
                        "WHERE job.id=%s AND job.user_id=%s "
                        "AND job.expected_runtime_generation="
                        "state.runtime_generation",
                        (source_job_id, str(user_id)),
                    )
                    if cur.fetchone() is None:
                        return None
                cur.execute(
                    "INSERT INTO runtime_state (user_id, state_json, updated_at) "
                    "VALUES (%s,%s,now()) "
                    "ON CONFLICT (user_id) DO UPDATE "
                    "SET state_json = runtime_state.state_json || EXCLUDED.state_json, "
                    "    updated_at = now() "
                    "RETURNING state_json",
                    (user_id, Jsonb(dict(patch or {}))),
                )
                return dict(cur.fetchone()["state_json"])


def get_chat_tail_anchor(user_id: str) -> int | None:
    """Pinned start seq of the optional replay window, or None when this user
    has no anchor yet."""
    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT anchor_seq FROM v2_chat_tail_anchor WHERE user_id=%s",
            (str(user_id),),
        ).fetchone()
    return int(row[0]) if row is not None else None


def set_chat_tail_anchor(user_id: str, anchor_seq: int) -> None:
    """Advance the anchor.  Monotonic by construction: a concurrent turn
    holding a stale value can only lose, never drag the anchor backwards
    (a regressing anchor would widen the optional replay window and reorder
    the cached prompt prefix — precisely what the anchor exists to prevent)."""
    value = max(0, int(anchor_seq))
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_chat_tail_anchor (user_id, anchor_seq) "
            "VALUES (%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "anchor_seq=GREATEST(v2_chat_tail_anchor.anchor_seq, "
            "EXCLUDED.anchor_seq), "
            "updated_at=now()",
            (str(user_id), value),
        )


# ---------------------------------------------------------------------------
# History search（只读；见 model_api_runtime/v2/history_search.py 的纯逻辑内核）
# ---------------------------------------------------------------------------

# History-search 可见性 contract（spec §7）：只回用户可见的双方消息。角色白名
# 单 user/human/openclaw；合成流量（verify_ping / resident_maintenance，写法同
# db._CHAT_COVERAGE_SOURCE_PREDICATE）在 SQL LIMIT 之前排除，Python 侧后过滤会
# 让一段长合成行序列把真实候选挤出窗口（同 chat_capture_messages_after_seq 的
# 教训）。
_HISTORY_VISIBLE_PREDICATE = (
    # assistant 侧三个历史 role 都要含（serve_worker._ASSISTANT_ROLES：V1 时代
    # 写 'agent'，部分路径写 'assistant'，现行 'openclaw'）——漏掉前两个会让老
    # 用户的历史回复整段搜不到。
    "doc->>'role' IN ('user','human','openclaw','assistant','agent') "
    "AND COALESCE(doc->>'source','') "
    "NOT IN ('verify_ping','resident_maintenance')"
)


def list_level0_summary_leaves(user_id, *, through_seq: int) -> list[dict]:
    """History-search 扫描提示专用：end_seq<=through_seq 的全部 level-0 叶子。

    刻意不复用 get_summary_frontier_state 的 canonical cover——那个查询会把被
    更高层 checkpoint 覆盖的子节点排除掉，而扫描提示恰恰需要每片叶子的精确
    start/end 范围（spec §4）。返回的 summary_envelope 仍是密文，由调用方送
    enclave 解密后做归一化子串匹配；legacy_opaque 叶子（start_seq=0，无精确
    source witness）也一并返回，但绝不参与命中段的范围推断——其覆盖的原文可能
    已被旧 retention 清理，raw 兜底扫不到时置 coverage_gap。

    按 end_seq 降序返回（recent-first 的提示扫描顺序）。只读、无锁；叶子段
    append-only，end_seq<=snapshot 过滤即天然自洽。
    """
    upper = int(through_seq)
    if upper < 0:
        raise ValueError("through_seq must be >= 0")
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT segment_id,format_version,coverage_kind,level,"
                "start_seq,end_seq,source_message_count,"
                "legacy_opaque_through_seq,summary_envelope "
                "FROM v2_conversation_summary_segments "
                "WHERE user_id=%s AND level=0 AND end_seq<=%s "
                "ORDER BY end_seq DESC,start_seq DESC",
                (str(user_id), upper),
            )
            return [dict(row) for row in cur.fetchall()]


def chat_history_candidate_rows(
    user_id,
    *,
    min_seq: int,
    max_seq: int,
    start_ts: float | None = None,
    end_ts: float | None = None,
    limit: int,
) -> list[dict]:
    """一个降序候选窗口的**元数据**（绝不返回、也不解密正文）。

    planner（history_search.next_batch）给出 seq ∈ [min_seq, max_seq]，这里取
    其中最新的 ``limit`` 条可见候选，按 seq 降序（=扫描优先级顺序）返回。
    可见性过滤全部发生在 SQL LIMIT 之前（见 _HISTORY_VISIBLE_PREDICATE）；
    时间范围 start inclusive / end exclusive（spec §3.1）。

    每行只带：seq / msg_id / ts / role / content_type / has_ciphertext。
    ``has_ciphertext`` 镜像 serve_worker._decrypt_chat_rows 的可读性判据
    （body_ct 非空且 K_enclave 非空）：False 的行（本地-only、R2 指针化的
    图片/文件正文等）不可在本路径解密，调用方计入 unavailable_count 或按
    content_type 走 caption 分支——图片/文件二进制正文（R2）绝不读。
    """
    lower = int(min_seq)
    upper = int(max_seq)
    if lower < 0 or upper < 0:
        raise ValueError("history candidate seq bounds must be >= 0")
    bounded = max(1, min(int(limit), 1000))
    if upper < lower:
        return []
    predicate = (
        "WHERE user_id=%s AND seq>=%s AND seq<=%s "
        f"AND {_HISTORY_VISIBLE_PREDICATE} "
    )
    params: list = [str(user_id), lower, upper]
    if start_ts is not None:
        predicate += "AND ts>=%s "
        params.append(float(start_ts))
    if end_ts is not None:
        predicate += "AND ts<%s "
        params.append(float(end_ts))
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT seq,msg_id,ts,doc->>'role' AS role,"
                "COALESCE(doc->>'content_type','') AS content_type,"
                "(COALESCE(doc->>'body_ct','')<>'' "
                " AND doc->>'K_enclave' IS NOT NULL) AS has_ciphertext "
                "FROM chat_messages "
                + predicate
                + "ORDER BY seq DESC LIMIT %s",
                (*params, bounded),
            )
            return [
                {
                    "seq": int(row["seq"]),
                    "msg_id": str(row["msg_id"]),
                    "ts": float(row["ts"]),
                    "role": str(row["role"] or ""),
                    "content_type": str(row["content_type"] or ""),
                    "has_ciphertext": bool(row["has_ciphertext"]),
                }
                for row in cur.fetchall()
            ]


def _history_full_row(seq, msg_id, ts, doc) -> dict:
    """doc 与权威关系列合并（同 db.chat_messages_after_seq 的契约）：
    ``msg_id``/``ts``/``seq`` 覆盖 doc 里可能过期的副本。"""
    return {**dict(doc or {}), "id": str(msg_id), "ts": float(ts), "seq": int(seq)}


def chat_history_rows_by_seqs(user_id, seqs) -> list[dict]:
    """给定 seq 集合的**完整密文行**（含 body_ct/K_enclave/caption_*），seq 降序。

    候选选择走 chat_history_candidate_rows（元数据）在前，这里只按 seq 精确
    取回密文供 enclave 批量解密；可见性谓词再套一遍属防御（两次查询之间行
    不可变，谓词幂等）。附件行 body 密文的剥离（caption-only 契约，spec §7）
    由 readside 协调层在投影时做——本函数保持"取回原行"的单一职责。
    """
    wanted = sorted({int(s) for s in seqs}, reverse=True)
    if not wanted:
        return []
    if wanted[-1] < 0:
        raise ValueError("history row seqs must be >= 0")
    with _pool().connection() as conn:
        rows = conn.execute(
            "SELECT seq,msg_id,ts,doc FROM chat_messages "
            "WHERE user_id=%s AND seq=ANY(%s) "
            f"AND {_HISTORY_VISIBLE_PREDICATE} "
            "ORDER BY seq DESC",
            (str(user_id), wanted),
        ).fetchall()
    return [_history_full_row(r[0], r[1], r[2], r[3]) for r in rows]


def chat_history_anchor_row(user_id, message_id) -> dict | None:
    """history_fetch 的锚点行（完整密文行）。

    不存在与不可见（角色/合成流量排除）统一返回 None——调用方按
    ``not_found_or_not_visible`` 报，不区分（spec §3.2：不靠 message_id
    难猜当权限控制）。
    """
    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT seq,msg_id,ts,doc FROM chat_messages "
            "WHERE user_id=%s AND msg_id=%s "
            f"AND {_HISTORY_VISIBLE_PREDICATE} "
            "LIMIT 1",
            (str(user_id), str(message_id)),
        ).fetchone()
    if row is None:
        return None
    return _history_full_row(row[0], row[1], row[2], row[3])


def _neighbor_window_sql() -> str:
    """One statement: both neighbour windows and both existence counts.

    取行与计数**必须在同一个 statement snapshot 内**。分成多条 SQL 是不够的：
    连接池是 ``autocommit=True``（``db._pool``），READ COMMITTED 下每条语句各
    取一次快照；而 ``chat_messages`` 也**不是** append-only（存在单条删除路径，
    见 ``db.py`` 的 chat 删除）。并发删除落在两条语句之间时，取行看到的那批已
    消失、计数却数到后面补位的行，``available - len(rows)`` 就会算出一个凭空的
    ``omitted_*``——正是这个字段要根除的谎报。一条 CTE 里的所有分支共享同一个
    快照，这种错位在原理上就不存在（顺带把 fetch 的往返从三次降到一次）。

    每个 CTE 分支都自带 ``LIMIT``，在请求窗口处提前终止；计数结果因此是
    ``min(请求条数, 实际条数)``，永不全表扫。四个分支共用同一份可见性谓词。
    """
    pred = _HISTORY_VISIBLE_PREDICATE
    return (
        "WITH before_rows AS ("
        f" SELECT seq,msg_id,ts,doc FROM chat_messages"
        f" WHERE user_id=%(uid)s AND seq<%(anchor)s AND {pred}"
        "  ORDER BY seq DESC LIMIT %(n_before)s"
        "), after_rows AS ("
        f" SELECT seq,msg_id,ts,doc FROM chat_messages"
        f" WHERE user_id=%(uid)s AND seq>%(anchor)s AND {pred}"
        "  ORDER BY seq ASC LIMIT %(n_after)s"
        "), before_probe AS ("
        f" SELECT 1 FROM chat_messages"
        f" WHERE user_id=%(uid)s AND seq<%(anchor)s AND {pred}"
        "  ORDER BY seq DESC LIMIT %(want_before)s"
        "), after_probe AS ("
        f" SELECT 1 FROM chat_messages"
        f" WHERE user_id=%(uid)s AND seq>%(anchor)s AND {pred}"
        "  ORDER BY seq ASC LIMIT %(want_after)s"
        ")"
        " SELECT 'before' AS side, seq, msg_id, ts, doc FROM before_rows"
        " UNION ALL"
        " SELECT 'after', seq, msg_id, ts, doc FROM after_rows"
        " UNION ALL"
        " SELECT 'count_before', (SELECT count(*) FROM before_probe),"
        "        NULL, NULL, NULL"
        " UNION ALL"
        " SELECT 'count_after', (SELECT count(*) FROM after_probe),"
        "        NULL, NULL, NULL"
    )


def chat_history_neighbor_rows(
    user_id,
    anchor_seq: int,
    *,
    before: int,
    after: int,
    before_requested: int | None = None,
    after_requested: int | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """锚点邻居（该用户 seq 序取，客户端时间戳不可靠——spec §3.2）。

    ``before``/``after`` = 这次真正取回密文的条数（已被回合预算钳过）；
    ``before_requested``/``after_requested`` = 模型请求的窗口大小（省略则同上）。

    返回 ``(before_rows, after_rows, available)``：前两项按**旧→新**排列（返回
    结构的展示序），``available`` 是 ``{"before": n, "after": n}``——请求窗口内
    实际存在多少条可见邻居。调用方据此算 ``omitted_*``，**不许**从"取回条数"
    反推。取行与计数在**同一条语句**里完成，共享一个快照（见
    ``_neighbor_window_sql``）；可见性谓词与候选查询完全同一套。
    """
    anchor = int(anchor_seq)
    if anchor < 0:
        raise ValueError("anchor_seq must be >= 0")
    n_before = max(0, int(before))
    n_after = max(0, int(after))
    # 请求窗口至少要覆盖真的取回的条数，否则 available 会小于返回行数。
    want_before = max(n_before, 0 if before_requested is None
                      else int(before_requested))
    want_after = max(n_after, 0 if after_requested is None
                     else int(after_requested))
    older: list[dict] = []
    newer: list[dict] = []
    available = {"before": 0, "after": 0}
    if not (n_before or n_after or want_before or want_after):
        return older, newer, available
    with _pool().connection() as conn:
        rows = conn.execute(
            _neighbor_window_sql(),
            {
                "uid": str(user_id),
                "anchor": anchor,
                "n_before": n_before,
                "n_after": n_after,
                "want_before": want_before,
                "want_after": want_after,
            },
        ).fetchall()
    for side, seq, msg_id, ts, doc in rows:
        if side == "before":
            older.append(_history_full_row(seq, msg_id, ts, doc))
        elif side == "after":
            newer.append(_history_full_row(seq, msg_id, ts, doc))
        elif side == "count_before":
            available["before"] = int(seq or 0)
        elif side == "count_after":
            available["after"] = int(seq or 0)
    # before 分支按 seq DESC 取（要最靠近锚点的 N 条），展示序是旧→新。
    older.reverse()
    return older, newer, available
