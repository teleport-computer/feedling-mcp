"""v2 chat tail anchor: pinned start seq of the optional replay window per user.

Kept in its own table rather than as a column on v2_conversation_summary:
that head row is guarded by trg_v2_segmented_summary_head, which rejects any
update whose version changes without new canonical provenance (ERRCODE 55000).
An anchor advance carries no segment provenance, so it would always trip it.
"""
from alembic import op


revision = "0071_v2_chat_tail_anchor"
down_revision = "0070_tee_sync_prune"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_chat_tail_anchor (
            user_id    TEXT PRIMARY KEY
                       REFERENCES users(user_id) ON DELETE CASCADE,
            anchor_seq BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_v2_chat_tail_anchor_seq CHECK (anchor_seq >= 0)
        )
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS v2_chat_tail_anchor")
