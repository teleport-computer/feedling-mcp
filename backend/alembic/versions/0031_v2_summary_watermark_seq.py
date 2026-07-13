"""0031 v2 summary watermark_seq: `v2_conversation_summary` gains a
``watermark_seq BIGINT`` column alongside the existing ``watermark_ts``.

PR A built (but never wired) a stable seq cursor (``cursor.py``:
``load_seq``/``advance_effect``/``CURSOR_KEY``) anchored on
``chat_messages.seq`` — a real monotonic identity-column counter, unlike
wall-clock ``ts`` (two messages appended in the same instant can share an
identical ``ts``, which makes a ts-only boundary ambiguous under ties).
This migration is the first step of wiring that into production (PR D
Task 9 / D5): it gives the compaction watermark a seq column so later
consumers (the prompt-invariant catch-up check, the GC coverage gate) can
bound "what the summary covers" by seq instead of ts.

Scope note: this migration ONLY adds the column. The full reply-cursor /
turn-read-boundary seq migration (``since = last_replied_ts`` -> seq,
``v2_reply_cursor_seq`` outbox wiring) is deliberately deferred to a later
task — see the D5 task write-up. Existing rows default to 0; ``worker.py``'s
compaction now writes a real ``watermark_seq`` on every subsequent write, and
``db.seq_for_watermark_ts`` gives a conservative (strictly-less) one-time
ts->seq translation for rows that still carry the migration default.

Revision ID: 0031_v2_summary_watermark_seq
"""
from alembic import op

revision = "0031_v2_summary_watermark_seq"
down_revision = "0030_v2_runtime_control"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE v2_conversation_summary
  ADD COLUMN IF NOT EXISTS watermark_seq BIGINT NOT NULL DEFAULT 0;
"""

_DOWN = """
ALTER TABLE v2_conversation_summary DROP COLUMN IF EXISTS watermark_seq;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
