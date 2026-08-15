"""Add a durable claim fence for delayed Runtime V2 jobs.

Revision ID: 0088_agent_jobs_available_at
Revises: 0087_v2_first_chat_activation
"""

from alembic import op


revision = "0088_agent_jobs_available_at"
down_revision = "0087_v2_first_chat_activation"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE agent_jobs
  ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS ix_agent_jobs_pending_available_at
  ON agent_jobs (available_at, priority DESC, created_at)
  WHERE status='pending';
"""

_DOWN = """
DROP INDEX IF EXISTS ix_agent_jobs_pending_available_at;
ALTER TABLE agent_jobs DROP COLUMN IF EXISTS available_at;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
