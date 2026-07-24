"""Durable Runtime V2 Capture batches.

Revision ID: 0051_v2_capture_batches
Revises: 0050_v2_trajectory_access_audit
"""

from alembic import op


revision = "0051_v2_capture_batches"
down_revision = "0050_v2_trajectory_access_audit"
branch_labels = None
depends_on = None


_UP = r"""
CREATE TABLE IF NOT EXISTS v2_capture_batches (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  runtime_generation BIGINT NOT NULL,
  after_seq BIGINT NOT NULL,
  through_seq BIGINT NOT NULL,
  after_message_id TEXT NOT NULL DEFAULT '',
  until_message_id TEXT NOT NULL,
  until_ts DOUBLE PRECISION NOT NULL DEFAULT 0,
  actions_json JSONB NOT NULL,
  action_count INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'prepared',
  prepared_by_job_id BIGINT REFERENCES agent_jobs(id) ON DELETE SET NULL,
  applied_by_job_id BIGINT REFERENCES agent_jobs(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  applied_at TIMESTAMPTZ,
  CONSTRAINT ux_v2_capture_batch_frontier
    UNIQUE (user_id, runtime_generation, after_seq),
  CONSTRAINT ck_v2_capture_batch_seq
    CHECK (after_seq >= 0 AND through_seq > after_seq),
  CONSTRAINT ck_v2_capture_batch_count
    CHECK (action_count BETWEEN 0 AND 20),
  CONSTRAINT ck_v2_capture_batch_actions
    CHECK (jsonb_typeof(actions_json) = 'array'
           AND jsonb_array_length(actions_json) = action_count),
  CONSTRAINT ck_v2_capture_batch_status
    CHECK (status IN ('prepared','applied')),
  CONSTRAINT ck_v2_capture_batch_applied_shape CHECK (
    (status='prepared' AND applied_by_job_id IS NULL AND applied_at IS NULL)
    OR (status='applied' AND applied_by_job_id IS NOT NULL AND applied_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS ix_v2_capture_batches_pending
  ON v2_capture_batches (user_id, runtime_generation, after_seq)
  WHERE status='prepared';
"""


_DOWN = "DROP TABLE IF EXISTS v2_capture_batches;"


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
