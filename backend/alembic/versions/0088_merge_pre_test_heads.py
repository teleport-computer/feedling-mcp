"""Merge the PRE voice/wake chain with the Runtime V2 activation chain.

Revision ID: 0088_merge_pre_test_heads
Revises: 0086_merge_voice_wake, 0087_v2_first_chat_activation
"""


revision = "0088_merge_pre_test_heads"
down_revision = ("0086_merge_voice_wake", "0087_v2_first_chat_activation")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
