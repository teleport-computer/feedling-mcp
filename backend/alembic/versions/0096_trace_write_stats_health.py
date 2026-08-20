"""Cross-process health for T138's persistent trace-rate ruler.

Revision ID: 0096_trace_write_stats_health
Revises: 0095_trace_write_stats
Create Date: 2026-08-20

Failure cannot reliably publish "I failed" through the same database channel
that is failing.  Each active writer therefore publishes a success heartbeat;
readers detect failure when that positive signal expires.  A graceful exit is
tombstoned, while a crash intentionally leaves an unstopped stale row.
"""

from alembic import op


revision = "0096_trace_write_stats_health"
down_revision = "0095_trace_write_stats"
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


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trace_write_stats_health")
