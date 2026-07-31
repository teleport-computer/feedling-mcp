"""Merge the deployed legacy chat activity revision into the test head.

Revision ID: 0064_merge_legacy_chat_activity
Revises: 0063_tee_sync_snapshot_metrics, 0059_chat_activity_lookup_idx
"""


revision = "0064_merge_legacy_chat_activity"
down_revision = (
    "0063_tee_sync_snapshot_metrics",
    "0059_chat_activity_lookup_idx",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
