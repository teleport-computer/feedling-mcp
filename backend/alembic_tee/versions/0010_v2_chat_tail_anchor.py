"""Add v2_chat_tail_anchor to the TEE SNAPSHOT lane.

Revision ID: 0008_v2_chat_tail_anchor
Revises: 0007_chat_activity_snapshot
Create Date: 2026-07-29

Mirrors backend/alembic/versions/0068_v2_chat_tail_anchor.py on the RDS side.
``anchor_seq`` is a plain monotonic integer (no envelope), so the TEE copy is
a direct column-for-column replica, same as ``v2_runtime_state``.
"""

from alembic import op


revision = "0010_v2_chat_tail_anchor"
down_revision = "0009_provider_latency"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS v2_chat_tail_anchor (
    user_id    TEXT NOT NULL,
    anchor_seq BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_chat_tail_anchor;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
