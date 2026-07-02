"""observability samples + agent_runtime_resource

Revision ID: 0012_observability
Revises: 0011_world_book_entries
Create Date: 2026-07-02
"""
from alembic import op

revision = "0012_observability"
down_revision = "0011_world_book_entries"
branch_labels = None
depends_on = None

_DDL = """
CREATE TABLE IF NOT EXISTS observability_samples (
    ts                     TIMESTAMPTZ PRIMARY KEY,
    host_load1             REAL,
    host_mem_avail_bytes   BIGINT,
    backend_mem_bytes      BIGINT,
    enclave_mem_bytes      BIGINT,
    agentrunner_mem_bytes  BIGINT,
    backend_cpu_pct        REAL,
    enclave_cpu_pct        REAL,
    agentrunner_cpu_pct    REAL,
    live_agents            INT,
    orphan                 INT,
    errors                 INT,
    db_conns               INT,
    backend_5xx            INT,
    extra                  JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS observability_samples_ts_idx ON observability_samples (ts DESC);

CREATE TABLE IF NOT EXISTS agent_runtime_resource (
    service         TEXT PRIMARY KEY,
    cpu_usage_usec  BIGINT,
    mem_bytes       BIGINT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

def upgrade():
    op.execute(_DDL)

def downgrade():
    op.execute("DROP TABLE IF EXISTS observability_samples; DROP TABLE IF EXISTS agent_runtime_resource;")
