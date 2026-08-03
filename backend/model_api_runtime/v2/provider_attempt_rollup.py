"""Fail-open, atomic daily rollups for canonical provider attempts.

This module is maintenance-only.  It is not imported from provider dispatch,
reply, retry/failover, heartbeat, or job-state paths.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

import db


log = logging.getLogger("feedling.runtime_v2.provider_attempt_rollup")

ROLLUP_NAME = "hosted_v2_attempt_usage"
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
ADVISORY_LOCK_KEY = 0x4656324154540001
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000
DEFAULT_POOL_TIMEOUT_SECONDS = 0.5
DEFAULT_MAX_DAYS = 2
DEFAULT_MAX_CHANGED_ROWS = 6_000
DEFAULT_MAX_DIRTY_DAYS = 400
DEFAULT_MAX_STALE_ROWS = 500
DEFAULT_STALE_AFTER_SECONDS = 900.0
DEFAULT_RETENTION_DAYS = 400
MAX_RETENTION_DAYS = 36_500
DEFAULT_MAX_RETENTION_ROWS = 500

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_miss_tokens",
)


class _CASConflict(RuntimeError):
    pass


class _RetentionCancelled(RuntimeError):
    pass


def _bounded_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool):
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
    """Default on; only an explicit false-like value disables maintenance."""

    return os.environ.get(
        "FEEDLING_PROVIDER_ATTEMPT_ROLLUP_ENABLED", "1"
    ).strip().lower() not in {"0", "false", "no", "off"}


def retention_days() -> int:
    """Return the same-RDS attempt retention window, never below 400 days."""

    raw = os.environ.get(
        "FEEDLING_PROVIDER_ATTEMPT_RETENTION_DAYS",
        str(DEFAULT_RETENTION_DAYS),
    )
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    if isinstance(raw, bool):
        return DEFAULT_RETENTION_DAYS
    return max(DEFAULT_RETENTION_DAYS, min(parsed, MAX_RETENTION_DAYS))


def _rollup_sql_observer(*, section: str, statement: str, params: tuple) -> None:
    """Optional test/performance seam; production deliberately does nothing."""

    del section, statement, params


def _reconciler_source_observer(
    *, stream: str, fetched: int, advanced: int, limit: int, pending: bool
) -> None:
    """Optional test/load seam; production deliberately does nothing."""

    del stream, fetched, advanced, limit, pending


def _retention_sql_observer(*, section: str, statement: str | None = None) -> None:
    """Test seam after bounded mutations; production deliberately does nothing."""

    del section, statement


def _local_day_bounds(local_day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(local_day, time.min, tzinfo=LOCAL_TIMEZONE)
    end = datetime.combine(
        local_day + timedelta(days=1), time.min, tzinfo=LOCAL_TIMEZONE
    )
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _bounded_timeout(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STATEMENT_TIMEOUT_MS
    if isinstance(value, bool) or not math.isfinite(float(parsed)):
        return DEFAULT_STATEMENT_TIMEOUT_MS
    return max(100, min(parsed, 120_000))


def _set_transaction(cur, timeout_ms: int) -> None:
    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cur.execute(
        "SELECT set_config('statement_timeout',%s,true),"
        "set_config('lock_timeout',%s,true)",
        (str(timeout_ms), str(min(timeout_ms, 2_000))),
    )


def _effective_attempt_ctes(
    *,
    cohort_where: str = "m.created_at >= %s AND m.created_at < %s",
) -> str:
    """Shared set-based correction/pricing pipeline.

    Maintenance uses the default full-day predicate.  Read-side hybrid reports
    pass an already-built, parameterized raw-edge predicate; values never enter
    this SQL fragment through string interpolation.
    """

    corrected_tokens = ",\n".join(
        f"CASE WHEN a.{field} IS NULL AND c.{field}_delta IS NULL THEN NULL "
        f"ELSE coalesce(a.{field},0)+coalesce(c.{field}_delta,0) END AS {field}"
        for field in _TOKEN_FIELDS
    )
    return f"""
