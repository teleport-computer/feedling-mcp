"""tee-pg: record SNAPSHOT-lane metrics on tee_sync_runs

Revision ID: 0062_tee_sync_snapshot_metrics
Revises: 0061_v2_adaptive_tail_metrics
Create Date: 2026-07-28

Task 6 wired the SNAPSHOT lane (backend/tee_shadow/snapshot.py — full-table
TRUNCATE+COPY atomic replace) into the scheduler's per-tick summary
(``snapshot_copied`` / ``snapshot_failures``), but those two keys never made it
into ``db._TEE_SYNC_RUN_COLS`` — so today they only ever show up in the log
line, never as a queryable column on ``tee_sync_runs`` (unlike the sibling
``reconcile_copied`` / ``replicate_copied`` counters). This closes that gap:
column here, ``_TEE_SYNC_RUN_COLS`` entry in the same commit.

``tee_sync_runs`` is the sync mechanism's own control-plane/history table
(registered SKIP lane in ``tee_shadow.table_registry`` — it lives in RDS, not
mirrored to the TEE it's monitoring), so this is an RDS-side (``alembic``, not
``alembic_tee``) revision, same as 0015/0016/0019 before it.

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0062_tee_sync_snapshot_metrics"
down_revision = "0061_v2_adaptive_tail_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tee_sync_runs "
        "ADD COLUMN IF NOT EXISTS snapshot_copied   INTEGER, "
        "ADD COLUMN IF NOT EXISTS snapshot_failures INTEGER"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE tee_sync_runs "
        "DROP COLUMN IF EXISTS snapshot_copied, "
        "DROP COLUMN IF EXISTS snapshot_failures"
    )
