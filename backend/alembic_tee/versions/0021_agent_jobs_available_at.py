"""Add the durable Runtime V2 job claim fence to the TEE primary.

Revision ID: 0021_agent_jobs_available_at
Revises: 0020_v2_first_chat_activation
"""

from alembic import op


revision = "0021_agent_jobs_available_at"
down_revision = "0020_v2_first_chat_activation"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE agent_jobs
  ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS ix_agent_jobs_pending_available_at
  ON agent_jobs (available_at, priority DESC, created_at)
  WHERE status='pending';
"""

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0021_agent_jobs_available_at"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
