"""DB 存取：agent_jobs / agent_action_queue / agent_status_events / runtime_state.

CONTRIBUTING §2：新表存取逻辑全部收进本模块（jobs_store）。连接走 db.get_pool()
（autocommit）；需要跨语句持行锁的地方（SKIP LOCKED claim / single-flight 选举）
用显式 conn.transaction()。行返回 dict 用 psycopg.rows.dict_row 游标。
"""
from __future__ import annotations

import logging
import math
import os
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
        "SELECT id,status,CASE "
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
    if existing is not None and not bool(existing["stale"]):
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
        cur.execute(
            "UPDATE agent_jobs SET status='expired',finished_at=now(), "
            "attempt_count=attempt_count+1, "
            "last_error=CASE WHEN status='pending' "
            "THEN 'queue_timeout' ELSE 'lease_timeout' END "
            "WHERE id=%s",
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

    expected_generation：入队方（通常是 send/wake 入口，已经读过
    db.get_runtime_generation(user_id)）观测到的运行时代数，原样落到新建行的
    expected_runtime_generation 列（None 表示调用方未接入 cutover 代数校验，
    该行永不因代数过期被判 superseded）。只影响新建分支——coalesce 到既有 pending
    行时沿用那一行已经落库的代数，不覆盖。
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
                        return coalesce_or_insert_on_cursor(
                            cur, user_id, lane, reason=reason, trace_id=trace_id,
                            priority=priority, deadline_at=deadline_at,
                            expected_generation=expected_generation,
                        )
        except psycopg.errors.UniqueViolation:
            continue  # 并发 racer 抢先建了 active job；重读并 coalesce
    # A very busy terminal/enqueue race can exhaust the optimistic retries.
    # The fallback must still record that new input arrived; merely returning
    # the row id would let finalization miss the follow-up generation.
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                return coalesce_or_insert_on_cursor(
                    cur, user_id, lane, reason=reason, trace_id=trace_id,
                    priority=priority, deadline_at=deadline_at,
                    expected_generation=expected_generation,
                )


def claim_next_job(worker_id: str, *, lanes: set[str] | None = None) -> dict | None:
    """抢下一个 pending job（priority DESC, created_at）。用 FOR UPDATE SKIP LOCKED 让
    多进程/多 slot 无争用地各抢各的。pending → claimed，落 claimed_by/claimed_at。
    返回整行 dict（含 id/user_id/lane/trace_id/expected_runtime_generation/...），
    无活可抢返回 None。

    lanes：可选 lane 白名单（预留槽位场景，如某个 slot 只允许抢 {"chat",
    "manual_wake"}，保证聊天回复不被 heartbeat/capture 之类的后台唤醒风暴饿死）。
    None（默认）＝不限制 lane，行为与改动前完全一致。

    代过期早退（PR A / spec A3）：候选行的 expected_runtime_generation 若非空且
    小于该用户当前 db.get_runtime_generation(user_id)（意味着入队之后发生过一次
    resident<->v2 cutover，这一行是为旧运行时排的队，新运行时不认它），本次
    claim 不把它交给任何 worker 过一轮——同一事务内直接把它判终态 'superseded'，
    然后继续看下一个候选，直到拿到一个非过期的可抢行或彻底抢空。必须在同一个
    claim 事务里做，否则两个并发 worker 可能都读到这个陈旧代的行、一个刚判
    superseded、另一个已经把它当活的 claimed 出去。"""
    if lanes is None:
        select_sql = (
            "SELECT j.id, j.user_id, j.expected_runtime_generation, "
            "COALESCE(rs.runtime_generation, 1) AS current_generation FROM agent_jobs j "
            "JOIN users u ON u.user_id=j.user_id "
            "LEFT JOIN v2_runtime_state rs ON rs.user_id=j.user_id "
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
            "ORDER BY j.priority DESC, j.created_at "
            "FOR UPDATE OF j,u SKIP LOCKED LIMIT 1"
        )
        select_args = (float(PENDING_CHAT_TTL_SEC), float(PENDING_CHAT_TTL_SEC))
    else:
        select_sql = (
            "SELECT j.id, j.user_id, j.expected_runtime_generation, "
            "COALESCE(rs.runtime_generation, 1) AS current_generation FROM agent_jobs j "
            "JOIN users u ON u.user_id=j.user_id "
            "LEFT JOIN v2_runtime_state rs ON rs.user_id=j.user_id "
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
            "ORDER BY j.priority DESC, j.created_at "
            "FOR UPDATE OF j,u SKIP LOCKED LIMIT 1"
        )
        select_args = (float(PENDING_CHAT_TTL_SEC), float(PENDING_CHAT_TTL_SEC), list(lanes))

    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                while True:
                    cur.execute(select_sql, select_args)
                    head = cur.fetchone()
                    if head is None:
                        return None
                    expected_gen = head["expected_runtime_generation"]
                    if expected_gen is not None and int(expected_gen) < int(head["current_generation"]):
                        cur.execute(
                            "UPDATE agent_jobs SET status='superseded', finished_at=now(), "
                            "last_error='stale_runtime_generation' WHERE id=%s",
                            (head["id"],),
                        )
                        continue
                    cur.execute(
                        "UPDATE agent_jobs SET status='claimed', claimed_by=%s, claimed_at=now(), "
                        "lease_expires_at = now() + make_interval(secs => %s), "
                        "deadline_at = now() + make_interval(secs => %s) "
                        "WHERE id=%s RETURNING *",
                        (worker_id, float(RUNNING_TTL_SEC), float(RUNNING_TTL_SEC), head["id"]),
                    )
                    return cur.fetchone()


def mark_running(job_id, *, claimed_by: str) -> bool:
    with _pool().connection() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET status='running', started_at=now(), "
            "lease_expires_at = now() + make_interval(secs => %s), "
            "deadline_at = now() + make_interval(secs => %s) "
            "WHERE id=%s AND status='claimed' "
            "AND (lease_expires_at IS NULL OR lease_expires_at > now()) "
            "AND claimed_by=%s",
            (float(RUNNING_TTL_SEC), float(RUNNING_TTL_SEC), job_id, str(claimed_by)),
        )
        return cur.rowcount == 1


