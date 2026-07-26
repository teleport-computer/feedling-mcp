"""Indexes for the authenticated V2 chat activity read path.

Activity snapshots resolve a user's chat job from its public turn id, then
read the status rows for that job. Both tables are append-only, so these
indexes keep polling proportional to one turn instead of table size.

Revision ID: 0062_chat_activity_lookup_idx
"""
from alembic import op

revision = "0062_chat_activity_lookup_idx"
down_revision = "0061_v2_adaptive_tail_metrics"
branch_labels = None
depends_on = None

_JOB_LOOKUP_INDEX = "ix_agent_jobs_user_lane_trace"
_STATUS_LOOKUP_INDEX = "ix_agent_status_events_job_id_id"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_JOB_LOOKUP_INDEX} "
        "ON agent_jobs (user_id, lane, trace_id) WHERE trace_id IS NOT NULL"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_STATUS_LOOKUP_INDEX} "
        "ON agent_status_events (job_id, id)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_STATUS_LOOKUP_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_JOB_LOOKUP_INDEX}")
