"""Universal sink-level exactly-once dedup guard (Hosted Runtime V2 PR A / spec A6).

The outbox applier (`effect_outbox.apply_pending_effects`) is rerunnable: a
crash between "sink write landed" and "row flipped to applied" replays the
same row, calling `dispatch` again with the same `effect_id`. Rather than
give each of the 7 sinks (reply/status/cursor/job/memory/identity/schedule)
its own per-table effect_id column, one shared claim table gives sink-level
exactly-once for ALL of them: the FIRST dispatch for a given `effect_id`
claims the row (`INSERT ... ON CONFLICT DO NOTHING` returns True) and
proceeds to the real write; every replay after that gets False and no-ops.

Revision ID: 0028_v2_effect_sink_applied
"""
from alembic import op

revision = "0028_v2_effect_sink_applied"
down_revision = "0027_v2_effect_outbox"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS v2_effect_sink_applied (
    effect_id  TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_effect_sink_applied;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
