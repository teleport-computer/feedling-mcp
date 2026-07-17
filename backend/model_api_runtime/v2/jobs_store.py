"""DB access for V2 jobs, status events, summaries, schedules, and metrics.

CONTRIBUTING §2：新表存取逻辑全部收进本模块（jobs_store）。连接走 db.get_pool()
（autocommit）；需要跨语句持行锁的地方（SKIP LOCKED claim / single-flight 选举）
用显式 conn.transaction()。行返回 dict 用 psycopg.rows.dict_row 游标。
"""
from __future__ import annotations

import logging
import math
import os
import re
import time

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import db
from core import wake_bus

log = logging.getLogger("feedling.runtime_v2.jobs_store")

LANES = {"chat", "manual_wake", "heartbeat", "scheduled", "capture", "maintenance", "dream", "screen_watch"}


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
}
# Chat admission and execution use separate columns and clocks. Pending rows
# have a short queue deadline so an admitted turn cannot wait forever when the
# fleet dies. Claim starts a distinct owner-fenced execution lease. Workers
# renew only at explicit progress boundaries; a provider call that is itself
# wedged therefore cannot keep a blind heartbeat alive forever.
PENDING_CHAT_TTL_SEC = _positive_float_env("FEEDLING_V2_CHAT_PENDING_TTL_SEC", "120")
RUNNING_TTL_SEC = _positive_float_env("FEEDLING_V2_LEASE_TTL_SEC", "300")

_ACTIVE_STATUSES = ("pending", "claimed", "running")

_TERMINAL_ERROR_CODE_RE = re.compile(r"^[a-z0-9_:-]{1,120}$")


def _terminal_error_code(error: object) -> str:
    """Return the only form allowed to cross the user-visible failure outbox.

    Worker call sites normally pass stable codes already.  This final boundary
    prevents a future/legacy caller from copying an exception message into
    ``last_runtime_error`` through the reconciler.
    """
    value = str(error or "")
    return value if _TERMINAL_ERROR_CODE_RE.fullmatch(value) else "runtime_failed"


