"""TEE twin of RDS 0092: safe timestamp parser for lane-rollup bucketing.

Revision ID: 0024_lane_rollup_safe_ts
Revises: 0023_lane_daily_rollup

The freeze runs against whichever database is primary, and test promoted the
TEE database to primary on 2026-08-18 (deploy 82c4c019) — so this function
has to exist on both chains or the resident freezer breaks the moment the
promotion lands. Body is byte-identical to RDS 0092 (rationale there).
"""

from alembic import op


revision = "0024_lane_rollup_safe_ts"
down_revision = "0023_lane_daily_rollup"
branch_labels = None
depends_on = None


_UP = """
CREATE OR REPLACE FUNCTION lane_rollup_safe_ts(raw text)
RETURNS timestamptz
LANGUAGE plpgsql STABLE STRICT
AS $$
BEGIN
    RETURN raw::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$;
"""

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0024_lane_rollup_safe_ts"]'::jsonb)::text,
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
