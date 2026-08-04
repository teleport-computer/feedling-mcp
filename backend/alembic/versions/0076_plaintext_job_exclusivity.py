"""Enforce one processing plaintext Genesis import per user.

Revision ID: 0076_plaintext_job_exclusivity
Revises: 0075_v2_usage_rollup
"""

from alembic import op

revision = "0076_plaintext_job_exclusivity"
down_revision = "0075_v2_usage_rollup"
branch_labels = None
depends_on = None


_UP = """
WITH ranked AS (
    SELECT user_id, job_id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id ORDER BY updated_at DESC, job_id DESC
           ) AS rn
    FROM genesis_import_jobs
    WHERE status = 'processing'
      AND metadata->>'ingest' = 'plaintext'
)
UPDATE genesis_import_jobs g
SET status = 'failed',
    error = 'superseded_by_migration_0076_plaintext_exclusivity',
    updated_at = now()
FROM ranked
WHERE g.user_id = ranked.user_id
  AND g.job_id = ranked.job_id
  AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS genesis_jobs_plaintext_active_uidx
    ON genesis_import_jobs (user_id)
    WHERE status = 'processing' AND metadata->>'ingest' = 'plaintext';
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS genesis_jobs_plaintext_active_uidx")