def _pool():
    return db.get_pool()


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
        "SELECT id,status,expected_runtime_generation,CASE "
        "WHEN status='pending' THEN "
        "  COALESCE(queue_deadline_at, deadline_at, "
        "    CASE WHEN lane='chat' THEN "
        "      created_at + make_interval(secs => %s) END) <= now() "
        "ELSE COALESCE(lease_expires_at, deadline_at) IS NOT NULL "
        "  AND COALESCE(lease_expires_at, deadline_at) <= now() "
        "END AS stale "
        "FROM agent_jobs "
        "WHERE user_id=%s AND lane=%s "
        "AND status IN ('pending','claimed','running') "
        "ORDER BY id LIMIT 1 FOR UPDATE",
        (float(PENDING_CHAT_TTL_SEC), user_id, lane),
    )
    existing = cur.fetchone()
    generation_stale = (
        existing is not None
        and expected_generation is not None
        and (
            existing["expected_runtime_generation"] is None
            or int(existing["expected_runtime_generation"])
            != int(expected_generation)
        )
    )
    if existing is not None and not bool(existing["stale"]) and not generation_stale:
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
            cur.execute(
                "INSERT INTO v2_terminal_failure_outbox "
                "(job_id,user_id,error_code,target_route_id,target_route_updated_at) "
                "SELECT j.id,j.user_id,j.last_error,r.id,r.updated_at "
                "FROM agent_jobs j LEFT JOIN LATERAL ("
                "  SELECT id,updated_at FROM model_api_routes "
                "  WHERE user_id=j.user_id AND is_active LIMIT 1"
                ") r ON TRUE WHERE j.id=%s AND j.lane='chat' "
                "ON CONFLICT (job_id) DO NOTHING",
                (existing["id"],),
            )
    cur.execute(
        "INSERT INTO agent_jobs "
        "(user_id, lane, status, reason, trace_id, priority, queue_deadline_at, "
        "expected_runtime_generation) "
        "VALUES (%s,%s,'pending',%s,%s,%s,"
        "CASE WHEN %s::timestamptz IS NOT NULL THEN %s::timestamptz "
        "     WHEN %s='chat' THEN now() + make_interval(secs => %s) "
        "     ELSE NULL END, %s) RETURNING id",
        (
            user_id, lane, reason, trace_id, int(priority),
            deadline_at, deadline_at, lane, float(PENDING_CHAT_TTL_SEC),
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
                                    control["runtime_generation"])
                        return coalesce_or_insert_on_cursor(
                            cur, user_id, lane, reason=reason, trace_id=trace_id,
                            priority=priority, deadline_at=deadline_at,
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
                    cur, user_id, lane, reason=reason, trace_id=trace_id,
                    priority=priority, deadline_at=deadline_at,
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
    if lanes is None:
        select_sql = (
            "SELECT j.id, j.user_id, j.expected_runtime_generation FROM agent_jobs j "
            "JOIN users u ON u.user_id=j.user_id "
            "WHERE j.status='pending' "
            "AND (COALESCE(j.queue_deadline_at, j.deadline_at, "
            "CASE WHEN j.lane='chat' THEN "
            "j.created_at + make_interval(secs => %s) END) IS NULL OR "
            "COALESCE(j.queue_deadline_at, j.deadline_at, "
            "CASE WHEN j.lane='chat' THEN "
            "j.created_at + make_interval(secs => %s) END) > now()) "
            "AND NOT EXISTS (SELECT 1 FROM agent_jobs active "
            "WHERE active.user_id=j.user_id "
            "AND active.status IN ('claimed','running')) "
            "ORDER BY j.priority DESC, j.created_at LIMIT 1"
        )
        select_args = (float(PENDING_CHAT_TTL_SEC), float(PENDING_CHAT_TTL_SEC))
    else:
        select_sql = (
            "SELECT j.id, j.user_id, j.expected_runtime_generation FROM agent_jobs j "
            "JOIN users u ON u.user_id=j.user_id "
            "WHERE j.status='pending' "
            "AND (COALESCE(j.queue_deadline_at, j.deadline_at, "
            "CASE WHEN j.lane='chat' THEN "
            "j.created_at + make_interval(secs => %s) END) IS NULL OR "
            "COALESCE(j.queue_deadline_at, j.deadline_at, "
            "CASE WHEN j.lane='chat' THEN "
            "j.created_at + make_interval(secs => %s) END) > now()) "
            "AND j.lane = ANY(%s) "
            "AND NOT EXISTS (SELECT 1 FROM agent_jobs active "
            "WHERE active.user_id=j.user_id "
            "AND active.status IN ('claimed','running')) "
            "ORDER BY j.priority DESC, j.created_at LIMIT 1"
        )
        select_args = (float(PENDING_CHAT_TTL_SEC), float(PENDING_CHAT_TTL_SEC), list(lanes))

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
                    cur.execute(select_sql, select_args)
                    head = cur.fetchone()
                    if head is None:
                        return None
                    # v2_runtime_state is the ownership authority. Lock it in
                    # the same transaction as the pending->claimed transition
                    # so a cutover cannot race between validation and claim.
                    cur.execute(
                        "SELECT hosted_runtime_state, runtime_generation "
                        "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                        (head["user_id"],),
                    )
                    control = cur.fetchone()
                    cur.execute(
                        "SELECT expected_runtime_generation FROM agent_jobs j "
                        "WHERE j.id=%s AND j.status='pending' "
                        "AND (COALESCE(j.queue_deadline_at, j.deadline_at, "
                        "CASE WHEN j.lane='chat' THEN "
                        "j.created_at + make_interval(secs => %s) END) IS NULL OR "
                        "COALESCE(j.queue_deadline_at, j.deadline_at, "
                        "CASE WHEN j.lane='chat' THEN "
                        "j.created_at + make_interval(secs => %s) END) > now()) "
                        "AND NOT EXISTS (SELECT 1 FROM agent_jobs active "
                        "WHERE active.user_id=j.user_id "
                        "AND active.status IN ('claimed','running')) "
                        "FOR UPDATE OF j SKIP LOCKED",
                        (
                            head["id"],
                            float(PENDING_CHAT_TTL_SEC),
                            float(PENDING_CHAT_TTL_SEC),
                        ),
                    )
                    locked_job = cur.fetchone()
                    if locked_job is None:
                        continue
                    if control is None or str(control["hosted_runtime_state"]) != "v2":
                        cur.execute(
                            "UPDATE agent_jobs SET status='superseded', finished_at=now(), "
                            "last_error='runtime_state_not_v2' WHERE id=%s",
                            (head["id"],),
                        )
                        continue
                    expected_gen = locked_job["expected_runtime_generation"]
                    current_generation = int(control["runtime_generation"])
                    if expected_gen is not None and int(expected_gen) != current_generation:
                        cur.execute(
                            "UPDATE agent_jobs SET status='superseded', finished_at=now(), "
                            "last_error='stale_runtime_generation' WHERE id=%s",
                            (head["id"],),
                        )
                        continue
                    cur.execute(
                        "UPDATE agent_jobs SET status='claimed', claimed_by=%s, claimed_at=now(), "
                        "expected_runtime_generation="
                        "COALESCE(expected_runtime_generation,%s), "
                        "lease_expires_at = now() + make_interval(secs => %s), "
                        "deadline_at = now() + make_interval(secs => %s) "
                        "WHERE id=%s RETURNING *",
                        (
                            worker_id,
                            current_generation,
                            float(RUNNING_TTL_SEC),
                            float(RUNNING_TTL_SEC),
                            head["id"],
                        ),
                    )
                    return cur.fetchone()


def mark_running(job_id, *, claimed_by: str) -> bool:
    with _pool().connection() as conn:
        with conn.transaction():
            # Discover the user without taking a job lock, then preserve the
            # global runtime-state -> job lock order. Holding the state row
            # through the transition gives turn start a real ownership
            # linearization point instead of a SELECT/UPDATE TOCTOU window.
            row = conn.execute(
                "SELECT user_id FROM agent_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            control = conn.execute(
                "SELECT hosted_runtime_state, runtime_generation "
                "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                (row[0],),
            ).fetchone()
            if control is None or str(control[0]) != "v2":
                return False
            cur = conn.execute(
                "UPDATE agent_jobs SET status='running', started_at=now(), "
                "lease_expires_at = now() + make_interval(secs => %s), "
                "deadline_at = now() + make_interval(secs => %s) "
                "WHERE id=%s AND status='claimed' "
                "AND (lease_expires_at IS NULL OR lease_expires_at > now()) "
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


def renew_job_lease(job_id, claimed_by: str, *, ttl_sec: float = RUNNING_TTL_SEC) -> bool:
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
                "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                (row[0],),
            ).fetchone()
            if control is None or str(control[0]) != "v2":
                return False
            cur = conn.execute(
                "UPDATE agent_jobs SET "
                "lease_expires_at=now() + make_interval(secs => %s), "
                "deadline_at=now() + make_interval(secs => %s) "
                "WHERE id=%s AND claimed_by=%s "
                "AND status IN ('claimed','running') "
                "AND lease_expires_at > now() "
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


def mark_completed(job_id, *, claimed_by: str) -> bool:
    with _pool().connection() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET status='completed', finished_at=now() "
            "WHERE id=%s AND status IN ('claimed','running') "
            "AND claimed_by=%s AND lease_expires_at > now()",
            (job_id, str(claimed_by)),
        )
        return cur.rowcount == 1


def mark_failed(job_id, error: str, *, claimed_by: str) -> bool:
    """Fail an owned job and transactionally queue chat failure visibility.

    The CTE is intentionally one statement: there is no process-crash window
    in which ``agent_jobs`` is terminal but the status/error obligation is not
    durable.  Background lanes remain silent and do not get an outbox row.
    """
    visible_error = _terminal_error_code(error)
    with _pool().connection() as conn:
        cur = conn.execute(
            "WITH terminal AS ("
            "  UPDATE agent_jobs SET status='failed', finished_at=now(), "
            "    last_error=%s, attempt_count=attempt_count+1 "
            "  WHERE id=%s AND status IN ('claimed','running') "
            "    AND claimed_by=%s AND lease_expires_at > now() "
            "  RETURNING id,user_id,lane"
            "), queued AS ("
            "  INSERT INTO v2_terminal_failure_outbox "
            "  (job_id,user_id,error_code,target_route_id,target_route_updated_at) "
            "  SELECT t.id,t.user_id,%s,r.id,r.updated_at FROM terminal t "
            "  LEFT JOIN LATERAL (SELECT id,updated_at FROM model_api_routes "
            "    WHERE user_id=t.user_id AND is_active LIMIT 1) r ON TRUE "
            "  WHERE t.lane='chat' "
            "  ON CONFLICT (job_id) DO NOTHING RETURNING job_id"
            ") SELECT EXISTS (SELECT 1 FROM terminal)",
            (str(error)[:500], job_id, str(claimed_by), visible_error),
        )
        row = cur.fetchone()
        return bool(row and row[0])


def mark_expired(job_id, error: str = "runtime_expired") -> None:
    visible_error = _terminal_error_code(error)
    with _pool().connection() as conn:
        conn.execute(
            "WITH terminal AS ("
            "  UPDATE agent_jobs SET status='expired',finished_at=now(),last_error=%s "
            "  WHERE id=%s RETURNING id,user_id,lane"
            "), queued AS ("
            "  INSERT INTO v2_terminal_failure_outbox "
            "  (job_id,user_id,error_code,target_route_id,target_route_updated_at) "
            "  SELECT t.id,t.user_id,%s,r.id,r.updated_at FROM terminal t "
            "  LEFT JOIN LATERAL (SELECT id,updated_at FROM model_api_routes "
            "    WHERE user_id=t.user_id AND is_active LIMIT 1) r ON TRUE "
            "  WHERE t.lane='chat' ON CONFLICT (job_id) DO NOTHING"
            ") SELECT 1",
            (str(error)[:500], job_id, visible_error),
        )


def reap_stuck_job_rows(now=None) -> list[dict]:
    """Expire overdue pending admissions and claimed/running execution leases.

    The terminal transition releases the single-flight slot. ``now`` is an
    injectable epoch for deterministic tests; ``None`` uses database time.
    Returned rows let the independent watchdog surface chat timeouts.
    """
    ts = float(now) if now is not None else None
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "WITH terminal AS ("
                "  UPDATE agent_jobs SET status='expired', finished_at=now(), "
                "  attempt_count=attempt_count+1, "
                "  last_error=CASE WHEN status='pending' "
                "                  THEN 'queue_timeout' ELSE 'lease_timeout' END "
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
                "), queued AS ("
                "  INSERT INTO v2_terminal_failure_outbox "
                "  (job_id,user_id,error_code,target_route_id,target_route_updated_at) "
                "  SELECT t.id,t.user_id,t.last_error,r.id,r.updated_at FROM terminal t "
                "  LEFT JOIN LATERAL (SELECT id,updated_at FROM model_api_routes "
                "    WHERE user_id=t.user_id AND is_active LIMIT 1) r ON TRUE "
                "  WHERE t.lane='chat' "
                "  ON CONFLICT (job_id) DO NOTHING RETURNING job_id"
                ") SELECT id,user_id,lane,last_error,claimed_by FROM terminal",
                (float(PENDING_CHAT_TTL_SEC), ts, ts),
            )
            return [dict(row) for row in cur.fetchall()]


def ensure_terminal_failure_outbox(job_id, user_id: str, error: str) -> bool:
    """Idempotently ensure a durable visibility marker exists for ``job_id``.

    ``mark_failed`` and the timeout reaper create this in the terminal
    transaction.  The explicit helper also covers post-completion delivery
    uncertainty, which is user-visible even though the reply job itself stays
    completed.
    """
    with _pool().connection() as conn:
        cur = conn.execute(
            "INSERT INTO v2_terminal_failure_outbox "
            "(job_id,user_id,error_code,target_route_id,target_route_updated_at) "
            "SELECT j.id,j.user_id,%s,r.id,r.updated_at FROM agent_jobs j "
            "LEFT JOIN LATERAL (SELECT id,updated_at FROM model_api_routes "
            "  WHERE user_id=j.user_id AND is_active LIMIT 1) r ON TRUE "
            "WHERE j.id=%s AND j.user_id=%s "
            "ON CONFLICT (job_id) DO NOTHING",
            (_terminal_error_code(error), job_id, str(user_id)),
        )
        return cur.rowcount == 1


def _pending_terminal_failure_rows(
    sink: str, *, job_id=None, limit: int = 100, now=None,
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
                "runtime_error_delivered_at FROM v2_terminal_failure_outbox "
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
    job_id, *, kind: str, label: str | None, detail: dict | None,
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
    *, record_terminal_error=None, job_id=None, limit: int = 100, now=None,
) -> dict[str, int]:
    """Best-effort replay of both user-visible terminal failure sinks.

    Each sink has an independent due queue, so poisoned route delivery cannot
    hide newer status errors.  Production leaves ``record_terminal_error``
    unset and uses the route-version-fenced atomic DB sink above.  The callback
    remains only as a compatibility/test seam for dependency-isolated callers.
    """
    # Local import keeps jobs_store's storage primitives independent of the
    # status vocabulary during module initialization.
    from model_api_runtime.v2 import status_stream

    status_rows = _pending_terminal_failure_rows(
        "status", job_id=job_id, limit=limit, now=now)
    runtime_rows = _pending_terminal_failure_rows(
        "runtime_error", job_id=job_id, limit=limit, now=now)
    status_count = 0
    runtime_error_count = 0
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
                _defer_terminal_failure_sink(
                    current_job_id, "runtime_error", now=now)
            except Exception:  # noqa: BLE001 — original DB outage may persist
                pass
            log.warning(
                "terminal runtime-error reconciliation failed job=%s code=%s",
                current_job_id,
                type(exc).__name__.lower(),
            )
    examined = {row["job_id"] for row in status_rows}
    examined.update(row["job_id"] for row in runtime_rows)
    return {
        "examined": len(examined),
        "status_delivered": status_count,
        "runtime_error_delivered": runtime_error_count,
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


def get_expected_runtime_generation(
    job_id, *, claimed_by: str,
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
) -> tuple[bool, int | None]:
    """Complete an owned chat job and atomically create one late-input successor.

    Sends coalesced after ``observed_generation`` increment the active row under
    the same row lock. If they won the race, this transaction terminates the old
    row and inserts exactly one new pending chat job before releasing the lock.
    If finalization wins first, a concurrent enqueue sees the successor or creates
    a fresh job after the old row is terminal. Either ordering preserves input.
    """
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT user_id,lane,input_generation,priority FROM agent_jobs "
                    "WHERE id=%s AND claimed_by=%s AND status='running' "
                    "AND lease_expires_at > now() FOR UPDATE",
                    (job_id, str(claimed_by)),
                )
                row = cur.fetchone()
                if row is None or str(row["lane"]) != "chat":
                    return False, None
                cur.execute(
                    "UPDATE agent_jobs SET status='completed',finished_at=now() WHERE id=%s",
                    (job_id,),
                )
                # The success clear and completed outcome are one transaction.
                # A delayed older failure therefore cannot race in after this:
                # its captured route.updated_at predicate no longer matches.
                cur.execute(
                    "UPDATE model_api_routes SET last_runtime_error='',"
                    "last_runtime_error_class='',updated_at=now() "
                    "WHERE user_id=%s AND is_active",
                    (row["user_id"],),
                )
                successor_id = None
                if int(row["input_generation"] or 0) > int(observed_generation):
                    cur.execute(
                        "INSERT INTO agent_jobs "
                        "(user_id,lane,status,reason,priority,queue_deadline_at) "
                        "VALUES (%s,'chat','pending','coalesced_followup',%s,"
                        "now() + make_interval(secs => %s)) RETURNING id",
                        (row["user_id"], int(row["priority"]), float(PENDING_CHAT_TTL_SEC)),
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
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO agent_status_events "
                "(job_id, user_id, kind, label, detail_json, seq) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (job_id, user_id, str(kind), label, Jsonb(dict(detail or {})), int(seq)),
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
            cur.execute("SELECT state_json FROM runtime_state WHERE user_id=%s", (user_id,))
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
    """插一行到 v2_turn_metrics（append-only，无 FK）。由 turn 调用方在
    一轮结束后调用；provider 未回 usage 时 prompt/completion_tokens 传 None，
    该行仍落地（latency 仍可信），只是不参与 recent_mean_tokens_per_turn 的均值。"""
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics "
            "(job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (job_id, str(user_id), str(lane), prompt_tokens, completion_tokens, latency_ms),
        )


def record_whole_turn_metric(
    job_id, user_id, lane, *, prompt_tokens, completion_tokens,
    latency_ms, model_calls, retries, failed, status,
    cache_read_tokens=None, cache_write_tokens=None, cache_miss_tokens=None,
    usage_reported_calls=0, cache_reported_calls=0,
    provider=None, model=None, cache_route_fingerprint=None,
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
                "retries, failed, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
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
                "updated_at=now()",
                (
                    job_id, user_id, lane, prompt_tokens, completion_tokens,
                    cache_read_tokens, cache_write_tokens, cache_miss_tokens,
                    int(usage_reported_calls), int(cache_reported_calls),
                    provider, model, cache_route_fingerprint,
                    latency_ms, model_calls, retries, failed, status,
                ))
    except Exception as e:  # noqa: BLE001 — best-effort instrumentation, never fail the turn
        log.error("[jobs_store] record_whole_turn_metric(%s) failed: %s", job_id, e)


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
                "WITH recent AS (SELECT id, job_id, created_at, model_calls, "
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
            if sampled_turns else None
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
    if include_turns or any(value is not None for value in (
        provider_filter, model_filter, route_filter, user_filter, since_ts, until_ts,
    )):
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
                "SELECT summary_envelope, watermark_ts, version, watermark_seq "
                "FROM v2_conversation_summary WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    watermark_ts = float(row["watermark_ts"])
    watermark_seq = int(row["watermark_seq"] or 0)
    if watermark_seq == 0 and watermark_ts > 0:
        watermark_seq = db.seq_for_watermark_ts(user_id, watermark_ts)
    return {
        "summary_envelope": dict(row["summary_envelope"]) if row["summary_envelope"] is not None else None,
        "watermark_ts": watermark_ts,
        "version": int(row["version"]),
        "watermark_seq": watermark_seq,
    }


def upsert_summary_row_cas(
    user_id, *, summary_envelope: dict, watermark_ts: float, expected_version: int,
    watermark_seq: int | None = None,
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
        with conn.cursor() as cur:
            if int(expected_version) == 0:
                cur.execute(
                    "INSERT INTO v2_conversation_summary "
                    "(user_id, summary_envelope, watermark_ts, version, watermark_seq) "
                    "VALUES (%s, %s, %s, 1, %s) ON CONFLICT (user_id) DO NOTHING",
                    (
                        user_id, Jsonb(dict(summary_envelope or {})), float(watermark_ts),
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
                "last_screen_watch_frame_id, updated_at "
                "FROM v2_wake_schedule WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    result = dict(row)
    if result["next_screen_watch_at"] is not None:
        result["next_screen_watch_at"] = float(result["next_screen_watch_at"])
    return result


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
                float(payment_cooldown_until) if payment_cooldown_until is not None else None,
                float(next_screen_watch_at) if next_screen_watch_at is not None else None,
                str(last_screen_watch_frame_id) if last_screen_watch_frame_id is not None else None,
            ),
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
                "SELECT user_id FROM v2_wake_schedule "
                "WHERE next_heartbeat_at IS NOT NULL "
                "AND next_heartbeat_at <= COALESCE(to_timestamp(%s), now()) "
                "AND (payment_cooldown_until IS NULL "
                "     OR payment_cooldown_until <= COALESCE(to_timestamp(%s), now())) "
                "ORDER BY next_heartbeat_at LIMIT %s",
                (ts, ts, int(limit)),
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
        "SELECT DISTINCT user_id FROM ("
        "  SELECT DISTINCT ON (user_id, item_key) user_id, doc"
        "  FROM user_logs WHERE stream = %s"
        "  ORDER BY user_id, item_key, seq DESC"
        ") latest "
        "WHERE COALESCE(NULLIF(doc->>'due_at','')::float8, 0) <= %s "
        "  AND (doc->>'status' = 'pending' OR (doc->>'status' = 'claimed' "
        "       AND COALESCE(NULLIF(doc->>'claim_expires_at','')::float8, 0) <= %s)) "
        "LIMIT %s"
    )
    with _pool().connection() as conn:
        rows = conn.execute(sql, (SCHEDULED_WAKE_STREAM, ts, ts, limit)).fetchall()
    return [str(r[0]) for r in rows]


def upsert_runtime_state(user_id, patch: dict) -> dict:
    """浅合并 patch 进 state_json（JSONB || 合并），返回合并后的 state。"""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
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
