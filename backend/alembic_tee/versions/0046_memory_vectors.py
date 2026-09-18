"""Derived plaintext vectors; primary/TEE DDL is identical. No backfill."""
from alembic import op

revision = "0046_memory_vectors"
down_revision = "0045_account_recover_challenges"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    user_id TEXT NOT NULL,
    moment_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    projection_hash TEXT NOT NULL,
    dim INT NOT NULL CHECK (dim > 0),
    vector BYTEA NOT NULL CHECK (octet_length(vector) = dim * 4),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, moment_id, model_id)
);
CREATE INDEX IF NOT EXISTS memory_vectors_user_model_idx ON memory_vectors(user_id, model_id);
"""


_UPDATE_PREPARED_HEAD = """UPDATE server_config SET value=convert_to(
        jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
        '["0046_memory_vectors"]'::jsonb)::text, 'UTF8')
        WHERE key='phase4_primary_prepared'
        AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared','false')='true'"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    # Rollback disables the scanner/reader; leave data for explicit cleanup.
    pass
