"""Runtime V2 adaptive-tail observability.

Revision ID: 0061_v2_adaptive_tail_metrics
"""

from alembic import op


revision = "0061_v2_adaptive_tail_metrics"
down_revision = "0060_v2_wake_failure_backoff"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE v2_turn_metrics
  ADD COLUMN IF NOT EXISTS effective_tail_turns INT,
  ADD COLUMN IF NOT EXISTS tail_fallback BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS prompt_frontier_exhaustion_count INT NOT NULL DEFAULT 0;

ALTER TABLE v2_turn_metrics
  DROP CONSTRAINT IF EXISTS ck_v2_turn_metrics_effective_tail_turns;
ALTER TABLE v2_turn_metrics
  ADD CONSTRAINT ck_v2_turn_metrics_effective_tail_turns
  CHECK (effective_tail_turns IS NULL OR effective_tail_turns >= 0);

ALTER TABLE v2_turn_metrics
  DROP CONSTRAINT IF EXISTS ck_v2_turn_metrics_frontier_exhaustion_count;
ALTER TABLE v2_turn_metrics
  ADD CONSTRAINT ck_v2_turn_metrics_frontier_exhaustion_count
  CHECK (prompt_frontier_exhaustion_count >= 0);

CREATE INDEX IF NOT EXISTS idx_v2_turn_metrics_tail_lane_created
  ON v2_turn_metrics (lane, created_at DESC)
  WHERE effective_tail_turns IS NOT NULL;
"""


_DOWN = """
DROP INDEX IF EXISTS idx_v2_turn_metrics_tail_lane_created;
ALTER TABLE v2_turn_metrics
  DROP CONSTRAINT IF EXISTS ck_v2_turn_metrics_frontier_exhaustion_count,
  DROP CONSTRAINT IF EXISTS ck_v2_turn_metrics_effective_tail_turns,
  DROP COLUMN IF EXISTS prompt_frontier_exhaustion_count,
  DROP COLUMN IF EXISTS tail_fallback,
  DROP COLUMN IF EXISTS effective_tail_turns;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
