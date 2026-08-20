"""Persist auditable Runtime V2 wake outcomes in the TEE primary.

Revision ID: 0022_v2_wake_outcomes
Revises: 0021_agent_jobs_available_at
"""

from alembic import op


revision = "0022_v2_wake_outcomes"
down_revision = "0021_agent_jobs_available_at"
branch_labels = None
depends_on = None


_UP = (
    "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS wake_result TEXT, "
    "ADD COLUMN IF NOT EXISTS wake_result_reason TEXT"
)

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0022_v2_wake_outcomes"]'::jsonb)::text,
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
