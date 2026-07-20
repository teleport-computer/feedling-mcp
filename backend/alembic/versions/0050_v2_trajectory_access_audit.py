"""Update-immutable break-glass trajectory access audit.

Revision ID: 0050_v2_trajectory_access_audit
Revises: 0049_merge_test_pre_heads
"""

from alembic import op


revision = "0050_v2_trajectory_access_audit"
down_revision = "0049_merge_test_pre_heads"
branch_labels = None
depends_on = None


_UP = r"""
CREATE TABLE IF NOT EXISTS v2_trajectory_access_audit (
  id BIGSERIAL PRIMARY KEY,
  access_id UUID NOT NULL,
  phase TEXT NOT NULL,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  -- Intentionally not an FK: a denied request for a nonexistent or
  -- wrong-owner job must still leave an audit record.
  job_id BIGINT NOT NULL,
  operator_id TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  case_ref TEXT NOT NULL,
  event_count INTEGER,
  result_code TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT ux_v2_trajectory_access_phase UNIQUE (access_id, phase),
  CONSTRAINT ck_v2_trajectory_access_phase
    CHECK (phase IN ('requested','succeeded','failed')),
  CONSTRAINT ck_v2_trajectory_access_job CHECK (job_id > 0),
  CONSTRAINT ck_v2_trajectory_access_operator
    CHECK (operator_id ~ '^[A-Za-z0-9][A-Za-z0-9._@:-]{2,79}$'),
  CONSTRAINT ck_v2_trajectory_access_reason
    CHECK (reason_code IN ('incident','support','security','debug')),
  CONSTRAINT ck_v2_trajectory_access_case
    CHECK (case_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:/#-]{2,119}$'),
  CONSTRAINT ck_v2_trajectory_access_result
    CHECK (result_code ~ '^[a-z][a-z0-9_]{0,79}$'),
  CONSTRAINT ck_v2_trajectory_access_event_count
    CHECK (event_count IS NULL OR event_count BETWEEN 1 AND 100000),
  CONSTRAINT ck_v2_trajectory_access_phase_shape CHECK (
    (phase='requested' AND event_count IS NULL AND result_code='pending')
    OR (phase='succeeded' AND event_count IS NOT NULL AND result_code='ok')
    OR (phase='failed' AND event_count IS NULL AND result_code <> 'pending')
  )
);

CREATE INDEX IF NOT EXISTS ix_v2_trajectory_access_user_job_created
  ON v2_trajectory_access_audit (user_id, job_id, created_at DESC);

CREATE OR REPLACE FUNCTION v2_reject_trajectory_access_audit_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'v2 trajectory access audit rows are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_v2_trajectory_access_audit_immutable
  ON v2_trajectory_access_audit;
CREATE TRIGGER trg_v2_trajectory_access_audit_immutable
BEFORE UPDATE ON v2_trajectory_access_audit
FOR EACH ROW EXECUTE FUNCTION v2_reject_trajectory_access_audit_update();
"""


_DOWN = """
DROP TRIGGER IF EXISTS trg_v2_trajectory_access_audit_immutable
  ON v2_trajectory_access_audit;
DROP FUNCTION IF EXISTS v2_reject_trajectory_access_audit_update();
DROP TABLE IF EXISTS v2_trajectory_access_audit;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
