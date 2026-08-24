"""Preserve classified V1 terminal outcomes in frozen lane cells.

Revision ID: 0098_v1_lane_outcome_counts
Revises: 0097_v2_job_recovery_events

Existing resident cells predate these counters and cannot be reconstructed
after the per-user log ring has moved on.  ``outcomes_from`` therefore marks
the first day written by the new freezer; readers must keep older or partially
covered windows unavailable instead of interpreting the column defaults as
measured zeroes.
"""

from alembic import op


revision = "0098_v1_lane_outcome_counts"
down_revision = "0097_v2_job_recovery_events"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE lane_daily_rollup
    ADD COLUMN IF NOT EXISTS operational_failures INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS control_outcomes     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS user_unavailable     INTEGER NOT NULL DEFAULT 0;

ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_outcomes_nonneg;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_outcomes_nonneg
    CHECK (
        operational_failures >= 0
        AND control_outcomes >= 0
        AND user_unavailable >= 0
        AND operational_failures + control_outcomes + user_unavailable
            <= failed
    );

ALTER TABLE lane_rollup_watermark
    ADD COLUMN IF NOT EXISTS outcomes_from TEXT;
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_outcomes_format;
ALTER TABLE lane_rollup_watermark
    ADD CONSTRAINT lane_rollup_watermark_outcomes_format
    CHECK (outcomes_from IS NULL OR outcomes_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("""
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_outcomes_format;
ALTER TABLE lane_rollup_watermark DROP COLUMN IF EXISTS outcomes_from;
ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_outcomes_nonneg;
ALTER TABLE lane_daily_rollup
    DROP COLUMN IF EXISTS user_unavailable,
    DROP COLUMN IF EXISTS control_outcomes,
    DROP COLUMN IF EXISTS operational_failures;
""")
