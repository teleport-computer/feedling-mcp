"""0029 whole-turn metric: v2_turn_metrics gains model_calls/retries/failed/status
+ UNIQUE(job_id) so one idempotent row per job (spec B5). Dedups any pre-existing
duplicate job_id rows (keep newest) before adding the constraint — the table is
best-effort instrumentation, so dropping stale dupes is safe.

Revision ID: 0029_v2_turn_metrics_whole_turn
"""
from alembic import op

revision = "0029_v2_turn_metrics_whole_turn"
down_revision = "0028_v2_effect_sink_applied"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS model_calls INT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS retries INT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS failed BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS status TEXT")
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("DELETE FROM v2_turn_metrics a USING v2_turn_metrics b "
               "WHERE a.job_id = b.job_id AND a.id < b.id")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_turn_metrics_job ON v2_turn_metrics(job_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ux_v2_turn_metrics_job")
    for col in ("model_calls", "retries", "failed", "status", "updated_at"):
        op.execute(f"ALTER TABLE v2_turn_metrics DROP COLUMN IF EXISTS {col}")
