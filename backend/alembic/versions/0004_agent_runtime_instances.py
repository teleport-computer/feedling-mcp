"""agent-runner: agent_runtime_instances lease table

Revision ID: 0004_agent_runtime_instances
Revises: 0003_drop_perception_permissions
Create Date: 2026-06-25

The hosted agent runtime (backend/agent_runtime/, see
docs/AGENT_RUNTIME_CC_CODEX_PLAN.zh.md) spawns one consumer per active user.
The supervisor's lease must be safe across workers/processes, so it lives in
Postgres rather than in-memory: exactly one supervisor holds a live lease for a
user at a time, and a crashed holder's lease expires so another can take over.

Only the current ``lease_owner`` may renew, stop, or update a user's
``session_ref``. ``runtime.json`` in each user's runtime home holds the same
non-secret state for local inspection; this table is the source of truth.

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_agent_runtime_instances"
down_revision = "0003_drop_perception_permissions"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS agent_runtime_instances (
    user_id           TEXT PRIMARY KEY,
    driver            TEXT NOT NULL,
    status            TEXT NOT NULL,          -- starting | running | idle | error
    pid               INTEGER,
    lease_owner       TEXT,                   -- supervisor id holding the lease
    lease_expires_at  TIMESTAMPTZ,
    session_ref       TEXT,                   -- driver resume handle (Claude/Codex)
    runtime_home      TEXT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ,
    last_active_at    TIMESTAMPTZ,
    error             TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runtime_instances_lease_idx
    ON agent_runtime_instances (lease_owner, lease_expires_at);
"""

_DROP = """
DROP TABLE IF EXISTS agent_runtime_instances;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute(_DROP)
