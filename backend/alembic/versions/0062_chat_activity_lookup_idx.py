"""Runtime-neutral chat activity storage and lookup indexes.

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
_RESIDENT_TURN_INDEX = "ix_chat_turn_activity_events_user_turn_id"


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS chat_turn_activity_events ("
        "id BIGSERIAL PRIMARY KEY,"
        "user_id TEXT NOT NULL,"
        "turn_id TEXT NOT NULL,"
        "activity_id TEXT NOT NULL,"
        "tool_name TEXT NOT NULL,"
        "state TEXT NOT NULL CHECK (state IN ('running','success','failure')),"
        "call_id TEXT,"
        "detail_json JSONB NOT NULL DEFAULT '{}',"
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        "FOREIGN KEY (user_id,turn_id) REFERENCES chat_messages(user_id,msg_id) "
        "ON DELETE CASCADE,"
        "UNIQUE (user_id,turn_id,activity_id,state)"
        ")"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_RESIDENT_TURN_INDEX} "
        "ON chat_turn_activity_events (user_id,turn_id,id)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_JOB_LOOKUP_INDEX} "
        "ON agent_jobs (user_id, lane, trace_id) WHERE trace_id IS NOT NULL"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_STATUS_LOOKUP_INDEX} "
        "ON agent_status_events (job_id, id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_turn_activity_events")
    op.execute(f"DROP INDEX IF EXISTS {_STATUS_LOOKUP_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_JOB_LOOKUP_INDEX}")
