"""TEE twin of RDS 0096: cross-process trace ruler health.

Revision ID: 0028_trace_write_stats_health
Revises: 0027_trace_write_stats
"""

from alembic import op


revision = "0028_trace_write_stats_health"
down_revision = "0027_trace_write_stats"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS trace_write_stats_health (
    writer_id                TEXT PRIMARY KEY,
    process_started_at       TIMESTAMPTZ NOT NULL,
    last_success_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_failure_at          TIMESTAMPTZ,
    failures_total           BIGINT NOT NULL DEFAULT 0,
    max_consecutive_failures BIGINT NOT NULL DEFAULT 0,
    dirty_rows               BIGINT NOT NULL DEFAULT 0,
    stopped_at               TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT trace_write_stats_health_nonneg
        CHECK (
            failures_total >= 0
            AND max_consecutive_failures >= 0
            AND dirty_rows >= 0
        )
);
CREATE INDEX IF NOT EXISTS ix_trace_write_stats_health_last_success
    ON trace_write_stats_health (last_success_at DESC);
"""


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0028_trace_write_stats_health"]'::jsonb)::text,
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
