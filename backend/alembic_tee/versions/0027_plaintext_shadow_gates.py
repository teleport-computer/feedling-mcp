"""Add plaintext frame storage and bound infrastructure evidence.

Revision ID: 0027_plaintext_shadow_gates
Revises: 0026_plaintext_shadow_control

Old restore-evidence rows predate the endpoint/capacity/HA binding contract and
cannot safely authorize Gate 2, so this migration invalidates them explicitly.
"""

from alembic import op


revision = "0027_plaintext_shadow_gates"
down_revision = "0026_plaintext_shadow_control"
branch_labels = None
depends_on = None


_UP = r"""
ALTER TABLE frames ADD COLUMN IF NOT EXISTS body_plaintext BYTEA;

TRUNCATE TABLE plaintext_shadow_restore_evidence;
ALTER TABLE plaintext_shadow_restore_evidence
    ADD COLUMN IF NOT EXISTS backup_artifact_digest TEXT NOT NULL,
    ADD COLUMN IF NOT EXISTS target_fingerprint TEXT NOT NULL,
    ADD COLUMN IF NOT EXISTS target_capacity_bytes BIGINT NOT NULL,
    ADD COLUMN IF NOT EXISTS target_connection_limit INTEGER NOT NULL,
    ADD COLUMN IF NOT EXISTS ha_verified BOOLEAN NOT NULL,
    ADD COLUMN IF NOT EXISTS attestation_key_fingerprint TEXT NOT NULL,
    ADD COLUMN IF NOT EXISTS attestation_signature_digest TEXT NOT NULL;

ALTER TABLE plaintext_shadow_restore_evidence
    ADD CONSTRAINT plaintext_shadow_restore_bound_scalars_nonempty
        CHECK (backup_artifact_digest <> '' AND target_fingerprint <> ''
               AND attestation_key_fingerprint <> ''
               AND attestation_signature_digest <> ''),
    ADD CONSTRAINT plaintext_shadow_restore_capacity_positive
        CHECK (target_capacity_bytes > 0 AND target_connection_limit > 0);
"""


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0027_plaintext_shadow_gates"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
