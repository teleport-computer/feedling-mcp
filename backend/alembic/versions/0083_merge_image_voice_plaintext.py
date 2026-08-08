"""Merge image/voice and optional-plaintext migration heads.

Revision ID: 0083_merge_image_voice_plaintext
Revises: 0082_merge_image_voice, 0079_merge_admin_plaintext
"""

revision = "0083_merge_image_voice_plaintext"
down_revision = (
    "0082_merge_image_voice",
    "0079_merge_admin_plaintext",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
