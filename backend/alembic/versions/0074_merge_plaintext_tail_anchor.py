"""Merge optional plaintext storage with the latest test migration chain.

``0072_relax_v2_envelope_shape`` belongs to PR #131 and branched from
``0071_runtime_health_idx``.  The latest test branch independently merged its
tail-anchor and DeepSeek heads into ``0073_merge_tail_anchor_deepseek``.  Rejoin
those two heads so database-backed tests and deployments can target one head.

Revision ID: 0074_merge_plaintext_tail
Revises: 0072_relax_v2_envelope_shape, 0073_merge_tail_anchor_deepseek
"""

revision = "0074_merge_plaintext_tail"
down_revision = (
    "0072_relax_v2_envelope_shape",
    "0073_merge_tail_anchor_deepseek",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No schema change: both parent migrations touch disjoint constraints/tables.
    pass


def downgrade() -> None:
    pass
