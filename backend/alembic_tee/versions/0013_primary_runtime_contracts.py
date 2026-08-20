"""Complete TEE-primary checks, foreign keys, and runtime indexes.

Revision ID: 0013_primary_runtime_contracts
Revises: 0012_primary_runtime_uniques
Create Date: 2026-08-02

The original shadow schema copied table shapes but intentionally did not carry
every writer-side constraint or query index.  Once TEE is ``DATABASE_URL`` it
must enforce the same validity/cascade rules and support the same hot paths as
RDS.  The migration validates existing rows while adding constraints and fails
closed if the frozen/reconciled copy is not promotable.
"""

from alembic import op


revision = "0013_primary_runtime_contracts"
down_revision = "0012_primary_runtime_uniques"
branch_labels = None
depends_on = None


_UP = r"""
ALTER TABLE chat_message_archive
  ADD CONSTRAINT ck_chat_message_archive_clear_generation CHECK (clear_generation > 0),
  ADD CONSTRAINT ck_chat_message_archive_source_seq CHECK (source_seq > 0),
  ADD CONSTRAINT ck_chat_message_archive_storage_generation CHECK (storage_generation >= 0);

ALTER TABLE chat_turn_activity_events
  ADD CONSTRAINT chat_turn_activity_events_user_id_turn_id_fkey
  FOREIGN KEY (user_id, turn_id) REFERENCES chat_messages(user_id, msg_id)
  ON DELETE CASCADE;

ALTER TABLE model_api_routes
  ADD CONSTRAINT model_api_routes_context_window_tokens_check
  CHECK (context_window_tokens IS NULL OR
         context_window_tokens >= 2048 AND context_window_tokens <= 2000000),
  ADD CONSTRAINT model_api_routes_credential_fkey
  FOREIGN KEY (user_id, credential_id)
  REFERENCES model_api_credentials(user_id, id) ON DELETE CASCADE;

ALTER TABLE provider_health
  ADD CONSTRAINT provider_health_provider_state_check
  CHECK (provider_state IN ('ok', 'needs_user_action'));

ALTER TABLE v2_capture_batches
  ADD CONSTRAINT ck_v2_capture_batch_actions
    CHECK (jsonb_typeof(actions_json) = 'array' AND
           jsonb_array_length(actions_json) = action_count),
  ADD CONSTRAINT ck_v2_capture_batch_applied_shape
    CHECK ((status = 'prepared' AND applied_by_job_id IS NULL AND applied_at IS NULL)
        OR (status = 'applied' AND applied_at IS NOT NULL)),
  ADD CONSTRAINT ck_v2_capture_batch_count
    CHECK (action_count >= 0 AND action_count <= 20),
  ADD CONSTRAINT ck_v2_capture_batch_seq
    CHECK (after_seq >= 0 AND through_seq > after_seq),
  ADD CONSTRAINT ck_v2_capture_batch_status
    CHECK (status IN ('prepared', 'applied')),
  ADD CONSTRAINT v2_capture_batches_applied_by_job_id_fkey
    FOREIGN KEY (applied_by_job_id) REFERENCES agent_jobs(id) ON DELETE SET NULL,
  ADD CONSTRAINT v2_capture_batches_prepared_by_job_id_fkey
    FOREIGN KEY (prepared_by_job_id) REFERENCES agent_jobs(id) ON DELETE SET NULL;

ALTER TABLE v2_chat_tail_anchor
  ADD CONSTRAINT ck_v2_chat_tail_anchor_seq CHECK (anchor_seq >= 0);

ALTER TABLE v2_conversation_summary_segments
  ADD CONSTRAINT ck_v2_summary_segment_children
    CHECK ((level = 0 AND cardinality(child_segment_ids) = 0)
        OR (level > 0 AND cardinality(child_segment_ids) > 0)),
  ADD CONSTRAINT ck_v2_summary_segment_coverage
    CHECK ((coverage_kind = 'exact' AND start_seq > 0 AND end_seq >= start_seq
            AND source_message_count > 0 AND legacy_opaque_through_seq = 0)
        OR (coverage_kind = 'legacy_opaque' AND start_seq = 0
            AND end_seq >= legacy_opaque_through_seq
            AND legacy_opaque_through_seq >= 0 AND source_message_count >= 0)),
  ADD CONSTRAINT ck_v2_summary_segment_format CHECK (format_version = 1),
  ADD CONSTRAINT ck_v2_summary_segment_level CHECK (level >= 0);

ALTER TABLE v2_effect_outbox
  ADD CONSTRAINT ck_v2_effect_input_frontier
  CHECK (input_frontier_seq IS NULL OR input_frontier_seq >= 0);

ALTER TABLE v2_mcp_mutation_attempts
  ADD CONSTRAINT ck_v2_mcp_mutation_frontier CHECK (input_frontier_seq >= 0),
  ADD CONSTRAINT ck_v2_mcp_mutation_outcome
    CHECK (outcome IS NULL OR outcome IN ('known', 'unknown')),
  ADD CONSTRAINT v2_mcp_mutation_attempts_job_id_fkey
    FOREIGN KEY (job_id) REFERENCES agent_jobs(id) ON DELETE CASCADE;

ALTER TABLE v2_sandbox_usage_events
  ADD CONSTRAINT ck_v2_sandbox_duration CHECK (duration_ms IS NULL OR duration_ms >= 0),
  ADD CONSTRAINT ck_v2_sandbox_outcome_nonempty CHECK (length(outcome) BETWEEN 1 AND 40),
  ADD CONSTRAINT ck_v2_sandbox_provider_nonempty CHECK (length(provider) BETWEEN 1 AND 80),
  ADD CONSTRAINT ck_v2_sandbox_purpose_nonempty CHECK (length(purpose) BETWEEN 1 AND 80);

ALTER TABLE v2_terminal_failure_outbox
  ADD CONSTRAINT v2_terminal_failure_outbox_error_code_check
    CHECK (error_code <> '' AND length(error_code) <= 120),
  ADD CONSTRAINT v2_terminal_failure_outbox_job_id_fkey
    FOREIGN KEY (job_id) REFERENCES agent_jobs(id) ON DELETE CASCADE,
  ADD CONSTRAINT v2_terminal_failure_outbox_runtime_error_attempt_count_check
    CHECK (runtime_error_attempt_count >= 0),
  ADD CONSTRAINT v2_terminal_failure_outbox_status_attempt_count_check
    CHECK (status_attempt_count >= 0);

ALTER TABLE v2_trajectory_access_audit
  ADD CONSTRAINT ck_v2_trajectory_access_case
    CHECK (case_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:/#-]{2,119}$'),
  ADD CONSTRAINT ck_v2_trajectory_access_event_count
    CHECK (event_count IS NULL OR event_count BETWEEN 1 AND 100000),
  ADD CONSTRAINT ck_v2_trajectory_access_job CHECK (job_id > 0),
  ADD CONSTRAINT ck_v2_trajectory_access_operator
    CHECK (operator_id ~ '^[A-Za-z0-9][A-Za-z0-9._@:-]{2,79}$'),
  ADD CONSTRAINT ck_v2_trajectory_access_phase
    CHECK (phase IN ('requested', 'succeeded', 'failed')),
  ADD CONSTRAINT ck_v2_trajectory_access_phase_shape
    CHECK ((phase = 'requested' AND event_count IS NULL AND result_code = 'pending')
        OR (phase = 'succeeded' AND event_count IS NOT NULL AND result_code = 'ok')
        OR (phase = 'failed' AND event_count IS NULL AND result_code <> 'pending')),
  ADD CONSTRAINT ck_v2_trajectory_access_reason
    CHECK (reason_code IN ('incident', 'support', 'security', 'debug')),
  ADD CONSTRAINT ck_v2_trajectory_access_result
    CHECK (result_code ~ '^[a-z][a-z0-9_]{0,79}$');

ALTER TABLE v2_trajectory_reviews
  ADD CONSTRAINT ck_v2_trajectory_review_attempts CHECK (attempt_count BETWEEN 0 AND 3),
  ADD CONSTRAINT ck_v2_trajectory_review_envelope CHECK (
    review_envelope IS NULL OR (
      jsonb_typeof(review_envelope) = 'object'
      AND review_envelope ? 'owner_user_id'
      AND review_envelope ? 'id'
      AND review_envelope ? 'visibility'
      AND jsonb_typeof(review_envelope->'owner_user_id') = 'string'
      AND jsonb_typeof(review_envelope->'id') = 'string'
      AND jsonb_typeof(review_envelope->'visibility') = 'string'
      AND review_envelope->>'owner_user_id' = user_id
      AND review_envelope->>'visibility' = 'shared'
      AND length(review_envelope->>'id') > 0
      AND (
        (
          review_envelope ? 'body_ct' AND review_envelope ? 'nonce'
          AND review_envelope ? 'K_user' AND review_envelope ? 'K_enclave'
          AND review_envelope ? 'v'
          AND jsonb_typeof(review_envelope->'body_ct') = 'string'
          AND jsonb_typeof(review_envelope->'nonce') = 'string'
          AND jsonb_typeof(review_envelope->'K_user') = 'string'
          AND jsonb_typeof(review_envelope->'K_enclave') = 'string'
          AND jsonb_typeof(review_envelope->'v') = 'number'
          AND length(review_envelope->>'body_ct') > 0
          AND length(review_envelope->>'nonce') > 0
          AND length(review_envelope->>'K_user') > 0
          AND length(review_envelope->>'K_enclave') > 0
          AND review_envelope - ARRAY[
            'v','id','owner_user_id','visibility','body_ct','nonce','K_user',
            'K_enclave','enclave_pk_fpr','content_pk_fpr'
          ] = '{}'::jsonb
        ) OR (
          review_envelope ? 'body'
          AND jsonb_typeof(review_envelope->'body') = 'string'
          AND NOT review_envelope ? 'body_ct'
          AND review_envelope - ARRAY['id','owner_user_id','visibility','body']
              = '{}'::jsonb
        )
      )
    )
  ),
  ADD CONSTRAINT ck_v2_trajectory_review_status
    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  ADD CONSTRAINT v2_trajectory_reviews_claimed_by_job_id_fkey
    FOREIGN KEY (claimed_by_job_id) REFERENCES agent_jobs(id) ON DELETE SET NULL,
  ADD CONSTRAINT v2_trajectory_reviews_source_job_id_fkey
    FOREIGN KEY (source_job_id) REFERENCES agent_jobs(id) ON DELETE CASCADE;

ALTER TABLE v2_trajectory_streams
  ADD CONSTRAINT ck_v2_trajectory_next_index CHECK (next_event_index >= 0),
  ADD CONSTRAINT v2_trajectory_streams_job_id_fkey
    FOREIGN KEY (job_id) REFERENCES agent_jobs(id) ON DELETE CASCADE;

ALTER TABLE v2_workspace_entries
  ADD CONSTRAINT ck_v2_workspace_kind
    CHECK (kind IN ('artifact', 'workspace', 'working_memory', 'skill')),
  ADD CONSTRAINT ck_v2_workspace_path
    CHECK (length(path) BETWEEN 2 AND 512 AND left(path, 1) = '/'),
  ADD CONSTRAINT ck_v2_workspace_revision CHECK (revision > 0);

CREATE INDEX IF NOT EXISTS ix_action_queue_job ON agent_action_queue (job_id, seq);
CREATE INDEX IF NOT EXISTS ix_agent_jobs_active_lease ON agent_jobs (lease_expires_at)
  WHERE status IN ('claimed', 'running');
CREATE INDEX IF NOT EXISTS ix_agent_jobs_chat_terminal_finished
  ON agent_jobs (finished_at DESC, id DESC)
  WHERE lane = 'chat' AND status IN ('completed','failed','expired','superseded');
CREATE INDEX IF NOT EXISTS ix_agent_jobs_claim
  ON agent_jobs (status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS ix_agent_jobs_created_at ON agent_jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_agent_jobs_hb_history ON agent_jobs (created_at, user_id)
  WHERE lane = 'heartbeat';
CREATE INDEX IF NOT EXISTS ix_agent_jobs_pending_queue_deadline
  ON agent_jobs (queue_deadline_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_agent_jobs_terminal_finished_at
  ON agent_jobs (finished_at DESC)
  WHERE status IN ('completed','failed','expired','superseded');
CREATE INDEX IF NOT EXISTS ix_agent_jobs_user_chat_failure_finished
  ON agent_jobs (user_id, finished_at DESC, id DESC)
  WHERE lane = 'chat' AND status IN ('failed','expired');
CREATE INDEX IF NOT EXISTS ix_agent_jobs_user_lane_trace
  ON agent_jobs (user_id, lane, trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_agent_status_events_job_id_id
  ON agent_status_events (job_id, id);
CREATE INDEX IF NOT EXISTS ix_status_events_user ON agent_status_events (user_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_chat_turn_activity_events_user_turn_id
  ON chat_turn_activity_events (user_id, turn_id, id);
CREATE INDEX IF NOT EXISTS dau_daily_snapshot_frozen_at_idx
  ON dau_daily_snapshot (frozen_at DESC);
CREATE INDEX IF NOT EXISTS ix_provider_health_state ON provider_health (provider_state)
  WHERE provider_state = 'needs_user_action';
CREATE INDEX IF NOT EXISTS ix_v2_capture_batches_pending
  ON v2_capture_batches (user_id, runtime_generation, after_seq)
  WHERE status = 'prepared';
CREATE INDEX IF NOT EXISTS ix_v2_summary_segments_user_range
  ON v2_conversation_summary_segments (user_id, start_seq, end_seq, level);
CREATE INDEX IF NOT EXISTS ix_v2_effect_dispatch_pending_v0041
  ON v2_effect_outbox (user_id, enqueue_seq)
  WHERE status IN ('pending', 'pending_fenced_v1');
CREATE INDEX IF NOT EXISTS ix_v2_effect_user_frontier
  ON v2_effect_outbox (user_id, input_frontier_seq)
  WHERE input_frontier_seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS v2_effect_outbox_pending
  ON v2_effect_outbox (user_id, enqueue_seq) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_v2_mcp_mutation_user_frontier
  ON v2_mcp_mutation_attempts (user_id, input_frontier_seq);
CREATE INDEX IF NOT EXISTS ix_v2_sandbox_usage_user_time
  ON v2_sandbox_usage_events (user_id, acquired_at DESC);
CREATE INDEX IF NOT EXISTS v2_terminal_failure_runtime_pending_idx
  ON v2_terminal_failure_outbox
  (runtime_error_next_attempt_at, runtime_error_last_attempt_at, created_at, job_id)
  WHERE runtime_error_delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS v2_terminal_failure_status_pending_idx
  ON v2_terminal_failure_outbox
  (status_next_attempt_at, status_last_attempt_at, created_at, job_id)
  WHERE status_delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_v2_trajectory_access_user_job_created
  ON v2_trajectory_access_audit (user_id, job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_v2_trajectory_events_user_created
  ON v2_trajectory_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_v2_trajectory_reviews_active ON v2_trajectory_reviews (status)
  WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS ix_v2_trajectory_reviews_claimed
  ON v2_trajectory_reviews (claimed_by_job_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS ix_v2_trajectory_reviews_pending
  ON v2_trajectory_reviews (user_id, created_at, source_job_id)
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_v2_turn_metrics_cache_proof
  ON v2_turn_metrics
  (lane, provider, model, cache_route_fingerprint, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_v2_turn_metrics_created_at
  ON v2_turn_metrics (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_v2_turn_metrics_lane_created_at
  ON v2_turn_metrics (lane, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_v2_turn_metrics_user_id ON v2_turn_metrics (user_id);
CREATE INDEX IF NOT EXISTS ix_v2_worker_heartbeats_kind_beat
  ON v2_worker_heartbeats (kind, beat_at DESC);
CREATE INDEX IF NOT EXISTS ix_v2_workspace_entries_user_kind_path
  ON v2_workspace_entries (user_id, kind, path);

UPDATE server_config
SET value = convert_to(
    jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
              '["0013_primary_runtime_contracts"]'::jsonb)::text,
    'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


_CONSTRAINTS = {
    "chat_message_archive": (
        "ck_chat_message_archive_clear_generation",
        "ck_chat_message_archive_source_seq",
        "ck_chat_message_archive_storage_generation",
    ),
    "chat_turn_activity_events": ("chat_turn_activity_events_user_id_turn_id_fkey",),
    "model_api_routes": (
        "model_api_routes_context_window_tokens_check",
        "model_api_routes_credential_fkey",
    ),
    "provider_health": ("provider_health_provider_state_check",),
    "v2_capture_batches": (
        "ck_v2_capture_batch_actions", "ck_v2_capture_batch_applied_shape",
        "ck_v2_capture_batch_count", "ck_v2_capture_batch_seq",
        "ck_v2_capture_batch_status", "v2_capture_batches_applied_by_job_id_fkey",
        "v2_capture_batches_prepared_by_job_id_fkey",
    ),
    "v2_chat_tail_anchor": ("ck_v2_chat_tail_anchor_seq",),
    "v2_conversation_summary_segments": (
        "ck_v2_summary_segment_children", "ck_v2_summary_segment_coverage",
        "ck_v2_summary_segment_format", "ck_v2_summary_segment_level",
    ),
    "v2_effect_outbox": ("ck_v2_effect_input_frontier",),
    "v2_mcp_mutation_attempts": (
        "ck_v2_mcp_mutation_frontier", "ck_v2_mcp_mutation_outcome",
        "v2_mcp_mutation_attempts_job_id_fkey",
    ),
    "v2_sandbox_usage_events": (
        "ck_v2_sandbox_duration", "ck_v2_sandbox_outcome_nonempty",
        "ck_v2_sandbox_provider_nonempty", "ck_v2_sandbox_purpose_nonempty",
    ),
    "v2_terminal_failure_outbox": (
        "v2_terminal_failure_outbox_error_code_check",
        "v2_terminal_failure_outbox_job_id_fkey",
        "v2_terminal_failure_outbox_runtime_error_attempt_count_check",
        "v2_terminal_failure_outbox_status_attempt_count_check",
    ),
    "v2_trajectory_access_audit": (
        "ck_v2_trajectory_access_case", "ck_v2_trajectory_access_event_count",
        "ck_v2_trajectory_access_job", "ck_v2_trajectory_access_operator",
        "ck_v2_trajectory_access_phase", "ck_v2_trajectory_access_phase_shape",
        "ck_v2_trajectory_access_reason", "ck_v2_trajectory_access_result",
    ),
    "v2_trajectory_reviews": (
        "ck_v2_trajectory_review_attempts", "ck_v2_trajectory_review_envelope",
        "ck_v2_trajectory_review_status", "v2_trajectory_reviews_claimed_by_job_id_fkey",
        "v2_trajectory_reviews_source_job_id_fkey",
    ),
    "v2_trajectory_streams": (
        "ck_v2_trajectory_next_index", "v2_trajectory_streams_job_id_fkey",
    ),
    "v2_workspace_entries": (
        "ck_v2_workspace_kind", "ck_v2_workspace_path", "ck_v2_workspace_revision",
    ),
}

_INDEXES = (
    "ix_action_queue_job", "ix_agent_jobs_active_lease",
    "ix_agent_jobs_chat_terminal_finished", "ix_agent_jobs_claim",
    "ix_agent_jobs_created_at", "ix_agent_jobs_hb_history",
    "ix_agent_jobs_pending_queue_deadline", "ix_agent_jobs_terminal_finished_at",
    "ix_agent_jobs_user_chat_failure_finished", "ix_agent_jobs_user_lane_trace",
    "ix_agent_status_events_job_id_id", "ix_status_events_user",
    "ix_chat_turn_activity_events_user_turn_id", "dau_daily_snapshot_frozen_at_idx",
    "ix_provider_health_state", "ix_v2_capture_batches_pending",
    "ix_v2_summary_segments_user_range", "ix_v2_effect_dispatch_pending_v0041",
    "ix_v2_effect_user_frontier", "v2_effect_outbox_pending",
    "ix_v2_mcp_mutation_user_frontier", "ix_v2_sandbox_usage_user_time",
    "v2_terminal_failure_runtime_pending_idx", "v2_terminal_failure_status_pending_idx",
    "ix_v2_trajectory_access_user_job_created", "ix_v2_trajectory_events_user_created",
    "ix_v2_trajectory_reviews_active", "ix_v2_trajectory_reviews_claimed",
    "ix_v2_trajectory_reviews_pending", "ix_v2_turn_metrics_cache_proof",
    "ix_v2_turn_metrics_created_at", "ix_v2_turn_metrics_lane_created_at",
    "ix_v2_turn_metrics_user_id", "ix_v2_worker_heartbeats_kind_beat",
    "ix_v2_workspace_entries_user_kind_path",
)


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    for index in _INDEXES:
        op.execute(f'DROP INDEX IF EXISTS "{index}"')
    for table, constraints in _CONSTRAINTS.items():
        for constraint in constraints:
            op.execute(
                f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{constraint}"'
            )
    op.execute(
        """
        UPDATE server_config
        SET value = convert_to(
            jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
                      '["0012_primary_runtime_uniques"]'::jsonb)::text,
            'UTF8'
        )
        WHERE key = 'phase4_primary_prepared'
          AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true'
        """
    )
