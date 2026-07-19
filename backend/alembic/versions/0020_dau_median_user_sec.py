"""Add median per-user daily foreground seconds to the DAU snapshot.

Revision ID: 0020_dau_median_user_sec
Revises: 0019_tee_reconcile_state
Create Date: 2026-07-19

``foreground_sec / session_dau`` is the MEAN daily foreground time per active
user, which a few heavy users skew high. ``median_user_sec`` is the median of
each user's daily foreground total -- the "typical user" counterpart. The frozen
snapshot only stores aggregates (not the per-user distribution), so days frozen
before this column shipped keep the default 0 and the admin view shows them as
"-"; live days and days frozen afterwards carry the real value.
"""

from alembic import op


revision = "0020_dau_median_user_sec"
down_revision = "0019_tee_reconcile_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE dau_daily_snapshot "
        "ADD COLUMN IF NOT EXISTS median_user_sec DOUBLE PRECISION NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE dau_daily_snapshot DROP COLUMN IF EXISTS median_user_sec")
