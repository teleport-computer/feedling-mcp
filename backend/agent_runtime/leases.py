"""DB-backed runtime lease over the agent_runtime_instances table.

The supervisor uses this to ensure exactly one consumer per user across
workers/processes (plan §进程模型). The lease is a row whose ``lease_owner`` +
``lease_expires_at`` are updated atomically: a supervisor acquires by taking an
absent/expired/own lease, renews by heartbeating while it still owns a live
lease, and a crashed holder's lease simply expires so another supervisor takes
over. Only the current owner may renew, release, or update ``session_ref``.

``now`` is an explicit epoch-seconds arg so TTL behavior is deterministic and
testable; production passes ``time.time()``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg

import db

log = logging.getLogger("feedling.agent_runtime.leases")


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def acquire(
    user_id: str,
    *,
    driver: str,
    runtime_home: str,
    lease_owner: str,
    ttl: float,
    pid: int | None = None,
    now: float | None = None,
) -> bool:
    """Take the lease if it is absent, expired, or already ours. Atomic.

    Returns True iff we hold the lease afterwards.
    """
    clock = _now(now)
    expires = clock + ttl
    # tee-mirror: 本文件所有写点有意不镜像（TTL lease/心跳，与 proactive
    # store_v2 的 user_blobs lease 同规则：ephemeral 锁，TEE 侧 supervisor
    # 自行重建，reconciler 收敛残留行）。
    sql = """
        INSERT INTO agent_runtime_instances
            (user_id, driver, status, pid, lease_owner, lease_expires_at,
             runtime_home, updated_at)
        VALUES (%s, %s, 'starting', %s, %s, to_timestamp(%s), %s, now())
        ON CONFLICT (user_id) DO UPDATE SET
            driver = EXCLUDED.driver,
            status = 'starting',
            pid = EXCLUDED.pid,
            lease_owner = EXCLUDED.lease_owner,
            lease_expires_at = EXCLUDED.lease_expires_at,
            runtime_home = EXCLUDED.runtime_home,
            updated_at = now()
        WHERE agent_runtime_instances.lease_expires_at IS NULL
           OR agent_runtime_instances.lease_expires_at < to_timestamp(%s)
           OR agent_runtime_instances.lease_owner = EXCLUDED.lease_owner
        RETURNING lease_owner
    """
    try:
        with db.get_pool().connection() as conn:
            row = conn.execute(
                sql, (user_id, driver, pid, lease_owner, expires, runtime_home, clock)
            ).fetchone()
    except psycopg.errors.ForeignKeyViolation:
        # The account was reset (delete_user removed its users row) after this
        # supervisor snapshotted the roster but before this acquire. The FK
        # (0009) refuses to (re)create an instance row for a now-deleted user —
        # which is exactly what we want: it stops the TOCTOU race that otherwise
        # spawns a consumer for a gone account and leaves an idle orphan. Treat
        # it as "did not get the lease"; the tick silently skips this user.
        log.info("acquire skipped for %s: users row absent (account deleted) — "
                 "not creating orphan instance", user_id)
        return False
    return bool(row and row[0] == lease_owner)


def renew(
    user_id: str,
    lease_owner: str,
    *,
    ttl: float,
    status: str | None = None,
    pid: int | None = None,
    session_ref: str | None = None,
    driver: str | None = None,
    now: float | None = None,
) -> bool:
    """Heartbeat: extend the lease iff we still own the row (``lease_owner`` is us),
    regardless of expiry.

    Matching on ownership alone — not "ownership AND still-live" — is the reclaim
    fix: an owner whose own lease briefly lapsed on a slow lap (its row is still
    stamped with our id, since only ``acquire`` by another or ``release`` by us
    changes the owner) renews it instead of being told it "lost" a lease nobody
    took. The original "must still own a LIVE lease" rule killed healthy consumers
    on every brief self-expiry — on a slow many-user lap a kill→respawn death
    spiral (and 503'd sends against the expired lease).

    Returns False for every row NOT owned by us — another owner's lease (live OR
    expired-but-not-released; renew must never steal a row that may have a fresh
    consumer behind it) AND a released/unowned row (``lease_owner IS NULL``; a
    row the tick just released for a removed user must not be reanimated with the
    now-dead pid by an in-flight renew). All ownership (re)acquisition goes
    through ``acquire``; renew only refreshes what is already ours.

    ``driver`` updates the recorded agent when a live consumer is respawned in
    place under a new driver (e.g. the user switches API key openai→anthropic, so
    codex→claude). Heartbeats pass it as None and leave the column untouched.
    """
    clock = _now(now)
    expires = clock + ttl
    sql = """
        UPDATE agent_runtime_instances SET
            lease_expires_at = to_timestamp(%s),
            last_heartbeat_at = to_timestamp(%s),
            status = COALESCE(%s, status),
            pid = COALESCE(%s, pid),
            session_ref = COALESCE(%s, session_ref),
            driver = COALESCE(%s, driver),
            updated_at = now()
        WHERE user_id = %s
          AND lease_owner = %s
        RETURNING user_id
    """
    with db.get_pool().connection() as conn:
        row = conn.execute(
            sql, (expires, clock, status, pid, session_ref, driver,
                  user_id, lease_owner)
        ).fetchone()
    return bool(row)


def renew_many(
    items: list[tuple[str, int]],
    lease_owner: str,
    *,
    ttl: float,
    status: str = "running",
    now: float | None = None,
) -> set[str]:
    """Renew leases for many ``(user_id, pid)`` pairs in ONE round-trip.

    Same ownership-only reclaim as ``renew``: a pair is renewed iff we still own
    the row (``lease_owner`` is us), regardless of expiry; any row owned by
    another supervisor OR released/unowned (``lease_owner IS NULL``) is skipped —
    (re)acquiring ownership is ``acquire``'s job, never renew's. Returns the set
    of user_ids we hold afterwards; any pair NOT in the set is no longer ours and
    the caller reaps its child.

    Collapses the supervisor's per-tick N×serial lease heartbeats (the dominant
    renew cost at host-all scale) into a single statement, so a growing fleet
    can't push a renew pass past the TTL and re-open the churn window.
    """
    if not items:
        return set()
    clock = _now(now)
    expires = clock + ttl
    user_ids = [str(u) for u, _ in items]
    pids = [int(p) for _, p in items]
    sql = """
        UPDATE agent_runtime_instances AS a SET
            lease_expires_at = to_timestamp(%s),
            last_heartbeat_at = to_timestamp(%s),
            status = %s,
            pid = v.pid,
            updated_at = now()
        FROM unnest(%s::text[], %s::bigint[]) AS v(user_id, pid)
        WHERE a.user_id = v.user_id
          AND a.lease_owner = %s
        RETURNING a.user_id
    """
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            sql, (expires, clock, status, user_ids, pids, lease_owner)
        ).fetchall()
    return {r[0] for r in rows}


def set_session_ref(user_id: str, lease_owner: str, session_ref: str, *, now: float | None = None) -> bool:
    """Persist the driver's resume handle; only the live owner may write it."""
    clock = _now(now)
    sql = """
        UPDATE agent_runtime_instances SET
            session_ref = %s, last_active_at = to_timestamp(%s), updated_at = now()
        WHERE user_id = %s AND lease_owner = %s AND lease_expires_at >= to_timestamp(%s)
        RETURNING user_id
    """
    with db.get_pool().connection() as conn:
        row = conn.execute(sql, (session_ref, clock, user_id, lease_owner, clock)).fetchone()
    return bool(row)


