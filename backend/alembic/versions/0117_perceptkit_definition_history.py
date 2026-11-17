"""PerceptKit persistent rule-definition history.

Revision ID: 0117_perceptkit_def_history
Revises: 0116_perceptkit_v010_storage
"""
from alembic import op


revision = "0117_perceptkit_def_history"
down_revision = "0116_perceptkit_v010_storage"
branch_labels = None
depends_on = None


_UP = r"""
CREATE TABLE IF NOT EXISTS perceptkit_definition_history (
  definition_id TEXT NOT NULL, version INT NOT NULL,
  definition JSONB NOT NULL, archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (definition_id, version)
);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Definition history is audit evidence referenced by durable Events.
    pass
