"""Merge plaintext-storage and durable-perception migration branches.

Revision ID: 0078_merge_plaintext_perception
Revises: 0077_merge_plaintext_usage, 0077_perception_signal_state_v2
"""


revision = "0078_merge_plaintext_perception"
down_revision = (
    "0077_merge_plaintext_usage",
    "0077_perception_signal_state_v2",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
