"""Job pins expected_runtime_generation (Hosted Runtime V2 PR A / spec A3).

Each agent_jobs row records the runtime generation the enqueuer observed at
admission time. If the user's runtime generation advances past that pin before
the job is claimed (a resident<->v2 cutover happened mid-queue), the job is
stale: it was queued for a runtime that no longer owns the user. claim_next_job
detects this and marks the row terminal (`superseded`) instead of handing it to
a worker for a turn.

Revision ID: 0026_v2_job_expected_generation
"""
from alembic import op

revision = "0026_v2_job_expected_generation"
down_revision = "0025_v2_runtime_generation"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE agent_jobs
  ADD COLUMN IF NOT EXISTS expected_runtime_generation BIGINT;
"""

_DOWN = "ALTER TABLE agent_jobs DROP COLUMN IF EXISTS expected_runtime_generation;"


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
