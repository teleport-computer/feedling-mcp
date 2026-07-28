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
import time
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import db
from core import wake_bus
from notices import catalog as notices_catalog

log = logging.getLogger("feedling.runtime_v2.jobs_store")

LANES = {
    "chat",
    "manual_wake",
    "heartbeat",
    "scheduled",
    "capture",
    "maintenance",
    "dream",
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
    # Offline analysis must never contend with foreground chat/wake or memory
    # maintenance. One generic job drains one encrypted failed-turn review.
    "trajectory_review": 1,
}
# Chat admission and execution use separate columns and clocks. Pending rows
# have a short queue deadline so an admitted turn cannot wait forever when the
# fleet dies. Claim starts a distinct owner-fenced execution lease. Workers
# renew only at explicit progress boundaries; a provider call that is itself
# wedged therefore cannot keep a blind heartbeat alive forever.
PENDING_CHAT_TTL_SEC = _positive_float_env("FEEDLING_V2_CHAT_PENDING_TTL_SEC", "120")
RUNNING_TTL_SEC = _positive_float_env("FEEDLING_V2_LEASE_TTL_SEC", "300")

_ACTIVE_STATUSES = ("pending", "claimed", "running")
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
    if code in {"queue_timeout", "lease_timeout", "runtime_expired"}:
        return "turn_timeout"
    if "prompt_frontier_exhausted" in code:
        return "context_overflow"
    if code.endswith(":empty_reply"):
        return "reply_parse_failed"
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
    """Capture one chat failure's route and input frontier in its transaction."""
    cur.execute(
        "INSERT INTO v2_terminal_failure_outbox "
        "(job_id,user_id,error_code,error_class,"
        " target_route_id,target_route_updated_at,"
        " reply_frontier_seq,reply_parent_message_id) "
        "SELECT j.id,j.user_id,%s,%s,r.id,r.updated_at,"
        " input.seq,input.msg_id FROM agent_jobs j "
        "LEFT JOIN LATERAL (SELECT id,updated_at FROM model_api_routes "
        "  WHERE user_id=j.user_id AND is_active LIMIT 1) r ON TRUE "
        "LEFT JOIN LATERAL (SELECT seq,msg_id FROM chat_messages "
        "  WHERE user_id=j.user_id AND doc->>'role' IN ('user','human') "
        "  AND COALESCE(doc->>'source','') "
        "    NOT IN ('verify_ping','resident_maintenance') "
        "  ORDER BY seq DESC LIMIT 1) input ON TRUE "
        "WHERE j.id=%s AND j.user_id=%s AND j.lane='chat' "
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


def _recover_review_runner_on_cursor(cur, runner_job_id: int | str) -> None:
    """Release a review claim whose generic runner terminalized unexpectedly."""
    cur.execute(
        "UPDATE v2_trajectory_reviews r SET "
        "status=CASE WHEN r.attempt_count<%s THEN 'pending' ELSE 'failed' END, "
        "claimed_by_job_id=NULL, "
        "last_error=CASE WHEN r.attempt_count<%s THEN r.last_error "
        "                ELSE COALESCE(r.last_error,'review_runner_failed') END, "
        "finished_at=CASE WHEN r.attempt_count<%s THEN NULL ELSE now() END "
        "FROM agent_jobs j WHERE j.id=%s AND j.lane=%s "
        "AND r.claimed_by_job_id=j.id AND r.status='running' "
        "RETURNING r.user_id",
        (
            _TRAJECTORY_REVIEW_MAX_ATTEMPTS,
            _TRAJECTORY_REVIEW_MAX_ATTEMPTS,
            _TRAJECTORY_REVIEW_MAX_ATTEMPTS,
            runner_job_id,
            _TRAJECTORY_REVIEW_LANE,
        ),
    )
    rows = cur.fetchall()
    for row in rows:
        user_id = row["user_id"] if isinstance(row, dict) else row[0]
        _ensure_review_runner_on_cursor(cur, str(user_id))


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
    return int(cur.fetchone()[0] or 0)


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
    """Fail an owned job and transactionally queue chat failure visibility.

    Terminalization, the user-visible outbox obligation, and any trajectory
    review handoff share one explicit transaction, so there is no process-crash
    window between them. Background lanes remain silent and do not get an
    outbox row.
    """
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
            if str(row[2]) == "chat":
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
            _recover_review_runner_on_cursor(cur, job_id)
            _queue_failure_review_on_cursor(cur, job_id)
            return True


def mark_expired(job_id, error: str = "runtime_expired") -> None:
    with _pool().connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                "UPDATE agent_jobs SET status='expired',finished_at=now(),last_error=%s "
                "WHERE id=%s RETURNING id,user_id,lane",
                (str(error)[:500], job_id),
            )
            row = cur.fetchone()
            if row is not None:
                if str(row[2]) == "chat":
                    _queue_terminal_failure_on_cursor(
                        cur, row[0], str(row[1]), error
                    )
                _recover_review_runner_on_cursor(cur, job_id)
                _queue_failure_review_on_cursor(cur, job_id)


