"""Durably reconcile user-visible V2 chat terminal failures.

Revision ID: 0037_v2_terminal_failure_outbox
Revises: 0036_chat_r2_lifecycle
"""

from alembic import op


revision = "0037_v2_terminal_failure_outbox"
down_revision = "0036_chat_r2_lifecycle"
branch_labels = None
depends_on = None


_UP = r"""
-- The job transition and this marker are written by one SQL statement.  A
-- worker/reaper process may therefore die immediately after terminalization
-- without losing the obligation to expose the failure to the client.
CREATE TABLE IF NOT EXISTS v2_terminal_failure_outbox (
    job_id                       BIGINT PRIMARY KEY
                                 REFERENCES agent_jobs(id) ON DELETE CASCADE,
    user_id                      TEXT NOT NULL,
    error_code                   TEXT NOT NULL CHECK (
                                   error_code <> '' AND length(error_code) <= 120
                                 ),
    target_route_id              UUID,
    target_route_updated_at      TIMESTAMPTZ,
    status_delivered_at          TIMESTAMPTZ,
    runtime_error_delivered_at   TIMESTAMPTZ,
    status_attempt_count         INTEGER NOT NULL DEFAULT 0 CHECK (status_attempt_count >= 0),
    status_last_attempt_at       TIMESTAMPTZ,
    status_next_attempt_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    runtime_error_attempt_count  INTEGER NOT NULL DEFAULT 0 CHECK (runtime_error_attempt_count >= 0),
    runtime_error_last_attempt_at TIMESTAMPTZ,
    runtime_error_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS v2_terminal_failure_status_pending_idx
  ON v2_terminal_failure_outbox
     (status_next_attempt_at, status_last_attempt_at, created_at, job_id)
  WHERE status_delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS v2_terminal_failure_runtime_pending_idx
  ON v2_terminal_failure_outbox
     (runtime_error_next_attempt_at, runtime_error_last_attempt_at, created_at, job_id)
  WHERE runtime_error_delivered_at IS NULL;

-- Status insertion and status_delivered_at advance in one DB transaction.
-- This index is the final idempotency guard if an older process emitted the
-- event before an upgraded reconciler saw the marker.
-- SHARE ROW EXCLUSIVE conflicts with INSERT's ROW EXCLUSIVE lock.  Keep it
-- through both dedupe and index creation so an old worker cannot recreate a
-- duplicate in the migration race window.
LOCK TABLE agent_status_events IN SHARE ROW EXCLUSIVE MODE;

DELETE FROM agent_status_events AS newer
USING agent_status_events AS older
WHERE newer.kind = 'error'
  AND older.kind = 'error'
  AND newer.job_id IS NOT NULL
  AND newer.job_id = older.job_id
  AND newer.id > older.id;

CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_status_events_job_error
  ON agent_status_events (job_id)
  WHERE kind = 'error' AND job_id IS NOT NULL;
"""


_DOWN = r"""
DROP INDEX IF EXISTS ux_agent_status_events_job_error;
DROP INDEX IF EXISTS v2_terminal_failure_runtime_pending_idx;
DROP INDEX IF EXISTS v2_terminal_failure_status_pending_idx;
DROP TABLE IF EXISTS v2_terminal_failure_outbox;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
