"""Persist the V2 provider wake circuit; no timer-based recovery."""
from alembic import op

revision = "0113_v2_wake_circuit"
down_revision = "0112_user_logs_duration_sec"
branch_labels = None
depends_on = None

COLUMNS = (
    "provider_fail_streak INTEGER NOT NULL DEFAULT 0",
    "wake_circuit_opened_at TIMESTAMPTZ",
    "wake_circuit_reason TEXT NOT NULL DEFAULT ''",
    "wake_circuit_reset_at TIMESTAMPTZ",
)


def upgrade() -> None:
    # Release each metadata DDL lock before the next statement.
    with op.get_context().autocommit_block():
        for column in COLUMNS:
            op.execute("ALTER TABLE v2_wake_schedule ADD COLUMN IF NOT EXISTS " + column)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for column in reversed(COLUMNS):
            op.execute("ALTER TABLE v2_wake_schedule DROP COLUMN IF EXISTS " + column.split()[0])
