"""hosted runtime v2: durable jobs + action queue + status events + runtime_state.

DB-backed 工作队列地基（子项目 B）。agent_jobs 支持 FOR UPDATE SKIP LOCKED claim
+ per-user/lane single-flight 唯一索引（coalesce 的强制约束）。加密不变：canonical
长期态仍是加密 chat_messages/memory；runtime_state 只存非敏感 digest。

Revision ID: 0014_hosted_runtime_v2
"""
from alembic import op

revision = "0014_hosted_runtime_v2"
# Keep the historical parent. Some deployed databases are already stamped at
# 0020_v2_heartbeat_kind; rewriting this revision's parent would make Alembic
# believe the newly-added profiles branch had run when it had not. A dedicated
# merge revision after both heads preserves upgradeability for old and fresh DBs.
down_revision = "0013_genesis_resident_claim"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS agent_jobs (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  lane TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  trace_id TEXT,
  priority INT NOT NULL DEFAULT 0,
  attempt_count INT NOT NULL DEFAULT 0,
  last_error TEXT,
  claimed_by TEXT,
  invalidated_by_job_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  deadline_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_jobs_singleflight
  ON agent_jobs(user_id, lane) WHERE status IN ('pending','claimed','running');
CREATE INDEX IF NOT EXISTS ix_agent_jobs_claim
  ON agent_jobs(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS agent_action_queue (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  seq INT NOT NULL,
  type TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  visible BOOL NOT NULL DEFAULT false,
  requires_model_authorship BOOL NOT NULL DEFAULT false,
  result_json JSONB,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_action_queue_job ON agent_action_queue(job_id, seq);

CREATE TABLE IF NOT EXISTS agent_status_events (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  label TEXT,
  detail_json JSONB NOT NULL DEFAULT '{}',
  seq INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_status_events_user
  ON agent_status_events(user_id, id DESC);

CREATE TABLE IF NOT EXISTS runtime_state (
  user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  state_json JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = """
DROP TABLE IF EXISTS agent_status_events;
DROP TABLE IF EXISTS agent_action_queue;
DROP TABLE IF EXISTS runtime_state;
DROP TABLE IF EXISTS agent_jobs;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
