"""Add the deferred Runtime V2 heartbeat follow-up marker.

Revision ID: 0103_v2_wake_followup_marker
Revises: 0102_trace_events

The RDS and TEE migration literals are intentionally byte-identical. Runtime
V2 can still run with ``DATABASE_URL`` on either schema during promotion and
rollback, so the scheduler marker must exist on both sides.
"""

from alembic import op


revision = "0103_v2_wake_followup_marker"
down_revision = "0102_trace_events"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE v2_wake_schedule
    ADD COLUMN IF NOT EXISTS pending_followup_generation BIGINT,
    ADD COLUMN IF NOT EXISTS pending_followup_source_job_id BIGINT,
    ADD COLUMN IF NOT EXISTS pending_followup_consumed_context_seq BIGINT;

ALTER TABLE v2_wake_schedule
    DROP CONSTRAINT IF EXISTS ck_v2_wake_schedule_followup_marker_shape;
ALTER TABLE v2_wake_schedule
    ADD CONSTRAINT ck_v2_wake_schedule_followup_marker_shape CHECK (
        (pending_followup_generation IS NULL
         AND pending_followup_source_job_id IS NULL
         AND pending_followup_consumed_context_seq IS NULL)
        OR
        (pending_followup_generation > 0
         AND pending_followup_source_job_id > 0
         AND pending_followup_consumed_context_seq >= 0)
    );
"""


_DOWN = """
ALTER TABLE v2_wake_schedule
    DROP CONSTRAINT IF EXISTS ck_v2_wake_schedule_followup_marker_shape,
    DROP COLUMN IF EXISTS pending_followup_consumed_context_seq,
    DROP COLUMN IF EXISTS pending_followup_source_job_id,
    DROP COLUMN IF EXISTS pending_followup_generation;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
