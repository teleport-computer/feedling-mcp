"""TEE twin of RDS 0094: per-user per-Beijing-day chat rollup cells.

Revision ID: 0026_chat_daily_rollup
Revises: 0025_lane_rollup_voice

test promoted the TEE database to primary on 2026-08-18 (deploy 82c4c019), so
the freeze runs against this chain — the tables have to exist here or it
breaks. DDL is byte-identical to RDS 0094; what a cell means, and why freezing
is sound for a historical question but would NOT be sound for a live-set
question, is documented there.
"""

from alembic import op


revision = "0026_chat_daily_rollup"
down_revision = "0025_lane_rollup_voice"
branch_labels = None
depends_on = None


# Byte-identical to RDS 0094's ``_UP``. Alembic revision modules are not
# importable by name (they start with a digit), so the two chains cannot share
# the literal at runtime — ``test_tee_migrations_reuse_the_rds_contract_sql``
# asserts the two strings are equal instead, the same guard 0018-0025 use.
_UP = """
CREATE TABLE IF NOT EXISTS chat_daily_rollup (
    user_id                  TEXT NOT NULL,
    day                      TEXT NOT NULL,
    total                    INTEGER NOT NULL DEFAULT 0,
    user_messages            INTEGER NOT NULL DEFAULT 0,
    agent_messages           INTEGER NOT NULL DEFAULT 0,
    image_messages           INTEGER NOT NULL DEFAULT 0,
    proactive_messages       INTEGER NOT NULL DEFAULT 0,
    model_api_user_messages  INTEGER NOT NULL DEFAULT 0,
    model_api_agent_messages INTEGER NOT NULL DEFAULT 0,
    model_api_greetings      INTEGER NOT NULL DEFAULT 0,
    first_ts                 DOUBLE PRECISION,
    last_ts                  DOUBLE PRECISION,
    proactive_last_ts        DOUBLE PRECISION,
    last_user_ts             DOUBLE PRECISION,
    last_agent_ts            DOUBLE PRECISION,
    by_role                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    by_source                JSONB NOT NULL DEFAULT '{}'::jsonb,
    by_content_type          JSONB NOT NULL DEFAULT '{}'::jsonb,
    live_activity_status     JSONB NOT NULL DEFAULT '{}'::jsonb,
    alert_status             JSONB NOT NULL DEFAULT '{}'::jsonb,
    frozen_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day),
    CONSTRAINT chat_daily_rollup_day_format
        CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT chat_daily_rollup_counts_nonneg
        CHECK (total >= 0 AND user_messages >= 0 AND agent_messages >= 0
               AND image_messages >= 0 AND proactive_messages >= 0
               AND model_api_user_messages >= 0
               AND model_api_agent_messages >= 0
               AND model_api_greetings >= 0)
);

CREATE INDEX IF NOT EXISTS ix_chat_daily_rollup_day
    ON chat_daily_rollup (day);

-- Coverage watermark, separate from lane_rollup_watermark on purpose: the two
-- sources lose history in different ways, so one shared bound would be wrong
-- for both. ``user_logs`` is ring-buffered, so its oldest days are simply gone.
-- ``chat_messages`` has no ring, but it is not an intact archive either — a
-- history clear moves rows out of the live set. Precise meaning of
-- ``backfill_from``: cells can be built from the oldest row still present at
-- the time of the first freeze. Anything cleared before that first run was
-- never counted and is honestly reported as outside coverage rather than as
-- a real zero.
CREATE TABLE IF NOT EXISTS chat_rollup_watermark (
    scope         TEXT PRIMARY KEY,
    backfill_from TEXT NOT NULL,
    through_day   TEXT NOT NULL,
    frozen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chat_rollup_watermark_from_format
        CHECK (backfill_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT chat_rollup_watermark_through_format
        CHECK (through_day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
);
"""

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0026_chat_daily_rollup"]'::jsonb)::text,
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