def mark_error(user_id: str, lease_owner: str, error: str) -> None:
    sql = """
        UPDATE agent_runtime_instances SET status = 'error', error = %s, updated_at = now()
        WHERE user_id = %s AND lease_owner = %s
    """
    with db.get_pool().connection() as conn:
        conn.execute(sql, (error[:500], user_id, lease_owner))


def release(user_id: str, lease_owner: str, *, now: float | None = None) -> None:
    """Give up the lease (consumer exited). Only clears it if we still own it."""
    sql = """
        UPDATE agent_runtime_instances SET
            status = 'idle', lease_owner = NULL, lease_expires_at = NULL,
            pid = NULL, last_active_at = to_timestamp(%s), updated_at = now()
        WHERE user_id = %s AND lease_owner = %s
    """
    with db.get_pool().connection() as conn:
        conn.execute(sql, (_now(now), user_id, lease_owner))


def get(user_id: str) -> dict[str, Any] | None:
    sql = "SELECT * FROM agent_runtime_instances WHERE user_id = %s"
    with db.get_pool().connection() as conn:
        cur = conn.execute(sql, (user_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def list_active(*, now: float | None = None, lease_owner: str | None = None) -> list[dict[str, Any]]:
    """Rows with a live (non-expired) lease, optionally scoped to one owner."""
    clock = _now(now)
    sql = "SELECT * FROM agent_runtime_instances WHERE lease_expires_at >= to_timestamp(%s)"
    params: list[Any] = [clock]
    if lease_owner is not None:
        sql += " AND lease_owner = %s"
        params.append(lease_owner)
    with db.get_pool().connection() as conn:
        cur = conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]
