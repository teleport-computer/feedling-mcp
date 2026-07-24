"""Merge the deployed Runtime V2 head with test's tee-reconcile-state head.

The Runtime V2 lineage advanced to ``0038_v2_prompt_cache_metrics`` (…→ 0036
chat R2 lifecycle → 0037 terminal-failure outbox → 0038 prompt-cache metrics).
Separately, when ``pre`` was rebased onto ``test``, ``test`` had extended the
tee shadow lineage with ``0019_tee_reconcile_state`` (forked off
``0018_tee_reconcile_cursors``, which is already joined into the V2 lineage via
``0033_merge_tee_reconcile`` / ``0035_merge_v2_tee_reconcile``).

That leaves two valid deployed heads. Reparenting either would rewrite deployed
history, so this no-op merge (unrelated tables — tee reconcile-state vs the V2
tables) lets a database stamped at either head execute the missing sibling
lineage before reaching one common head. Same pattern as
``0033_merge_tee_reconcile`` / ``0035_merge_v2_tee_reconcile``.

Revision ID: 0039_merge_tee_recon_state
"""

revision = "0039_merge_tee_recon_state"
down_revision = (
    "0038_v2_prompt_cache_metrics",
    "0019_tee_reconcile_state",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
