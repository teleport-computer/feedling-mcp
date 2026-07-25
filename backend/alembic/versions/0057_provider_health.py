"""Provider health shared by resident V1 and hosted Runtime V2.

The table is deliberately runtime-neutral.  Both runtimes write provider
outcomes, and the shared proactive gate reads the resulting state.

Revision ID: 0057_provider_health
Revises: 0056_agent_jobs_hb_idx
"""

from alembic import op


revision = "0057_provider_health"
down_revision = "0056_agent_jobs_hb_idx"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS provider_health (
  user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  provider_state TEXT NOT NULL DEFAULT 'ok'
    CHECK (provider_state IN ('ok', 'needs_user_action')),
  last_provider_success_at TIMESTAMPTZ,
  last_provider_failure_at TIMESTAMPTZ,
  last_provider_error_class TEXT NOT NULL DEFAULT '',
  last_provider_error_blame TEXT NOT NULL DEFAULT '',
  user_provider_failure_started_at TIMESTAMPTZ,
  last_probe_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_provider_health_state
  ON provider_health (provider_state)
  WHERE provider_state = 'needs_user_action';
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_health")
