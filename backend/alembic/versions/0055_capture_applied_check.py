"""Relax v2_capture_batches applied-shape CHECK to tolerate a GC'd apply job.

``applied_by_job_id`` references ``agent_jobs(id) ON DELETE SET NULL``, but the
original ``ck_v2_capture_batch_applied_shape`` (0051) required it to be NOT NULL
whenever ``status='applied'``. On ``DELETE FROM users`` the ``users -> agent_jobs``
cascade (that FK's constraint was created in 0014) fires *before* the
``users -> v2_capture_batches`` cascade (0051), so the SET NULL lands on a
still-present applied row and violates the CHECK, aborting the whole
account-deletion / reset transaction. Any independent agent_jobs GC that removes
a job still referenced by an applied batch hits the same wall.

``applied_at`` is the real "applied" marker; ``applied_by_job_id`` is provenance
that may legitimately become NULL once the apply job is gone. This relaxes only
the applied branch to drop the ``applied_by_job_id IS NOT NULL`` requirement.

Revision ID: 0055_capture_applied_check
Revises: 0054_merge_pre_v2_heads
"""

from alembic import op


revision = "0055_capture_applied_check"
down_revision = "0054_merge_pre_v2_heads"
branch_labels = None
depends_on = None


_UP = r"""
ALTER TABLE v2_capture_batches
  DROP CONSTRAINT IF EXISTS ck_v2_capture_batch_applied_shape;
ALTER TABLE v2_capture_batches
  ADD CONSTRAINT ck_v2_capture_batch_applied_shape CHECK (
    (status='prepared' AND applied_by_job_id IS NULL AND applied_at IS NULL)
    OR (status='applied' AND applied_at IS NOT NULL)
  );
"""

_DOWN = r"""
ALTER TABLE v2_capture_batches
  DROP CONSTRAINT IF EXISTS ck_v2_capture_batch_applied_shape;
ALTER TABLE v2_capture_batches
  ADD CONSTRAINT ck_v2_capture_batch_applied_shape CHECK (
    (status='prepared' AND applied_by_job_id IS NULL AND applied_at IS NULL)
    OR (status='applied' AND applied_by_job_id IS NOT NULL AND applied_at IS NOT NULL)
  );
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
