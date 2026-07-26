"""0060 Runtime V2 short-horizon wake failure backoff.

Persist the per-user failure streak and retry deadline beside the existing
wake schedule. A genuine user message lazily invalidates an older deadline via
the recorded chat sequence.

Revision ID: 0060_v2_wake_failure_backoff
"""
from alembic import op


revision = "0060_v2_wake_failure_backoff"
down_revision = "0059_v2_incident_wake_guards"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE v2_wake_schedule
  ADD COLUMN IF NOT EXISTS proactive_fail_streak INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS proactive_fail_user_seq BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS proactive_backoff_until TIMESTAMPTZ;
"""

_DOWN = """
ALTER TABLE v2_wake_schedule
  DROP COLUMN IF EXISTS proactive_backoff_until,
  DROP COLUMN IF EXISTS proactive_fail_user_seq,
  DROP COLUMN IF EXISTS proactive_fail_streak;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
