"""Owner-only maintenance for the selected primary's trace partitions.

The application role deliberately has no DDL path.  This command runs with
``TRACE_EVENTS_MIGRATION_DATABASE_URL`` after an RDS or TEE migration (and may
be run again manually).  ``TEE_MIGRATION_DATABASE_URL`` remains a compatibility
fallback for the existing TEE workflow.  The DEFAULT partition preserves writes
when maintenance is late; seeing any row there is still a degraded-state signal
and is reported by the independent application monitor before this command
repairs it.
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql


_ZONE = ZoneInfo("Asia/Shanghai")
_PARTITION_RE = re.compile(r"^trace_events_p(\d{8})$")


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


def maintain(
    conn: psycopg.Connection,
    *,
    today: date | None = None,
    retention_days: int = 30,
    future_days: int = 60,
) -> dict:
    """Repair DEFAULT, maintain the rolling window, and drop expired days.

    One parent ``ACCESS EXCLUSIVE`` lock makes detach/move/attach atomic with
    respect to writers.  If anything fails, the surrounding transaction puts
    the DEFAULT partition and all of its rows back exactly as they were.
    """
    current = today or _beijing_today()
    retention = max(1, int(retention_days))
    horizon = max(1, int(future_days))
    keep_from = current - timedelta(days=retention - 1)
    created: list[str] = []
    dropped: list[str] = []
    default_before = 0
    moved = 0
    expired_default = 0

    with conn.transaction():
        conn.execute("LOCK TABLE trace_events IN ACCESS EXCLUSIVE MODE")
        row = conn.execute("SELECT count(*) FROM trace_events_default").fetchone()
        default_before = int(row[0] if row else 0)

        detached = default_before > 0
        if detached:
            conn.execute(
                "ALTER TABLE trace_events DETACH PARTITION trace_events_default"
            )
            row = conn.execute(
                "WITH gone AS ("
                " DELETE FROM trace_events_default "
                " WHERE ts < %s RETURNING 1"
                ") SELECT count(*) FROM gone",
                (_bounds(keep_from)[0],),
            ).fetchone()
            expired_default = int(row[0] if row else 0)
            rows = conn.execute(
                "SELECT DISTINCT (ts AT TIME ZONE 'Asia/Shanghai')::date "
                "FROM trace_events_default ORDER BY 1"
            ).fetchall()
            stranded_days = {row[0] for row in rows}
        else:
            stranded_days = set()

        wanted = {
            keep_from + timedelta(days=offset)
            for offset in range(retention + horizon)
        }
        wanted.update(stranded_days)
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT child.relname FROM pg_inherits i "
                "JOIN pg_class parent ON parent.oid=i.inhparent "
                "JOIN pg_class child ON child.oid=i.inhrelid "
                "WHERE parent.oid='trace_events'::regclass"
            ).fetchall()
        }
        for day in sorted(wanted):
            name = _name(day)
            if name not in existing:
                _create_partition(conn, day)
                created.append(name)

        if detached:
            row = conn.execute(
                "WITH put AS ("
                " INSERT INTO trace_events SELECT * FROM trace_events_default "
                " RETURNING 1"
                ") SELECT count(*) FROM put"
            ).fetchone()
            moved = int(row[0] if row else 0)
            conn.execute("DELETE FROM trace_events_default")
            conn.execute(
                "ALTER TABLE trace_events "
                "ATTACH PARTITION trace_events_default DEFAULT"
            )

        child_names = [
            str(row[0])
            for row in conn.execute(
                "SELECT child.relname FROM pg_inherits i "
                "JOIN pg_class parent ON parent.oid=i.inhparent "
                "JOIN pg_class child ON child.oid=i.inhrelid "
                "WHERE parent.oid='trace_events'::regclass"
            ).fetchall()
        ]
        for child_name in child_names:
            match = _PARTITION_RE.fullmatch(child_name)
            if not match:
                continue
            child_day = datetime.strptime(match.group(1), "%Y%m%d").date()
            if child_day < keep_from:
                conn.execute(
                    sql.SQL("DROP TABLE {}").format(sql.Identifier(child_name))
                )
                dropped.append(child_name)

        row = conn.execute("SELECT count(*) FROM trace_events_default").fetchone()
        default_after = int(row[0] if row else 0)

    return {
        "today": current.isoformat(),
        "keep_from": keep_from.isoformat(),
        "future_through": (current + timedelta(days=horizon)).isoformat(),
        "default_rows_before": default_before,
        "default_rows_after": default_after,
        "moved_rows": moved,
        "expired_default_rows": expired_default,
        "created": created,
        "dropped": dropped,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--future-days", type=int, default=60)
    args = parser.parse_args()
    dsn = _migration_database_url()
    with psycopg.connect(dsn) as conn:
        report = maintain(
            conn,
            retention_days=args.retention_days,
            future_days=args.future_days,
        )
    print(report, flush=True)
    if report["default_rows_after"]:
        raise RuntimeError("trace_events_default remains non-empty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
