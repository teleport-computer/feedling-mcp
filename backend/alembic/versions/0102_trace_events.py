"""Create append-only trace event storage on the RDS-primary chain.

Revision ID: 0102_trace_events
Revises: 0101_chat_change_events

Production remains RDS-primary while the TEE PostgreSQL transport is being
reworked.  The flow-trace reader and writer use the selected primary pool, so
RDS and TEE must carry the same table, partition, and index contract.  Keep
``_UP`` byte-identical to ``alembic_tee/0033_trace_events.py``; the migration
convergence test fails if either chain drifts.

The table deliberately has no account foreign key.  Trace rows survive account
deletion for the bounded incident window and are retired by partition age or an
explicit operator clear.  Historical blob-ring rows are not backfilled.
"""

from alembic import op


revision = "0102_trace_events"
down_revision = "0101_chat_change_events"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS trace_events (
  id BIGSERIAL,
  user_id TEXT NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  subsystem TEXT NOT NULL,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  outcome_class TEXT NOT NULL DEFAULT 'operational_failure',
  actor TEXT NOT NULL DEFAULT 'backend',
  lane TEXT,
  trace_id TEXT,
  turn_id TEXT,
  job_id TEXT,
  provider TEXT,
  model TEXT,
  enqueue_source TEXT,
  summary TEXT,
  explain TEXT,
  detail JSONB,
  content_excerpt JSONB,
  dur_ms DOUBLE PRECISION,
  PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS trace_events_default
  PARTITION OF trace_events DEFAULT;

-- Pre-create a window of daily partitions.  This is not an optimisation: a row
-- that lands in DEFAULT permanently blocks creating the partition covering its
-- day ("would be violated by some row"), so that day can never be reclaimed by
-- DROP PARTITION and DEFAULT grows without bound.  The catch-all keeps writes
-- alive; only pre-creation keeps retention working.  A non-empty DEFAULT
-- therefore means partition maintenance has fallen behind and must alarm --
-- recovering costs a detach, a row move, and a re-attach.
DO $$
DECLARE
  d date;
  lower_bound timestamptz;
  upper_bound timestamptz;
BEGIN
  FOR d IN
    SELECT generate_series(
      (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date - 29,
      (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date + 60,
      '1 day'
    )::date
  LOOP
    -- Partition days are product Beijing days, independent of the owner
    -- connection's mutable TimeZone setting.
    lower_bound := d::timestamp AT TIME ZONE 'Asia/Shanghai';
    upper_bound := (d + 1)::timestamp AT TIME ZONE 'Asia/Shanghai';
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS trace_events_p%s '
      'PARTITION OF trace_events FOR VALUES FROM (%L) TO (%L)',
      to_char(d, 'YYYYMMDD'), lower_bound, upper_bound);
  END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS ix_trace_events_user_ts
  ON trace_events (user_id, ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_trace_events_ts
  ON trace_events (ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_trace_events_trace_id
  ON trace_events (trace_id, ts DESC, id DESC) WHERE trace_id <> '';
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # RDS migrations support the repository's downgrade/replay validation.
    # Dropping the partitioned parent removes its partitions and indexes; this
    # is intentionally destructive and operators must preserve trace evidence
    # before rolling application code back across this revision.
    op.execute(
        "DROP TABLE IF EXISTS trace_events CASCADE"
    )