WITH turn_cohort AS MATERIALIZED (
  SELECT m.job_id,m.user_id,
         coalesce(nullif(m.lane,''),'unknown') AS cohort_lane
  FROM v2_turn_metrics m
  WHERE {cohort_where}
), attempt_base AS MATERIALIZED (
  SELECT a.*,t.user_id AS cohort_user_id,t.cohort_lane
  FROM turn_cohort t JOIN llm_provider_attempts a ON a.job_id=t.job_id
  WHERE a.source='runtime_recorder'
), correction AS MATERIALIZED (
  SELECT c.attempt_id,
    sum(c.input_tokens_delta)::bigint AS input_tokens_delta,
    sum(c.output_tokens_delta)::bigint AS output_tokens_delta,
    sum(c.reasoning_tokens_delta)::bigint AS reasoning_tokens_delta,
    sum(c.cache_read_tokens_delta)::bigint AS cache_read_tokens_delta,
    sum(c.cache_write_tokens_delta)::bigint AS cache_write_tokens_delta,
    sum(c.cache_miss_tokens_delta)::bigint AS cache_miss_tokens_delta,
    sum(c.cost_delta) AS cost_delta,
    count(*) FILTER (
      WHERE c.input_tokens_delta IS NOT NULL OR c.output_tokens_delta IS NOT NULL
         OR c.reasoning_tokens_delta IS NOT NULL
         OR c.cache_read_tokens_delta IS NOT NULL
         OR c.cache_write_tokens_delta IS NOT NULL
         OR c.cache_miss_tokens_delta IS NOT NULL
    ) > 0 AS has_usage_correction,
    bool_or(c.cost_delta IS NOT NULL AND c.currency IS NULL)
      AS cost_currency_missing,
    min(c.currency) FILTER (WHERE c.cost_delta IS NOT NULL) AS cost_currency,
    count(DISTINCT c.currency) FILTER (WHERE c.cost_delta IS NOT NULL)
      AS cost_currency_count
  FROM llm_provider_attempt_corrections c
  JOIN attempt_base a ON a.attempt_id=c.attempt_id
  GROUP BY c.attempt_id
), corrected AS MATERIALIZED (
  SELECT a.attempt_id,a.cohort_user_id AS user_id,a.cohort_lane,a.call_id,
    a.outer_attempt_ordinal,a.inner_attempt_ordinal,a.retry_kind,
    a.requested_provider,a.requested_model,a.resolved_provider,a.resolved_model,
    a.outcome,a.possibly_billed,a.ttft_ms,a.started_at,
    (a.usage_known OR coalesce(c.has_usage_correction,false))
      AS effective_usage_known,
    {corrected_tokens},
    CASE WHEN a.cost IS NULL AND c.cost_delta IS NULL THEN NULL
         ELSE coalesce(a.cost,0)+coalesce(c.cost_delta,0) END
      AS authoritative_cost,
    CASE
      WHEN a.cost IS NOT NULL AND a.currency IS NULL THEN NULL
      WHEN coalesce(c.cost_currency_missing,false) THEN NULL
      WHEN a.cost IS NOT NULL AND c.cost_currency IS NOT NULL
           AND a.currency<>c.cost_currency THEN NULL
      WHEN coalesce(c.cost_currency_count,0)>1 THEN NULL
      ELSE coalesce(CASE WHEN a.cost IS NOT NULL THEN a.currency END,c.cost_currency)
    END AS authoritative_currency
  FROM attempt_base a LEFT JOIN correction c USING (attempt_id)
), rate_ranges AS MATERIALIZED (
  SELECT r.*,
    lead(r.effective_at) OVER (
      PARTITION BY r.provider,r.model ORDER BY r.effective_at,r.version
    ) AS effective_before
  FROM llm_rate_cards r
), rate_resolved AS MATERIALIZED (
  SELECT c.*,r.version AS estimated_rate_card_version,
    r.currency AS rate_currency,r.input_cost_per_million,
    r.output_cost_per_million,r.reasoning_cost_per_million,
    r.cache_read_cost_per_million,r.cache_write_cost_per_million,
    r.cache_miss_cost_per_million
  FROM corrected c LEFT JOIN rate_ranges r
    ON r.provider=c.resolved_provider AND r.model=c.resolved_model
   AND c.started_at>=r.effective_at
   AND (r.effective_before IS NULL OR c.started_at<r.effective_before)
), priced AS MATERIALIZED (
  SELECT c.*,
    CASE WHEN c.authoritative_cost IS NULL AND c.effective_usage_known
              AND c.estimated_rate_card_version IS NOT NULL
              AND (c.input_tokens IS NOT NULL OR c.output_tokens IS NOT NULL
                OR c.reasoning_tokens IS NOT NULL OR c.cache_read_tokens IS NOT NULL
                OR c.cache_write_tokens IS NOT NULL OR c.cache_miss_tokens IS NOT NULL)
              AND (c.input_cost_per_million=0 OR c.input_tokens IS NOT NULL)
              AND (c.output_cost_per_million=0 OR c.output_tokens IS NOT NULL)
              AND (c.reasoning_cost_per_million=0 OR c.reasoning_tokens IS NOT NULL)
              AND (c.cache_read_cost_per_million=c.input_cost_per_million
                   OR c.cache_read_tokens IS NOT NULL)
              AND (c.cache_write_cost_per_million=c.input_cost_per_million
                   OR c.cache_write_tokens IS NOT NULL)
              AND (c.cache_miss_cost_per_million=c.input_cost_per_million
                   OR c.cache_miss_tokens IS NOT NULL)
      THEN (
        greatest(coalesce(c.input_tokens,0)-coalesce(c.cache_read_tokens,0)
          -CASE WHEN c.cache_miss_tokens IS NOT NULL THEN c.cache_miss_tokens
                ELSE coalesce(c.cache_write_tokens,0) END,0)
          *c.input_cost_per_million
        +coalesce(c.cache_read_tokens,0)*c.cache_read_cost_per_million
        +coalesce(c.cache_write_tokens,0)*c.cache_write_cost_per_million
        +CASE WHEN c.cache_miss_tokens IS NULL THEN 0 ELSE
          greatest(c.cache_miss_tokens-coalesce(c.cache_write_tokens,0),0)
          END*c.cache_miss_cost_per_million
        +coalesce(c.output_tokens,0)*c.output_cost_per_million
        +coalesce(c.reasoning_tokens,0)*c.reasoning_cost_per_million
      )/1000000::numeric END AS estimated_cost
  FROM rate_resolved c
)
"""


def _day_rebuild_statement() -> str:
    token_columns = ",".join(
        item
        for field in _TOKEN_FIELDS
        for item in (f"{field}_sum", f"{field}_known_count")
    )
    token_selects = ",\n".join(
        item
        for field in _TOKEN_FIELDS
        for item in (
            f"coalesce(sum({field}) FILTER (WHERE {field} IS NOT NULL),0)::bigint",
            f"count({field})::bigint",
        )
    )
    return (
        _effective_attempt_ctes()
        + f""",
