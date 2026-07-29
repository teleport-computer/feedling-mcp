"""Track a smoothed provider round-trip so a slow relay is visible.

The existing provider_health columns describe a provider that FAILS. They
cannot describe one that merely answers very slowly, because a slow answer is
still a success. A prod user ran a relay averaging 293 s for five days: every
background fold hit compaction's 60 s timeout, the backlog grew past 1200, and
no column in this table ever changed to say why.

Shadow-DB note: provider_health is on the SNAPSHOT lane, which copies the
INTERSECTION of both sides' columns and reports the difference as drift
(tee_shadow/snapshot.py). Adding a column here is therefore safe without a
matching alembic_tee revision; the column simply shows up in `missing_in_tee`
until one is written.

Revision ID: 0068_provider_latency
Revises: 0067_voice_turn_state
"""

from alembic import op


revision = "0068_provider_latency"
down_revision = "0067_voice_turn_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE provider_health
          ADD COLUMN IF NOT EXISTS recent_latency_ms DOUBLE PRECISION
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE provider_health
          DROP COLUMN IF EXISTS recent_latency_ms
        """
    )
