"""Merge plaintext storage with the latest Runtime V2 usage chain.

The pre branch carries the optional-plaintext constraint widening through
``0074_merge_plaintext_tail``.  The test branch independently advanced from
``0073_merge_tail_anchor_deepseek`` through the usage and job-exclusivity
migrations.  Rejoin both heads without rewriting either already-deployed
history.

Revision ID: 0077_merge_plaintext_usage
Revises: 0074_merge_plaintext_tail, 0076_plaintext_job_exclusivity
"""


revision = "0077_merge_plaintext_usage"
down_revision = (
    "0074_merge_plaintext_tail",
    "0076_plaintext_job_exclusivity",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
