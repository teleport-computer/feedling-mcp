"""genesis serve-worker claim attribution (deploy-orphan fast reclaim).

The uploaded->processing claim (db.genesis_claim_uploaded_jobs) recorded no worker
id, so a job whose worker was killed (especially by a container deploy) could only
be recovered by the 30-min time reaper. These columns let a fast death-detected
reclaim tell whose claim went stale (by comparing against live kind='genesis'
heartbeats). Distinct from the resident path's resident_* columns: that path is the
legacy agent-runner resident consumer; this is the serve-worker genesis thread.

Revision ID: 0040_genesis_worker_claim
"""
from alembic import op

revision = "0040_genesis_worker_claim"
down_revision = "0039_merge_tee_recon_state"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE genesis_import_jobs
    ADD COLUMN IF NOT EXISTS worker_claimed_by TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS worker_claimed_at TIMESTAMPTZ;
"""
_DOWN = """
ALTER TABLE genesis_import_jobs
    DROP COLUMN IF EXISTS worker_claimed_by,
    DROP COLUMN IF EXISTS worker_claimed_at;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
