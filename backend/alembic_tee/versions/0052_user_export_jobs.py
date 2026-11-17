"""User-requested asynchronous Markdown exports."""
from alembic import op

revision = "0052_user_export_jobs"
down_revision = "0051_outbox_source_fact"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS user_export_jobs (
 job_id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 email TEXT,
 status TEXT NOT NULL DEFAULT 'queued'
   CHECK(status IN ('queued','processing','completed','failed','expired')),
 object_key TEXT NOT NULL UNIQUE,
 attempts INTEGER NOT NULL DEFAULT 0,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ,
 error_code TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS user_export_jobs_one_active ON user_export_jobs(user_id)
 WHERE status IN ('queued','processing');
CREATE INDEX IF NOT EXISTS user_export_jobs_queue ON user_export_jobs(available_at,created_at)
 WHERE status='queued';
CREATE INDEX IF NOT EXISTS user_export_jobs_user ON user_export_jobs(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS user_export_jobs_expiry ON user_export_jobs(expires_at)
 WHERE status='completed';
"""

_UPDATE_PREPARED_HEAD = """UPDATE server_config SET value=convert_to(jsonb_set(convert_from(value,'UTF8')::jsonb,'{tee_heads}','["0052_user_export_jobs"]'::jsonb)::text,'UTF8') WHERE key='phase4_primary_prepared' AND COALESCE(convert_from(value,'UTF8')::jsonb->>'prepared','false')='true'"""

def upgrade():
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade():
    # Preserve queued jobs, recipients and cleanup coordinates on rollback.
    pass
