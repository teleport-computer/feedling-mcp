"""Content-free registry contract rejection counters.

Revision ID: 0099_contract_rejection_stats
Revises: 0098_v1_lane_outcome_counts
"""
from alembic import op


revision = "0099_contract_rejection_stats"
down_revision = "0098_v1_lane_outcome_counts"
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


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS contract_rejection_stats")
