"""Freeze access-path and runtime-mode provenance on lane rollup cells.

Revision ID: 0100_lane_rollup_access_paths
Revises: 0099_contract_rejection_stats

Existing cells cannot be classified retroactively: access bindings and hosted
runtime controls are mutable current state.  They therefore receive the
literal ``unavailable`` sentinel, while ``access_path_from`` marks the first
day written by the new freezer.  Readers must not interpret the sentinel (or a
day before that watermark) as measured zero.
"""

from alembic import op


revision = "0100_lane_rollup_access_paths"
down_revision = "0099_contract_rejection_stats"
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


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Rows produced after this migration may differ only by the new PK axes.
    # They cannot be represented by the old key without lying or merging
    # provenance, so discard only those new rows before restoring the schema.
    op.execute("""
DELETE FROM lane_daily_rollup
WHERE access_path <> 'unavailable' OR mode_source <> 'unavailable';
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_access_path_format;
ALTER TABLE lane_rollup_watermark DROP COLUMN IF EXISTS access_path_from;
ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_pkey;
DROP INDEX IF EXISTS uq_lane_daily_rollup_live_cell;
ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_access_path_values;
ALTER TABLE lane_daily_rollup
    DROP COLUMN IF EXISTS mode_source,
    DROP COLUMN IF EXISTS access_path;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_pkey PRIMARY KEY
        (user_id, day, route, lane, enqueue_source);
""")
