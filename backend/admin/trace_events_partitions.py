"""Owner-only maintenance for the selected primary's trace partitions.

The application role deliberately has no DDL path.  This command runs with
``TRACE_EVENTS_MIGRATION_DATABASE_URL`` after an RDS or TEE migration (and may
be run daily or manually).  ``TEE_MIGRATION_DATABASE_URL`` remains a compatibility
fallback for the existing TEE workflow.  The DEFAULT partition preserves writes
when maintenance is late; seeing any row there is still a degraded-state signal
and is reported by the independent application monitor before this command
repairs it.
"""

from __future__ import annotations

import argparse
import json
import os
import time as clock
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql


_ZONE = ZoneInfo("Asia/Shanghai")
_MAINTENANCE_LOCK = (184, 839)
LOSSLESS_PARTITION_MAINTENANCE = True
_AUTOMATIC_BACKLOG_ROWS = 10000
_AUTOMATIC_BACKLOG_BYTES = 8 * 1024 * 1024


class _DeferredDay(RuntimeError):
    """Future-only maintenance must not move occupied DEFAULT dates."""


def _migration_database_url() -> str:
    """Return the explicit owner/migration DSN for the selected primary."""
    dsn = (
        os.environ.get("TRACE_EVENTS_MIGRATION_DATABASE_URL", "").strip()
        or os.environ.get("TEE_MIGRATION_DATABASE_URL", "").strip()
    )
    if not dsn:
        raise RuntimeError(
            "TRACE_EVENTS_MIGRATION_DATABASE_URL is required "
            "(TEE_MIGRATION_DATABASE_URL is accepted for the TEE workflow)"
        )
    return dsn


def _beijing_today(now: datetime | None = None) -> date:
    current = now or datetime.now(tz=_ZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_ZONE)
    return current.astimezone(_ZONE).date()


def _bounds(day: date) -> tuple[datetime, datetime]:
    lower = datetime.combine(day, time.min, tzinfo=_ZONE)
    return lower, lower + timedelta(days=1)


def _name(day: date) -> str:
    return f"trace_events_p{day:%Y%m%d}"


def _create_partition(conn: psycopg.Connection, day: date) -> None:
    lower, upper = _bounds(day)
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} PARTITION OF trace_events "
            "FOR VALUES FROM ({}) TO ({})"
        ).format(
            sql.Identifier(_name(day)),
            sql.Literal(lower),
            sql.Literal(upper),
        )
    )


