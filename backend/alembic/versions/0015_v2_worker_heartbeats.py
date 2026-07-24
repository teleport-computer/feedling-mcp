"""v2 worker liveness heartbeats.

Send-time guard (Task 2 of subproject B follow-up): a `db_action_v2` chat/send
skips the resident-supervisor wedge guard (that check only means something for
the CLI-consumer world), but had no replacement — if every serve_worker process
is dead, a job just queues forever with no error surfaced to the user. This
table gives ``jobs_store.workers_alive()`` something to check: each running
serve_worker process UPSERTs its own row every ~10s (see
``serve_worker._heartbeat_loop``); the send path refuses with a distinct 503
when no row has beaten recently.

Revision ID: 0015_v2_worker_heartbeats
"""
from alembic import op

revision = "0015_v2_worker_heartbeats"
down_revision = "0014_hosted_runtime_v2"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS v2_worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  beat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_worker_heartbeats;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
