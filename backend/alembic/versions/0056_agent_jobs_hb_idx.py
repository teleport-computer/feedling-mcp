"""agent_jobs heartbeat-history partial index for the admin proactive report.

The V2 observability aggregates (db.admin_v2_heartbeat_daily and the
UNION side of admin_proactive_heartbeat_overspeed) filter
``lane='heartbeat'`` over ``created_at``, but agent_jobs only carries
active-work indexes (singleflight/claim/deadline/lease) — the table is
append-only with no retention and dominated by chat jobs, so one admin
page load was two full-table scans (codex review, 2026-07-24). Partial
index keeps it tiny: only heartbeat rows are indexed.

Revision id ≤32 chars (alembic_version VARCHAR(32)).
"""
from alembic import op

revision = "0056_agent_jobs_hb_idx"
down_revision = "0055_capture_applied_check"
branch_labels = None
depends_on = None

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_agent_jobs_hb_history "
    "ON agent_jobs (created_at, user_id) WHERE lane = 'heartbeat'"
)


def upgrade() -> None:
    op.execute(_INDEX_SQL)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_jobs_hb_history")
