"""Persist content-free wake decision shadow observations.

Revision ID: 0085_v2_wake_shadow_decisions
Revises: 0084_wake_support_indexes
"""

from alembic import op


revision = "0085_v2_wake_shadow_decisions"
down_revision = "0084_wake_support_indexes"
branch_labels = None
depends_on = None


_SCHEMA_UP = """
CREATE TABLE IF NOT EXISTS v2_wake_shadow_decisions (
  job_id BIGINT PRIMARY KEY,
  local_day DATE NOT NULL,
  local_hour SMALLINT NOT NULL,
  local_minute SMALLINT NOT NULL,
  lane TEXT NOT NULL,
  decision_allowed BOOLEAN NOT NULL,
  apns_alert_sent BOOLEAN NOT NULL,
  decided_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_v2_wake_shadow_local_hour
    CHECK (local_hour >= 0 AND local_hour <= 23),
  CONSTRAINT ck_v2_wake_shadow_local_minute
    CHECK (local_minute >= 0 AND local_minute <= 59),
  CONSTRAINT ck_v2_wake_shadow_lane CHECK (lane <> ''),
  CONSTRAINT ck_v2_wake_shadow_alert_requires_allow
    CHECK (NOT apns_alert_sent OR decision_allowed)
);

CREATE INDEX IF NOT EXISTS ix_v2_wake_shadow_report
  ON v2_wake_shadow_decisions
  (local_day DESC, local_hour, decision_allowed, apns_alert_sent, lane);

CREATE INDEX IF NOT EXISTS ix_v2_wake_shadow_recorded_at
  ON v2_wake_shadow_decisions (recorded_at);
"""


def upgrade() -> None:
    op.execute(_SCHEMA_UP)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS v2_wake_shadow_decisions")