def reap_stuck_job_rows(now=None) -> list[dict]:
    """Expire overdue pending admissions and claimed/running execution leases.

    The terminal transition releases the single-flight slot. ``now`` is an
    injectable epoch for deterministic tests; ``None`` uses database time.
    Returned rows let the independent watchdog surface chat timeouts.
    """
    ts = float(now) if now is not None else None
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
                    if str(row["lane"]) == "chat":
                        _queue_terminal_failure_on_cursor(
                            cur,
                            row["id"],
                            str(row["user_id"]),
                            str(row["last_error"]),
                        )
                    _recover_review_runner_on_cursor(cur, row["id"])
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
                        now_iso = datetime.now(timezone.utc).isoformat().replace(
                            "+00:00", "Z"
                        )
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
                                "last_capture_completed_at": time.time(),
                                "pending_capture_key": "",
                                "capture_fail_streak": 0,
                                "last_capture_failed_at": 0.0,
                                "updated_at": now_iso,
                            }
                        )
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
    where_job = " AND job_id=%s" if job_id is not None else ""
    ts = float(now) if now is not None else None
    args: tuple = (ts, job_id, bounded) if job_id is not None else (ts, bounded)
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT job_id,user_id,error_code,target_route_id,"
                "target_route_updated_at,status_delivered_at,"
                "runtime_error_delivered_at,error_class,reply_frontier_seq,"
                "reply_parent_message_id,reply_delivered_at "
                "FROM v2_terminal_failure_outbox "
                f"WHERE {delivered_column} IS NULL "
                f"AND {next_column} <= COALESCE(to_timestamp(%s),now())"
                f"{where_job} ORDER BY {last_column} NULLS FIRST,"
                f"{next_column},created_at,job_id LIMIT %s",
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
                    "SELECT user_id,status_delivered_at "
                    "FROM v2_terminal_failure_outbox WHERE job_id=%s FOR UPDATE",
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
                cur.execute(
                    "SELECT 1 FROM v2_effect_outbox e "
                    "WHERE e.user_id=%s AND e.job_id>%s AND e.status='applied' "
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


def _deliver_terminal_failure_reply(row: dict) -> bool:
    """Write one encrypted, parent-linked failure bubble exactly once."""
    from core import envelope as core_envelope
    from core import store as core_store

    job_id = row["job_id"]
    user_id = str(row["user_id"])
    frontier = int(row.get("reply_frontier_seq") or 0)
    parent_id = str(row.get("reply_parent_message_id") or "").strip()
    if frontier <= 0 or not parent_id:
        return _ack_terminal_failure_reply(job_id)

    message_id = hashlib.sha256(
        f"v2-terminal-failure:{job_id}".encode("utf-8")
    ).hexdigest()[:32]
    existing = db.chat_get_strict(user_id, message_id)
    if existing is not None:
        if str(existing.get("terminal_failure_job_id") or "") != str(job_id):
            raise RuntimeError("terminal failure reply id collision")
        return _ack_terminal_failure_reply(job_id)

    error_class = _terminal_error_class(
        row.get("error_code"), row.get("error_class")
    )
    blame = notices_catalog.blame_for(error_class)
    user_text = notices_catalog.user_text_for(error_class)
    reply_text = (
        user_text
        if blame == "user_provider"
        else _TERMINAL_FAILURE_FALLBACK_REPLY
    )
    store = core_store.get_store(user_id)
    envelope, error = core_envelope._build_shared_envelope_for_store(
        store,
        reply_text.encode("utf-8"),
        item_id=message_id,
    )
    if envelope is None:
        raise RuntimeError(error or "terminal failure envelope build failed")
    message = store._build_chat_message(
        "openclaw",
        "model_api",
        envelope,
        extra={
            "turn_failure_error_class": error_class,
            "turn_failure_blame": blame,
            "turn_failure_user_text": user_text,
            "terminal_failure_job_id": str(job_id),
            "reply_to_message_id": parent_id,
        },
    )
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
                    "SELECT user_id,error_code,target_route_id,"
                    "target_route_updated_at,runtime_error_delivered_at "
                    "FROM v2_terminal_failure_outbox WHERE job_id=%s FOR UPDATE",
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None or row["runtime_error_delivered_at"] is not None:
                    return False
                route_id = row.get("target_route_id")
                if route_id is not None:
                    cur.execute(
                        "UPDATE model_api_routes SET last_runtime_error=%s,"
                        "last_runtime_error_class='',updated_at=now() "
                        "WHERE id=%s AND user_id=%s AND is_active "
                        "AND updated_at IS NOT DISTINCT FROM %s "
                        "AND NOT EXISTS (SELECT 1 FROM agent_jobs newer "
                        "  WHERE newer.user_id=%s AND newer.lane='chat' "
                        "  AND newer.status='completed' AND newer.id>%s)",
                        (
                            str(row["error_code"]),
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
            if record_terminal_error is not None:
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
    worker_id: str, *, kind: str = "turn", capacity: int = 1
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
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at, kind, capacity) "
            "VALUES (%s, now(), %s, %s) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = now(), "
            "kind = EXCLUDED.kind, capacity = EXCLUDED.capacity",
            (str(worker_id), str(kind), max(0, int(capacity))),
        )


def workers_alive(*, within_sec: int = 30) -> bool:
    """True iff at least one serve_worker TURN process has recorded a heartbeat
    within the last ``within_sec`` seconds. Used by the chat/send v2 liveness
    guard. Genesis heartbeats are deliberately invisible here — a live genesis
    thread says nothing about whether any turn slot exists to drain the job."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM v2_worker_heartbeats "
                "WHERE kind = 'turn' AND capacity > 0 "
                "AND beat_at > now() - make_interval(secs => %s))",
                (int(within_sec),),
            )
            return bool(cur.fetchone()[0])


def live_worker_count(*, within_sec: int = 30) -> int:
    """窗口内有心跳的 serve_worker TURN 进程数（workers_alive 的计数版，喂 admission
    ceiling）。genesis 心跳不计入——它不占 turn 槽位。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM v2_worker_heartbeats "
                "WHERE kind = 'turn' AND capacity > 0 "
                "AND beat_at > now() - make_interval(secs => %s)",
                (int(within_sec),),
            )
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


