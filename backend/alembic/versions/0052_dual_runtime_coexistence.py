"""Dual-runtime coexistence: restore V1 supervisor state + user allowlist.

Revision ID: 0052_dual_runtime_coexistence
Revises: 0051_web_settings_backfill

Restores the resident-supervisor tables dropped by 0045 (prod's test-lineage
DB has NOT yet run 0045; on prod the upgrade chain runs 0045 then this — the
transient drop window lasts only for the migration run itself) and adds the
v2_user_allowlist control table for the per-user canary rollout.
"""
from alembic import op

revision = "0052_dual_runtime_coexistence"
down_revision = "0051_web_settings_backfill"
branch_labels = None
depends_on = None

_UP = """
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

CREATE TABLE IF NOT EXISTS v2_user_allowlist (
    user_id     TEXT PRIMARY KEY,
    desired     TEXT NOT NULL CHECK (desired IN ('v2', 'resident')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_user_allowlist;
DROP TABLE IF EXISTS agent_runtime_supervisor_heartbeats;
DROP TABLE IF EXISTS agent_runtime_instances;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
