"""Encrypted Runtime V2 virtual workspace entries.

Revision ID: 0042_v2_workspace_foundation
Revises: 0041_v2_mcp_mutation_attempts
"""
from alembic import op


revision = "0042_v2_workspace_foundation"
down_revision = "0041_v2_mcp_mutation_attempts"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS v2_workspace_entries (
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  content_envelope JSONB NOT NULL,
  mime_type TEXT NOT NULL DEFAULT 'text/plain',
  source_ref TEXT NOT NULL DEFAULT '',
  revision BIGINT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, path),
  CONSTRAINT ck_v2_workspace_path
    CHECK (length(path) BETWEEN 2 AND 512 AND left(path, 1) = '/'),
  CONSTRAINT ck_v2_workspace_kind
    CHECK (kind IN ('artifact','workspace','working_memory','skill')),
  CONSTRAINT ck_v2_workspace_revision CHECK (revision > 0)
);

CREATE INDEX IF NOT EXISTS ix_v2_workspace_entries_user_kind_path
  ON v2_workspace_entries (user_id, kind, path);

CREATE TABLE IF NOT EXISTS v2_sandbox_usage_events (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  purpose TEXT NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_v2_sandbox_provider_nonempty CHECK (length(provider) BETWEEN 1 AND 80),
  CONSTRAINT ck_v2_sandbox_purpose_nonempty CHECK (length(purpose) BETWEEN 1 AND 80)
);

CREATE INDEX IF NOT EXISTS ix_v2_sandbox_usage_user_time
  ON v2_sandbox_usage_events (user_id, acquired_at DESC);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_sandbox_usage_events;
DROP TABLE IF EXISTS v2_workspace_entries;
"""

_FRONTIER_TRIGGER_UP = """
CREATE OR REPLACE FUNCTION v2_fill_effect_input_frontier()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'pending'
     AND NEW.effect_type IN (
       'reply_final_fenced_v1',
       'reply_terminal_fenced_v1',
       'reply_intermediate_fenced_v1'
     )
  THEN
    NEW.status := 'pending_fenced_v1';
  END IF;

  IF NEW.input_frontier_seq IS NULL
     AND NEW.effect_type IN (
       'memory_encrypted_v1',
       'identity_encrypted_v1',
       'schedule_encrypted_v1',
       'workspace_encrypted_v1'
     )
     AND EXISTS (
       SELECT 1 FROM agent_jobs job
       WHERE job.id=NEW.job_id AND job.lane='chat'
     )
  THEN
    SELECT COALESCE(MAX(chat.seq), 0)
      INTO NEW.input_frontier_seq
    FROM chat_messages chat
    WHERE chat.user_id=NEW.user_id
      AND chat.doc->>'role' IN ('user','human');
  END IF;
  RETURN NEW;
END;
$$;
"""

_FRONTIER_TRIGGER_DOWN = _FRONTIER_TRIGGER_UP.replace(
    "       'schedule_encrypted_v1',\n       'workspace_encrypted_v1'",
    "       'schedule_encrypted_v1'",
)


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_FRONTIER_TRIGGER_UP)


def downgrade() -> None:
    op.execute(_FRONTIER_TRIGGER_DOWN)
    op.execute(_DOWN)