def live_worker_capacity(*, within_sec: int = 30) -> int:
    """Sum executable turn slots, not heartbeat processes."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(sum(capacity),0) FROM v2_worker_heartbeats "
                "WHERE kind='turn' AND beat_at > now() - make_interval(secs => %s)",
                (int(within_sec),),
            )
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
                "SELECT worker_id, kind, capacity, "
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


def inflight_job_count() -> int:
    """在飞 job 数（pending/claimed/running）。单飞唯一索引 → 约等活跃用户数。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM agent_jobs "
                "WHERE status IN ('pending','claimed','running')"
            )
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
                "prompt_frontier_exhaustion_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s) "
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


def pending_job_count() -> int:
    """当前排队中（status='pending'，尚未被任何 worker claim）的 job 数。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM agent_jobs WHERE status='pending'")
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
                return cur.rowcount == 1


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
                        "WHERE user_id=%s AND seq>%s AND seq<=%s",
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
                cur.execute(
                    "SELECT COALESCE(MIN(seq),0) AS first_seq,"
                    "COALESCE(MAX(seq),0) AS last_seq,COUNT(*) AS n "
                    "FROM chat_messages WHERE user_id=%s AND seq>%s AND seq<=%s",
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
                    return dict(row) if row is not None else None
    except psycopg.errors.UniqueViolation:
        # Concurrent revision-0 creators: one wins; the loser observes a clean
        # conflict rather than surfacing a database exception to the model.
        return None


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
                    if admitted and cur.rowcount:
                        _ensure_review_runner_on_cursor(cur, str(user_id))
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
                return dict(cur.fetchone())


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
    时间 + BYOK 支付冷却截止 + screen_watch 的跨-tick 状态 last_screen_watch_frame_id），
    无行返回 None（该用户尚未被调度器接管过）。

    **四个时间列一律以 epoch 浮点数返回**（`EXTRACT(EPOCH FROM ...)`），与
    `upsert_wake_schedule` 收的 epoch float 对称，调用方可以直接做算术比较。
    曾经只有 `next_screen_watch_at` 是 float、其余三列是 `datetime` —— 同一个 dict 里四个
    同类字段两种类型是个地雷（谁会记得只有第四个能做减法？）。本函数没有生产调用方，
    只有测试，所以统一成 float 的代价是零。`updated_at` 保持 datetime：它是审计字段，
    没人拿它做算术。"""
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
                "last_screen_watch_frame_id, self_wake_streak, "
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
            "next_screen_watch_at, last_screen_watch_frame_id, updated_at) "
            "VALUES (%s, to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), "
            "to_timestamp(%s), %s, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "next_heartbeat_at = COALESCE(EXCLUDED.next_heartbeat_at, v2_wake_schedule.next_heartbeat_at), "
            "next_capture_at = COALESCE(EXCLUDED.next_capture_at, v2_wake_schedule.next_capture_at), "
            "payment_cooldown_until = COALESCE(EXCLUDED.payment_cooldown_until, v2_wake_schedule.payment_cooldown_until), "
            "next_screen_watch_at = COALESCE(EXCLUDED.next_screen_watch_at, v2_wake_schedule.next_screen_watch_at), "
            "last_screen_watch_frame_id = COALESCE(EXCLUDED.last_screen_watch_frame_id, v2_wake_schedule.last_screen_watch_frame_id), "
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
            ),
        )


