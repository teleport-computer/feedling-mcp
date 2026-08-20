"""Create the append-only trace_events table (replaces the per-user blob ring).

Revision ID: 0033_trace_events
Revises: 0032_v2_job_recovery_events

The blob ring (``user_blobs`` one row = the whole ring) rewrote the entire JSON
document on every event: O(n) per write, and a hard 2500-event cap that silently
evicted the oldest events.  One row per event makes a write an O(1) INSERT, so
nothing is ever "pushed out"; retention becomes a number of days instead of a
number of events.

Seven's decisions (2026-08-20), recorded because they are not derivable
from the code:

* **TEE only.**  "prod 之后也会变成 tee 的, rds 都不用了" — so this migration
  exists solely in the TEE chain.  There is deliberately no RDS twin, which is
  why this file has no byte-identical-literal convergence test (0030 needed one
  because it had an RDS counterpart at 0081).
* **No account-deletion cascade.**  "用户把账号消除时, 他的 trace 根本不需要删除
  ...删掉并没有什么意义" — traces exist to debug problems after the fact, and a
  deleted account is exactly when you want to know what happened.  Hence
  ``user_id`` carries no foreign key: the row outlives the user.  Relying on
  ON DELETE CASCADE also has a history here — the two DDL chains have drifted
  on cascade rules before, so deletion is explicit everywhere else too.
* **Old traces are discarded, not migrated.**  "之前老的 trace 我们也可以暂时都
  不要了" — the ring only held a few hours anyway.  There is no backfill and no
  union-read transition period.

``PARTITION BY RANGE (ts)`` forces every unique constraint to contain the
partition key, so the primary key is ``(id, ts)`` rather than ``id`` alone --
``id`` is still globally unique because it draws from one shared sequence.

The DEFAULT partition is a safety net, not a design element: ``trace_event()``
is best-effort and swallows its own errors, so an INSERT that lands outside
every partition would make tracing stop **silently** — the one failure mode
this whole overhaul exists to remove.  With a catch-all, late partition
maintenance costs a slow query, never a lost event.
"""

from alembic import op


revision = "0033_trace_events"
down_revision = "0032_v2_job_recovery_events"
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
CREATE INDEX IF NOT EXISTS ix_trace_events_trace_id
  ON trace_events (trace_id, ts DESC, id DESC) WHERE trace_id <> '';
"""

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0033_trace_events"]'::jsonb)::text,
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
