"""Track a smoothed provider round-trip so a slow relay is visible.

The existing provider_health columns describe a provider that FAILS. They
cannot describe one that merely answers very slowly, because a slow answer is
still a success. A prod user ran a relay averaging 293 s for five days: every
background fold hit compaction's 60 s timeout, the backlog grew past 1200, and
no column in this table ever changed to say why.

Shadow-DB note: provider_health is on the SNAPSHOT lane, which copies the
INTERSECTION of both sides' columns (tee_shadow/snapshot.py). That makes a new
column safe in the sense that it cannot break replication — but NOT in the
sense that it can be skipped: the intersection keeps reporting `ok: true` with
the right row count while this column's data silently never reaches the shadow,
and `missing_in_tee` is the only signal. Adding a column does not trip the
"no common columns" guard the way adding a table does, so it is invisible to
snapshot failures and to CI (see TEE_POSTGRES_SHADOW_PROVISIONING.md §3).
The matching alembic_tee revision is 0009_provider_latency, in this same batch,
and it still needs a manual run — the migration workflow's secrets do not exist
yet.

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