def renew_job_lease(job_id, claimed_by: str, *, ttl_sec: float = RUNNING_TTL_SEC) -> bool:
    with _pool().connection() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET "
            "lease_expires_at=now() + make_interval(secs => %s), "
            "deadline_at=now() + make_interval(secs => %s) "
            "WHERE id=%s AND claimed_by=%s AND status IN ('claimed','running') "
            "AND lease_expires_at > now()",
            (float(ttl_sec), float(ttl_sec), job_id, str(claimed_by)),
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
    with _pool().connection() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET status='failed', finished_at=now(), "
            "last_error=%s, attempt_count=attempt_count+1 "
            "WHERE id=%s AND status IN ('claimed','running') "
            "AND claimed_by=%s AND lease_expires_at > now()",
            (str(error)[:500], job_id, str(claimed_by)),
        )
        return cur.rowcount == 1


def mark_expired(job_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='expired', finished_at=now() WHERE id=%s",
            (job_id,),
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
                "UPDATE agent_jobs SET status='expired', finished_at=now(), "
                "attempt_count=attempt_count+1, "
                "last_error=CASE WHEN status='pending' THEN 'queue_timeout' ELSE 'lease_timeout' END "
                "WHERE (status='pending' "
                "       AND COALESCE(queue_deadline_at, deadline_at, "
                "           CASE WHEN lane='chat' THEN "
                "             created_at + make_interval(secs => %s) END) "
                "           <= COALESCE(to_timestamp(%s), now())) "
                "   OR (status IN ('claimed','running') "
                "       AND COALESCE(lease_expires_at, deadline_at) IS NOT NULL "
                "       AND COALESCE(lease_expires_at, deadline_at) "
                "           <= COALESCE(to_timestamp(%s), now())) "
                "RETURNING id,user_id,lane,last_error,claimed_by",
                (float(PENDING_CHAT_TTL_SEC), ts, ts),
            )
            return [dict(row) for row in cur.fetchall()]


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


def add_actions(job_id, user_id, actions: list[dict]) -> list[int]:
    """把一批 action 追加进 job 的队列（seq 接续现有最大 seq）。action 形状：
    {type, payload?, visible?, requires_model_authorship?}。返回新建 action id 列表。
    注意：user_id 由调用方（parent job 的 user_id）统一传入，逐行写入每个 action
    （agent_action_queue.user_id 只经 job_id 间接 FK 到 users，没有直接约束——
    这里不信任按 action 逐条区分 user_id，一律用传入的这一个）。"""
    ids: list[int] = []
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(seq), -1) AS m FROM agent_action_queue WHERE job_id=%s",
                    (job_id,),
                )
                start = int(cur.fetchone()["m"]) + 1
                for offset, action in enumerate(actions):
                    cur.execute(
                        "INSERT INTO agent_action_queue "
                        "(job_id, user_id, seq, type, payload_json, visible, requires_model_authorship) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (
                            job_id,
                            user_id,
                            start + offset,
                            str(action["type"]),
                            # Planner payloads can contain decrypted memory text,
                            # search terms, URLs, or identity patches. Execution
                            # uses the in-memory plan; no production consumer
                            # reloads payload_json. Persist trajectory shape, not
                            # conversation data, until encrypted trajectories land.
                            Jsonb({}),
                            bool(action.get("visible", False)),
                            bool(action.get("requires_model_authorship", False)),
                        ),
                    )
                    ids.append(int(cur.fetchone()["id"]))
    return ids


