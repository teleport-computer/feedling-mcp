"""Add the Runtime V2 heartbeat follow-up merge marker.

Revision ID: 0038_v2_wake_followup_marker
Revises: 0037_chat_poll_index

The RDS and TEE migration literals are intentionally byte-identical. Runtime
V2 can still run with ``DATABASE_URL`` on either schema during promotion and
rollback, so the completion marker must exist on both sides.
"""

from alembic import op


revision = "0038_v2_wake_followup_marker"
down_revision = "0037_chat_poll_index"
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


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0038_v2_wake_followup_marker"]'::jsonb)::text,
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
