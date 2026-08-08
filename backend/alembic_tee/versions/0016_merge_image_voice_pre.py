"""Merge image/voice and PRE plaintext TEE migration heads.

Revision ID: 0016_merge_image_voice_pre
Revises: 0013_merge_image_voice, 0015_merge_pre_perception
"""

from alembic import op


revision = "0016_merge_image_voice_pre"
down_revision = (
    "0013_merge_image_voice",
    "0015_merge_pre_perception",
)
branch_labels = None
depends_on = None


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0016_merge_image_voice_pre"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


_ALIGN_RUNTIME_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_one_image_generation
  ON model_api_routes (user_id) WHERE is_image_generation;
CREATE INDEX IF NOT EXISTS ix_chat_messages_ts ON chat_messages (ts);
CREATE INDEX IF NOT EXISTS ix_user_logs_proactive_jobs_ts
  ON user_logs (ts) WHERE stream='proactive_jobs';
CREATE INDEX IF NOT EXISTS ix_user_logs_app_session_end_ts
  ON user_logs (ts)
  WHERE stream='tracking_events' AND doc->>'type'='app_session_end';

ALTER TABLE voice_transcripts
  ADD COLUMN IF NOT EXISTS transcript_envelope JSONB;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'voice_transcripts'
      AND column_name = 'doc'
  ) THEN
    EXECUTE 'UPDATE voice_transcripts '
            'SET transcript_envelope = doc '
            'WHERE transcript_envelope IS NULL';
  END IF;
END $$;
ALTER TABLE voice_transcripts
  ALTER COLUMN transcript_envelope SET NOT NULL;
ALTER TABLE voice_transcripts DROP COLUMN IF EXISTS doc;
ALTER TABLE voice_transcripts
  DROP CONSTRAINT IF EXISTS ck_voice_transcript_call_id,
  DROP CONSTRAINT IF EXISTS ck_voice_transcript_counts,
  DROP CONSTRAINT IF EXISTS ck_voice_transcript_envelope;
ALTER TABLE voice_transcripts
  ADD CONSTRAINT ck_voice_transcript_call_id
    CHECK (call_id ~ '^vcall_' AND length(call_id) BETWEEN 7 AND 96),
  ADD CONSTRAINT ck_voice_transcript_counts
    CHECK (turn_count >= 0 AND duration_sec >= 0 AND char_count >= 0),
  ADD CONSTRAINT ck_voice_transcript_envelope
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
    );
CREATE INDEX IF NOT EXISTS ix_voice_transcripts_user_created
  ON voice_transcripts (user_id, created_at DESC);
"""


def upgrade() -> None:
    op.execute(_ALIGN_RUNTIME_SCHEMA)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
