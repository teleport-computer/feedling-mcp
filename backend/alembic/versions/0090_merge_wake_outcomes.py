"""Merge PRE history with auditable Runtime V2 wake outcomes.

Revision ID: 0090_merge_wake_outcomes
Revises: 0089_merge_pre_test_agent_jobs, 0089_v2_wake_outcomes
"""

revision = "0090_merge_wake_outcomes"
down_revision = (
    "0089_merge_pre_test_agent_jobs",
    "0089_v2_wake_outcomes",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
