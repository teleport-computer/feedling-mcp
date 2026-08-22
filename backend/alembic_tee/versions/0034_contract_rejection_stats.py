"""Content-free registry contract rejection counters.

Revision ID: 0034_contract_rejection_stats
Revises: 0033_trace_events
"""
from alembic import op


revision = "0034_contract_rejection_stats"
down_revision = "0033_trace_events"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS contract_rejection_stats (
    contract_domain TEXT NOT NULL,
    boundary TEXT NOT NULL,
    fallback TEXT NOT NULL,
    release_sha TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    total BIGINT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (contract_domain, boundary, fallback, release_sha, writer_id),
    CONSTRAINT contract_rejection_stats_nonnegative CHECK (total >= 0),
    CONSTRAINT contract_rejection_stats_seen_order CHECK (last_seen >= first_seen)
);
CREATE INDEX IF NOT EXISTS ix_contract_rejection_stats_last_seen
    ON contract_rejection_stats (last_seen DESC);
"""

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0034_contract_rejection_stats"]'::jsonb)::text,
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
