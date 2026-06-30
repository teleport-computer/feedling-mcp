"""agent-runner: per-owner supervisor heartbeats (multi-node)

Revision ID: 0010_supervisor_heartbeats
Revises: 0009_agent_runtime_user_fk
Create Date: 2026-06-30

The legacy supervisor heartbeat is a single ``server_config`` key, so multiple
runners (multi-CVM / multi-container hosting, see
docs/AGENT_RUNNER_MULTI_NODE_IMPLEMENTATION_PLAN.zh.md) clobber each other's
heartbeat and the backend can only see the last writer. This table gives each
runner its OWN per-owner row, so the backend's wedge guard can aggregate them
and decide whether ANY runner is hosting.

Beyond liveness (which the lease table's last_heartbeat_at already carries per
owner), this row carries the cluster-capability flags a lease row does not:
host_all / gateway (is this runner actually hosting / is its gateway up) and the
shard + capacity config. The legacy single-key heartbeat stays as a transitional
fallback until both backend and runners run this version.

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_supervisor_heartbeats"
down_revision = "0009_agent_runtime_user_fk"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS agent_runtime_supervisor_heartbeats (
    owner           TEXT PRIMARY KEY,        -- "<hostname>:<pid>" of the runner
    host            TEXT,
    shard_index     INTEGER NOT NULL DEFAULT 0,
    shard_count     INTEGER NOT NULL DEFAULT 1,
    max_children    INTEGER NOT NULL DEFAULT 0,   -- 0 = unlimited
    active_children INTEGER NOT NULL DEFAULT 0,
    host_all        BOOLEAN NOT NULL DEFAULT false,
    gateway         BOOLEAN NOT NULL DEFAULT false,
    version         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',  -- full rich payload for diagnostics
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runtime_supervisor_heartbeats_updated_idx
    ON agent_runtime_supervisor_heartbeats (updated_at);
"""

_DROP = """
DROP TABLE IF EXISTS agent_runtime_supervisor_heartbeats;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute(_DROP)
