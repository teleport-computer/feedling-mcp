"""TEE alignment for foreground screen-share grounding.

Revision ID: 0014_screen_chat_frames
"""

from alembic import op


revision = "0014_screen_chat_frames"
down_revision = "0013_merge_image_voice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE v2_wake_schedule ADD COLUMN IF NOT EXISTS "
        "last_screen_chat_frame_id TEXT"
    )
    op.execute(
        "ALTER TABLE v2_turn_metrics "
        "ADD COLUMN IF NOT EXISTS screen_frames_pushed INT NOT NULL DEFAULT 0, "
        "ADD COLUMN IF NOT EXISTS screen_frame_cache_hits INT NOT NULL DEFAULT 0, "
        "ADD COLUMN IF NOT EXISTS screen_frame_cache_misses INT NOT NULL DEFAULT 0, "
        "ADD COLUMN IF NOT EXISTS visible_reply_count INT NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE v2_turn_metrics "
        "DROP COLUMN IF EXISTS visible_reply_count, "
        "DROP COLUMN IF EXISTS screen_frame_cache_misses, "
        "DROP COLUMN IF EXISTS screen_frame_cache_hits, "
        "DROP COLUMN IF EXISTS screen_frames_pushed"
    )
    op.execute(
        "ALTER TABLE v2_wake_schedule DROP COLUMN IF EXISTS last_screen_chat_frame_id"
    )
