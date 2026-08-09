"""Merge image-generation and voice-call migration heads.

Revision ID: 0082_merge_image_voice
Revises: 0073_image_generation_route, 0081_voice_call_sessions
"""

revision = "0082_merge_image_voice"
down_revision = (
    "0073_image_generation_route",
    "0081_voice_call_sessions",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
