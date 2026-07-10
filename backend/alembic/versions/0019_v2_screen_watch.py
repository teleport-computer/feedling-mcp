"""v2 screen-watch state: persist the resident's in-process last-frame id.

resident 把 `last_screen_watch_frame_id` 放在进程内存里；V2 没有 per-user 常驻进程，
不落库的话每个 scheduler tick 都会把同一帧当成「新内容」，变成 120s 一次的唤醒风暴。

Revision ID: 0019_v2_screen_watch
"""
from alembic import op

revision = "0019_v2_screen_watch"
down_revision = "0018_v2_wake_schedule"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("ALTER TABLE v2_wake_schedule ADD COLUMN IF NOT EXISTS next_screen_watch_at TIMESTAMPTZ")
    op.execute("ALTER TABLE v2_wake_schedule ADD COLUMN IF NOT EXISTS last_screen_watch_frame_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE v2_wake_schedule DROP COLUMN IF EXISTS last_screen_watch_frame_id")
    op.execute("ALTER TABLE v2_wake_schedule DROP COLUMN IF EXISTS next_screen_watch_at")
