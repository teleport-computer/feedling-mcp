"""Separate queue deadlines from worker leases and track coalesced input.

``queue_deadline_at`` is the admission-to-claim deadline. ``lease_expires_at``
fences claimed/running ownership, while legacy ``deadline_at`` mirrors that
active lease for safe mixed-version rollback. ``input_generation`` lets
finalization atomically create a successor when a chat send was coalesced after
the worker's last read.

Revision ID: 0023_v2_job_liveness
"""
from alembic import op

revision = "0023_v2_job_liveness"
down_revision = "0022_v2_action_queue_privacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_jobs "
        "ADD COLUMN IF NOT EXISTS input_generation BIGINT NOT NULL DEFAULT 0, "
        "ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ, "
        "ADD COLUMN IF NOT EXISTS queue_deadline_at TIMESTAMPTZ"
    )
    op.execute(
        "UPDATE agent_jobs SET "
        "queue_deadline_at = COALESCE("
        "  deadline_at, CASE WHEN lane='chat' THEN created_at + interval '120 seconds' END"
        "), "
        # Keep deadline_at NULL while pending.  A previous worker version uses
        # COALESCE(deadline_at, now()+300s) at claim; preserving the queue SLA
        # here could give it only seconds of execution in a mixed deploy.
        "deadline_at = NULL "
        "WHERE status='pending'"
    )
    op.execute(
        "UPDATE agent_jobs SET "
        "lease_expires_at = COALESCE("
        "  deadline_at, COALESCE(started_at, claimed_at, now()) + interval '300 seconds'"
        "), "
        # Compatibility mirror: a rollback to the previous image still has a
        # reaper that reads deadline_at for active rows.
        "deadline_at = COALESCE("
        "  deadline_at, COALESCE(started_at, claimed_at, now()) + interval '300 seconds'"
        ") "
        "WHERE status IN ('claimed','running')"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_agent_jobs_pending_deadline"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_pending_queue_deadline "
        "ON agent_jobs(queue_deadline_at) WHERE status='pending'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_active_lease "
        "ON agent_jobs(lease_expires_at) WHERE status IN ('claimed','running')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_jobs_active_lease")
    op.execute("DROP INDEX IF EXISTS ix_agent_jobs_pending_queue_deadline")
    op.execute(
        "UPDATE agent_jobs SET deadline_at=COALESCE(deadline_at, queue_deadline_at) "
        "WHERE status='pending'"
    )
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS queue_deadline_at")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS lease_expires_at")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS input_generation")
