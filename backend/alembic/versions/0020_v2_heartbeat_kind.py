"""Discriminate turn workers from the genesis import worker on the liveness table.

`v2_worker_heartbeats` is the chat/send admission gate, not just observability:
`workers_alive()` backs the 503 `workers_unavailable` guard and
`live_worker_count()` feeds `admission.estimate_wait_sec(workers=...)`. Once the
genesis import worker moved into the serve_worker process (it needs its own
liveness row — its thread can die while the turn loops keep beating), an
un-discriminated row would count as a turn worker and halve the estimated queue
wait. Both readers now filter `kind = 'turn'`; genesis reads `kind = 'genesis'`.

Existing rows are turn workers, hence the DEFAULT.

Revision ID: 0020_v2_heartbeat_kind
"""
from alembic import op

revision = "0020_v2_heartbeat_kind"
down_revision = "0019_v2_screen_watch"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE v2_worker_heartbeats
  ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'turn';
CREATE INDEX IF NOT EXISTS ix_v2_worker_heartbeats_kind_beat
  ON v2_worker_heartbeats (kind, beat_at DESC);
"""

_DOWN = """
DROP INDEX IF EXISTS ix_v2_worker_heartbeats_kind_beat;
ALTER TABLE v2_worker_heartbeats DROP COLUMN IF EXISTS kind;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
