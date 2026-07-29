"""Remember how large a fold this conversation can actually digest.

The catch-up and maintenance folds both shrink their batch when the provider
refuses or times out, but the shrunk value is a local variable: the next job
starts at the full configured batch again and spends one guaranteed-to-fail
model call rediscovering the same limit. For a user whose content is uniformly
thin, that is one wasted call per fold — roughly 100 of them to drain a 1200
message backlog, billed to the user's own key.

Persisting the working value turns that into AIMD: multiplicative decrease on
refusal, additive increase on success, so the cost is paid once and then
amortised.

NULL means "never measured" and reads as the configured default, so existing
rows and brand-new users are unchanged.

Shadow-DB note: v2_conversation_summary is on the CIPHERTEXT lane, whose
replicator selects an explicit column list (tee_replicator/worker.py). A new
column here is simply not copied and needs no alembic_tee counterpart.

Revision ID: 0069_batch_cap
Revises: 0068_provider_latency
"""

from alembic import op


revision = "0069_batch_cap"
down_revision = "0068_provider_latency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE v2_conversation_summary
          ADD COLUMN IF NOT EXISTS effective_batch_cap INTEGER
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE v2_conversation_summary
          DROP COLUMN IF EXISTS effective_batch_cap
        """
    )
