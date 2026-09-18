"""Derived plaintext vectors; primary/TEE DDL is identical. No backfill."""
from alembic import op

revision = "0111_memory_vectors"
down_revision = "0110_divergence_observed_at"
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


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Rollback disables the scanner/reader; leave data for explicit cleanup.
    pass
