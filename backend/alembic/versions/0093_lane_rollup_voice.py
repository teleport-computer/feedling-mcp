"""Speak/silence counters on lane_daily_rollup (Seven 2026-08-18 ruling).

Revision ID: 0093_lane_rollup_voice
Revises: 0092_lane_rollup_safe_ts
Create Date: 2026-08-18

The failure rate alone cannot tell "the machinery broke" apart from "the agent
chose to stay quiet": a wake turn that runs cleanly and says nothing is
``completed``, exactly like one that spoke. Seven's ruling puts two numbers
side by side on the same denominator (terminated attempts):

    failure rate = failed / terminated        -- is the machinery healthy
    speak rate   = spoke  / terminated        -- is the agent's choice right

``spoke`` is anchored on OUTPUT, never on a status field (the standing rule
from the broadcast=on-yet-zero-frames case: a status column describes a
different channel and can drift away from what was actually produced):

- route='model_api' — the attempt has at least one ``v2_effect_outbox`` row of
  a reply effect type in a delivered status. Predicate imported from
  ``v2/effect_outbox.py`` (REPLY_EFFECT_TYPES / DELIVERED_EFFECT_STATUSES),
  never hand-copied. Note this is the WIDE set: an intermediate bubble is
  still the agent speaking. jobs_store's chat *delivery* rate uses a
  deliberately NARROWER set and the two must not be conflated.
- route='resident' — a ``chat_messages`` row carrying
  ``doc->>'proactive_job_id'`` for that job (chat_core.py writes it into the
  message extra), so V1 attributes delivery per attempt at the same strength
  as V2 rather than falling back to ``status='posted'``.

Silence is then split, because "silent" hides two very different things:

- ``silent_declared``   — terminated ok, no output, the model explicitly chose
  silence (V2: ``agent_jobs.wake_result='sleep'``, written only when the model
  called the stay-silent tool).
- ``silent_undeclared`` — terminated ok, no output, NO declaration. This is a
  measured BLIND SPOT, not a clean category: for every wake lane except
  ``scheduled``, ``require_reply=False`` means the empty-reply detection never
  runs (worker.py), so "the provider returned nothing" is currently
  indistinguishable from "the model produced no text". Quantifying it is the
  point — the speak rate ships with the size of its own blind spot next to it
  instead of silently absorbing it. When the planned silent_empty_response
  trace lands, it subdivides THIS column; the schema does not change.

``spoke_failed`` keeps the decomposition exact for attempts that delivered a
bubble and then failed, giving the load-bearing invariant:

    completed = (spoke - spoke_failed) + silent_declared + silent_undeclared

Backfill is deliberately absent: existing frozen cells keep 0 in the new
columns rather than being retro-computed, because the sources they were frozen
from (V1 ring buffer) may already have rolled off, and a silent 0 that means
"not measured" is exactly the confusion coverage.partial_before exists to
prevent. The read side reports voice coverage from its own watermark.
"""

from alembic import op


revision = "0093_lane_rollup_voice"
down_revision = "0092_lane_rollup_safe_ts"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE lane_daily_rollup
    ADD COLUMN IF NOT EXISTS spoke             INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS spoke_failed      INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS silent_declared   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS silent_undeclared INTEGER NOT NULL DEFAULT 0;

ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_voice_nonneg;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_voice_nonneg
    CHECK (spoke >= 0 AND spoke_failed >= 0
           AND silent_declared >= 0 AND silent_undeclared >= 0
           AND spoke_failed <= spoke);

-- One watermark row per route already records count coverage; voice coverage
-- starts later than the counts do (the columns did not exist before this
-- migration), so it needs its own day. NULL means "no cell has voice numbers
-- yet" and the reader must not present 0 as a measurement.
ALTER TABLE lane_rollup_watermark
    ADD COLUMN IF NOT EXISTS voice_from TEXT;
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_voice_format;
ALTER TABLE lane_rollup_watermark
    ADD CONSTRAINT lane_rollup_watermark_voice_format
    CHECK (voice_from IS NULL OR voice_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');

-- Converge a silent 0091/0023 divergence: the RDS chain created both tables
-- WITH these checks, the TEE twin created them without. Nothing caught it —
-- the TEE/RDS parity test compares constraints only for tables that appear in
-- BOTH constraint listings, and a table with zero constraints on TEE simply
-- drops out of the comparison. Adding the first TEE check above is what made
-- the table visible and the gap fall out.
--
-- This matters concretely: test promoted the TEE database to primary
-- (2026-08-18), so the unconstrained copy is the one actually being written.
-- Re-asserting by DROP-IF-EXISTS + ADD keeps this SQL byte-identical across
-- both chains while being a no-op wherever the constraint already exists.
ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_day_format;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_day_format
    CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_counts_nonneg;
ALTER TABLE lane_daily_rollup
    ADD CONSTRAINT lane_daily_rollup_counts_nonneg
    CHECK (completed >= 0 AND failed >= 0 AND expired >= 0 AND superseded >= 0);
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_from_format;
ALTER TABLE lane_rollup_watermark
    ADD CONSTRAINT lane_rollup_watermark_from_format
    CHECK (backfill_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_through_format;
ALTER TABLE lane_rollup_watermark
    ADD CONSTRAINT lane_rollup_watermark_through_format
    CHECK (through_day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
"""

_DOWN = """
ALTER TABLE lane_rollup_watermark
    DROP CONSTRAINT IF EXISTS lane_rollup_watermark_voice_format;
ALTER TABLE lane_rollup_watermark DROP COLUMN IF EXISTS voice_from;
ALTER TABLE lane_daily_rollup
    DROP CONSTRAINT IF EXISTS lane_daily_rollup_voice_nonneg;
ALTER TABLE lane_daily_rollup
    DROP COLUMN IF EXISTS silent_undeclared,
    DROP COLUMN IF EXISTS silent_declared,
    DROP COLUMN IF EXISTS spoke_failed,
    DROP COLUMN IF EXISTS spoke;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
