"""Merge image-generation and voice-transcript TEE heads.

Revision ID: 0013_merge_image_voice
Revises: 0010_image_generation_cols, 0012_voice_transcripts
"""

revision = "0013_merge_image_voice"
down_revision = (
    "0010_image_generation_cols",
    "0012_voice_transcripts",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
