"""Catch the TEE-primary schema up to the latest test runtime release.

Revision ID: 0014_test_runtime_catchup
Revises: 0013_primary_runtime_contracts
Create Date: 2026-08-04

The test branch added Hosted V2 usage rollups, delivery-report indexes, and a
database-backed plaintext Genesis single-flight claim.  A promoted TEE database
is ``DATABASE_URL`` and therefore needs the same tables and index contracts even
though the rollup rows are derived and can be rebuilt from ``v2_turn_metrics``.
"""

from alembic import op


revision = "0014_test_runtime_catchup"
down_revision = "0013_primary_runtime_contracts"
branch_labels = None
depends_on = None


_METRICS = (
    "turns", "model_calls", "retries", "failed_turns",
    "usage_reported_calls", "cache_reported_calls", "unknown_usage_calls",
    "prompt_tokens_sum", "prompt_tokens_known_count",
    "completion_tokens_sum", "completion_tokens_known_count",
    "cache_read_tokens_sum", "cache_read_tokens_known_count",
    "cache_write_tokens_sum", "cache_write_tokens_known_count",
    "cache_miss_tokens_sum", "cache_miss_tokens_known_count",
)
_PREFIXES = ("all", "metered", "unknown")


def _columns(*, latency: bool) -> str:
    columns = [
        f"{prefix}_{metric} BIGINT NOT NULL DEFAULT 0"
        for prefix in _PREFIXES
        for metric in _METRICS
    ]
    if latency:
        columns.extend(
            f"{prefix}_latency_samples INTEGER[] NOT NULL DEFAULT '{{}}'::INTEGER[]"
            for prefix in _PREFIXES
        )
    return ",\n  ".join(columns)


def _nonnegative(*, latency: bool) -> str:
    checks = [
        f"{prefix}_{metric} >= 0"
        for prefix in _PREFIXES
        for metric in _METRICS
    ]
    if latency:
        checks.extend(
            f"array_position({prefix}_latency_samples, NULL) IS NULL "
            f"AND 0 <= ALL ({prefix}_latency_samples)"
            for prefix in _PREFIXES
        )
    return " AND\n    ".join(checks)


def _bounds(*, latency: bool) -> str:
    checks = []
    for prefix in _PREFIXES:
        checks.extend((
            f"{prefix}_failed_turns <= {prefix}_turns",
            f"{prefix}_prompt_tokens_known_count <= {prefix}_turns",
            f"{prefix}_completion_tokens_known_count <= {prefix}_turns",
            f"{prefix}_cache_read_tokens_known_count <= {prefix}_turns",
            f"{prefix}_cache_write_tokens_known_count <= {prefix}_turns",
            f"{prefix}_cache_miss_tokens_known_count <= {prefix}_turns",
            f"{prefix}_usage_reported_calls <= {prefix}_model_calls",
            f"{prefix}_cache_reported_calls <= {prefix}_model_calls",
            f"{prefix}_unknown_usage_calls <= {prefix}_model_calls",
        ))
        if latency:
            checks.append(f"cardinality({prefix}_latency_samples) <= {prefix}_turns")
    checks.extend(("metered_turns <= all_turns", "unknown_turns <= all_turns"))
    return " AND\n    ".join(checks)


