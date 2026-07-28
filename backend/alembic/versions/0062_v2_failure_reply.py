"""Add durable V2 terminal-failure reply delivery and chat history indexes.

Revision ID: 0062_v2_failure_reply
Revises: 0061_v2_adaptive_tail_metrics
"""

from alembic import op


revision = "0062_v2_failure_reply"
down_revision = "0061_v2_adaptive_tail_metrics"
branch_labels = None
depends_on = None


_UP = r"""
ALTER TABLE v2_terminal_failure_outbox
  ADD COLUMN IF NOT EXISTS error_class TEXT NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS reply_frontier_seq BIGINT,
  ADD COLUMN IF NOT EXISTS reply_parent_message_id TEXT,
  ADD COLUMN IF NOT EXISTS reply_delivered_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS reply_attempt_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS reply_last_attempt_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS reply_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Existing failures already emitted their legacy status/settings surfaces.
-- Do not replay a new chat bubble days later when this migration lands.
UPDATE v2_terminal_failure_outbox
SET reply_delivered_at = COALESCE(reply_delivered_at, now())
WHERE reply_delivered_at IS NULL;

ALTER TABLE v2_terminal_failure_outbox
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_error_class_check,
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_reply_frontier_check,
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_reply_attempt_check,
  ADD CONSTRAINT v2_terminal_failure_error_class_check
    CHECK (error_class <> '' AND length(error_class) <= 64),
  ADD CONSTRAINT v2_terminal_failure_reply_frontier_check
    CHECK (reply_frontier_seq IS NULL OR reply_frontier_seq >= 0),
  ADD CONSTRAINT v2_terminal_failure_reply_attempt_check
    CHECK (reply_attempt_count >= 0);

CREATE INDEX IF NOT EXISTS v2_terminal_failure_reply_pending_idx
  ON v2_terminal_failure_outbox
     (reply_next_attempt_at, reply_last_attempt_at, created_at, job_id)
  WHERE reply_delivered_at IS NULL;

-- Fleet metrics and support detail both read terminal chat history. Before
-- this migration each endpoint scan walked the append-only agent_jobs table.
CREATE INDEX IF NOT EXISTS ix_agent_jobs_chat_terminal_finished
  ON agent_jobs (finished_at DESC, id DESC)
  WHERE lane = 'chat'
    AND status IN ('completed', 'failed', 'expired', 'superseded');

CREATE INDEX IF NOT EXISTS ix_agent_jobs_user_chat_failure_finished
  ON agent_jobs (user_id, finished_at DESC, id DESC)
  WHERE lane = 'chat' AND status IN ('failed', 'expired');
"""


_DOWN = r"""
DROP INDEX IF EXISTS ix_agent_jobs_user_chat_failure_finished;
DROP INDEX IF EXISTS ix_agent_jobs_chat_terminal_finished;
DROP INDEX IF EXISTS v2_terminal_failure_reply_pending_idx;

ALTER TABLE v2_terminal_failure_outbox
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_reply_attempt_check,
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_reply_frontier_check,
  DROP CONSTRAINT IF EXISTS v2_terminal_failure_error_class_check,
  DROP COLUMN IF EXISTS reply_next_attempt_at,
  DROP COLUMN IF EXISTS reply_last_attempt_at,
  DROP COLUMN IF EXISTS reply_attempt_count,
  DROP COLUMN IF EXISTS reply_delivered_at,
  DROP COLUMN IF EXISTS reply_parent_message_id,
  DROP COLUMN IF EXISTS reply_frontier_seq,
  DROP COLUMN IF EXISTS error_class;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
