"""Durable cross-worker account recovery challenges.

Revision ID: 0105_account_recover_challenges
Revises: 0104_distill_artifact_ledger

The recover challenge store was a per-process in-memory dict
(``accounts/recover.py``). Production runs FEEDLING_BACKEND_WORKERS=6, so a
challenge created on worker A was invisible to a verify request hitting worker
B → 401 ``invalid_or_expired_challenge``. This migration adds a shared Postgres
table that every worker reads/writes; verify consumes atomically via
``DELETE ... RETURNING``. It does not touch users, api keys, or any existing
data and is fully rollback-safe (downgrade only drops the new table).
"""

from alembic import op


revision = "0105_account_recover_challenges"
down_revision = "0104_distill_artifact_ledger"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE account_recover_challenges (
    challenge_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    public_key TEXT NOT NULL,
    answer_sha256 TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX ix_account_recover_challenges_user_id
    ON account_recover_challenges (user_id);
CREATE INDEX ix_account_recover_challenges_expires_at
    ON account_recover_challenges (expires_at);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS account_recover_challenges")
