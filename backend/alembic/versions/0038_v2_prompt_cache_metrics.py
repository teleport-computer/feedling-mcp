"""Add provider prompt-cache telemetry and widen token counters.

Revision ID: 0038_v2_prompt_cache_metrics
Revises: 0037_v2_terminal_failure_outbox
"""

from alembic import op


revision = "0038_v2_prompt_cache_metrics"
down_revision = "0037_v2_terminal_failure_outbox"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE v2_turn_metrics
  ALTER COLUMN prompt_tokens TYPE BIGINT USING prompt_tokens::BIGINT,
  ALTER COLUMN completion_tokens TYPE BIGINT USING completion_tokens::BIGINT,
  ADD COLUMN IF NOT EXISTS provider TEXT,
  ADD COLUMN IF NOT EXISTS model TEXT,
  ADD COLUMN IF NOT EXISTS cache_route_fingerprint TEXT,
  ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT,
  ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT,
  ADD COLUMN IF NOT EXISTS cache_miss_tokens BIGINT,
  ADD COLUMN IF NOT EXISTS usage_reported_calls INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cache_reported_calls INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_v2_turn_metrics_lane_created_at
  ON v2_turn_metrics (lane, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_v2_turn_metrics_cache_proof
  ON v2_turn_metrics
  (lane, provider, model, cache_route_fingerprint, user_id, created_at DESC);
"""


_DOWN = """
DROP INDEX IF EXISTS ix_v2_turn_metrics_cache_proof;
DROP INDEX IF EXISTS ix_v2_turn_metrics_lane_created_at;
ALTER TABLE v2_turn_metrics
  DROP COLUMN IF EXISTS cache_reported_calls,
  DROP COLUMN IF EXISTS usage_reported_calls,
  DROP COLUMN IF EXISTS cache_miss_tokens,
  DROP COLUMN IF EXISTS cache_write_tokens,
  DROP COLUMN IF EXISTS cache_read_tokens,
  DROP COLUMN IF EXISTS cache_route_fingerprint,
  DROP COLUMN IF EXISTS model,
  DROP COLUMN IF EXISTS provider,
  ALTER COLUMN prompt_tokens TYPE INTEGER USING (
    CASE WHEN prompt_tokens BETWEEN 0 AND 2147483647
      THEN prompt_tokens::INTEGER ELSE NULL END
  ),
  ALTER COLUMN completion_tokens TYPE INTEGER USING (
    CASE WHEN completion_tokens BETWEEN 0 AND 2147483647
      THEN completion_tokens::INTEGER ELSE NULL END
  );
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
