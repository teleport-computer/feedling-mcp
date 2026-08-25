"""TEE twin of RDS 0098: classified V1 outcomes in frozen lane cells.

Revision ID: 0034_v1_lane_outcome_counts
Revises: 0033_trace_events
"""

from alembic import op


revision = "0034_v1_lane_outcome_counts"
down_revision = "0033_trace_events"
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

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0034_v1_lane_outcome_counts"]'::jsonb)::text,
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
