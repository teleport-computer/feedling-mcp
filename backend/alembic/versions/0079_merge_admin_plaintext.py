"""Merge the admin-index and plaintext/perception migration branches.

Revision ID: 0079_merge_admin_plaintext
Revises: 0078_admin_dashboard_indexes, 0078_merge_plaintext_perception
"""


revision = "0079_merge_admin_plaintext"
down_revision = (
    "0078_admin_dashboard_indexes",
    "0078_merge_plaintext_perception",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
