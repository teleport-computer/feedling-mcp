"""v2 turn metrics: append-only per-turn token/latency samples.

Feeds the §6 admission-ceiling read `jobs_store.recent_mean_tokens_per_turn`
and the `/v1/admin/v2-metrics` endpoint (later task). One row per completed
turn — provider usage counts + wall-clock latency, no ciphertext, no FK
(append-only, best-effort instrumentation; a metrics row surviving past its
job/user is harmless and simplifies write paths that fire after the job's
own bookkeeping already happened).

Revision ID: 0017_v2_turn_metrics
"""
from alembic import op

revision = "0017_v2_turn_metrics"
down_revision = "0016_v2_conversation_summary"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS v2_turn_metrics (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT,
  user_id TEXT,
  lane TEXT,
  prompt_tokens INT,
  completion_tokens INT,
  latency_ms INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_turn_metrics;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
