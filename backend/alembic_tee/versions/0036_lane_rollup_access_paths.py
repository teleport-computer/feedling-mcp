"""TEE twin of RDS 0100: access-path provenance on lane rollups.

Revision ID: 0036_lane_rollup_access_paths
Revises: 0035_contract_rejection_stats
"""

from alembic import op


revision = "0036_lane_rollup_access_paths"
down_revision = "0035_contract_rejection_stats"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE lane_daily_rollup
    ADD COLUMN IF NOT EXISTS access_path TEXT NOT NULL DEFAULT 'unavailable',
    ADD COLUMN IF NOT EXISTS mode_source TEXT NOT NULL DEFAULT 'unavailable';

ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_access_path_values;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_access_path_values CHECK (
        access_path IN (
            'unavailable', 'apikey_v1', 'resident_v1', 'self_hosted',
            'unbound_no_route', 'hosted_unclassified_v1', 'apikey_v2',
            'v2_control_v1_source'
        )
        AND mode_source IN (
            'unavailable', 'explicit', 'default', 'not_applicable'
        )
    );

ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_pkey;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_pkey PRIMARY KEY
        (user_id, day, route, lane, enqueue_source, access_path, mode_source);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lane_daily_rollup_live_cell
    ON lane_daily_rollup (user_id, day, route, lane, enqueue_source)
    WHERE user_id <> 'deleted';

ALTER TABLE lane_rollup_watermark
    ADD COLUMN IF NOT EXISTS access_path_from TEXT;
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_access_path_format;
ALTER TABLE lane_rollup_watermark
    ADD CONSTRAINT lane_rollup_watermark_access_path_format CHECK (
        access_path_from IS NULL
        OR access_path_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
    );
"""

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0036_lane_rollup_access_paths"]'::jsonb)::text,
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
