"""Merge TEST plaintext-shadow history with the voice-primary branch.

Revision ID: 0031_merge_voice_primary
Revises: 0029_plaintext_shadow_merge, 0030_voice_call_sessions_primary

TEST had already advanced beyond the PRE/PROD TEE chain when the shared voice
session migration was added.  This revision converges both parents without
repeating DDL.  The schema change invalidates restore evidence tied to 0029 and
advances any prepared-primary marker to the new single head.
"""

from alembic import op


revision = "0031_merge_voice_primary"
down_revision = (
    "0029_plaintext_shadow_merge",
    "0030_voice_call_sessions_primary",
)
branch_labels = None
depends_on = None


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0031_merge_voice_primary"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute("TRUNCATE TABLE plaintext_shadow_restore_evidence")
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
