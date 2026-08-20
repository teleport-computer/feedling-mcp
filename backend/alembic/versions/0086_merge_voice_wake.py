"""Merge the pre voice-shape and test wake-index migration branches.

Revision ID: 0086_merge_voice_wake
Revises: 0085_voice_transcript_shapes, 0084_wake_support_indexes
"""

revision = "0086_merge_voice_wake"
down_revision = (
    "0085_voice_transcript_shapes",
    "0084_wake_support_indexes",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
