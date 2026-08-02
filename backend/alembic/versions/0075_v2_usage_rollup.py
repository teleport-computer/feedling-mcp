"""Deletion-safe daily rollups for the Hosted V2 usage report.

Revision ID: 0075_v2_usage_rollup
Revises: 0074_runtime_user_delivery_idx
"""

from alembic import op


revision = "0075_v2_usage_rollup"
down_revision = "0074_runtime_user_delivery_idx"
branch_labels = None
depends_on = None


_SUBAGGREGATE_METRICS = (
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
_PREFIXES = ("all", "metered", "unknown")


def _subaggregate_columns(*, latency: bool) -> str:
    columns = [
        f"{prefix}_{metric} BIGINT NOT NULL DEFAULT 0"
        for prefix in _PREFIXES
        for metric in _SUBAGGREGATE_METRICS
    ]
    if latency:
        columns.extend(
            f"{prefix}_latency_samples INTEGER[] NOT NULL DEFAULT '{{}}'::INTEGER[]"
            for prefix in _PREFIXES
        )
    return ",\n  ".join(columns)


def _nonnegative_check(*, latency: bool) -> str:
    checks = [
        f"{prefix}_{metric} >= 0"
        for prefix in _PREFIXES
        for metric in _SUBAGGREGATE_METRICS
    ]
    if latency:
        checks.extend(
            f"array_position({prefix}_latency_samples, NULL) IS NULL "
            f"AND 0 <= ALL ({prefix}_latency_samples)"
            for prefix in _PREFIXES
        )
    return " AND\n    ".join(checks)


def _bounds_check(*, latency: bool) -> str:
    checks = []
    for prefix in _PREFIXES:
        checks.extend(
            (
                f"{prefix}_failed_turns <= {prefix}_turns",
                f"{prefix}_prompt_tokens_known_count <= {prefix}_turns",
                f"{prefix}_completion_tokens_known_count <= {prefix}_turns",
                f"{prefix}_cache_read_tokens_known_count <= {prefix}_turns",
                f"{prefix}_cache_write_tokens_known_count <= {prefix}_turns",
                f"{prefix}_cache_miss_tokens_known_count <= {prefix}_turns",
                f"{prefix}_usage_reported_calls <= {prefix}_model_calls",
                f"{prefix}_unknown_usage_calls <= {prefix}_model_calls",
            )
        )
        if latency:
            checks.append(
                f"cardinality({prefix}_latency_samples) <= {prefix}_turns"
            )
    checks.extend(
        (
            "metered_turns <= all_turns",
            "unknown_turns <= all_turns",
        )
    )
    return " AND\n    ".join(checks)


_SCHEMA_UP = f"""
CREATE TABLE IF NOT EXISTS v2_usage_daily_users (
  id BIGSERIAL PRIMARY KEY,
  local_day DATE NOT NULL,
  user_id TEXT REFERENCES users(user_id) ON DELETE CASCADE,
  {_subaggregate_columns(latency=False)},
  first_metric_at TIMESTAMPTZ,
  last_metric_at TIMESTAMPTZ,
  last_model_call_at TIMESTAMPTZ,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ux_v2_usage_daily_users_grain
    UNIQUE NULLS NOT DISTINCT (local_day, user_id),
  CONSTRAINT ck_v2_usage_daily_users_nonnegative CHECK (
    {_nonnegative_check(latency=False)}
  ),
  CONSTRAINT ck_v2_usage_daily_users_bounds CHECK (
    {_bounds_check(latency=False)}
  )
);

CREATE TABLE IF NOT EXISTS v2_usage_daily_dimensions (
  id BIGSERIAL PRIMARY KEY,
  local_day DATE NOT NULL,
  user_id TEXT REFERENCES users(user_id) ON DELETE CASCADE,
  lane TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  {_subaggregate_columns(latency=True)},
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
    {_nonnegative_check(latency=True)}
  ),
  CONSTRAINT ck_v2_usage_daily_dimensions_bounds CHECK (
    {_bounds_check(latency=True)}
  )
);

-- The grain indexes lead with local_day.  These child-key indexes keep
-- account deletion from scanning the whole derived history for one user.
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
    dirty_from_day IS NULL OR dirty_through_day IS NULL
    OR dirty_from_day <= dirty_through_day
  ),
  CONSTRAINT ck_v2_usage_rollup_watermarks_version CHECK (version >= 0)
);
"""


_SOURCE_CURSOR_INDEX = (
    "CREATE INDEX CONCURRENTLY ix_v2_turn_metrics_updated_id "
    "ON v2_turn_metrics (updated_at, id) INCLUDE (created_at)"
)


def _source_index_validity() -> bool | None:
    row = op.get_bind().exec_driver_sql(
        "SELECT idx.indisvalid FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        "WHERE cls.relkind='i' "
        "AND cls.relname='ix_v2_turn_metrics_updated_id' "
        "AND pg_table_is_visible(cls.oid)"
    ).fetchone()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    # Intentionally schema-only: the worker bootstraps/rebuilds from
    # v2_turn_metrics in bounded transactions after deployment.
    op.execute(_SCHEMA_UP)
    validity = _source_index_validity()
    with op.get_context().autocommit_block():
        if validity is False:
            op.execute(
                "DROP INDEX CONCURRENTLY IF EXISTS ix_v2_turn_metrics_updated_id"
            )
        if validity is not True:
            op.execute(_SOURCE_CURSOR_INDEX)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_v2_turn_metrics_updated_id"
        )
    op.execute("DROP TABLE IF EXISTS v2_usage_daily_dimensions")
    op.execute("DROP TABLE IF EXISTS v2_usage_daily_users")
    op.execute("DROP TABLE IF EXISTS v2_usage_rollup_watermarks")
