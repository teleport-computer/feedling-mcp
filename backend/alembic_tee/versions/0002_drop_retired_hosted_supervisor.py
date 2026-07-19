"""Drop retired managed-hosted supervisor mirrors from the TEE database.

Revision ID: 0002_drop_retired_supervisor
Revises: 0001_tee_baseline

The upgrade irreversibly deletes obsolete mirror rows. Downgrade recreates only
empty schema for migration recovery under V2-only binaries.
"""
from alembic import op


revision = "0002_drop_retired_supervisor"
down_revision = "0001_tee_baseline"
branch_labels = None
depends_on = None


_UP = """
DROP TABLE IF EXISTS agent_runtime_supervisor_heartbeats;
DROP TABLE IF EXISTS agent_runtime_instances;
DELETE FROM server_config WHERE key = 'agent_runtime_supervisor_heartbeat';
"""

_DOWN = """
CREATE TABLE IF NOT EXISTS agent_runtime_instances (
    user_id           TEXT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    driver            TEXT NOT NULL,
    status            TEXT NOT NULL,
    pid               INTEGER,
    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,
    session_ref       TEXT,
    runtime_home      TEXT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ,
    last_active_at    TIMESTAMPTZ,
    error             TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runtime_instances_lease_idx
    ON agent_runtime_instances (lease_owner, lease_expires_at);

CREATE TABLE IF NOT EXISTS agent_runtime_supervisor_heartbeats (
    owner           TEXT PRIMARY KEY,
    host            TEXT,
    shard_index     INTEGER NOT NULL DEFAULT 0,
    shard_count     INTEGER NOT NULL DEFAULT 1,
    max_children    INTEGER NOT NULL DEFAULT 0,
    active_children INTEGER NOT NULL DEFAULT 0,
    host_all        BOOLEAN NOT NULL DEFAULT false,
    gateway         BOOLEAN NOT NULL DEFAULT false,
    version         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runtime_supervisor_heartbeats_updated_idx
    ON agent_runtime_supervisor_heartbeats (updated_at);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
