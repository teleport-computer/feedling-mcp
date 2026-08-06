"""Permanent per-call voice transcript archive.

The full call transcript is archived here at hangup and kept forever: the user
reads it back from Settings, the agent pulls it on demand through the
voice_transcript_* tools, and Capture renders it into the memory window instead
of the tiny chat card that stands in for the call in the transcript stream.

Deliberately a real table, not a ``user_logs`` stream: ``db.log_append``
swallows every exception, and archiving must fail loudly — it happens BEFORE the
per-turn rows are deleted, so a silent archive failure would destroy the call.

Revision ID: 0080_voice_transcripts
Revises: 0079_home_hotpath_indexes
"""

from alembic import op


revision = "0080_voice_transcripts"
down_revision = "0079_home_hotpath_indexes"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS voice_transcripts (
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  call_id TEXT NOT NULL,
  -- The chat card row that represents this call in the transcript stream.
  -- Lets the client jump card -> archive without a second lookup.
  chat_message_id TEXT NOT NULL DEFAULT '',
  turn_count INTEGER NOT NULL DEFAULT 0,
  duration_sec INTEGER NOT NULL DEFAULT 0,
  -- Plaintext length, recorded at write time. Capture reads it to decide
  -- whether the transcript needs bounded sampling WITHOUT decrypting first.
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
      AND transcript_envelope ? 'body_ct'
      AND transcript_envelope ? 'nonce'
      AND transcript_envelope ? 'K_user'
      AND transcript_envelope ? 'K_enclave'
      AND transcript_envelope ? 'owner_user_id'
      AND transcript_envelope ? 'id'
      AND transcript_envelope ? 'v'
      AND transcript_envelope ? 'visibility'
      AND jsonb_typeof(transcript_envelope->'v') = 'number'
      AND jsonb_typeof(transcript_envelope->'id') = 'string'
      AND jsonb_typeof(transcript_envelope->'owner_user_id') = 'string'
      AND jsonb_typeof(transcript_envelope->'visibility') = 'string'
      AND jsonb_typeof(transcript_envelope->'body_ct') = 'string'
      AND jsonb_typeof(transcript_envelope->'nonce') = 'string'
      AND jsonb_typeof(transcript_envelope->'K_user') = 'string'
      AND jsonb_typeof(transcript_envelope->'K_enclave') = 'string'
      AND transcript_envelope->>'owner_user_id' = user_id
      AND transcript_envelope->>'visibility' = 'shared'
      AND length(transcript_envelope->>'id') > 0
      AND length(transcript_envelope->>'body_ct') > 0
      AND length(transcript_envelope->>'nonce') > 0
      AND length(transcript_envelope->>'K_user') > 0
      AND length(transcript_envelope->>'K_enclave') > 0
      AND transcript_envelope - ARRAY[
        'v','id','owner_user_id','visibility','body_ct','nonce','K_user',
        'K_enclave','enclave_pk_fpr','content_pk_fpr'
      ]::text[] = '{}'::jsonb
    )
);

-- Settings lists a user's calls newest-first.
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
