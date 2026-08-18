"""Restore runtime uniqueness contracts before TEE-primary operation.

Revision ID: 0012_primary_runtime_uniques
Revises: 0011_primary_runtime_bridge
Create Date: 2026-08-02

The shadow database originally omitted several uniqueness constraints because
RDS was the only writer.  A promoted TEE database is itself the concurrency and
idempotency authority: runtime ``ON CONFLICT`` statements and single-flight
claims therefore require the same unique indexes as RDS.

Index creation deliberately fails if the frozen/reconciled destination already
contains duplicates.  Silently deleting operational rows during a primary
promotion would be less safe than stopping the migration for explicit repair.
"""

from alembic import op


revision = "0012_primary_runtime_uniques"
down_revision = "0011_primary_runtime_bridge"
branch_labels = None
depends_on = None


_UP = r"""
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_jobs_singleflight
  ON agent_jobs (user_id, lane)
  WHERE status IN ('pending', 'claimed', 'running');

CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_status_events_job_error
  ON agent_status_events (job_id)
  WHERE kind = 'error' AND job_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS genesis_jobs_redistill_active_uidx
  ON genesis_import_jobs (user_id)
  WHERE source_kind = 'resident_redistill'
    AND status IN ('awaiting_resident', 'processing');

CREATE UNIQUE INDEX IF NOT EXISTS model_api_credentials_user_id_uniq
  ON model_api_credentials (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_uniq
  ON model_api_routes (credential_id, model);
CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_one_active
  ON model_api_routes (user_id) WHERE is_active;
CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_one_vision
  ON model_api_routes (user_id) WHERE is_vision;

-- This index was intentionally absent while TEE was a repairable shadow: a
-- stale token binding had to survive until reconcile prune.  As primary, this
-- database owns enroll idempotency and ON CONFLICT(device_token).
CREATE UNIQUE INDEX IF NOT EXISTS notify_relay_configs_device_token_key
  ON notify_relay_configs (device_token);

CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_capture_batch_frontier
  ON v2_capture_batches (user_id, runtime_generation, after_seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v2_summary_segment_cover
  ON v2_conversation_summary_segments (user_id, level, start_seq, end_seq);
CREATE UNIQUE INDEX IF NOT EXISTS v2_effect_outbox_enqueue_seq_unique
  ON v2_effect_outbox (enqueue_seq);
CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_trajectory_access_phase
  ON v2_trajectory_access_audit (access_id, phase);
CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_trajectory_event_idempotency
  ON v2_trajectory_events (job_id, idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_trajectory_stream_job_user
  ON v2_trajectory_streams (job_id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_turn_metrics_job
  ON v2_turn_metrics (job_id);

-- A prepared primary marker is head-bound.  Preserve the frozen-copy digests
-- while advancing only its schema head after every index above succeeds.
UPDATE server_config
SET value = convert_to(
    jsonb_set(
      convert_from(value, 'UTF8')::jsonb,
      '{tee_heads}',
      '["0012_primary_runtime_uniques"]'::jsonb
    )::text,
    'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


_DOWN = r"""
DROP INDEX IF EXISTS ux_v2_turn_metrics_job;
DROP INDEX IF EXISTS ux_v2_trajectory_stream_job_user;
DROP INDEX IF EXISTS ux_v2_trajectory_event_idempotency;
DROP INDEX IF EXISTS ux_v2_trajectory_access_phase;
DROP INDEX IF EXISTS v2_effect_outbox_enqueue_seq_unique;
DROP INDEX IF EXISTS uq_v2_summary_segment_cover;
DROP INDEX IF EXISTS ux_v2_capture_batch_frontier;
DROP INDEX IF EXISTS notify_relay_configs_device_token_key;
DROP INDEX IF EXISTS model_api_routes_one_vision;
DROP INDEX IF EXISTS model_api_routes_one_active;
DROP INDEX IF EXISTS model_api_routes_uniq;
DROP INDEX IF EXISTS model_api_credentials_user_id_uniq;
DROP INDEX IF EXISTS genesis_jobs_redistill_active_uidx;
DROP INDEX IF EXISTS ux_agent_status_events_job_error;
DROP INDEX IF EXISTS ux_agent_jobs_singleflight;

UPDATE server_config
SET value = convert_to(
    jsonb_set(
      convert_from(value, 'UTF8')::jsonb,
      '{tee_heads}',
      '["0011_primary_runtime_bridge"]'::jsonb
    )::text,
    'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