def _repair_day(conn: psycopg.Connection, day: date, *, lock_timeout_ms: int,
                statement_timeout_ms: int, repair_default: bool = True,
                current_day: date | None = None) -> dict:
    """One committed day; every removed DEFAULT row is inserted before ATTACH.

    Copy and index preparation allow ordinary writes. Past-day repairs allow
    writes to other daily partitions while DEFAULT is fenced. Strict writers
    retry a stale DEFAULT route only after rolling back their whole batch.
    Current/future days retain the parent fence. Failure rolls back the day.
    """
    lower, upper = _bounds(day)
    name = _name(day)
    started = clock.monotonic()
    acquired = started
    with conn.transaction():
        conn.execute("SELECT set_config('lock_timeout', %s, true)",
                     (f"{lock_timeout_ms}ms",))
        conn.execute("SELECT set_config('statement_timeout', %s, true)",
                     (f"{statement_timeout_ms}ms",))
        existing = conn.execute(
            "SELECT 1 FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
            "WHERE i.inhparent='trace_events'::regclass AND c.relname=%s",
            (name,),
        ).fetchone()
        moved = 0
        if not existing:
            # CREATE PARTITION OF fails when DEFAULT contains this day's rows.
            # Stage a structurally identical table, move first, then ATTACH.
            # No IF NOT EXISTS: an unrelated unattached table is a hard error.
            conn.execute(sql.SQL("CREATE TABLE {} (LIKE trace_events INCLUDING ALL)")
                         .format(sql.Identifier(name)))
            # Build the day table and its indexes while ordinary writes can
            # still proceed. This transaction's ACCESS SHARE locks allow DML.
            if repair_default:
                _copy_day(conn, day)
            prepared = clock.monotonic()
            # Never weaken today's/future routing fence. Past-day repair allows
            # current partition traffic; strict writers retry a stale DEFAULT
            # route's 23514 only after their whole transaction has rolled back.
            past_repair = repair_default and current_day is not None and day < current_day
            parent_lock = "SHARE UPDATE EXCLUSIVE" if past_repair else "ACCESS EXCLUSIVE"
            conn.execute(f"LOCK TABLE ONLY trace_events IN {parent_lock} MODE")
            acquired = clock.monotonic()
            conn.execute("LOCK TABLE trace_events_default IN ACCESS EXCLUSIVE MODE")
            if not repair_default and conn.execute(
                "SELECT EXISTS (SELECT 1 FROM trace_events_default WHERE ts >= %s AND ts < %s)",
                (lower, upper),
            ).fetchone()[0]:
                raise _DeferredDay("occupied date requires explicit repair window")
            if repair_default:
                delta_started = clock.monotonic()
                # The source is fenced. Synchronize only real deltas rather than
                # probing ON CONFLICT for every already-copied row/index entry.
                # This also prevents concurrent explicit clears being resurrected.
                conn.execute(sql.SQL(
                    "DELETE FROM {} AS staged WHERE NOT EXISTS "
                    "(SELECT 1 FROM trace_events_default d "
                    "WHERE (d.id,d.ts)=(staged.id,staged.ts))"
                ).format(sql.Identifier(name)))
                conn.execute(sql.SQL(
                    "INSERT INTO {} SELECT d.* FROM trace_events_default d "
                    "WHERE d.ts >= %s AND d.ts < %s AND NOT EXISTS "
                    "(SELECT 1 FROM {} staged WHERE (staged.id,staged.ts)=(d.id,d.ts))"
                ).format(sql.Identifier(name), sql.Identifier(name)), (lower, upper))
                columns = [r[0] for r in conn.execute(
                    "SELECT attname FROM pg_attribute WHERE attrelid='trace_events'::regclass "
                    "AND attnum>0 AND NOT attisdropped AND attname NOT IN ('id','ts') "
                    "ORDER BY attnum"
                ).fetchall()]
                updates = sql.SQL(", ").join(sql.SQL("{}=d.{}").format(
                    sql.Identifier(c), sql.Identifier(c)) for c in columns)
                conn.execute(sql.SQL(
                    "UPDATE {} staged SET {} FROM trace_events_default d "
                    "WHERE (staged.id,staged.ts)=(d.id,d.ts) "
                    "AND ROW(staged.*) IS DISTINCT FROM ROW(d.*)"
                ).format(sql.Identifier(name), updates))
                delete_started = clock.monotonic()
                moved = conn.execute(
                    "DELETE FROM trace_events_default WHERE ts >= %s AND ts < %s",
                    (lower, upper),
                ).rowcount
                copied = conn.execute(sql.SQL("SELECT count(*) FROM {}")
                                      .format(sql.Identifier(name))).fetchone()[0]
                if copied != moved:
                    raise RuntimeError("trace row conservation failed; rolling back day")
                attach_started = clock.monotonic()
            else:
                delta_started = delete_started = attach_started = clock.monotonic()
            conn.execute(sql.SQL(
                "ALTER TABLE trace_events ATTACH PARTITION {} "
                "FOR VALUES FROM ({}) TO ({})"
            ).format(sql.Identifier(name), sql.Literal(lower), sql.Literal(upper)))
        else:
            prepared = acquired = delta_started = delete_started = attach_started = clock.monotonic()
    finished = clock.monotonic()
    return {"day": day.isoformat(), "created": not bool(existing),
            "moved_rows": moved, "delta_seconds": delete_started - delta_started,
            "delete_seconds": attach_started - delete_started,
            "attach_commit_seconds": finished - attach_started,
            "prepare_seconds": prepared - started,
            "lock_wait_seconds": acquired - prepared,
            "lock_held_seconds": finished - acquired if not existing else 0.0,
            "elapsed_seconds": finished - started}


def _copy_day(conn: psycopg.Connection, day: date) -> None:
    lower, upper = _bounds(day)
    conn.execute(sql.SQL(
        "INSERT INTO {} SELECT * FROM trace_events_default WHERE ts >= %s AND ts < %s"
    ).format(sql.Identifier(_name(day))), (lower, upper))


