"""Fail-open, atomic daily rollups for canonical provider attempts.

This module is maintenance-only.  It is not imported from provider dispatch,
reply, retry/failover, heartbeat, or job-state paths.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

import db


log = logging.getLogger("feedling.runtime_v2.provider_attempt_rollup")

ROLLUP_NAME = "hosted_v2_attempt_usage"
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000
DEFAULT_POOL_TIMEOUT_SECONDS = 0.5

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_miss_tokens",
)


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


def _effective_attempt_ctes() -> str:
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
  WHERE m.created_at >= %s AND m.created_at < %s
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


def _insert_dimensions(cur, local_day: date, start_at: datetime, end_at: datetime,
                       refreshed_at: datetime) -> int:
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
    cur.execute(
        _effective_attempt_ctes()
        + f"""
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
""",
        (start_at, end_at, local_day, refreshed_at),
    )
    return cur.rowcount


def _insert_memberships(cur, local_day: date, start_at: datetime, end_at: datetime,
                        refreshed_at: datetime) -> int:
    cur.execute(
        _effective_attempt_ctes()
        + """,
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
)
INSERT INTO llm_usage_daily_call_memberships (
  local_day,user_id,cohort_lane,call_id,requested_provider,requested_model,
  resolved_provider,resolved_model,effective_usage_known,
  missing_outer_ordinals,missing_inner_ordinals,refreshed_at
)
SELECT DISTINCT %s,p.user_id,p.cohort_lane,p.call_id,p.requested_provider,
  p.requested_model,p.resolved_provider,p.resolved_model,p.effective_usage_known,
  coalesce(g.missing_outer_ordinals,0),coalesce(g.missing_inner_ordinals,0),%s
FROM priced p LEFT JOIN call_gaps g USING (call_id)
""",
        (start_at, end_at, local_day, refreshed_at),
    )
    return cur.rowcount


def _recompute_on_cursor(cur, local_day: date, *, refreshed_at: datetime) -> dict:
    start_at, end_at = _local_day_bounds(local_day)
    cur.execute(
        "DELETE FROM llm_usage_daily_attempt_dimensions WHERE local_day=%s",
        (local_day,),
    )
    cur.execute(
        "DELETE FROM llm_usage_daily_call_memberships WHERE local_day=%s",
        (local_day,),
    )
    dimensions = _insert_dimensions(cur, local_day, start_at, end_at, refreshed_at)
    memberships = _insert_memberships(cur, local_day, start_at, end_at, refreshed_at)
    cur.execute(
        "DELETE FROM llm_usage_rollup_dirty_days "
        "WHERE rollup_name=%s AND local_day=%s",
        (ROLLUP_NAME, local_day),
    )
    return {
        "status": "ok",
        "dimensions": dimensions,
        "memberships": memberships,
    }


def recompute_local_day(
    local_day: date,
    *,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    refreshed_at: datetime | None = None,
) -> dict:
    """Atomically replace both attempt rollups; return safely on any failure."""

    empty = {"status": "error", "dimensions": 0, "memberships": 0}
    try:
        if not isinstance(local_day, date) or isinstance(local_day, datetime):
            raise TypeError("local_day must be a date")
        refreshed = refreshed_at or datetime.now(timezone.utc)
        if refreshed.tzinfo is None:
            raise TypeError("refreshed_at must be timezone-aware")
        timeout_ms = _bounded_timeout(statement_timeout_ms)
        with db.get_pool().connection(timeout=DEFAULT_POOL_TIMEOUT_SECONDS) as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    _set_transaction(cur, timeout_ms)
                    return _recompute_on_cursor(
                        cur, local_day, refreshed_at=refreshed
                    )
    except Exception as exc:  # noqa: BLE001 - optional accounting stays fail-open
        error = type(exc).__name__[:120]
        log.warning("[attempt_rollup] local day rebuild failed: %s", error)
        return {**empty, "error": error}
