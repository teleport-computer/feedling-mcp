"""TEE-primary-compatible shape for the voice transcript archive.

The JSON envelope column accepts both ciphertext and plaintext bodies. This
keeps the promoted TEE schema identical to the runtime schema while allowing
the replicator to replace encrypted envelopes with plaintext envelopes.

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
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  call_id TEXT NOT NULL,
  chat_message_id TEXT NOT NULL DEFAULT '',
  turn_count INTEGER NOT NULL DEFAULT 0,
  duration_sec INTEGER NOT NULL DEFAULT 0,
  char_count INTEGER NOT NULL DEFAULT 0,
  transcript_envelope JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, call_id),
  CONSTRAINT ck_voice_transcript_call_id
    CHECK (call_id ~ '^vcall_' AND length(call_id) BETWEEN 7 AND 96),
  CONSTRAINT ck_voice_transcript_counts
    CHECK (turn_count >= 0 AND duration_sec >= 0 AND char_count >= 0),
  CONSTRAINT ck_voice_transcript_envelope
    CHECK (
      jsonb_typeof(transcript_envelope) = 'object'
      AND transcript_envelope ? 'owner_user_id'
      AND transcript_envelope ? 'id'
      AND transcript_envelope ? 'visibility'
      AND jsonb_typeof(transcript_envelope->'id') = 'string'
      AND jsonb_typeof(transcript_envelope->'owner_user_id') = 'string'
      AND jsonb_typeof(transcript_envelope->'visibility') = 'string'
      AND transcript_envelope->>'owner_user_id' = user_id
      AND transcript_envelope->>'visibility' = 'shared'
      AND length(transcript_envelope->>'id') > 0
      AND (
        (
          transcript_envelope ? 'body_ct'
          AND transcript_envelope ? 'nonce'
          AND transcript_envelope ? 'K_user'
          AND transcript_envelope ? 'K_enclave'
          AND transcript_envelope ? 'v'
          AND jsonb_typeof(transcript_envelope->'v') = 'number'
          AND jsonb_typeof(transcript_envelope->'body_ct') = 'string'
          AND jsonb_typeof(transcript_envelope->'nonce') = 'string'
          AND jsonb_typeof(transcript_envelope->'K_user') = 'string'
          AND jsonb_typeof(transcript_envelope->'K_enclave') = 'string'
          AND length(transcript_envelope->>'body_ct') > 0
          AND length(transcript_envelope->>'nonce') > 0
          AND length(transcript_envelope->>'K_user') > 0
          AND length(transcript_envelope->>'K_enclave') > 0
          AND transcript_envelope - ARRAY[
            'v','id','owner_user_id','visibility','body_ct','nonce','K_user',
            'K_enclave','enclave_pk_fpr','content_pk_fpr'
          ]::text[] = '{}'::jsonb
        )
        OR
        (
          transcript_envelope ? 'body'
          AND jsonb_typeof(transcript_envelope->'body') = 'string'
          AND NOT (transcript_envelope ? 'body_ct')
          AND transcript_envelope - ARRAY[
            'id','owner_user_id','visibility','body'
          ]::text[] = '{}'::jsonb
        )
      )
    )
);
CREATE INDEX IF NOT EXISTS ix_voice_transcripts_user_created
    ON voice_transcripts (user_id, created_at DESC);
"""

_DOWN = """
DROP TABLE IF EXISTS voice_transcripts;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
