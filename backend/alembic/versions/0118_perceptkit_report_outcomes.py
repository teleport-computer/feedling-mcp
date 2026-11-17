"""Persist replayable per-observation PerceptKit Report outcomes.

Revision ID: 0118_perceptkit_report_outcomes
Revises: 0117_perceptkit_def_history
"""
from alembic import op


revision = "0118_perceptkit_report_outcomes"
down_revision = "0117_perceptkit_def_history"
branch_labels = None
depends_on = None


_UP = r"""
ALTER TABLE perceptkit_ingest_receipt
  ADD COLUMN IF NOT EXISTS observations_rejected JSONB NOT NULL DEFAULT '[]'::jsonb;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Report outcome evidence is immutable audit/replay state. Preserve it.
    pass
