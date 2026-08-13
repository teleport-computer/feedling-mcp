"""TEE-side plaintext shape for the voice transcript archive.

RDS keeps ``transcript_envelope`` (a v1 content envelope); the replicator
decrypts it and writes the plaintext ``doc`` here, same shape rule as
memory_moments / world_book_entries.

Revision ID: 0012_voice_transcripts
Revises: 0011_perception_signal_state_v2
"""

from alembic import op


revision = "0012_voice_transcripts"
down_revision = "0011_perception_signal_state_v2"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS voice_transcripts (
    user_id         TEXT NOT NULL,
    call_id         TEXT NOT NULL,
    chat_message_id TEXT NOT NULL DEFAULT '',
    turn_count      INTEGER NOT NULL DEFAULT 0,
    duration_sec    INTEGER NOT NULL DEFAULT 0,
    char_count      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    doc             JSONB NOT NULL,
    PRIMARY KEY (user_id, call_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS voice_transcripts_user_created_idx
    ON voice_transcripts (user_id, created_at DESC);
"""

_DOWN = """
DROP TABLE IF EXISTS voice_transcripts;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
