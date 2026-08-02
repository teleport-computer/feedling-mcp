"""Bounded, fail-open daily usage rollups for the Admin Usage report.

``v2_turn_metrics`` remains authoritative.  This module is called only by the
independent Runtime V2 maintenance loop: it never participates in a turn's
provider, retry, reply, or metric-recording path.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

import db


log = logging.getLogger("feedling.runtime_v2.usage_rollup")

ROLLUP_NAME = "hosted_v2_usage"
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
# Stable, repository-local namespace.  Session scope makes replicas compete
# without holding a transaction open while separate day transactions commit.
ADVISORY_LOCK_KEY = 0x4656325553410001

DEFAULT_MAX_DAYS = 2
DEFAULT_MAX_CHANGED_ROWS = 5_000
DEFAULT_OVERLAP_SECONDS = 6 * 60 * 60
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000
DEFAULT_POOL_TIMEOUT_SECONDS = 0.5

_PREFIX_CONDITIONS = {
    "all": "TRUE",
    "metered": "m.usage_reported_calls > 0",
    "unknown": "m.usage_reported_calls < m.model_calls",
}
_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_miss_tokens",
)


class _CASConflict(RuntimeError):
    pass


def _bounded_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _bounded_float(
    value: object, default: float, *, minimum: float, maximum: float
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(parsed, maximum))


def enabled() -> bool:
    """Default on; an explicit false-like value is the deployment opt-out."""

    return os.environ.get(
        "FEEDLING_V2_USAGE_ROLLUP_ENABLED", "1"
    ).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _local_day_bounds(local_day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(local_day, time.min, tzinfo=LOCAL_TIMEZONE)
    end = datetime.combine(
        local_day + timedelta(days=1), time.min, tzinfo=LOCAL_TIMEZONE
    )
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _aggregate_selects(*, include_latency: bool) -> str:
    expressions: list[str] = []
    for prefix, condition in _PREFIX_CONDITIONS.items():
        expressions.append(
            f"count(*) FILTER (WHERE {condition})::bigint AS {prefix}_turns"
        )
        for field in ("model_calls", "retries"):
            expressions.append(
                "coalesce(sum(m."
                + field
                + ") FILTER (WHERE "
                + condition
                + f"),0)::bigint AS {prefix}_{field}"
            )
        expressions.append(
            "count(*) FILTER (WHERE "
            + condition
            + f" AND m.failed)::bigint AS {prefix}_failed_turns"
        )
        for field in ("usage_reported_calls", "cache_reported_calls"):
            expressions.append(
                "coalesce(sum(m."
                + field
                + ") FILTER (WHERE "
                + condition
                + f"),0)::bigint AS {prefix}_{field}"
            )
        expressions.append(
            "coalesce(sum(GREATEST(m.model_calls-m.usage_reported_calls,0)) "
            f"FILTER (WHERE {condition}),0)::bigint AS {prefix}_unknown_usage_calls"
        )
        for field in _TOKEN_FIELDS:
            expressions.extend(
                (
                    "coalesce(sum(m."
                    + field
                    + ") FILTER (WHERE "
                    + condition
                    + " AND m."
                    + field
                    + f" IS NOT NULL),0)::bigint AS {prefix}_{field}_sum",
                    "count(m."
                    + field
                    + ") FILTER (WHERE "
                    + condition
                    + f")::bigint AS {prefix}_{field}_known_count",
                )
            )
    if include_latency:
        for prefix, condition in _PREFIX_CONDITIONS.items():
            expressions.append(
                "coalesce(array_agg(m.latency_ms ORDER BY m.latency_ms,m.id) "
                f"FILTER (WHERE {condition} AND m.latency_ms IS NOT NULL),"
                f"'{{}}'::integer[]) AS {prefix}_latency_samples"
            )
    return ",\n  ".join(expressions)


_COMMON_FACT_COLUMNS = tuple(
    f"{prefix}_{field}"
    for prefix in _PREFIX_CONDITIONS
    for field in (
        "turns",
        "model_calls",
        "retries",
        "failed_turns",
        "usage_reported_calls",
        "cache_reported_calls",
        "unknown_usage_calls",
        "prompt_tokens_sum",
        "prompt_tokens_known_count",
        "completion_tokens_sum",
        "completion_tokens_known_count",
        "cache_read_tokens_sum",
        "cache_read_tokens_known_count",
        "cache_write_tokens_sum",
        "cache_write_tokens_known_count",
        "cache_miss_tokens_sum",
        "cache_miss_tokens_known_count",
    )
)
_LATENCY_COLUMNS = tuple(f"{prefix}_latency_samples" for prefix in _PREFIX_CONDITIONS)


def _begin_repeatable_read(cur) -> None:
    # Must be the first statement after BEGIN.  Every multi-statement helper
    # derives its facts and cursor metadata from one PostgreSQL MVCC snapshot.
    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")


def _set_local_limits(cur, statement_timeout_ms: int) -> None:
    cur.execute(
        "SELECT set_config('statement_timeout', %s, true), "
        "set_config('lock_timeout', %s, true)",
        (str(statement_timeout_ms), str(min(statement_timeout_ms, 2_000))),
    )


def _recompute_on_cursor(cur, local_day: date, *, refreshed_at: datetime) -> dict:
    start_at, end_at = _local_day_bounds(local_day)
    cur.execute(
        "DELETE FROM v2_usage_daily_dimensions WHERE local_day=%s", (local_day,)
    )
    cur.execute("DELETE FROM v2_usage_daily_users WHERE local_day=%s", (local_day,))

    user_columns = ",".join(_COMMON_FACT_COLUMNS)
    cur.execute(
        "INSERT INTO v2_usage_daily_users "
        f"(local_day,user_id,{user_columns},first_metric_at,last_metric_at,"
        "last_model_call_at,refreshed_at) "
        "SELECT %s,m.user_id,"
        + _aggregate_selects(include_latency=False)
        + ",min(m.created_at),max(m.created_at),"
        "max(m.created_at) FILTER (WHERE m.model_calls>0),%s "
        "FROM v2_turn_metrics m WHERE m.created_at >= %s AND m.created_at < %s "
        "GROUP BY m.user_id",
        (local_day, refreshed_at, start_at, end_at),
    )
    users = cur.rowcount

    dimension_columns = ",".join(_COMMON_FACT_COLUMNS + _LATENCY_COLUMNS)
    cur.execute(
        "INSERT INTO v2_usage_daily_dimensions "
        f"(local_day,user_id,lane,provider,model,{dimension_columns},"
        "first_metric_at,last_metric_at,last_model_call_at,refreshed_at) "
        "SELECT %s,m.user_id,"
        "coalesce(nullif(m.lane,''),'unknown'),"
        "coalesce(nullif(m.provider,''),'unknown'),"
        "coalesce(nullif(m.model,''),'unknown'),"
        + _aggregate_selects(include_latency=True)
        + ",min(m.created_at),max(m.created_at),"
        "max(m.created_at) FILTER (WHERE m.model_calls>0),%s "
        "FROM v2_turn_metrics m WHERE m.created_at >= %s AND m.created_at < %s "
        "GROUP BY m.user_id,coalesce(nullif(m.lane,''),'unknown'),"
        "coalesce(nullif(m.provider,''),'unknown'),"
        "coalesce(nullif(m.model,''),'unknown')",
        (local_day, refreshed_at, start_at, end_at),
    )
    return {"users": users, "dimensions": cur.rowcount}


def recompute_local_day(
    local_day: date,
    *,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    refreshed_at: datetime | None = None,
) -> dict:
    """Atomically replace both canonical fact tables for one Shanghai day."""

    if not isinstance(local_day, date):
        raise TypeError("local_day must be a date")
    timeout_ms = _bounded_int(
        statement_timeout_ms,
        DEFAULT_STATEMENT_TIMEOUT_MS,
        minimum=100,
        maximum=120_000,
    )
    refreshed = refreshed_at or datetime.now(timezone.utc)
    with db.get_pool().connection(timeout=DEFAULT_POOL_TIMEOUT_SECONDS) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _begin_repeatable_read(cur)
                _set_local_limits(cur, timeout_ms)
                return _recompute_on_cursor(cur, local_day, refreshed_at=refreshed)


def ensure_watermark() -> None:
    with db.get_pool().connection(timeout=DEFAULT_POOL_TIMEOUT_SECONDS) as conn:
        conn.execute(
            "INSERT INTO v2_usage_rollup_watermarks (rollup_name) VALUES (%s) "
            "ON CONFLICT (rollup_name) DO NOTHING",
            (ROLLUP_NAME,),
        )


def compare_and_set_watermark(
    *, expected_version: int, source_updated_at: datetime, source_id: int
) -> bool:
    """Defensive public CAS used by refresh orchestration and concurrency tests."""

    with db.get_pool().connection(timeout=DEFAULT_POOL_TIMEOUT_SECONDS) as conn:
        row = conn.execute(
            "UPDATE v2_usage_rollup_watermarks SET source_updated_at=%s,source_id=%s,"
            "version=version+1,updated_at=now() "
            "WHERE rollup_name=%s AND version=%s RETURNING version",
            (source_updated_at, int(source_id), ROLLUP_NAME, int(expected_version)),
        ).fetchone()
    return row is not None


def _watermark_on_cursor(cur, *, for_update: bool = False) -> dict:
    cur.execute(
        "SELECT * FROM v2_usage_rollup_watermarks WHERE rollup_name=%s"
        + (" FOR UPDATE" if for_update else ""),
        (ROLLUP_NAME,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("usage rollup watermark missing")
    return row


def _cursor_max_on_cursor(
    cur, *, through: datetime | None = None
) -> tuple[datetime, int] | None:
    if through is None:
        cur.execute(
            "SELECT updated_at,id FROM v2_turn_metrics "
            "ORDER BY updated_at DESC,id DESC LIMIT 1"
        )
    else:
        cur.execute(
            "SELECT updated_at,id FROM v2_turn_metrics WHERE updated_at <= %s "
            "ORDER BY updated_at DESC,id DESC LIMIT 1",
            (through,),
        )
    row = cur.fetchone()
    return None if row is None else (row["updated_at"], int(row["id"]))


def _source_lag_seconds(
    cursor: tuple[datetime, int], source_head: tuple[datetime, int] | None
) -> float:
    """Expose tuple backlog even when head and cursor timestamps are identical."""

    if source_head is None or source_head <= cursor:
        return 0.0
    return max((source_head[0] - cursor[0]).total_seconds(), 0.001)


def _initialize_bootstrap(
    conn,
    *,
    now_utc: datetime,
    overlap_seconds: float,
    timeout_ms: int,
) -> dict:
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            _set_local_limits(cur, timeout_ms)
            state = _watermark_on_cursor(cur, for_update=True)
            if state["bootstrap_started_at"] is not None:
                return state
            cur.execute(
                "SELECT min((created_at AT TIME ZONE 'Asia/Shanghai')::date) AS first_day,"
                "max((created_at AT TIME ZONE 'Asia/Shanghai')::date) AS last_day "
                "FROM v2_turn_metrics"
            )
            days = cur.fetchone()
            safe_horizon = now_utc - timedelta(seconds=overlap_seconds)
            cursor = _cursor_max_on_cursor(cur, through=safe_horizon)
            source_head = _cursor_max_on_cursor(cur)
            complete = days["first_day"] is None
            source_at, source_id = cursor or (
                datetime.fromtimestamp(0, tz=timezone.utc),
                0,
            )
            observed_at = source_head[0] if source_head is not None else None
            source_lag = _source_lag_seconds((source_at, source_id), source_head)
            cur.execute(
                "UPDATE v2_usage_rollup_watermarks SET "
                "bootstrap_started_at=%s,bootstrap_complete=%s,"
                "bootstrap_completed_at=CASE WHEN %s THEN %s ELSE NULL END,"
                "dirty_from_day=%s,dirty_through_day=%s,"
                "source_updated_at=%s,source_id=%s,"
                "source_observed_updated_at=%s,"
                "source_lag_seconds=%s,"
                "last_success_at=CASE WHEN %s THEN %s ELSE last_success_at END,"
                "last_error=NULL,version=version+1,updated_at=%s "
                "WHERE rollup_name=%s AND version=%s RETURNING *",
                (
                    now_utc,
                    complete,
                    complete,
                    now_utc,
                    days["first_day"],
                    days["last_day"],
                    source_at,
                    source_id,
                    observed_at,
                    source_lag,
                    complete,
                    now_utc,
                    now_utc,
                    ROLLUP_NAME,
                    state["version"],
                ),
            )
            updated = cur.fetchone()
            if updated is None:
                raise _CASConflict("usage bootstrap watermark changed")
            return updated


def _merge_dirty_range(
    current_from: date | None,
    current_through: date | None,
    discovered: list[date],
) -> tuple[date | None, date | None]:
    values = list(discovered)
    if current_from is not None:
        values.append(current_from)
    if current_through is not None:
        values.append(current_through)
    return (min(values), max(values)) if values else (None, None)


def _discover_dirty_days(
    conn,
    *,
    now_utc: datetime,
    max_changed_rows: int,
    overlap_seconds: float,
    timeout_ms: int,
) -> dict:
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            _set_local_limits(cur, timeout_ms)
            state = _watermark_on_cursor(cur, for_update=True)
            if not state["bootstrap_complete"]:
                return state
            cursor_at = state["source_updated_at"]
            cursor_id = int(state["source_id"])
            safe_horizon = now_utc - timedelta(seconds=overlap_seconds)
            cur.execute(
                "SELECT updated_at,id,(created_at AT TIME ZONE 'Asia/Shanghai')::date "
                "AS local_day FROM v2_turn_metrics "
                "WHERE (updated_at,id)>(%s,%s) AND updated_at <= %s "
                "ORDER BY updated_at,id LIMIT %s",
                (cursor_at, cursor_id, safe_horizon, max_changed_rows),
            )
            changed = cur.fetchall()
            discovered = [row["local_day"] for row in changed]
            new_cursor_at, new_cursor_id = cursor_at, cursor_id
            if changed:
                last = changed[-1]
                new_cursor_at, new_cursor_id = last["updated_at"], int(last["id"])
            dirty_from, dirty_through = _merge_dirty_range(
                state["dirty_from_day"], state["dirty_through_day"], discovered
            )
            source_head = _cursor_max_on_cursor(cur)
            observed = source_head[0] if source_head is not None else None
            source_lag = _source_lag_seconds(
                (new_cursor_at, new_cursor_id), source_head
            )
            cur.execute(
                "UPDATE v2_usage_rollup_watermarks SET source_updated_at=%s,source_id=%s,"
                "source_observed_updated_at=%s,"
                "source_lag_seconds=%s,"
                "dirty_from_day=%s,dirty_through_day=%s,last_error=NULL,"
                "version=version+1,updated_at=%s "
                "WHERE rollup_name=%s AND version=%s RETURNING *",
                (
                    new_cursor_at,
                    new_cursor_id,
                    observed,
                    source_lag,
                    dirty_from,
                    dirty_through,
                    now_utc,
                    ROLLUP_NAME,
                    state["version"],
                ),
            )
            updated = cur.fetchone()
            if updated is None:
                raise _CASConflict("usage cursor watermark changed")
            return updated


def _refresh_next_day(
    conn, *, now_utc: datetime, timeout_ms: int
) -> tuple[date | None, dict | None]:
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            _set_local_limits(cur, timeout_ms)
            state = _watermark_on_cursor(cur, for_update=True)
            local_day = state["dirty_from_day"]
            if local_day is None:
                return None, state
            counts = _recompute_on_cursor(cur, local_day, refreshed_at=now_utc)
            next_day = local_day + timedelta(days=1)
            done = next_day > state["dirty_through_day"]
            bootstrap_finished = bool(done and not state["bootstrap_complete"])
            cur.execute(
                "UPDATE v2_usage_rollup_watermarks SET "
                "dirty_from_day=CASE WHEN %s THEN NULL ELSE %s END,"
                "dirty_through_day=CASE WHEN %s THEN NULL ELSE dirty_through_day END,"
                "bootstrap_complete=bootstrap_complete OR %s,"
                "bootstrap_completed_at=CASE WHEN %s THEN %s ELSE bootstrap_completed_at END,"
                "refreshed_at=%s,last_success_at=%s,last_error=NULL,"
                "version=version+1,updated_at=%s "
                "WHERE rollup_name=%s AND version=%s RETURNING *",
                (
                    done,
                    next_day,
                    done,
                    bootstrap_finished,
                    bootstrap_finished,
                    now_utc,
                    now_utc,
                    now_utc,
                    now_utc,
                    ROLLUP_NAME,
                    state["version"],
                ),
            )
            updated = cur.fetchone()
            if updated is None:
                raise _CASConflict("usage day watermark changed")
            return local_day, {**counts, "state": updated}


def _record_error(error: BaseException, *, now_utc: datetime) -> None:
    try:
        with db.get_pool().connection(timeout=DEFAULT_POOL_TIMEOUT_SECONDS) as conn:
            conn.execute(
                "UPDATE v2_usage_rollup_watermarks SET last_error_at=%s,last_error=%s,"
                "updated_at=%s WHERE rollup_name=%s",
                (now_utc, type(error).__name__[:120], now_utc, ROLLUP_NAME),
            )
    except Exception:  # noqa: BLE001 - failure telemetry must itself be fail-open
        pass


def run_maintenance_tick(
    *,
    max_days: int = DEFAULT_MAX_DAYS,
    max_changed_rows: int = DEFAULT_MAX_CHANGED_ROWS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS,
    now_utc: datetime | None = None,
) -> dict:
    """Run one bounded refresh attempt and never raise to the worker service."""

    if not enabled():
        return {"status": "disabled", "days_refreshed": 0}
    days_limit = _bounded_int(max_days, DEFAULT_MAX_DAYS, minimum=1, maximum=31)
    rows_limit = _bounded_int(
        max_changed_rows,
        DEFAULT_MAX_CHANGED_ROWS,
        minimum=1,
        maximum=100_000,
    )
    overlap = _bounded_float(
        overlap_seconds,
        DEFAULT_OVERLAP_SECONDS,
        minimum=0,
        maximum=7 * 24 * 60 * 60,
    )
    timeout_ms = _bounded_int(
        statement_timeout_ms,
        DEFAULT_STATEMENT_TIMEOUT_MS,
        minimum=100,
        maximum=120_000,
    )
    pool_timeout = _bounded_float(
        pool_timeout_seconds,
        DEFAULT_POOL_TIMEOUT_SECONDS,
        minimum=0.05,
        maximum=5.0,
    )
    now = now_utc or datetime.now(timezone.utc)
    try:
        with db.get_pool().connection(timeout=pool_timeout) as conn:
            # The pool currently creates autocommit connections, but make the
            # maintenance invariant explicit: advisory admission and idle
            # orchestration must not open an implicit outer transaction.  Each
            # helper's ``conn.transaction()`` is therefore one real, short day
            # transaction rather than a savepoint in a tick-wide transaction.
            if not conn.autocommit:
                conn.autocommit = True
            locked = bool(
                conn.execute(
                    "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,)
                ).fetchone()[0]
            )
            if not locked:
                return {"status": "lock_busy", "days_refreshed": 0}
            try:
                conn.execute(
                    "INSERT INTO v2_usage_rollup_watermarks (rollup_name) VALUES (%s) "
                    "ON CONFLICT (rollup_name) DO NOTHING",
                    (ROLLUP_NAME,),
                )
                state = _initialize_bootstrap(
                    conn,
                    now_utc=now,
                    overlap_seconds=overlap,
                    timeout_ms=timeout_ms,
                )
                if state["bootstrap_complete"]:
                    state = _discover_dirty_days(
                        conn,
                        now_utc=now,
                        max_changed_rows=rows_limit,
                        overlap_seconds=overlap,
                        timeout_ms=timeout_ms,
                    )
                refreshed: list[str] = []
                for _ in range(days_limit):
                    local_day, outcome = _refresh_next_day(
                        conn, now_utc=now, timeout_ms=timeout_ms
                    )
                    if local_day is None:
                        break
                    refreshed.append(local_day.isoformat())
                    state = outcome["state"]
                return {
                    "status": "ok",
                    "days_refreshed": len(refreshed),
                    "refreshed_days": refreshed,
                    "bootstrap_complete": bool(state["bootstrap_complete"]),
                    "dirty_from_day": state["dirty_from_day"],
                    "dirty_through_day": state["dirty_through_day"],
                    "source_updated_at": state["source_updated_at"],
                    "source_id": int(state["source_id"]),
                    "source_lag_seconds": state["source_lag_seconds"],
                }
            finally:
                # Unlock while this connection is still checked out.  Returning
                # a session lock to the pool would let an unrelated borrower
                # inherit admission ownership until that session dies.
                try:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
                except Exception:  # noqa: BLE001 - close also releases it
                    pass
    except Exception as exc:  # noqa: BLE001 - optional reporting must never kill worker
        _record_error(exc, now_utc=now)
        log.warning("[v2.usage_rollup] maintenance tick failed: %s", type(exc).__name__)
        return {
            "status": "error",
            "days_refreshed": 0,
            "error": type(exc).__name__,
        }
