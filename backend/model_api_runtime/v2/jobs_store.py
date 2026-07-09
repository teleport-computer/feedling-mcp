"""DB 存取：agent_jobs / agent_action_queue / agent_status_events / runtime_state.

CONTRIBUTING §2：新表存取逻辑全部收进本模块（jobs_store）。连接走 db.get_pool()
（autocommit）；需要跨语句持行锁的地方（SKIP LOCKED claim / single-flight 选举）
用显式 conn.transaction()。行返回 dict 用 psycopg.rows.dict_row 游标。
"""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import db
from core import wake_bus

LANES = {"chat", "manual_wake", "heartbeat", "scheduled", "capture", "maintenance"}
# mark_running 时若 job 无 deadline_at，补一个（now + 该秒数），供 reaper 兜底回收
# 卡死的 claimed/running job。chat lane 的 enqueue 不带 deadline，全靠这个兜底。
#
# 300s（非最初的 120s）：一旦 serve_worker 接上周期性 reap_stuck_jobs()（FIX 1），一个
# replan 密集的官方 turn 有可能撑到 120s 以上（planner ≤30s ×≤3 + responder ≤60s +
# 开销），若 TTL 仍是 120s，reaper 可能在 turn 仍存活时就把它 expire 掉——single-flight
# 槽位一放开，第二个 worker 就可能抢到同一 user 的新 job 并发跑同一轮对话，双写回复，
# 违反 §16「无双回复」。300s 留足这个最坏情况的余量，reaper 只回收真正卡死（进程崩溃/
# 崩在 claim 和 mark_running 之间）的 job。
# TODO(more-robust follow-up): mark_completed/mark_failed 在写终态前应该先确认这行仍是
# 'claimed'/'running' 且仍归本 worker 所有（ownership check），这样即使 TTL 判断失误
# （reaper 提前收了一个仍存活的 job），旧 worker 收尾时也会因为状态已经是 'expired' 而
# 自然短路，不会覆盖新 worker 的结果——比单纯拉长 TTL 更彻底，但改动面更大，留到下一轮。
RUNNING_TTL_SEC = 300.0

_ACTIVE_STATUSES = ("pending", "claimed", "running")


def _pool():
    return db.get_pool()


def enqueue_job(
    user_id, lane, *, reason=None, trace_id=None, priority=0, deadline_at=None
) -> tuple[int, bool]:
    """入队一个 job。命中 per-user/lane single-flight（已有 active job）则合并到现有
    pending，返回 (existing_id, True)；否则新建，返回 (new_id, False)。

    实现：事务内先 SELECT ... FOR UPDATE 现有 active job；无则 INSERT。两个并发 enqueue
    可能都读不到现有行而各自 INSERT → 第二个撞 ux_agent_jobs_singleflight 唯一索引抛
    UniqueViolation → 重试一轮即读到赢家并 coalesce。唯一索引是最终防线。
    """
    if lane not in LANES:
        raise ValueError(f"unknown lane: {lane!r}")
    for _ in range(3):
        try:
            with _pool().connection() as conn:
                with conn.transaction():
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(
                            "SELECT id FROM agent_jobs "
                            "WHERE user_id=%s AND lane=%s AND status IN ('pending','claimed','running') "
                            "ORDER BY id LIMIT 1 FOR UPDATE",
                            (user_id, lane),
                        )
                        existing = cur.fetchone()
                        if existing is not None:
                            return int(existing["id"]), True
                        cur.execute(
                            "INSERT INTO agent_jobs "
                            "(user_id, lane, status, reason, trace_id, priority, deadline_at) "
                            "VALUES (%s,%s,'pending',%s,%s,%s,%s) RETURNING id",
                            (user_id, lane, reason, trace_id, int(priority), deadline_at),
                        )
                        return int(cur.fetchone()["id"]), False
        except psycopg.errors.UniqueViolation:
            continue  # 并发 racer 抢先建了 active job；重读并 coalesce
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id FROM agent_jobs "
                "WHERE user_id=%s AND lane=%s AND status IN ('pending','claimed','running') "
                "ORDER BY id LIMIT 1",
                (user_id, lane),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("enqueue_job: coalesce read found no active job after conflict")
    return int(row["id"]), True


def claim_next_job(worker_id: str) -> dict | None:
    """抢下一个 pending job（priority DESC, created_at）。用 FOR UPDATE SKIP LOCKED 让
    多进程/多 slot 无争用地各抢各的。pending → claimed，落 claimed_by/claimed_at。
    返回整行 dict（含 id/user_id/lane/trace_id/...），无活可抢返回 None。"""
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id FROM agent_jobs "
                    "WHERE status='pending' AND (deadline_at IS NULL OR deadline_at > now()) "
                    "ORDER BY priority DESC, created_at "
                    "FOR UPDATE SKIP LOCKED LIMIT 1"
                )
                head = cur.fetchone()
                if head is None:
                    return None
                cur.execute(
                    "UPDATE agent_jobs SET status='claimed', claimed_by=%s, claimed_at=now(), "
                    "deadline_at = COALESCE(deadline_at, now() + make_interval(secs => %s)) "
                    "WHERE id=%s RETURNING *",
                    (worker_id, float(RUNNING_TTL_SEC), head["id"]),
                )
                return cur.fetchone()


def mark_running(job_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='running', started_at=now(), "
            "deadline_at = COALESCE(deadline_at, now() + make_interval(secs => %s)) "
            "WHERE id=%s",
            (float(RUNNING_TTL_SEC), job_id),
        )


def mark_completed(job_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed', finished_at=now() WHERE id=%s",
            (job_id,),
        )


def mark_failed(job_id, error: str) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='failed', finished_at=now(), "
            "last_error=%s, attempt_count=attempt_count+1 WHERE id=%s",
            (str(error)[:500], job_id),
        )


def mark_expired(job_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='expired', finished_at=now() WHERE id=%s",
            (job_id,),
        )


def reap_stuck_jobs(now=None) -> int:
    """把 claimed/running 且已过 deadline_at 的 job 置为 expired（终态，释放 single-flight
    槽位，下一条 chat/send 可重新入队）。now 可注入用于确定性测试（不必真等超时）；
    None → 用 DB now()。返回被回收的行数。重试（re-pending）留给 C 的 replan。"""
    ts = float(now) if now is not None else None
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_jobs SET status='expired', finished_at=now(), "
                "attempt_count=attempt_count+1, "
                "last_error=COALESCE(last_error,'stuck_timeout') "
                "WHERE status IN ('claimed','running') "
                "AND deadline_at IS NOT NULL "
                "AND deadline_at <= COALESCE(to_timestamp(%s), now())",
                (ts,),
            )
            return cur.rowcount


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
                            Jsonb(dict(action.get("payload") or {})),
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
            (Jsonb(dict(result or {})), action_id),
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


def record_worker_heartbeat(worker_id: str) -> None:
    """UPSERT this process's liveness row (called every ~10s by
    serve_worker._heartbeat_loop). Backs workers_alive() — the chat/send guard
    that refuses db_action_v2 sends when no worker pool process is alive."""
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at) VALUES (%s, now()) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = now()",
            (str(worker_id),),
        )


def workers_alive(*, within_sec: int = 30) -> bool:
    """True iff at least one serve_worker has recorded a heartbeat within the
    last ``within_sec`` seconds. Used by the chat/send v2 liveness guard."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM v2_worker_heartbeats "
                "WHERE beat_at > now() - make_interval(secs => %s))",
                (int(within_sec),),
            )
            return bool(cur.fetchone()[0])


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
