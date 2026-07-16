"""Persist reconcile-pass completion the moment reconcile finishes, so replicate
stops being starved by a reconcile that re-runs on every worker recycle.

Revision ID: 0019_tee_reconcile_state
Revises: 0018_tee_reconcile_cursors
Create Date: 2026-07-15

The sync tick is reconcile → replicate → verify → record(end). The reconcile's
"done" marker was only the end-of-tick tee_sync_runs row. On prod the ciphertext
replicate (chat 33k rows via per-row enclave decrypt) is slower than a
max_requests worker lifetime, so the tick died in replicate before recording —
every new leader then saw no completed reconcile and redid reconcile-first,
which ate each window and left replicate at ~5% forever (2026-07-15 prod:
plaintext all 100% via the resume cursor, yet chat 5% / frames 2%, tee_sync_runs
frozen since the day before).

This single-row table is stamped IMMEDIATELY after reconcile completes (before
replicate), so a mid-tick death still leaves reconcile marked done. The next
leader then skips reconcile (within the interval) and runs replicate-only ticks
that finally let the ciphertext tables converge.
"""

from alembic import op


revision = "0019_tee_reconcile_state"
down_revision = "0018_tee_reconcile_cursors"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS tee_reconcile_state (
    id               BOOLEAN PRIMARY KEY DEFAULT TRUE,
    last_success_at  TIMESTAMPTZ NOT NULL,
    CONSTRAINT tee_reconcile_state_singleton CHECK (id)
);
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tee_reconcile_state")
