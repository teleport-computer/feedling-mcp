"""Add explicit runtime pool identity and bounded worker state.

Revision ID: 0086_v2_worker_pool_heartbeats
Revises: 0085_v2_wake_shadow_decisions
"""

from alembic import op


revision = "0086_v2_worker_pool_heartbeats"
down_revision = "0085_v2_wake_shadow_decisions"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE v2_worker_heartbeats
  ADD COLUMN IF NOT EXISTS pool TEXT NOT NULL DEFAULT 'unassigned';
ALTER TABLE v2_worker_heartbeats
  ADD COLUMN IF NOT EXISTS runtime_state JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS ix_v2_worker_heartbeats_pool_kind_beat
  ON v2_worker_heartbeats (pool, kind, beat_at DESC);
"""

_DOWN = """
DROP INDEX IF EXISTS ix_v2_worker_heartbeats_pool_kind_beat;
ALTER TABLE v2_worker_heartbeats DROP COLUMN IF EXISTS runtime_state;
ALTER TABLE v2_worker_heartbeats DROP COLUMN IF EXISTS pool;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
