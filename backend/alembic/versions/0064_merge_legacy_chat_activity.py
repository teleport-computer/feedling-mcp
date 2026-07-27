"""Merge the deployed legacy chat activity revision into the current head.

Revision ID: 0064_merge_legacy_chat_activity
"""


revision = "0064_merge_legacy_chat_activity"
down_revision = (
    "0063_model_api_vision_route",
    "0059_chat_activity_lookup_idx",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
