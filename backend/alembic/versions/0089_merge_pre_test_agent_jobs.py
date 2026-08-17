"""Merge PRE convergence with the durable agent-jobs claim fence.

Revision ID: 0089_merge_pre_test_agent_jobs
Revises: 0088_merge_pre_test_heads, 0088_agent_jobs_available_at
"""


revision = "0089_merge_pre_test_agent_jobs"
down_revision = (
    "0088_merge_pre_test_heads",
    "0088_agent_jobs_available_at",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