cohort_calls AS MATERIALIZED (
  SELECT DISTINCT call_id FROM corrected
), global_attempts AS MATERIALIZED (
  SELECT a.call_id,a.outer_attempt_ordinal,a.inner_attempt_ordinal
  FROM cohort_calls c JOIN llm_provider_attempts a USING (call_id)
  WHERE a.source='runtime_recorder'
), outer_gaps AS (
  SELECT call_id,
    greatest(
      coalesce(max(outer_attempt_ordinal) FILTER (WHERE outer_attempt_ordinal>=1),0)
      -count(DISTINCT outer_attempt_ordinal)
        FILTER (WHERE outer_attempt_ordinal>=1),0
    )::bigint AS missing_outer_ordinals
  FROM global_attempts GROUP BY call_id
), inner_gap_groups AS (
  SELECT call_id,outer_attempt_ordinal,
    greatest(
      coalesce(max(inner_attempt_ordinal) FILTER (WHERE inner_attempt_ordinal>=1),0)
      -count(DISTINCT inner_attempt_ordinal)
        FILTER (WHERE inner_attempt_ordinal>=1),0
    )::bigint AS missing_inner_ordinals
  FROM global_attempts GROUP BY call_id,outer_attempt_ordinal
), call_gaps AS (
  SELECT o.call_id,o.missing_outer_ordinals,
    coalesce(sum(i.missing_inner_ordinals),0)::bigint AS missing_inner_ordinals
  FROM outer_gaps o LEFT JOIN inner_gap_groups i USING (call_id)
  GROUP BY o.call_id,o.missing_outer_ordinals
), dimension_insert AS (
INSERT INTO llm_usage_daily_attempt_dimensions (
  local_day,user_id,cohort_lane,requested_provider,requested_model,
  resolved_provider,resolved_model,effective_usage_known,cost_kind,currency,
  attempts,retry_attempts,failover_attempts,failed_attempts,
  possibly_billed_attempts,{token_columns},authoritative_cost_attempts,
  estimated_cost_attempts,unknown_cost_attempts,cost_amount,ttft_samples,
  refreshed_at
)
SELECT %s,user_id,cohort_lane,requested_provider,requested_model,
  resolved_provider,resolved_model,effective_usage_known,
  CASE WHEN authoritative_cost IS NOT NULL THEN 'authoritative'
       WHEN estimated_cost IS NOT NULL THEN 'estimated' ELSE 'unknown' END,
  CASE WHEN authoritative_cost IS NOT NULL THEN authoritative_currency
       WHEN estimated_cost IS NOT NULL THEN rate_currency ELSE NULL END,
  count(*)::bigint,
  count(*) FILTER (WHERE retry_kind<>'initial')::bigint,
  count(*) FILTER (WHERE retry_kind='failover')::bigint,
  count(*) FILTER (WHERE outcome IN ('failed','timed_out','cancelled'))::bigint,
  count(*) FILTER (WHERE possibly_billed)::bigint,
  {token_selects},
  count(*) FILTER (WHERE authoritative_cost IS NOT NULL)::bigint,
  count(*) FILTER (
    WHERE authoritative_cost IS NULL AND estimated_cost IS NOT NULL
  )::bigint,
  count(*) FILTER (
    WHERE authoritative_cost IS NULL AND estimated_cost IS NULL
  )::bigint,
  coalesce(sum(CASE WHEN authoritative_cost IS NOT NULL THEN authoritative_cost
                    WHEN estimated_cost IS NOT NULL THEN estimated_cost ELSE 0 END),0),
  coalesce(array_agg(ttft_ms ORDER BY ttft_ms,attempt_id)
    FILTER (WHERE ttft_ms IS NOT NULL),'{{}}'::double precision[]),%s
FROM priced
GROUP BY user_id,cohort_lane,requested_provider,requested_model,
  resolved_provider,resolved_model,effective_usage_known,
  CASE WHEN authoritative_cost IS NOT NULL THEN 'authoritative'
       WHEN estimated_cost IS NOT NULL THEN 'estimated' ELSE 'unknown' END,
  CASE WHEN authoritative_cost IS NOT NULL THEN authoritative_currency
       WHEN estimated_cost IS NOT NULL THEN rate_currency ELSE NULL END
RETURNING 1
), membership_insert AS (
INSERT INTO llm_usage_daily_call_memberships (
  local_day,user_id,cohort_lane,call_id,requested_provider,requested_model,
  resolved_provider,resolved_model,effective_usage_known,
  missing_outer_ordinals,missing_inner_ordinals,refreshed_at
)
SELECT DISTINCT %s,p.user_id,p.cohort_lane,p.call_id,p.requested_provider,
  p.requested_model,p.resolved_provider,p.resolved_model,p.effective_usage_known,
  coalesce(g.missing_outer_ordinals,0),coalesce(g.missing_inner_ordinals,0),%s
FROM priced p LEFT JOIN call_gaps g USING (call_id)
RETURNING 1
)
SELECT (SELECT count(*)::int FROM dimension_insert) AS dimensions,
       (SELECT count(*)::int FROM membership_insert) AS memberships
