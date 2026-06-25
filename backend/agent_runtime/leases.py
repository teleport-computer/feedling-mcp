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

import time
from typing import Any

import db


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
    with db.get_pool().connection() as conn:
        row = conn.execute(
            sql, (user_id, driver, pid, lease_owner, expires, runtime_home, clock)
        ).fetchone()
    return bool(row and row[0] == lease_owner)


def renew(
    user_id: str,
    lease_owner: str,
    *,
    ttl: float,
    status: str | None = None,
    pid: int | None = None,
    session_ref: str | None = None,
    now: float | None = None,
) -> bool:
    """Heartbeat: extend the lease iff we still own a live lease.

    Returns False if we lost ownership or the lease already expired (the caller
    should then stop its consumer — another supervisor may have taken over).
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
            updated_at = now()
        WHERE user_id = %s
          AND lease_owner = %s
          AND lease_expires_at >= to_timestamp(%s)
        RETURNING user_id
    """
    with db.get_pool().connection() as conn:
        row = conn.execute(
            sql, (expires, clock, status, pid, session_ref, user_id, lease_owner, clock)
        ).fetchone()
    return bool(row)


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
