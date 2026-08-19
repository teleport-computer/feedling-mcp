"""TEE twin of RDS 0093: speak/silence counters on lane_daily_rollup.

Revision ID: 0025_lane_rollup_voice
Revises: 0024_lane_rollup_safe_ts

test promoted the TEE database to primary on 2026-08-18 (deploy 82c4c019), so
the freezer runs against this chain — the columns have to exist here or the
freeze breaks. DDL is byte-identical to RDS 0093 (rationale documented there);
the shared literal is asserted equal by test_pre_test_migration_convergence.
"""

from alembic import op


revision = "0025_lane_rollup_voice"
down_revision = "0024_lane_rollup_safe_ts"
branch_labels = None
depends_on = None


# Same index as RDS 0093 — and it matters most HERE: test's primary is the TEE
# database, so this is the chain the freezer actually queries. Why the resident
# anchor carries no time window at all is documented in 0093.
_PROACTIVE_ANCHOR_INDEX = "ix_chat_messages_proactive_job"
_PROACTIVE_ANCHOR_DDL = (
    f"CREATE INDEX CONCURRENTLY {_PROACTIVE_ANCHOR_INDEX} "
    "ON chat_messages (user_id, (doc->>'proactive_job_id')) "
    "WHERE COALESCE(doc->>'proactive_job_id','') <> ''"
)


def _index_validity(name: str) -> bool | None:
    """None = absent, False = an invalid shell left by a canceled build."""
    row = op.get_bind().exec_driver_sql(
        "SELECT idx.indisvalid FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        f"WHERE cls.relkind='i' AND cls.relname='{name}' "
        "AND pg_table_is_visible(cls.oid)"
    ).fetchone()
    return None if row is None else bool(row[0])


# Byte-identical to RDS 0093's ``_UP``. Alembic revision modules are not
# importable by name (they start with a digit), so the two chains cannot share
# the literal at runtime — instead ``test_tee_migrations_reuse_the_rds_contract_sql``
# asserts the two strings are equal, the same guard 0018-0022 already use. A
# hand edit on one chain therefore fails CI rather than silently diverging.
_UP = """
ALTER TABLE lane_daily_rollup
    ADD COLUMN IF NOT EXISTS spoke             INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS spoke_completed   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS silent_declared   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS silent_undeclared INTEGER NOT NULL DEFAULT 0;

ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_voice_nonneg;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_voice_nonneg
    CHECK (spoke >= 0 AND spoke_completed >= 0
           AND silent_declared >= 0 AND silent_undeclared >= 0
           AND spoke_completed <= spoke);

-- One watermark row per route already records count coverage; voice coverage
-- starts later than the counts do (the columns did not exist before this
-- migration), so it needs its own day. NULL means "no cell has voice numbers
-- yet" and the reader must not present 0 as a measurement.
ALTER TABLE lane_rollup_watermark
    ADD COLUMN IF NOT EXISTS voice_from TEXT;
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_voice_format;
ALTER TABLE lane_rollup_watermark
    ADD CONSTRAINT lane_rollup_watermark_voice_format
    CHECK (voice_from IS NULL OR voice_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');

-- Converge a silent 0091/0023 divergence: the RDS chain created both tables
-- WITH these checks, the TEE twin created them without. Nothing caught it —
-- the TEE/RDS parity test compares constraints only for tables that appear in
-- BOTH constraint listings, and a table with zero constraints on TEE simply
-- drops out of the comparison. Adding the first TEE check above is what made
-- the table visible and the gap fall out.
--
-- This matters concretely: test promoted the TEE database to primary
-- (2026-08-18), so the unconstrained copy is the one actually being written.
-- Re-asserting by DROP-IF-EXISTS + ADD keeps this SQL byte-identical across
-- both chains while being a no-op wherever the constraint already exists.
ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_day_format;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_day_format
    CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_counts_nonneg;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_counts_nonneg
    CHECK (completed >= 0 AND failed >= 0 AND expired >= 0 AND superseded >= 0);
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_from_format;
ALTER TABLE lane_rollup_watermark
    ADD CONSTRAINT lane_rollup_watermark_from_format
    CHECK (backfill_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_through_format;
ALTER TABLE lane_rollup_watermark
    ADD CONSTRAINT lane_rollup_watermark_through_format
    CHECK (through_day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
"""

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0025_lane_rollup_voice"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    validity = _index_validity(_PROACTIVE_ANCHOR_INDEX)
    with op.get_context().autocommit_block():
        if validity is False:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_PROACTIVE_ANCHOR_INDEX}")
        if validity is not True:
            op.execute(_PROACTIVE_ANCHOR_DDL)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
