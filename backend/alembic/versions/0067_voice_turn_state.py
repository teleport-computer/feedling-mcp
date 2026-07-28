"""Store encrypted short-lived voice turn state.

Revision ID: 0067_voice_turn_state
Revises: 0066_model_api_vision_route
"""

from alembic import op


revision = "0067_voice_turn_state"
down_revision = "0066_model_api_vision_route"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_turn_results (
          call_id TEXT NOT NULL,
          turn_id TEXT NOT NULL,
          user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
          message_id TEXT NOT NULL DEFAULT '',
          nonce BYTEA NOT NULL,
          ciphertext BYTEA NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (call_id, turn_id)
        );
        CREATE INDEX IF NOT EXISTS ix_voice_turn_results_expiry
          ON voice_turn_results(expires_at);

        CREATE TABLE IF NOT EXISTS voice_turn_streams (
          call_id TEXT NOT NULL,
          turn_id TEXT NOT NULL,
          segment INTEGER NOT NULL,
          user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
          text_len INTEGER NOT NULL,
          nonce BYTEA NOT NULL,
          ciphertext BYTEA NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          is_final BOOLEAN NOT NULL DEFAULT FALSE,
          PRIMARY KEY (call_id, turn_id, segment)
        );
        CREATE INDEX IF NOT EXISTS ix_voice_turn_streams_expiry
          ON voice_turn_streams(expires_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS voice_turn_streams;
        DROP TABLE IF EXISTS voice_turn_results;
        """
    )
