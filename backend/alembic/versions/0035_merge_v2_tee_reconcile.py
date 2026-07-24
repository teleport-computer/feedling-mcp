"""Merge the deployed Runtime V2 and tee-reconcile heads.

``0033_v2_seq_cursor_effect_order`` was deployed on top of
``0032_merge_tee_v2`` and is followed by the forward-repair migration
``0034_v2_legacy_sink_reconcile``. Separately, the tee shadow lineage grew
through ``0017_dau_daily_snapshot`` and ``0018_tee_reconcile_cursors`` before
``0033_merge_tee_reconcile`` joined it back to ``0032_merge_tee_v2``.

Those histories therefore have two valid deployed heads. Reparenting either
one would rewrite deployed history, so this no-op merge lets a database stamped
at either head execute the missing sibling lineage before reaching one common
head.

Revision ID: 0035_merge_v2_tee_reconcile
"""

revision = "0035_merge_v2_tee_reconcile"
down_revision = (
    "0034_v2_legacy_sink_reconcile",
    "0033_merge_tee_reconcile",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
