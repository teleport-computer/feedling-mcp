"""Durable Runtime V2 job recovery events.

Revision ID: 0097_v2_job_recovery_events
Revises: 0096_trace_write_stats_health

``agent_jobs.attempt_count`` mixes ordinary failures, retries, lease reaping,
and watchdog recovery, while ``last_error`` is overwritten by later outcomes.
This append-only event ledger is therefore the first trustworthy source for a
rolling count of watchdog recoveries.  It intentionally has no FK to
``agent_jobs`` so operational history survives job/user lifecycle cleanup.
"""

from alembic import op


revision = "0097_v2_job_recovery_events"
down_revision = "0096_trace_write_stats_health"
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


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS v2_job_recovery_events")
