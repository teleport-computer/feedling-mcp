"""Merge screen-chat and optional-plaintext migration heads.

Revision ID: 0084_merge_screen_plaintext
Revises: 0083_screen_chat_frames, 0079_merge_admin_plaintext
"""

revision = "0084_merge_screen_plaintext"
down_revision = (
    "0083_screen_chat_frames",
    "0079_merge_admin_plaintext",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