"""
    )


def _recompute_on_cursor(
    cur,
    local_day: date,
    *,
    refreshed_at: datetime,
    expected_generation: int | None = None,
) -> dict:
    start_at, end_at = _local_day_bounds(local_day)
    cur.execute(
        "DELETE FROM llm_usage_daily_attempt_dimensions WHERE local_day=%s",
        (local_day,),
    )
    cur.execute(
        "DELETE FROM llm_usage_daily_call_memberships WHERE local_day=%s",
        (local_day,),
    )
    statement = _day_rebuild_statement()
    params = (
        start_at,
        end_at,
        local_day,
        refreshed_at,
        local_day,
        refreshed_at,
    )
    _rollup_sql_observer(
        section="day_rebuild", statement=statement, params=params
    )
    cur.execute(statement, params)
    counts = cur.fetchone()
    if expected_generation is None:
        cur.execute(
            "DELETE FROM llm_usage_rollup_dirty_days "
            "WHERE rollup_name=%s AND local_day=%s",
            (ROLLUP_NAME, local_day),
        )
    else:
        cur.execute(
            "DELETE FROM llm_usage_rollup_dirty_days "
            "WHERE rollup_name=%s AND local_day=%s AND generation=%s",
            (ROLLUP_NAME, local_day, expected_generation),
        )
        if cur.rowcount != 1:
            raise _CASConflict("attempt rollup dirty generation changed")
    return {
        "status": "ok",
        "dimensions": int(counts["dimensions"]),
        "memberships": int(counts["memberships"]),
    }


def recompute_local_day(
    local_day: date,
    *,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    refreshed_at: datetime | None = None,
    expected_generation: int | None = None,
) -> dict:
    """Atomically replace both attempt rollups; return safely on any failure."""

    empty = {"status": "error", "dimensions": 0, "memberships": 0}
    try:
        if not isinstance(local_day, date) or isinstance(local_day, datetime):
            raise TypeError("local_day must be a date")
        refreshed = refreshed_at or datetime.now(timezone.utc)
        if refreshed.tzinfo is None:
            raise TypeError("refreshed_at must be timezone-aware")
        if expected_generation is not None and (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation must be nonnegative")
        timeout_ms = _bounded_timeout(statement_timeout_ms)
        with db.get_pool().connection(timeout=DEFAULT_POOL_TIMEOUT_SECONDS) as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    _set_transaction(cur, timeout_ms)
                    return _recompute_on_cursor(
                        cur,
                        local_day,
                        refreshed_at=refreshed,
                        expected_generation=expected_generation,
                    )
    except Exception as exc:  # noqa: BLE001 - optional accounting stays fail-open
        error = type(exc).__name__[:120]
        log.warning("[attempt_rollup] local day rebuild failed: %s", error)
        return {**empty, "error": error}


def _cancel_requested(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _watermark_on_cursor(cur, *, for_update: bool = False) -> dict:
    cur.execute(
        "SELECT * FROM llm_usage_rollup_watermarks WHERE rollup_name=%s"
        + (" FOR UPDATE" if for_update else ""),
        (ROLLUP_NAME,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("attempt rollup watermark missing")
    return row


def _upsert_dirty_days(
    cur,
    days: set[date] | list[date],
    *,
    reason: str,
    generation: int,
    now_utc: datetime,
) -> None:
    rows = sorted(set(days))
    cur.execute(
        "SELECT retained_from FROM llm_usage_rollup_watermarks "
        "WHERE rollup_name=%s",
        (ROLLUP_NAME,),
    )
    watermark = cur.fetchone()
    retained_from = watermark["retained_from"] if watermark else None
    if retained_from is not None:
        rows = [local_day for local_day in rows if local_day >= retained_from]
    if not rows:
        return
    cur.executemany(
        "INSERT INTO llm_usage_rollup_dirty_days "
        "(rollup_name,local_day,reason,generation,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (rollup_name,local_day) DO UPDATE SET "
        "reason=EXCLUDED.reason,generation=greatest("
        "llm_usage_rollup_dirty_days.generation,EXCLUDED.generation),"
        "updated_at=EXCLUDED.updated_at",
        [(ROLLUP_NAME, day, reason, generation, now_utc, now_utc) for day in rows],
    )


def _source_heads_on_cursor(cur) -> dict[str, object]:
    cur.execute(
        "SELECT updated_at,attempt_id FROM llm_provider_attempts "
        "WHERE source='runtime_recorder' ORDER BY updated_at DESC,attempt_id DESC LIMIT 1"
    )
    attempt = cur.fetchone()
    cur.execute("SELECT coalesce(max(id),0)::bigint AS id FROM llm_provider_attempt_corrections")
    correction = cur.fetchone()
    cur.execute(
        "SELECT updated_at,id FROM v2_turn_metrics "
        "ORDER BY updated_at DESC,id DESC LIMIT 1"
    )
    turn = cur.fetchone()
    cur.execute(
        "SELECT created_at,provider,model,version FROM llm_rate_cards "
        "ORDER BY created_at DESC,provider DESC,model DESC,version DESC LIMIT 1"
    )
    rate = cur.fetchone()
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    return {
        "attempt_updated_at": attempt["updated_at"] if attempt else epoch,
        "attempt_updated_id": attempt["attempt_id"] if attempt else "",
        "late_correction_id": int(correction["id"]),
        "turn_metric_updated_at": turn["updated_at"] if turn else epoch,
        "turn_metric_id": int(turn["id"]) if turn else 0,
        "rate_card_created_at": rate["created_at"] if rate else epoch,
        "rate_card_provider": rate["provider"] if rate else "",
        "rate_card_model": rate["model"] if rate else "",
        "rate_card_version": rate["version"] if rate else "",
    }


def _bootstrap_batch(
    conn,
    *,
    max_dirty_days: int,
    now_utc: datetime,
    timeout_ms: int,
) -> dict:
    """Enqueue one sparse bootstrap/replay page and atomically move its cursor."""

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            _set_transaction(cur, timeout_ms)
            state = _watermark_on_cursor(cur, for_update=True)
            if state["bootstrap_complete"]:
                return state
            generation = int(state["replay_generation"])
            if state["completed_through_day"] is None:
                # Initial bootstrap snapshots all four source heads. Any later
                # change is then discovered after the sparse day build.
                if generation == 0:
                    heads = _source_heads_on_cursor(cur)
                    cur.execute(
                        "UPDATE llm_usage_rollup_watermarks SET "
                        "attempt_updated_at=%s,attempt_updated_id=%s,late_correction_id=%s,"
                        "turn_metric_updated_at=%s,turn_metric_id=%s,"
                        "rate_card_created_at=%s,rate_card_provider=%s,rate_card_model=%s,"
                        "rate_card_version=%s,version=version+1,updated_at=%s "
                        "WHERE rollup_name=%s AND version=%s RETURNING *",
                        (
                            heads["attempt_updated_at"], heads["attempt_updated_id"],
                            heads["late_correction_id"], heads["turn_metric_updated_at"],
                            heads["turn_metric_id"], heads["rate_card_created_at"],
                            heads["rate_card_provider"], heads["rate_card_model"],
                            heads["rate_card_version"], now_utc, ROLLUP_NAME,
                            state["version"],
                        ),
                    )
                    state = cur.fetchone()
                    if state is None:
                        raise _CASConflict("attempt bootstrap head changed")
            cur.execute(
                "SELECT DISTINCT (created_at AT TIME ZONE 'Asia/Shanghai')::date AS local_day "
                "FROM v2_turn_metrics WHERE (%s::date IS NULL OR "
                "(created_at AT TIME ZONE 'Asia/Shanghai')::date>%s) "
                "AND (%s::date IS NULL OR "
                "(created_at AT TIME ZONE 'Asia/Shanghai')::date>=%s) "
                "ORDER BY local_day LIMIT %s",
                (
                    state["completed_through_day"],
                    state["completed_through_day"],
                    state["retained_from"],
                    state["retained_from"],
                    max_dirty_days,
                ),
            )
            days = [row["local_day"] for row in cur.fetchall()]
            _upsert_dirty_days(
                cur, days, reason="replay" if generation else "bootstrap",
                generation=generation, now_utc=now_utc,
            )
            through = days[-1] if days else state["completed_through_day"]
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM v2_turn_metrics WHERE "
                "(%s::date IS NULL OR (created_at AT TIME ZONE 'Asia/Shanghai')::date>%s) "
                "AND (%s::date IS NULL OR "
                "(created_at AT TIME ZONE 'Asia/Shanghai')::date>=%s))",
                (through, through, state["retained_from"], state["retained_from"]),
            )
            complete = not bool(cur.fetchone()["exists"])
            cur.execute(
                "UPDATE llm_usage_rollup_watermarks SET completed_through_day=%s,"
                "bootstrap_complete=%s,version=version+1,updated_at=%s "
                "WHERE rollup_name=%s AND version=%s RETURNING *",
                (through, complete, now_utc, ROLLUP_NAME, state["version"]),
            )
            updated = cur.fetchone()
            if updated is None:
                raise _CASConflict("attempt bootstrap cursor changed")
            return updated


def _discover_changes(
    conn,
    *,
    max_changed_rows: int,
    max_dirty_days: int,
    now_utc: datetime,
    timeout_ms: int,
) -> dict:
    """Discover four source streams; every cursor commits with its dirty rows."""

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            _set_transaction(cur, timeout_ms)
            state = _watermark_on_cursor(cur, for_update=True)
            generation = int(state["replay_generation"])
            dirty: set[date] = set()
            attempt_at = state["attempt_updated_at"]
            attempt_id = state["attempt_updated_id"]
            correction_id = int(state["late_correction_id"])
            turn_at = state["turn_metric_updated_at"]
            turn_id = int(state["turn_metric_id"])
            rate_cursor = (
                state["rate_card_created_at"],
                state["rate_card_provider"],
                state["rate_card_model"],
                state["rate_card_version"],
            )
            replay_requested = False

            def consume(rows: list[dict]) -> list[dict]:
                """Take only the cursor prefix whose new days fit this tick."""

                nonlocal replay_requested
                accepted: list[dict] = []
                for row in rows:
                    local_day = row["local_day"]
                    if (
                        local_day is not None
                        and local_day not in dirty
                        and len(dirty) >= max_dirty_days
                    ):
                        # No per-stream cursor contains a day subcursor. Switch
                        # to the bounded replay generation so this source may
                        # advance without losing the omitted day or starving a
                        # later stream behind perpetual earlier-stream writes.
                        replay_requested = True
                    accepted.append(row)
                    if local_day is not None and len(dirty) < max_dirty_days:
                        dirty.add(local_day)
                return accepted

            def fetch_stream(stream: str, limit: int) -> tuple[int, bool]:
                nonlocal attempt_at, attempt_id, correction_id, turn_at, turn_id
                if limit <= 0:
                    return 0, False
                if stream == "attempt":
                    cur.execute(
                        "SELECT a.updated_at,a.attempt_id,"
                        "(m.created_at AT TIME ZONE 'Asia/Shanghai')::date AS local_day "
                        "FROM llm_provider_attempts a LEFT JOIN v2_turn_metrics m "
                        "ON m.job_id=a.job_id WHERE a.source='runtime_recorder' "
                        "AND (a.updated_at,a.attempt_id)>(%s,%s) "
                        "ORDER BY a.updated_at,a.attempt_id LIMIT %s",
                        (attempt_at, attempt_id, limit),
                    )
                elif stream == "correction":
                    cur.execute(
                        "SELECT c.id,(m.created_at AT TIME ZONE 'Asia/Shanghai')::date "
                        "AS local_day FROM llm_provider_attempt_corrections c "
                        "JOIN llm_provider_attempts a ON a.attempt_id=c.attempt_id "
                        "LEFT JOIN v2_turn_metrics m ON m.job_id=a.job_id "
                        "WHERE c.id>%s ORDER BY c.id LIMIT %s",
                        (correction_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT updated_at,id,"
                        "(created_at AT TIME ZONE 'Asia/Shanghai')::date AS local_day "
                        "FROM v2_turn_metrics WHERE (updated_at,id)>(%s,%s) "
                        "ORDER BY updated_at,id LIMIT %s",
                        (turn_at, turn_id, limit),
                    )
                rows = cur.fetchall()
                accepted = consume(rows)
                if accepted:
                    last = accepted[-1]
                    if stream == "attempt":
                        attempt_at, attempt_id = last["updated_at"], last["attempt_id"]
                    elif stream == "correction":
                        correction_id = int(last["id"])
                    else:
                        turn_at, turn_id = last["updated_at"], int(last["id"])
                pending = len(rows) == limit or len(accepted) < len(rows)
                _reconciler_source_observer(
                    stream=stream,
                    fetched=len(rows),
                    advanced=len(accepted),
                    limit=limit,
                    pending=pending,
                )
                return len(rows), pending

            # Rate cards consume one row from the same hard global budget.
            cur.execute(
                "SELECT created_at,provider,model,version,effective_at FROM llm_rate_cards "
                "WHERE (created_at,provider,model,version)>(%s,%s,%s,%s) "
                "ORDER BY created_at,provider,model,version LIMIT 1",
                rate_cursor,
            )
            rate = cur.fetchone()
            rate_rows = int(rate is not None)
            _reconciler_source_observer(
                stream="rate_card",
                fetched=rate_rows,
                advanced=rate_rows,
                limit=1,
                pending=rate is not None,
            )

            main_budget = max_changed_rows - rate_rows
            base, remainder = divmod(main_budget, 3)
            quotas = {
                stream: base + int(index < remainder)
                for index, stream in enumerate(("attempt", "correction", "turn"))
            }
            pending_streams: list[str] = []
            unused = 0
            for stream in ("attempt", "correction", "turn"):
                fetched, pending = fetch_stream(stream, quotas[stream])
                unused += quotas[stream] - fetched
                if pending:
                    pending_streams.append(stream)
            # Reclaim unused reserved quota only after every main stream had a
            # fair first page. A bounded second pass cannot exceed the same
            # max_changed_rows hard cap.
            while unused > 0 and pending_streams:
                stream = pending_streams.pop(0)
                fetched, pending = fetch_stream(stream, unused)
                unused -= fetched
                if pending and fetched > 0:
                    pending_streams.append(stream)
                if fetched == 0:
                    break

            if rate is not None:
                remaining = max_dirty_days - len(dirty)
                cur.execute(
                    "SELECT min(effective_at) AS effective_before FROM llm_rate_cards "
                    "WHERE provider=%s AND model=%s AND effective_at>%s",
                    (rate["provider"], rate["model"], rate["effective_at"]),
                )
                effective_before = cur.fetchone()["effective_before"]
                cur.execute(
                    "SELECT DISTINCT (m.created_at AT TIME ZONE 'Asia/Shanghai')::date AS local_day "
                    "FROM llm_provider_attempts a JOIN v2_turn_metrics m ON m.job_id=a.job_id "
                    "WHERE a.source='runtime_recorder' AND a.resolved_provider=%s "
                    "AND a.resolved_model=%s AND a.started_at>=%s "
                    "AND (%s::timestamptz IS NULL OR a.started_at<%s) "
                    "ORDER BY local_day LIMIT %s",
                    (
                        rate["provider"], rate["model"], rate["effective_at"],
                        effective_before, effective_before, remaining + 1,
                    ),
                )
                rate_days = [row["local_day"] for row in cur.fetchall()]
                if len(rate_days) <= remaining:
                    dirty.update(rate_days)
                else:
                    # The durable cursor has no intra-card day component. A
                    # bounded sparse replay is the only way to advance without
                    # either losing days or retrying this card forever.
                    replay_requested = True
                rate_cursor = (
                    rate["created_at"], rate["provider"], rate["model"], rate["version"]
                )

            _upsert_dirty_days(
                cur, dirty, reason="source_change", generation=generation, now_utc=now_utc
            )
            cur.execute(
                "UPDATE llm_usage_rollup_watermarks SET "
                "attempt_updated_at=%s,attempt_updated_id=%s,late_correction_id=%s,"
                "turn_metric_updated_at=%s,turn_metric_id=%s,rate_card_created_at=%s,"
                "rate_card_provider=%s,rate_card_model=%s,rate_card_version=%s,"
                "replay_generation=%s,bootstrap_complete=%s,completed_through_day=%s,"
                "version=version+1,updated_at=%s WHERE rollup_name=%s AND version=%s "
                "RETURNING *",
                (
                    attempt_at, attempt_id, correction_id, turn_at, turn_id,
                    *rate_cursor,
                    generation + 1 if replay_requested else generation,
                    False if replay_requested else state["bootstrap_complete"],
                    None if replay_requested else state["completed_through_day"],
                    now_utc, ROLLUP_NAME, state["version"],
                ),
            )
            updated = cur.fetchone()
            if updated is None:
                raise _CASConflict("attempt source cursor changed")
            result = dict(updated)
            pending_times: dict[str, datetime | None] = {}
            cur.execute(
                "SELECT updated_at FROM llm_provider_attempts "
                "WHERE source='runtime_recorder' AND (updated_at,attempt_id)>(%s,%s) "
                "ORDER BY updated_at,attempt_id LIMIT 1",
                (attempt_at, attempt_id),
            )
            row = cur.fetchone()
            pending_times["attempt"] = row["updated_at"] if row else None
            cur.execute(
                "SELECT created_at FROM llm_provider_attempt_corrections "
                "WHERE id>%s ORDER BY id LIMIT 1", (correction_id,),
            )
            row = cur.fetchone()
            pending_times["correction"] = row["created_at"] if row else None
            cur.execute(
                "SELECT updated_at FROM v2_turn_metrics WHERE (updated_at,id)>(%s,%s) "
                "ORDER BY updated_at,id LIMIT 1", (turn_at, turn_id),
            )
            row = cur.fetchone()
            pending_times["turn"] = row["updated_at"] if row else None
            cur.execute(
                "SELECT created_at FROM llm_rate_cards "
                "WHERE (created_at,provider,model,version)>(%s,%s,%s,%s) "
                "ORDER BY created_at,provider,model,version LIMIT 1", rate_cursor,
            )
            row = cur.fetchone()
            pending_times["rate_card"] = row["created_at"] if row else None
            result["_source_backlog"] = {
                stream: value is not None for stream, value in pending_times.items()
            }
            result["_source_lag_seconds"] = max(
                (
                    max((now_utc - value).total_seconds(), 0.0)
                    for value in pending_times.values()
                    if value is not None
                ),
                default=0.0,
            )
            return result


def _reconcile_stale_started(
    conn,
    *,
    max_rows: int,
    stale_after_seconds: float,
    timeout_ms: int,
) -> int:
    if max_rows <= 0:
        return 0
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            _set_transaction(cur, timeout_ms)
            cur.execute(
                "WITH picked AS (SELECT attempt_id FROM llm_provider_attempts "
                "WHERE source='runtime_recorder' AND state='started' "
                "AND finished_at IS NULL AND possibly_billed=false "
                "AND started_at < now()-(%s * interval '1 second') "
                "ORDER BY started_at,attempt_id FOR UPDATE SKIP LOCKED LIMIT %s) "
                "UPDATE llm_provider_attempts a SET possibly_billed=true,"
                "updated_at=clock_timestamp() FROM picked p "
                "WHERE a.attempt_id=p.attempt_id RETURNING a.attempt_id",
                (stale_after_seconds, max_rows),
            )
            return len(cur.fetchall())


def _run_retention_batch(
    conn,
    *,
    cutoff: date,
    max_rows: int,
    timeout_ms: int,
    now_utc: datetime,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Prune one atomic, bounded retention page and publish only after proof."""

    empty = {
        "status": "error",
        "complete": False,
        "attempts_deleted": 0,
        "dimensions_deleted": 0,
        "memberships_deleted": 0,
        "dirty_days_deleted": 0,
        "retained_from": None,
    }
    if max_rows <= 0:
        return {**empty, "status": "ok"}
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                _set_transaction(cur, timeout_ms)
                state = _watermark_on_cursor(cur, for_update=True)
                published = state["retained_from"]
                if published is not None and cutoff <= published:
                    return {
                        **empty,
                        "status": "ok",
                        "complete": True,
                        "retained_from": published,
                    }
                effective_cutoff = max(cutoff, published) if published else cutoff
                cutoff_at, _ = _local_day_bounds(effective_cutoff)
                published_at = (
                    _local_day_bounds(published)[0]
                    if published is not None else None
                )
                if _cancel_requested(cancel_event):
                    raise _RetentionCancelled
                lower_turn = (
                    "AND m.created_at>=%s " if published_at is not None else ""
                )
                candidate_job_limit = min(max_rows * 4, 40_000)
                attempt_delete_job = (
                    "WITH old_jobs AS MATERIALIZED (SELECT m.job_id,m.created_at "
                    "FROM v2_turn_metrics m WHERE m.created_at<%s "
                    + lower_turn
                    + "AND EXISTS (SELECT 1 FROM llm_provider_attempts eligible "
                    "WHERE eligible.job_id=m.job_id "
                    "AND eligible.job_id IS NOT NULL "
                    "AND eligible.source='runtime_recorder' OFFSET 0) "
                    "ORDER BY m.created_at DESC,m.id DESC LIMIT %s), "
                    "picked AS (SELECT a.attempt_id FROM old_jobs m "
                    "JOIN LATERAL (SELECT candidate.attempt_id "
                    "FROM llm_provider_attempts candidate "
                    "WHERE candidate.job_id=m.job_id "
                    "AND candidate.job_id IS NOT NULL "
                    "AND candidate.source='runtime_recorder' OFFSET 0) candidate "
                    "ON true JOIN llm_provider_attempts a "
                    "ON a.attempt_id=candidate.attempt_id "
                    "ORDER BY m.created_at DESC,a.attempt_id "
                    "FOR UPDATE OF a SKIP LOCKED LIMIT %s), deleted AS ("
                    "DELETE FROM llm_provider_attempts a USING picked p "
                    "WHERE a.attempt_id=p.attempt_id RETURNING 1) "
                    "SELECT count(*)::int AS count FROM deleted"
                )
                _retention_sql_observer(
                    section="attempt_delete_job", statement=attempt_delete_job
                )
                job_params: tuple[object, ...] = (
                    (cutoff_at, published_at, candidate_job_limit, max_rows)
                    if published_at is not None
                    else (cutoff_at, candidate_job_limit, max_rows)
                )
                cur.execute(attempt_delete_job, job_params)
                attempts_deleted = int(cur.fetchone()["count"])

                remaining_limit = max_rows - attempts_deleted
                attempt_delete_orphan = (
                    "WITH picked AS (SELECT a.attempt_id "
                    "FROM llm_provider_attempts a "
                    "WHERE a.source='runtime_recorder' AND a.started_at<%s "
                    + ("AND a.started_at>=%s " if published_at is not None else "")
                    + "AND NOT EXISTS (SELECT 1 FROM v2_turn_metrics m "
                    "WHERE m.job_id=a.job_id) "
                    "ORDER BY a.started_at DESC,a.attempt_id "
                    "FOR UPDATE OF a SKIP LOCKED LIMIT %s), deleted AS ("
                    "DELETE FROM llm_provider_attempts a USING picked p "
                    "WHERE a.attempt_id=p.attempt_id RETURNING 1) "
                    "SELECT count(*)::int AS count FROM deleted"
                )
                _retention_sql_observer(
                    section="attempt_delete_orphan",
                    statement=attempt_delete_orphan,
                )
                orphan_params: tuple[object, ...] = (
                    (cutoff_at, published_at, remaining_limit)
                    if published_at is not None
                    else (cutoff_at, remaining_limit)
                )
                cur.execute(
                    attempt_delete_orphan,
                    orphan_params,
                )
                attempts_deleted += int(cur.fetchone()["count"])
                _retention_sql_observer(
                    section="after_attempt_delete", statement=None
                )
                if _cancel_requested(cancel_event):
                    raise _RetentionCancelled

                deleted: dict[str, int] = {}
                for key, table, extra_where in (
                    ("dimensions", "llm_usage_daily_attempt_dimensions", ""),
                    ("memberships", "llm_usage_daily_call_memberships", ""),
                    (
                        "dirty_days",
                        "llm_usage_rollup_dirty_days",
                        " AND rollup_name=%s",
                    ),
                ):
                    params: tuple[object, ...]
                    if extra_where:
                        params = (effective_cutoff, ROLLUP_NAME, max_rows)
                    else:
                        params = (effective_cutoff, max_rows)
                    cur.execute(
                        f"WITH picked AS (SELECT ctid FROM {table} "
                        f"WHERE local_day<%s{extra_where} ORDER BY local_day,ctid "
                        "FOR UPDATE SKIP LOCKED LIMIT %s), deleted AS ("
                        f"DELETE FROM {table} t USING picked p WHERE t.ctid=p.ctid "
                        "RETURNING 1) SELECT count(*)::int AS count FROM deleted",
                        params,
                    )
                    deleted[key] = int(cur.fetchone()["count"])
                    _retention_sql_observer(
                        section=f"after_{key}_delete", statement=None
                    )
                    if _cancel_requested(cancel_event):
                        raise _RetentionCancelled

                cur.execute(
                    "SELECT "
                    "(EXISTS (SELECT 1 FROM v2_turn_metrics m "
                    "JOIN llm_provider_attempts a ON a.job_id=m.job_id "
                    "WHERE m.created_at<%s "
                    + lower_turn
                    + "AND a.source='runtime_recorder') OR "
                    "EXISTS (SELECT 1 FROM llm_provider_attempts a "
                    "WHERE a.source='runtime_recorder' AND a.started_at<%s "
                    + ("AND a.started_at>=%s " if published_at is not None else "")
                    +
                    "AND NOT EXISTS (SELECT 1 FROM v2_turn_metrics m "
                    "WHERE m.job_id=a.job_id))) AS attempts,"
                    "EXISTS (SELECT 1 FROM llm_usage_daily_attempt_dimensions "
                    "WHERE local_day<%s) AS dimensions,"
                    "EXISTS (SELECT 1 FROM llm_usage_daily_call_memberships "
                    "WHERE local_day<%s) AS memberships,"
                    "EXISTS (SELECT 1 FROM llm_usage_rollup_dirty_days "
                    "WHERE rollup_name=%s AND local_day<%s) AS dirty_days",
                    tuple(
                        [cutoff_at]
                        + ([published_at] if published_at is not None else [])
                        + [cutoff_at]
                        + ([published_at] if published_at is not None else [])
                        + [
                            effective_cutoff,
                            effective_cutoff,
                            ROLLUP_NAME,
                            effective_cutoff,
                        ]
                    ),
                )
                remaining = cur.fetchone()
                complete = not any(bool(remaining[key]) for key in (
                    "attempts", "dimensions", "memberships", "dirty_days"
                ))
                retained_from = published
                if complete:
                    cur.execute(
                        "UPDATE llm_usage_rollup_watermarks SET retained_from=%s,"
                        "version=version+1,updated_at=%s WHERE rollup_name=%s "
                        "AND version=%s RETURNING retained_from",
                        (
                            effective_cutoff,
                            now_utc,
                            ROLLUP_NAME,
                            state["version"],
                        ),
                    )
                    updated = cur.fetchone()
                    if updated is None:
                        raise _CASConflict("attempt retention watermark changed")
                    retained_from = updated["retained_from"]
                return {
                    "status": "ok",
                    "complete": complete,
                    "attempts_deleted": attempts_deleted,
                    "dimensions_deleted": deleted["dimensions"],
                    "memberships_deleted": deleted["memberships"],
                    "dirty_days_deleted": deleted["dirty_days"],
                    "retained_from": retained_from,
                }
    except _RetentionCancelled:
        return {**empty, "status": "cancelled"}
    except Exception as exc:  # noqa: BLE001 - optional telemetry stays fail-open
        error = type(exc).__name__[:120]
        log.warning("[attempt_rollup] retention failed: %s", error)
        return {**empty, "error": error}


