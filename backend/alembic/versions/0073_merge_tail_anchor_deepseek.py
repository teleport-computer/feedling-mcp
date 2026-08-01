"""Merge the tail-anchor and DeepSeek model-status migration heads.

Both revisions branched independently from ``0071_runtime_health_idx`` and
touch disjoint database objects.  Rejoin them so ``alembic upgrade head`` has
a single target after the two feature branches land on ``test``.

Revision ID: 0073_merge_tail_anchor_deepseek
Revises: 0072_v2_chat_tail_anchor, 0072_deepseek_text_only
"""

revision = "0073_merge_tail_anchor_deepseek"
down_revision = (
    "0072_v2_chat_tail_anchor",
    "0072_deepseek_text_only",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No schema change: this revision only rejoins two independent chains.
    pass


def downgrade() -> None:
    pass
