"""Allow voice transcripts to use encrypted or plaintext content shapes.

``0080_voice_transcripts`` predated the per-user content-encryption cutover and
therefore required every archive to be a dual-recipient envelope.  The shared
writer now follows the user's effective preference, so plaintext-tier users
need the same exact plaintext body shape accepted by the other content tables.

Revision ID: 0085_relax_voice_transcript_shape
Revises: 0084_merge_screen_plaintext
"""

from alembic import op


revision = "0085_relax_voice_transcript_shape"
down_revision = "0084_merge_screen_plaintext"
branch_labels = None
depends_on = None


def _shape_check(col: str) -> str:
    return f"""
      jsonb_typeof({col}) = 'object'
      AND {col} ? 'owner_user_id'
      AND {col} ? 'id'
      AND {col} ? 'visibility'
      AND jsonb_typeof({col}->'owner_user_id') = 'string'
      AND jsonb_typeof({col}->'id') = 'string'
      AND jsonb_typeof({col}->'visibility') = 'string'
      AND {col}->>'owner_user_id' = user_id
      AND {col}->>'visibility' = 'shared'
      AND length({col}->>'id') > 0
      AND (
        (
          {col} ? 'body_ct' AND {col} ? 'nonce'
          AND {col} ? 'K_user' AND {col} ? 'K_enclave' AND {col} ? 'v'
          AND jsonb_typeof({col}->'body_ct') = 'string'
          AND jsonb_typeof({col}->'nonce') = 'string'
          AND jsonb_typeof({col}->'K_user') = 'string'
          AND jsonb_typeof({col}->'K_enclave') = 'string'
          AND jsonb_typeof({col}->'v') = 'number'
          AND length({col}->>'body_ct') > 0
          AND length({col}->>'nonce') > 0
          AND length({col}->>'K_user') > 0
          AND length({col}->>'K_enclave') > 0
          AND {col} - ARRAY[
            'v','id','owner_user_id','visibility','body_ct','nonce','K_user',
            'K_enclave','enclave_pk_fpr','content_pk_fpr'
          ]::text[] = '{{}}'::jsonb
        )
        OR
        (
          {col} ? 'body'
          AND jsonb_typeof({col}->'body') = 'string'
          AND NOT ({col} ? 'body_ct')
          AND {col} - ARRAY['id','owner_user_id','visibility','body']::text[]
              = '{{}}'::jsonb
        )
      )
    """


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE voice_transcripts
          DROP CONSTRAINT IF EXISTS ck_voice_transcript_envelope;
        ALTER TABLE voice_transcripts
          ADD CONSTRAINT ck_voice_transcript_envelope
          CHECK ({_shape_check('transcript_envelope')});
    """)


def downgrade() -> None:
    # Plaintext rows may already exist, so restoring the envelope-only 0080
    # constraint is unsafe.  Dropping the widened check is the safe rollback.
    op.execute("""
        ALTER TABLE voice_transcripts
          DROP CONSTRAINT IF EXISTS ck_voice_transcript_envelope;
    """)
