"""TEE twin of RDS 0097: durable Runtime V2 job recovery events.

Revision ID: 0032_v2_job_recovery_events
Revises: 0031_merge_voice_primary
"""

from alembic import op


revision = "0032_v2_job_recovery_events"
down_revision = "0031_merge_voice_primary"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS v2_job_recovery_events (
    job_id            BIGINT NOT NULL,
    job_attempt_count INTEGER NOT NULL CHECK (job_attempt_count > 0),
    lane              TEXT NOT NULL,
    recovery          TEXT NOT NULL CHECK (recovery IN ('requeued', 'terminal')),
    reason            TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (job_id, job_attempt_count)
);
CREATE INDEX IF NOT EXISTS ix_v2_job_recovery_events_window
    ON v2_job_recovery_events (created_at DESC, recovery, lane);
"""


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0032_v2_job_recovery_events"]'::jsonb)::text,
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
