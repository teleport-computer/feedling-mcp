"""Per-user per-lane per-Beijing-day frozen job-outcome rollup cells.

Revision ID: 0091_lane_daily_rollup
Revises: 0090_merge_wake_outcomes
Create Date: 2026-08-18

The admin data-track surface computes per-user aggregates at READ time over
the full history of every user (db.admin_data_track_snapshot), so "keep more
history" directly means "read slower" — measured on prod 2026-08-18: the JSON
endpoints take 99-115s and die on gunicorn's 120s timeout. Meanwhile the V1
``user_logs`` streams are ring-buffered (proactive_jobs keeps only the newest
500 rows per user), so heartbeat history is silently rolling off today.

``lane_daily_rollup`` splits recording from reading, same pattern as
``retention_cohort_snapshot`` (0021, "frozen cells win"): a single-leader
scheduler freezes one immutable cell per (user, Beijing day, route, lane,
enqueue_source) after the day closes; readers only touch cells. Cells are
tiny counts and are kept forever — after the source ring buffer rolls off,
the frozen numbers survive, so the dashboard outlives the raw data.

Deliberately NO foreign key to ``users`` — but per-user rows do NOT outlive
the account either (the 0021 precedent is an ANONYMOUS cohort aggregate, so
"frozen cells win" alone doesn't justify keeping deleted users' ids —
codex2 2026-08-18). Ruling (Seven 2026-08-18): on account deletion the
user's cells are additively merged into ``user_id='deleted'`` rows
(db.lane_rollup_anonymize_user, wired into delete_user_data) — aggregate
history stays true while the per-user linkage dies with the account. Rows
are content-free: counts plus sanitized failure-code identifiers only.

Column vocabulary (aligned with the trace-overhaul plan, 2026-08-18):
- ``lane`` is an OPEN set passed through verbatim from the source table —
  never a hand-copied enum (the first draft enum missed 3 of 9 live lanes).
- ``route`` is the runtime family that produced the record ('model_api' for
  agent_jobs; 'resident'/V1 sources land in a later phase).
- ``enqueue_source`` is only meaningful for heartbeat ('clock' when
  agent_jobs.reason IS NULL — serve_worker's hourly tick — else
  'perception'); every other lane stores ''.
"""

from alembic import op


revision = "0091_lane_daily_rollup"
down_revision = "0090_merge_wake_outcomes"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS lane_daily_rollup (
    user_id        TEXT NOT NULL,
    day            TEXT NOT NULL,
    route          TEXT NOT NULL,
    lane           TEXT NOT NULL,
    enqueue_source TEXT NOT NULL DEFAULT '',
    completed      INTEGER NOT NULL DEFAULT 0,
    failed         INTEGER NOT NULL DEFAULT 0,
    expired        INTEGER NOT NULL DEFAULT 0,
    superseded     INTEGER NOT NULL DEFAULT 0,
    failure_codes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    frozen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day, route, lane, enqueue_source),
    CONSTRAINT lane_daily_rollup_day_format
        CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT lane_daily_rollup_counts_nonneg
        CHECK (completed >= 0 AND failed >= 0 AND expired >= 0 AND superseded >= 0)
);

CREATE INDEX IF NOT EXISTS ix_lane_daily_rollup_day
    ON lane_daily_rollup (day);

-- Coverage watermark: one row per route source. ``backfill_from`` is the
-- oldest day the cells can honestly represent (data before it predates the
-- source table or was already ring-buffered away — "genuinely zero" and
-- "eaten by the ring" must stay distinguishable, so the read endpoint
-- returns this as coverage.partial_before). ``through_day`` is the newest
-- fully frozen day; it only advances AFTER that day's cells are inserted,
-- so a crash between the two steps re-freezes idempotently (ON CONFLICT
-- DO NOTHING on the cells).
CREATE TABLE IF NOT EXISTS lane_rollup_watermark (
    route         TEXT PRIMARY KEY,
    backfill_from TEXT NOT NULL,
    through_day   TEXT NOT NULL,
    frozen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT lane_rollup_watermark_from_format
        CHECK (backfill_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT lane_rollup_watermark_through_format
        CHECK (through_day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
);
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lane_rollup_watermark")
    op.execute("DROP TABLE IF EXISTS lane_daily_rollup")