_LATEST_GENUINE_USER_SEQ_SQL = (
    "(SELECT COALESCE(MAX(message.seq),0) FROM chat_messages AS message "
    "WHERE message.user_id=schedule.user_id "
    "AND message.doc->>'role' IN ('user','human') "
    "AND COALESCE(message.doc->>'source','') "
    "NOT IN ('verify_ping','resident_maintenance'))"
)


def due_heartbeat_users(*, now: float | None = None, limit: int = 500) -> list[str]:
    """到期需要心跳唤醒的 user_id 列表（next_heartbeat_at 已到且不在 BYOK 支付冷却
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
    """到期需要屏幕监看唤醒的 user_id 列表（next_screen_watch_at 已到且不在 BYOK 支付
    冷却窗口内），按 next_screen_watch_at 升序，供 D3 调度器 poll 后逐个
    enqueue_job(..., 'screen_watch')。镜像 due_heartbeat_users 的每一处语义（NULL 不
    算到期；now 可注入 epoch 浮点数用于确定性测试；payment_cooldown_until 排除——
    一个已死的 BYOK key 不该被屏幕轮询器持续锤）。"""
    ts = float(now) if now is not None else None
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM v2_wake_schedule "
                "WHERE next_screen_watch_at IS NOT NULL "
                "AND next_screen_watch_at <= COALESCE(to_timestamp(%s), now()) "
                "AND (payment_cooldown_until IS NULL "
                "     OR payment_cooldown_until <= COALESCE(to_timestamp(%s), now())) "
                "ORDER BY next_screen_watch_at LIMIT %s",
                (ts, ts, int(limit)),
            )
            return [row[0] for row in cur.fetchall()]


SCHEDULED_WAKE_STREAM = "proactive_scheduled_wakes_v2"


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
