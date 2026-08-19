"""Per-user per-Beijing-day chat rollup cells.

Revision ID: 0094_chat_daily_rollup
Revises: 0093_lane_rollup_voice
Create Date: 2026-08-18

``admin_data_track_snapshot`` aggregates every chat row of every user at READ
time with no time window (five separate full-history GROUP BYs), which is why
the JSON endpoint measured 99-115s on prod and dies on gunicorn's 120s timeout.
These cells move that work to a nightly freeze.

WHAT A CELL MEANS — read this before using the numbers
------------------------------------------------------
A cell records **how much happened on that day**, not **how much is still in
the live set now**. Once a day is frozen it is never revisited, so a later
history clear does not retroactively shrink it.

This is a deliberate product ruling (Seven 2026-08-19), not an implementation
shortcut. The reasoning: an admin surface is there to show whether someone is
using the product. If a user clears their history and the dashboard silently
drops 500 messages, it now says "this user barely used it" — which is false.
Activity that happened, happened.

The read side MUST label these columns as cumulative ("累计发生"), never as a
current inventory ("当前条数"). Same number, two different questions; the label
is the only thing that keeps them apart.

WHY "FROZEN" IS SAFE HERE — and why that is NOT the usual argument
-------------------------------------------------------------------
``lane_daily_rollup`` may freeze because its SOURCE is closed: a terminal
``agent_jobs`` row never changes after its day ends. That argument does NOT
transfer to chat — ``chat_messages`` can lose rows (``chat_clear`` empties the
live set; ``chat_delete`` removes single rows, though today its only callers
are our own housekeeping: verify-ping GC in chat/chat_core.py and voice
cleanup, and verify_ping rows are already excluded from user_messages).

These cells are frozen for a different reason: the QUESTION is historical.
"How many messages happened on 2026-08-18" has a fixed answer regardless of
what is deleted afterwards. So ON CONFLICT DO NOTHING is correct here, but do
not justify it by claiming the source is immutable — it is not. If a future
change makes these cells answer "what is in the live set now", the freeze is
no longer sound and a change-tracking mechanism becomes mandatory, because a
deletion cannot be discovered by polling the source afterwards (the row is
simply gone; no later scan can see that it ever existed).

Deliberately NO foreign key to ``users``, matching lane_daily_rollup; account
deletion removes rows explicitly. Rows are content-free — counts, sanitized
bucket keys and timestamps only.
"""

from alembic import op


revision = "0094_chat_daily_rollup"
down_revision = "0093_lane_rollup_voice"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS chat_daily_rollup (
    user_id                  TEXT NOT NULL,
    day                      TEXT NOT NULL,
    total                    INTEGER NOT NULL DEFAULT 0,
    user_messages            INTEGER NOT NULL DEFAULT 0,
    agent_messages           INTEGER NOT NULL DEFAULT 0,
    image_messages           INTEGER NOT NULL DEFAULT 0,
    proactive_messages       INTEGER NOT NULL DEFAULT 0,
    model_api_user_messages  INTEGER NOT NULL DEFAULT 0,
    model_api_agent_messages INTEGER NOT NULL DEFAULT 0,
    model_api_greetings      INTEGER NOT NULL DEFAULT 0,
    first_ts                 DOUBLE PRECISION,
    last_ts                  DOUBLE PRECISION,
    proactive_last_ts        DOUBLE PRECISION,
    last_user_ts             DOUBLE PRECISION,
    last_agent_ts            DOUBLE PRECISION,
    by_role                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    by_source                JSONB NOT NULL DEFAULT '{}'::jsonb,
    by_content_type          JSONB NOT NULL DEFAULT '{}'::jsonb,
    live_activity_status     JSONB NOT NULL DEFAULT '{}'::jsonb,
    alert_status             JSONB NOT NULL DEFAULT '{}'::jsonb,
    frozen_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day),
    CONSTRAINT chat_daily_rollup_day_format
        CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT chat_daily_rollup_counts_nonneg
        CHECK (total >= 0 AND user_messages >= 0 AND agent_messages >= 0
               AND image_messages >= 0 AND proactive_messages >= 0
               AND model_api_user_messages >= 0
               AND model_api_agent_messages >= 0
               AND model_api_greetings >= 0)
);

CREATE INDEX IF NOT EXISTS ix_chat_daily_rollup_day
    ON chat_daily_rollup (day);

-- Coverage watermark, separate from lane_rollup_watermark on purpose: the two
-- sources lose history in different ways, so one shared bound would be wrong
-- for both. ``user_logs`` is ring-buffered, so its oldest days are simply gone.
-- ``chat_messages`` has no ring, but it is not an intact archive either — a
-- history clear moves rows out of the live set. Precise meaning of
-- ``backfill_from``: cells can be built from the oldest row still present at
-- the time of the first freeze. Anything cleared before that first run was
-- never counted and is honestly reported as outside coverage rather than as
-- a real zero.
CREATE TABLE IF NOT EXISTS chat_rollup_watermark (
    scope         TEXT PRIMARY KEY,
    backfill_from TEXT NOT NULL,
    through_day   TEXT NOT NULL,
    frozen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chat_rollup_watermark_from_format
        CHECK (backfill_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT chat_rollup_watermark_through_format
        CHECK (through_day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
);
"""

_DOWN = """
DROP TABLE IF EXISTS chat_rollup_watermark;
DROP INDEX IF EXISTS ix_chat_daily_rollup_day;
DROP TABLE IF EXISTS chat_daily_rollup;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
