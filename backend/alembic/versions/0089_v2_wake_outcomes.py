"""Persist auditable Runtime V2 wake outcomes."""

from alembic import op

revision = "0089_v2_wake_outcomes"
down_revision = "0088_agent_jobs_available_at"
branch_labels = None
depends_on = None

_UP = (
    "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS wake_result TEXT, "
    "ADD COLUMN IF NOT EXISTS wake_result_reason TEXT"
)

_DOWN = (
    "ALTER TABLE agent_jobs DROP COLUMN IF EXISTS wake_result_reason, "
    "DROP COLUMN IF EXISTS wake_result"
)


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
