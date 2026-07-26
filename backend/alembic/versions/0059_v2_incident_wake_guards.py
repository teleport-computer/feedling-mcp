"""0059 Runtime V2 incident-hardened wake guards.

Persist the AI self-schedule streak beside the existing per-user wake timing
row. The last effect result makes an outbox replay idempotent: a sink retry
must not consume another streak slot.

Revision ID: 0059_v2_incident_wake_guards
"""
from alembic import op


revision = "0059_v2_incident_wake_guards"
down_revision = "0058_provider_usage_halted"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE v2_wake_schedule
  ADD COLUMN IF NOT EXISTS self_wake_streak INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS self_wake_user_seq BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS self_wake_last_effect_id TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS self_wake_last_effect_accepted BOOLEAN;
"""

_DOWN = """
ALTER TABLE v2_wake_schedule
  DROP COLUMN IF EXISTS self_wake_last_effect_accepted,
  DROP COLUMN IF EXISTS self_wake_last_effect_id,
  DROP COLUMN IF EXISTS self_wake_user_seq,
  DROP COLUMN IF EXISTS self_wake_streak;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