def maintain(
    conn: psycopg.Connection,
    *,
    today: date | None = None,
    future_days: int = 60,
    lock_timeout_ms: int = 2000,
    statement_timeout_ms: int = 30000,
    repair_default: bool = True,
) -> dict:
    """Keep every trace forever; repair occupied days and today..today+N only.

    Each day commits separately so writers can run between days. A failed day
    rolls back completely; earlier days stay committed and reruns are safe.
    Do not wrap this function in an outer transaction: that would retain every
    day's table locks until the caller committed, defeating the batching.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("maintenance requires an idle connection, no outer transaction")
    horizon = int(future_days)
    if not 0 <= horizon <= 366:
        raise ValueError("future_days must be between 0 and 366")
    if lock_timeout_ms <= 0 or statement_timeout_ms <= 0:
        raise ValueError("maintenance timeouts must be positive")
    current = today or _beijing_today()
    with conn.transaction():
        locked = conn.execute("SELECT pg_try_advisory_lock(%s,%s)",
                              _MAINTENANCE_LOCK).fetchone()[0]
    if not locked:
        raise RuntimeError("trace partition maintenance is already running")
    try:
        with conn.transaction():
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (f"{statement_timeout_ms}ms",))
            rows = conn.execute(
                "SELECT (ts AT TIME ZONE 'Asia/Shanghai')::date, count(*) "
                "FROM trace_events_default GROUP BY 1 ORDER BY 1"
            ).fetchall()
            default_bytes = conn.execute(
                "SELECT pg_total_relation_size('trace_events_default')"
            ).fetchone()[0]
            existing = {str(r[0]) for r in conn.execute(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                "WHERE i.inhparent='trace_events'::regclass"
            ).fetchall()}
        wanted = {current + timedelta(days=n) for n in range(horizon + 1)}
        occupied = {r[0] for r in rows}
        if repair_default:
            wanted.update(occupied)
        batches = []
        deferred = []
        for day in sorted(wanted):
            if _name(day) in existing:
                continue
            if not repair_default and day in occupied:
                deferred.append({"day": day.isoformat(), "reason": "occupied_date"})
                continue
            try:
                batches.append(_repair_day(
                    conn, day, repair_default=repair_default, current_day=current,
                    lock_timeout_ms=lock_timeout_ms if repair_default else min(lock_timeout_ms, 100),
                    statement_timeout_ms=statement_timeout_ms if repair_default else min(statement_timeout_ms, 250),
                ))
            except (_DeferredDay, psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as exc:
                if repair_default:
                    raise
                deferred.append({"day": day.isoformat(), "reason": type(exc).__name__})
        with conn.transaction():
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (f"{statement_timeout_ms}ms",))
            after = conn.execute("SELECT count(*) FROM trace_events_default").fetchone()[0]
        return {
            "today": current.isoformat(), "retention": "forever",
            "mode": "repair" if repair_default else "future_only",
            "default_bytes_before": default_bytes,
            "large_backlog": bool(rows) and (sum(r[1] for r in rows) > _AUTOMATIC_BACKLOG_ROWS
                              or default_bytes > _AUTOMATIC_BACKLOG_BYTES),
            "backlog_row_threshold": _AUTOMATIC_BACKLOG_ROWS,
            "backlog_byte_threshold": _AUTOMATIC_BACKLOG_BYTES,
            "future_through": (current + timedelta(days=horizon)).isoformat(),
            "default_rows_before": sum(r[1] for r in rows),
            "default_rows_after": after,
            "moved_rows": sum(b["moved_rows"] for b in batches),
            "created": [_name(date.fromisoformat(b["day"])) for b in batches if b["created"]],
            "batches": batches,
            "deferred": deferred,
        }
    finally:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_unlock(%s,%s)", _MAINTENANCE_LOCK)


def main() -> int:
    parser = argparse.ArgumentParser(description="Lossless trace partition maintenance; no expiry")
    parser.add_argument("--future-days", type=int, default=60)
    parser.add_argument("--lock-timeout-ms", type=int, default=2000)
    parser.add_argument("--statement-timeout-ms", type=int, default=30000)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--repair-default", action="store_true",
                      help="explicit manual window: move DEFAULT rows under table locks")
    mode.add_argument("--scheduled", action="store_true",
                      help="future-only, short lock/statement budgets, no backlog moves")
    args = parser.parse_args()
    with psycopg.connect(_migration_database_url()) as conn:
        report = maintain(conn, future_days=args.future_days,
                          lock_timeout_ms=args.lock_timeout_ms,
                          statement_timeout_ms=args.statement_timeout_ms,
                          repair_default=args.repair_default)
    print(json.dumps(report), flush=True)
    if report["default_rows_after"] or report["deferred"]:
        raise RuntimeError("trace maintenance degraded; rows preserved, explicit repair window required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
