"""Create the voice-call lifecycle fence in the TEE primary schema.

Revision ID: 0030_voice_call_sessions_primary
Revises: 0025_lane_rollup_voice

``voice_call_sessions`` must live beside whichever chat database is primary:
its row/advisory locks serialize cancel and finalize with chat writes.  The
RDS chain created the table in 0081; this shared TEE branch makes the same
contract available before PRE/PROD promotion and can later be merged into the
already-advanced TEST chain.
"""

from alembic import op


revision = "0030_voice_call_sessions_primary"
down_revision = "0025_lane_rollup_voice"
branch_labels = None
depends_on = None


# Keep byte-identical to RDS 0081.  Revision modules cannot import each other
# because their filenames start with digits, so a convergence test compares
# the two literals and fails if either side drifts.
_UP = """
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

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0030_voice_call_sessions_primary"]'::jsonb)::text,
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
