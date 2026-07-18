"""Recognize encrypted workspace batches in the mutation-frontier trigger.

Revision ID: 0044_v2_workspace_batches
Revises: 0043_v2_encrypted_trajectories
"""
from alembic import op


revision = "0044_v2_workspace_batches"
down_revision = "0043_v2_encrypted_trajectories"
branch_labels = None
depends_on = None


def _frontier_trigger(*, include_batch: bool) -> str:
    batch_line = (
        ",\n       'workspace_batch_encrypted_v1'"
        if include_batch
        else ""
    )
    return f"""
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
       'workspace_encrypted_v1'{batch_line}
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


def upgrade() -> None:
    op.execute(_frontier_trigger(include_batch=True))


def downgrade() -> None:
    op.execute(_frontier_trigger(include_batch=False))
