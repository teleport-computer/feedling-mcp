"""Merge the tee-reconcile cursor head back into the Hosted Runtime V2 chain.

``0032_merge_tee_v2`` joined the tee-pg shadow chain (at
``0016_tee_sync_table_failures``) with the Hosted Runtime V2 chain (at
``0031_v2_summary_watermark_seq``). After that merge landed on the feature
branch, ``test`` extended the tee-pg shadow chain further —
``0016_tee_sync_table_failures`` → ``0017_dau_daily_snapshot`` →
``0018_tee_reconcile_cursors`` — which forks off at ``0016`` and so becomes a
second head once ``feat/hosted-runtime-v2`` is rebased onto ``test``.

This joins the two heads (``0032_merge_tee_v2`` and
``0018_tee_reconcile_cursors``) into one. Neither the dau-snapshot /
tee-reconcile tables nor the V2 tables touch the other, so the merge is a pure
no-op join — same pattern as ``0021_merge_v2_profiles`` and
``0032_merge_tee_v2``. A DB stamped at either head executes the missing sibling
chain before arriving here.

Revision ID: 0033_merge_tee_reconcile
"""

revision = "0033_merge_tee_reconcile"
down_revision = ("0032_merge_tee_v2", "0018_tee_reconcile_cursors")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