_UP = f"""
CREATE TABLE IF NOT EXISTS v2_usage_daily_users (
  id BIGSERIAL PRIMARY KEY,
  local_day DATE NOT NULL,
  user_id TEXT REFERENCES users(user_id) ON DELETE CASCADE,
  {_columns(latency=False)},
  first_metric_at TIMESTAMPTZ,
  last_metric_at TIMESTAMPTZ,
  last_model_call_at TIMESTAMPTZ,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ux_v2_usage_daily_users_grain
    UNIQUE NULLS NOT DISTINCT (local_day, user_id),
  CONSTRAINT ck_v2_usage_daily_users_nonnegative CHECK (
    {_nonnegative(latency=False)}
  ),
  CONSTRAINT ck_v2_usage_daily_users_bounds CHECK (
    {_bounds(latency=False)}
  )
);

CREATE TABLE IF NOT EXISTS v2_usage_daily_dimensions (
  id BIGSERIAL PRIMARY KEY,
  local_day DATE NOT NULL,
  user_id TEXT REFERENCES users(user_id) ON DELETE CASCADE,
  lane TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  {_columns(latency=True)},
  first_metric_at TIMESTAMPTZ,
  last_metric_at TIMESTAMPTZ,
  last_model_call_at TIMESTAMPTZ,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ux_v2_usage_daily_dimensions_grain
    UNIQUE NULLS NOT DISTINCT (local_day, user_id, lane, provider, model),
  CONSTRAINT ck_v2_usage_daily_dimensions_identity CHECK (
    lane <> '' AND provider <> '' AND model <> ''
  ),
  CONSTRAINT ck_v2_usage_daily_dimensions_nonnegative CHECK (
    {_nonnegative(latency=True)}
  ),
  CONSTRAINT ck_v2_usage_daily_dimensions_bounds CHECK (
    {_bounds(latency=True)}
  )
);

CREATE INDEX IF NOT EXISTS ix_v2_usage_daily_users_user_id
  ON v2_usage_daily_users (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_v2_usage_daily_dimensions_user_id
  ON v2_usage_daily_dimensions (user_id) WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS v2_usage_rollup_watermarks (
  rollup_name TEXT PRIMARY KEY,
  bootstrap_complete BOOLEAN NOT NULL DEFAULT false,
  bootstrap_started_at TIMESTAMPTZ,
  bootstrap_completed_at TIMESTAMPTZ,
  source_updated_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch'::timestamptz,
  source_id BIGINT NOT NULL DEFAULT 0,
  source_observed_updated_at TIMESTAMPTZ,
  source_lag_seconds DOUBLE PRECISION,
  dirty_from_day DATE,
  dirty_through_day DATE,
  refreshed_at TIMESTAMPTZ,
  last_success_at TIMESTAMPTZ,
  last_error_at TIMESTAMPTZ,
  last_error TEXT,
  version BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_v2_usage_rollup_watermarks_cursor CHECK (source_id >= 0),
  CONSTRAINT ck_v2_usage_rollup_watermarks_lag CHECK (
    source_lag_seconds IS NULL OR source_lag_seconds >= 0
  ),
  CONSTRAINT ck_v2_usage_rollup_watermarks_dirty_range CHECK (
    (dirty_from_day IS NULL AND dirty_through_day IS NULL)
    OR (dirty_from_day IS NOT NULL AND dirty_through_day IS NOT NULL
        AND dirty_from_day <= dirty_through_day)
  ),
  CONSTRAINT ck_v2_usage_rollup_watermarks_version CHECK (version >= 0)
);

CREATE INDEX IF NOT EXISTS ix_v2_effect_report_created_at
  ON v2_effect_outbox (created_at DESC, user_id) INCLUDE (status, effect_type);
CREATE INDEX IF NOT EXISTS ix_v2_effect_report_unfinished
  ON v2_effect_outbox (user_id, status, created_at) INCLUDE (effect_type)
  WHERE status IN ('pending', 'pending_fenced_v1', 'needs_reconciliation');
CREATE INDEX IF NOT EXISTS ix_v2_terminal_failure_report_created_at
  ON v2_terminal_failure_outbox (created_at DESC, user_id)
  INCLUDE (reply_delivered_at, status_delivered_at, runtime_error_delivered_at);
CREATE INDEX IF NOT EXISTS ix_v2_terminal_failure_report_unfinished
  ON v2_terminal_failure_outbox (user_id, created_at)
  INCLUDE (reply_delivered_at, status_delivered_at, runtime_error_delivered_at)
  WHERE reply_delivered_at IS NULL OR status_delivered_at IS NULL
     OR runtime_error_delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_v2_turn_metrics_updated_id
  ON v2_turn_metrics (updated_at, id) INCLUDE (created_at);

WITH ranked AS (
  SELECT user_id, job_id,
         ROW_NUMBER() OVER (
           PARTITION BY user_id ORDER BY updated_at DESC, job_id DESC
         ) AS rn
  FROM genesis_import_jobs
  WHERE status = 'processing' AND metadata->>'ingest' = 'plaintext'
)
UPDATE genesis_import_jobs AS jobs
SET status = 'failed',
    error = 'superseded_by_migration_0014_plaintext_exclusivity',
    updated_at = now()
FROM ranked
WHERE jobs.user_id = ranked.user_id
  AND jobs.job_id = ranked.job_id
  AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS genesis_jobs_plaintext_active_uidx
  ON genesis_import_jobs (user_id)
  WHERE status = 'processing' AND metadata->>'ingest' = 'plaintext';

UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{{tee_heads}}',
            '["0014_test_runtime_catchup"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    raise NotImplementedError("alembic_tee downgrade is not supported; restore from backup")