def request_replay() -> int | None:
    """Fail-open operator seam: request a bounded full rebuild generation."""

    try:
        with db.get_pool().connection(timeout=DEFAULT_POOL_TIMEOUT_SECONDS) as conn:
            row = conn.execute(
                "UPDATE llm_usage_rollup_watermarks SET replay_generation=replay_generation+1,"
                "bootstrap_complete=false,completed_through_day=NULL,version=version+1,"
                "updated_at=now() WHERE rollup_name=%s RETURNING replay_generation",
                (ROLLUP_NAME,),
            ).fetchone()
            return None if row is None else int(row[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("[attempt_rollup] replay request failed: %s", type(exc).__name__)
        return None


def _record_error(error: BaseException, *, now_utc: datetime) -> None:
    try:
        with db.get_pool().connection(timeout=DEFAULT_POOL_TIMEOUT_SECONDS) as conn:
            conn.execute(
                "UPDATE llm_usage_rollup_watermarks SET updated_at=%s "
                "WHERE rollup_name=%s", (now_utc, ROLLUP_NAME),
            )
    except Exception:  # noqa: BLE001
        pass


def _tick_result(status: str, refreshed: list[str], state: dict | None = None, **extra) -> dict:
    result = {
        "status": status,
        "days_refreshed": len(refreshed),
        "refreshed_days": list(refreshed),
        **extra,
    }
    if state is not None:
        result.update(
            bootstrap_complete=bool(state["bootstrap_complete"]),
            replay_generation=int(state["replay_generation"]),
            completed_through_day=state["completed_through_day"],
            source_backlog=state.get(
                "_source_backlog",
                {
                    "attempt": False,
                    "correction": False,
                    "turn": False,
                    "rate_card": False,
                },
            ),
            source_lag_seconds=float(state.get("_source_lag_seconds", 0.0)),
        )
    return result


def run_maintenance_tick(
    *,
    max_days: int = DEFAULT_MAX_DAYS,
    max_changed_rows: int = DEFAULT_MAX_CHANGED_ROWS,
    max_dirty_days: int = DEFAULT_MAX_DIRTY_DAYS,
    max_stale_rows: int = DEFAULT_MAX_STALE_ROWS,
    max_retention_rows: int = DEFAULT_MAX_RETENTION_ROWS,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS,
    now_utc: datetime | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Run one bounded, single-leader maintenance tick; never raise."""

    if not enabled():
        return {"status": "disabled", "days_refreshed": 0}
    days_limit = _bounded_int(max_days, DEFAULT_MAX_DAYS, minimum=0, maximum=31)
    changed_limit = _bounded_int(
        max_changed_rows, DEFAULT_MAX_CHANGED_ROWS, minimum=4, maximum=100_000
    )
    dirty_limit = _bounded_int(
        max_dirty_days, DEFAULT_MAX_DIRTY_DAYS, minimum=1, maximum=2_000
    )
    stale_limit = _bounded_int(
        max_stale_rows, DEFAULT_MAX_STALE_ROWS, minimum=0, maximum=10_000
    )
    retention_limit = _bounded_int(
        max_retention_rows,
        DEFAULT_MAX_RETENTION_ROWS,
        minimum=0,
        maximum=10_000,
    )
    stale_after = _bounded_float(
        stale_after_seconds, DEFAULT_STALE_AFTER_SECONDS, minimum=1, maximum=86_400
    )
    timeout_ms = _bounded_timeout(statement_timeout_ms)
    pool_timeout = _bounded_float(
        pool_timeout_seconds, DEFAULT_POOL_TIMEOUT_SECONDS, minimum=0.05, maximum=5
    )
    now = now_utc or datetime.now(timezone.utc)
    refreshed: list[str] = []
    try:
        with db.get_pool().connection(timeout=pool_timeout) as conn:
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
                if _cancel_requested(cancel_event):
                    return _tick_result("cancelled", refreshed)
                conn.execute(
                    "INSERT INTO llm_usage_rollup_watermarks (rollup_name) VALUES (%s) "
                    "ON CONFLICT (rollup_name) DO NOTHING", (ROLLUP_NAME,)
                )
                stale_count = _reconcile_stale_started(
                    conn, max_rows=stale_limit, stale_after_seconds=stale_after,
                    timeout_ms=timeout_ms,
                )
                if _cancel_requested(cancel_event):
                    return _tick_result(
                        "cancelled", refreshed, stale_reconciled=stale_count
                    )
                state = _bootstrap_batch(
                    conn, max_dirty_days=dirty_limit, now_utc=now, timeout_ms=timeout_ms
                )
                if _cancel_requested(cancel_event):
                    return _tick_result(
                        "cancelled", refreshed, state, stale_reconciled=stale_count
                    )
                if state["bootstrap_complete"]:
                    state = _discover_changes(
                        conn, max_changed_rows=changed_limit,
                        max_dirty_days=dirty_limit, now_utc=now, timeout_ms=timeout_ms,
                    )
                if _cancel_requested(cancel_event):
                    return _tick_result(
                        "cancelled", refreshed, state, stale_reconciled=stale_count
                    )
                for _ in range(days_limit):
                    if _cancel_requested(cancel_event):
                        return _tick_result(
                            "cancelled", refreshed, state, stale_reconciled=stale_count
                        )
                    row = conn.execute(
                        "SELECT local_day,generation FROM llm_usage_rollup_dirty_days "
                        "WHERE rollup_name=%s AND ("
                        "(SELECT retained_from FROM llm_usage_rollup_watermarks "
                        "WHERE rollup_name=%s) IS NULL OR local_day>=("
                        "SELECT retained_from FROM llm_usage_rollup_watermarks "
                        "WHERE rollup_name=%s)) ORDER BY local_day LIMIT 1",
                        (ROLLUP_NAME, ROLLUP_NAME, ROLLUP_NAME),
                    ).fetchone()
                    if row is None:
                        break
                    outcome = recompute_local_day(
                        row[0], statement_timeout_ms=timeout_ms, refreshed_at=now,
                        expected_generation=int(row[1]),
                    )
                    if outcome.get("status") != "ok":
                        dirty_pending = bool(
                            conn.execute(
                                "SELECT EXISTS (SELECT 1 FROM llm_usage_rollup_dirty_days "
                                "WHERE rollup_name=%s)", (ROLLUP_NAME,),
                            ).fetchone()[0]
                        )
                        return _tick_result(
                            "error", refreshed, state, stale_reconciled=stale_count,
                            dirty_pending=dirty_pending,
                            error=outcome.get("error", "RollupBuildError"),
                        )
                    refreshed.append(row[0].isoformat())
                dirty_pending = bool(
                    conn.execute(
                        "SELECT EXISTS (SELECT 1 FROM llm_usage_rollup_dirty_days "
                        "WHERE rollup_name=%s AND ((SELECT retained_from "
                        "FROM llm_usage_rollup_watermarks WHERE rollup_name=%s) "
                        "IS NULL OR local_day>=(SELECT retained_from "
                        "FROM llm_usage_rollup_watermarks WHERE rollup_name=%s)))",
                        (ROLLUP_NAME, ROLLUP_NAME, ROLLUP_NAME),
                    ).fetchone()[0]
                )
                retention = None
                if not dirty_pending and retention_limit > 0:
                    cutoff = now.astimezone(LOCAL_TIMEZONE).date() - timedelta(
                        days=retention_days()
                    )
                    retention = _run_retention_batch(
                        conn,
                        cutoff=cutoff,
                        max_rows=retention_limit,
                        timeout_ms=timeout_ms,
                        now_utc=now,
                        cancel_event=cancel_event,
                    )
                    if retention["status"] == "cancelled":
                        return _tick_result(
                            "cancelled", refreshed, state,
                            stale_reconciled=stale_count,
                            dirty_pending=False,
                            retention=retention,
                        )
                    if retention["status"] != "ok":
                        return _tick_result(
                            "error", refreshed, state,
                            stale_reconciled=stale_count,
                            dirty_pending=False,
                            retention=retention,
                            error=retention.get("error", "RetentionError"),
                        )
                return _tick_result(
                    "ok", refreshed, state, stale_reconciled=stale_count,
                    dirty_pending=dirty_pending, retention=retention,
                )
            finally:
                try:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
                except Exception:  # noqa: BLE001 - discard uncertain session lock
                    conn.close()
    except Exception as exc:  # noqa: BLE001 - optional telemetry remains fail-open
        _record_error(exc, now_utc=now)
        log.warning("[attempt_rollup] maintenance tick failed: %s", type(exc).__name__)
        return {
            "status": "error", "days_refreshed": len(refreshed),
            "refreshed_days": refreshed, "error": type(exc).__name__,
        }
