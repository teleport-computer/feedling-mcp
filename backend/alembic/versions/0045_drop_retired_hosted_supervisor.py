"""Drop retired managed-hosted resident supervisor state.

Revision ID: 0045_drop_retired_supervisor
Revises: 0044_v2_workspace_batches

Managed hosted execution is Runtime V2-only.  The per-user process lease table,
per-supervisor heartbeat table, and legacy singleton heartbeat record no longer
have a writer or reader.  The independent API-key ``/v1/chat/*`` consumer does
not use any of them.

The upgrade irreversibly deletes ephemeral legacy rows. Downgrade recreates
only empty schema for migration recovery under V2-only binaries; it does not
authorize rolling application source back to a resident supervisor build.
"""
from alembic import op


revision = "0045_drop_retired_supervisor"
down_revision = "0044_v2_workspace_batches"
branch_labels = None
depends_on = None


_UP = """
DROP TABLE IF EXISTS agent_runtime_supervisor_heartbeats;
DROP TABLE IF EXISTS agent_runtime_instances;
DELETE FROM server_config WHERE key = 'agent_runtime_supervisor_heartbeat';
"""

# A schema-only downgrade is safe under current V2-only source because no
# process reads or writes these empty tables. It intentionally cannot restore
# deleted rows or the singleton server_config record.
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
