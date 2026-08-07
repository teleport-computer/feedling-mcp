"""Durable voice-call lifecycle tombstones.

Revision ID: 0081_voice_call_sessions
Revises: 0080_voice_transcripts
"""

from alembic import op


revision = "0081_voice_call_sessions"
down_revision = "0080_voice_transcripts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_call_sessions (
          user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
          call_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'finalizing', 'cancelled', 'finalized')),
          cancel_reason TEXT NOT NULL DEFAULT '',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          ended_at TIMESTAMPTZ,
          PRIMARY KEY (user_id, call_id)
        );
        CREATE INDEX IF NOT EXISTS ix_voice_call_sessions_status
          ON voice_call_sessions (user_id, status);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS voice_call_sessions")
