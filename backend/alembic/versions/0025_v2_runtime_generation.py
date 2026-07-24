"""Per-user runtime generation + cutover state (Hosted Runtime V2 PR A / spec A2).

The monotonic fence value behind the generation-fenced effect outbox. One row per
user; state advances resident->draining->v2 (and back) and each transition bumps
generation by exactly 1. A real BIGINT column (not JSONB) so the bump is an atomic
UPDATE and the outbox fence can join it cheaply.

Revision ID: 0025_v2_runtime_generation
"""
from alembic import op

revision = "0025_v2_runtime_generation"
down_revision = "0024_v2_worker_capacity"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS v2_runtime_state (
  user_id            TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  hosted_runtime_state TEXT NOT NULL DEFAULT 'resident',
  runtime_generation BIGINT NOT NULL DEFAULT 1,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = "DROP TABLE IF EXISTS v2_runtime_state;"


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
