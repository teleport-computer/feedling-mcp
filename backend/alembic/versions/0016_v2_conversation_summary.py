"""v2 conversation summary: per-user encrypted itemized summary + watermark.

The V2 hosted chat turn shows the model the full conversation as an encrypted
itemized SUMMARY (covering everything up to ``watermark_ts``) plus a verbatim
recent tail. This table is the storage for that summary row: the ENCRYPTED
summary envelope (JSONB — encryption itself happens in a later task, this
module just stores/reads the already-built envelope dict), a
``watermark_ts`` marking how far the summary covers, and a ``version`` int
for compare-and-swap so concurrent compaction writes can't silently clobber
each other. Co-locating all three in one row makes the compaction write
atomic and idempotent.

Revision ID: 0016_v2_conversation_summary
"""
from alembic import op

revision = "0016_v2_conversation_summary"
down_revision = "0015_v2_worker_heartbeats"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS v2_conversation_summary (
  user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  summary_envelope JSONB,
  watermark_ts DOUBLE PRECISION NOT NULL DEFAULT 0,
  version INT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_conversation_summary;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
