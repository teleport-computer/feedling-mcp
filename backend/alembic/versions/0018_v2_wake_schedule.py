"""v2 wake schedule: per-user next-due proactive wake times + BYOK payment cooldown.

Feeds the D3 scheduler's `jobs_store.due_heartbeat_users` read — a later task
polls this table for users whose `next_heartbeat_at` has come due (and are not
in a `payment_cooldown_until` window from a prior BYOK payment failure) and
enqueues `heartbeat` lane jobs for them. One row per user (FK CASCADE — deleted
with the user), upserted by whichever code path just advanced that user's
schedule (never inserted per-wake; there is exactly one row per user).

Revision ID: 0018_v2_wake_schedule
"""
from alembic import op

revision = "0018_v2_wake_schedule"
down_revision = "0017_v2_turn_metrics"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS v2_wake_schedule (
  user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  next_heartbeat_at TIMESTAMPTZ,
  next_capture_at TIMESTAMPTZ,
  payment_cooldown_until TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_wake_schedule;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
