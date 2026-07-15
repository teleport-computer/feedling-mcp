"""Persist per-table reconcile keyset cursors so a backfill resumes after
interruption instead of restarting from the first row.

Revision ID: 0018_tee_reconcile_cursors
Revises: 0017_dau_daily_snapshot
Create Date: 2026-07-15

The in-process TEE reconcile scheduler runs inside a gunicorn worker that
``max_requests`` recycles roughly every 15-30 min on a busy prod. A full
reconcile of a large table (``user_logs`` ~380k rows written one page at a time
over the Phala gateway's direct-TLS) outlasts a single worker lifetime, so
without a persisted cursor it restarted from row 1 on every recycle and never
reached the end (2026-07-14 prod: user_logs stuck ~44%, reconcile-first never
completing, every later table + the whole replicate phase starved behind it).

This table records the last-copied pk per table; ``reconciler.reconcile_table``
resumes the keyset scan from it and clears the row once that table's copy pass
finishes (so the next periodic reconcile starts fresh and re-catches updates).
"""

from alembic import op


revision = "0018_tee_reconcile_cursors"
down_revision = "0017_dau_daily_snapshot"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS tee_reconcile_cursors (
    table_name  TEXT PRIMARY KEY,
    cursor_pk   JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tee_reconcile_cursors")