def next_pending_action(job_id) -> dict | None:
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM agent_action_queue "
                "WHERE job_id=%s AND status='pending' ORDER BY seq ASC LIMIT 1",
                (job_id,),
            )
            return cur.fetchone()


def mark_action_running(action_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_action_queue SET status='running', started_at=now() WHERE id=%s",
            (action_id,),
        )


def mark_action_done(action_id, result: dict) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_action_queue SET status='completed', finished_at=now(), "
            "result_json=%s WHERE id=%s",
            # Capability data remains in memory for the responder. Persist only
            # the non-sensitive outcome bit; full result bodies may contain
            # decrypted cards, perception, or fetched web content.
            (Jsonb({"ok": bool((result or {}).get("ok", True))}), action_id),
        )


def mark_action_failed(action_id, error: str) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_action_queue SET status='failed', finished_at=now(), "
            "last_error=%s WHERE id=%s",
            (str(error)[:500], action_id),
        )


def mark_action_skipped(action_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_action_queue SET status='skipped', finished_at=now() WHERE id=%s",
            (action_id,),
        )


def invalidate_pending_actions(job_id, *, by_job_id: int) -> int:
    """把 job 现有 pending action 置为 invalidated，并在 job 上记 invalidated_by_job_id
    （replan/coalesce 的安全点，C 用）。返回被作废的 pending action 数。"""
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_action_queue SET status='invalidated', finished_at=now() "
                    "WHERE job_id=%s AND status='pending'",
                    (job_id,),
                )
                affected = cur.rowcount
                cur.execute(
                    "UPDATE agent_jobs SET invalidated_by_job_id=%s WHERE id=%s",
                    (int(by_job_id), job_id),
                )
    return affected


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
    """插一行到 v2_turn_metrics（append-only，无 FK）。由 responder/worker 在一轮
    turn 结束后调用；provider 未回 usage 时 prompt/completion_tokens 传 None，
    该行仍落地（latency 仍可信），只是不参与 recent_mean_tokens_per_turn 的均值。"""
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics "
            "(job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (job_id, str(user_id), str(lane), prompt_tokens, completion_tokens, latency_ms),
        )


def record_whole_turn_metric(job_id, user_id, lane, *, prompt_tokens, completion_tokens,
                             latency_ms, model_calls, retries, failed, status) -> None:
    """One idempotent whole-turn metric per job (spec B5): upsert on job_id so a
    re-drive (redelivery/retry of the same job) REPLACES rather than appends. Covers
    all model calls, retries, and failed turns. Best-effort: never raises to the turn."""
    try:
        with _pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics (job_id, user_id, lane, prompt_tokens, "
                "completion_tokens, latency_ms, model_calls, retries, failed, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (job_id) DO UPDATE SET "
                "prompt_tokens=EXCLUDED.prompt_tokens, completion_tokens=EXCLUDED.completion_tokens, "
                "latency_ms=EXCLUDED.latency_ms, model_calls=EXCLUDED.model_calls, "
                "retries=EXCLUDED.retries, failed=EXCLUDED.failed, status=EXCLUDED.status, "
                "updated_at=now()",
                (job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms,
                 model_calls, retries, failed, status))
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
    {"summary_envelope": dict|None, "watermark_ts": float, "version": int}，
    无行返回 None（该用户从未压缩过）。"""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT summary_envelope, watermark_ts, version "
                "FROM v2_conversation_summary WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "summary_envelope": dict(row["summary_envelope"]) if row["summary_envelope"] is not None else None,
        "watermark_ts": float(row["watermark_ts"]),
        "version": int(row["version"]),
    }


def upsert_summary_row_cas(
    user_id, *, summary_envelope: dict, watermark_ts: float, expected_version: int
) -> bool:
    """compare-and-swap 写入该用户的会话摘要行。expected_version==0 走首建
    （INSERT ... ON CONFLICT DO NOTHING，若行已存在说明输了竞态，返回 False）；
    否则走 UPDATE ... WHERE version=expected_version（不匹配说明摘要在别处已被
    推进，本次写入是过期/丢失的 CAS，返回 False）。成功返回 True。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            if int(expected_version) == 0:
                cur.execute(
                    "INSERT INTO v2_conversation_summary "
                    "(user_id, summary_envelope, watermark_ts, version) "
                    "VALUES (%s, %s, %s, 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, Jsonb(dict(summary_envelope or {})), float(watermark_ts)),
                )
            else:
                cur.execute(
                    "UPDATE v2_conversation_summary "
                    "SET summary_envelope=%s, watermark_ts=%s, version=version+1, updated_at=now() "
                    "WHERE user_id=%s AND version=%s",
                    (
                        Jsonb(dict(summary_envelope or {})),
                        float(watermark_ts),
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
