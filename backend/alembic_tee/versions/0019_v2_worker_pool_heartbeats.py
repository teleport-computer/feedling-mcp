"""Add Runtime V2 pool identity and state to the TEE primary.

Revision ID: 0019_v2_worker_pool_heartbeats
Revises: 0018_v2_wake_shadow_decisions
"""

from alembic import op


revision = "0019_v2_worker_pool_heartbeats"
down_revision = "0018_v2_wake_shadow_decisions"
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


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
