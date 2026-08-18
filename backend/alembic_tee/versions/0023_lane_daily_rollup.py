"""Add the frozen lane rollup cells + watermark to the TEE snapshot schema.

Revision ID: 0023_lane_daily_rollup
Revises: 0022_v2_wake_outcomes

TEE twins of RDS migration 0091 (MIRROR lane in tee_shadow/table_registry;
write-side dual-writes per frozen day, reconciler heals misses). Deliberately
NO foreign key to
``users``; on account deletion the user's cells are anonymize-merged into
``user_id='deleted'`` (Seven 2026-08-18 — not cascade-deleted, not kept
verbatim), replayed here via delete_user_data's mirror group. Once phase 2
folds in the ring-buffered user_logs sources these cells are the only
surviving record. Columns aligned one-to-one with RDS 0091.
"""

from alembic import op


revision = "0023_lane_daily_rollup"
down_revision = "0022_v2_wake_outcomes"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE lane_daily_rollup (
    user_id        TEXT NOT NULL,
    day            TEXT NOT NULL,
    route          TEXT NOT NULL,
    lane           TEXT NOT NULL,
    enqueue_source TEXT NOT NULL DEFAULT '',
    completed      INTEGER NOT NULL DEFAULT 0,
    failed         INTEGER NOT NULL DEFAULT 0,
    expired        INTEGER NOT NULL DEFAULT 0,
    superseded     INTEGER NOT NULL DEFAULT 0,
    failure_codes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    frozen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day, route, lane, enqueue_source)
);

CREATE INDEX ix_lane_daily_rollup_day ON lane_daily_rollup (day);

CREATE TABLE lane_rollup_watermark (
    route         TEXT PRIMARY KEY,
    backfill_from TEXT NOT NULL,
    through_day   TEXT NOT NULL,
    frozen_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Every new TEE head migration advances the phase4 prepared-head pin to
# itself (same statement shape as 0022), so an already-prepared TEE primary
# records which schema head it actually carries.
_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0023_lane_daily_rollup"]'::jsonb)::text,
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
