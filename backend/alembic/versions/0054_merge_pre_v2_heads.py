"""Merge the Runtime-V2 lifecycle-closure head into the current pre head.

Two independent chains branched off `0049_merge_test_pre_heads`:

* `pre` advanced `0050_v2_web_halted_columns` -> `0051_web_settings_backfill`
  -> `0052_dual_runtime_coexistence` -> `0053_merge_redistill_v2` (its head).
* the Runtime-V2 release-closure branch advanced
  `0050_v2_trajectory_access_audit` -> `0051_v2_capture_batches` ->
  `0052_chat_clear_archive` (its head).

Merging the release-closure branch into `pre` therefore leaves two alembic
heads, which makes `alembic upgrade head` non-linearizable and fails the
single-head deploy gate. This is a no-op merge revision that rejoins them into
a single head. The two chains touch disjoint objects — the web/dual-runtime
columns and V1 supervisor tables vs. the trajectory-audit, capture-batch, and
chat-archive tables plus the archive-aware R2 cleanup trigger — so either
application order is safe.

Revision ID: 0054_merge_pre_v2_heads
Revises: 0053_merge_redistill_v2, 0052_chat_clear_archive
"""

revision = "0054_merge_pre_v2_heads"
down_revision = ("0053_merge_redistill_v2", "0052_chat_clear_archive")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No schema change: this revision only rejoins two independent chains.
    pass


def downgrade() -> None:
    pass
