"""Durable account recovery challenges for the TEE primary.

Revision ID: 0045_account_recover_challenges
Revises: 0044_divergence_observed_at

Carry the RDS 0105 table and indexes into the independent TEE chain. Challenges
are generated locally after promotion; no historical recovery hashes are copied.
"""

from alembic import op


revision = "0045_account_recover_challenges"
down_revision = "0044_divergence_observed_at"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS account_recover_challenges (
    challenge_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    public_key TEXT NOT NULL,
    answer_sha256 TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_account_recover_challenges_user_id
    ON account_recover_challenges (user_id);
CREATE INDEX IF NOT EXISTS ix_account_recover_challenges_expires_at
    ON account_recover_challenges (expires_at);
"""


# Keep an existing prepared marker aligned with this chain's new head, as in 0044.
_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0045_account_recover_challenges"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS account_recover_challenges")
