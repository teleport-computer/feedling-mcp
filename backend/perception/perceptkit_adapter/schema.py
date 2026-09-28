"""PostgreSQL schema for the PerceptKit v0.10 storage contract.

``DDL`` is the fresh-database shape. Existing installations reach the same
shape through Alembic revision 0116; historical migrations remain immutable.
"""
from __future__ import annotations


DDL = r"""
CREATE TABLE IF NOT EXISTS perceptkit_ingest_receipt (
  subject_id TEXT NOT NULL, producer TEXT NOT NULL, report_id TEXT NOT NULL,
  payload_digest TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL, error_code TEXT, observations_applied INT NOT NULL DEFAULT 0,
  PRIMARY KEY (subject_id, producer, report_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_observation (
  subject_id TEXT NOT NULL, observation_id TEXT NOT NULL, signal TEXT NOT NULL,
  signal_schema_version INT NOT NULL, source TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL, received_at TIMESTAMPTZ NOT NULL,
  availability TEXT NOT NULL, effective_local_date DATE NOT NULL,
  typed_value JSONB, timezone TEXT, source_event_id TEXT,
  source_revision TEXT, source_revision_value JSONB, created_at TIMESTAMPTZ,
  source_units JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_values JSONB NOT NULL DEFAULT '{}'::jsonb,
  timezone_source TEXT NOT NULL DEFAULT 'legacy_unknown',
  PRIMARY KEY (subject_id, observation_id)
);
CREATE INDEX IF NOT EXISTS perceptkit_observation_timeline
  ON perceptkit_observation (subject_id, signal, occurred_at, observation_id);

CREATE TABLE IF NOT EXISTS perceptkit_current (
  subject_id TEXT NOT NULL, signal TEXT NOT NULL, dimension_key TEXT NOT NULL,
  typed_value JSONB, availability TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL, received_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ, source_observation_id TEXT,
  source_revision TEXT, source_revision_value JSONB,
  source TEXT, source_event_id TEXT, version INT NOT NULL DEFAULT 0,
  content_digest TEXT, timezone TEXT,
  timezone_source TEXT NOT NULL DEFAULT 'legacy_unknown',
  source_units JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_values JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (subject_id, signal, dimension_key)
);

CREATE TABLE IF NOT EXISTS perceptkit_aggregate_generation (
  subject_id TEXT NOT NULL, signal TEXT NOT NULL, aggregation_kind TEXT NOT NULL,
  generation_id TEXT NOT NULL, aggregation_version INT NOT NULL,
  requested_start_date DATE NOT NULL, requested_end_date DATE NOT NULL,
  status TEXT NOT NULL, completeness TEXT NOT NULL,
  accounted_dates JSONB NOT NULL DEFAULT '[]'::jsonb,
  incomplete_dates JSONB NOT NULL DEFAULT '[]'::jsonb,
  incomplete_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
  failure_reason TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
  activated_at TIMESTAMPTZ,
  PRIMARY KEY (subject_id, signal, aggregation_kind, generation_id),
  CHECK (requested_end_date >= requested_start_date)
);
CREATE INDEX IF NOT EXISTS perceptkit_aggregate_generation_window
  ON perceptkit_aggregate_generation
    (subject_id, signal, aggregation_kind, requested_start_date, requested_end_date,
     created_at, generation_id);

CREATE TABLE IF NOT EXISTS perceptkit_active_aggregate_generation (
  subject_id TEXT NOT NULL, signal TEXT NOT NULL, aggregation_kind TEXT NOT NULL,
  generation_id TEXT NOT NULL, activated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (subject_id, signal, aggregation_kind),
  FOREIGN KEY (subject_id, signal, aggregation_kind, generation_id)
    REFERENCES perceptkit_aggregate_generation
      (subject_id, signal, aggregation_kind, generation_id)
    ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS perceptkit_daily_aggregate (
  subject_id TEXT NOT NULL, signal TEXT NOT NULL, local_date DATE NOT NULL,
  aggregation_kind TEXT NOT NULL, aggregation_version INT NOT NULL,
  generation_id TEXT NOT NULL, typed_aggregate JSONB NOT NULL,
  completeness TEXT NOT NULL DEFAULT 'complete',
  incomplete_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
  timezone_attribution TEXT, source_coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ, version INT NOT NULL DEFAULT 0,
  PRIMARY KEY (subject_id, signal, local_date, aggregation_kind,
               aggregation_version, generation_id),
  FOREIGN KEY (subject_id, signal, aggregation_kind, generation_id)
    REFERENCES perceptkit_aggregate_generation
      (subject_id, signal, aggregation_kind, generation_id)
    ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS perceptkit_daily_aggregate_window
  ON perceptkit_daily_aggregate
    (subject_id, signal, aggregation_kind, local_date, aggregation_version, generation_id);

CREATE TABLE IF NOT EXISTS perceptkit_dedupe_identity (
  subject_id TEXT NOT NULL, signal TEXT NOT NULL, source TEXT NOT NULL,
  digest TEXT NOT NULL, first_applied_at TIMESTAMPTZ NOT NULL,
  aggregate_scope TEXT, retain_until TIMESTAMPTZ, fact_key TEXT,
  source_revision_value JSONB, semantic_digest TEXT, legacy_content_digest TEXT,
  effective_local_date DATE, dimension_key TEXT,
  PRIMARY KEY (subject_id, signal, source, digest)
);
CREATE INDEX IF NOT EXISTS perceptkit_identity_fact
  ON perceptkit_dedupe_identity (subject_id, signal, source, fact_key);
CREATE INDEX IF NOT EXISTS perceptkit_identity_legacy
  ON perceptkit_dedupe_identity (subject_id, signal, source)
  WHERE fact_key IS NULL;

CREATE TABLE IF NOT EXISTS perceptkit_conflict (
  subject_id TEXT NOT NULL, conflict_id TEXT NOT NULL, signal TEXT NOT NULL,
  source TEXT NOT NULL, fact_key TEXT NOT NULL, candidate_revision JSONB,
  semantic_digest TEXT NOT NULL, content_digest TEXT NOT NULL,
  kind TEXT NOT NULL, reason TEXT NOT NULL, candidate JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', resolved_at TIMESTAMPTZ,
  resolution_revision JSONB, resolution_semantic_digest TEXT,
  resolution_observation_id TEXT,
  PRIMARY KEY (subject_id, conflict_id)
);
CREATE INDEX IF NOT EXISTS perceptkit_conflict_query
  ON perceptkit_conflict
    (subject_id, status, signal, source, fact_key, created_at, conflict_id);

CREATE TABLE IF NOT EXISTS perceptkit_rule_state (
  subject_id TEXT NOT NULL, definition_id TEXT NOT NULL, scope_key TEXT NOT NULL,
  state JSONB NOT NULL,
  PRIMARY KEY (subject_id, definition_id, scope_key)
);

CREATE TABLE IF NOT EXISTS perceptkit_event_outbox (
  event_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL,
  definition_id TEXT NOT NULL, definition_version INT NOT NULL,
  event_type TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
  detected_at TIMESTAMPTZ NOT NULL, delivery_state TEXT NOT NULL,
  attempt_count INT NOT NULL DEFAULT 0, fact_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  next_attempt_at TIMESTAMPTZ, claim_token TEXT, claimed_by TEXT,
  claim_expires_at TIMESTAMPTZ, source TEXT, source_event_id TEXT,
  dedupe_key TEXT, budget_reservation_id TEXT, created_at TIMESTAMPTZ,
  fact_dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
  fact_dependencies_complete BOOLEAN NOT NULL DEFAULT FALSE,
  dispatch_started_at TIMESTAMPTZ, invalidated_at TIMESTAMPTZ,
  invalidation_reason TEXT, signal TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS perceptkit_event_outbox_claimable
  ON perceptkit_event_outbox (delivery_state, next_attempt_at, detected_at, event_id)
  WHERE delivery_state IN ('pending', 'claimed');
CREATE INDEX IF NOT EXISTS perceptkit_event_outbox_source
  ON perceptkit_event_outbox (subject_id, signal, source, source_event_id);
CREATE INDEX IF NOT EXISTS perceptkit_event_outbox_subject_event
  ON perceptkit_event_outbox (subject_id, event_id);

CREATE TABLE IF NOT EXISTS perceptkit_wake_receipt (
  event_id TEXT NOT NULL, attempt_id TEXT NOT NULL, status TEXT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL, runtime_ref TEXT, reason TEXT,
  PRIMARY KEY (event_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_calendar_mirror (
  subject_id TEXT NOT NULL, source TEXT NOT NULL, source_account_id TEXT NOT NULL,
  source_calendar_id TEXT NOT NULL, source_event_id TEXT NOT NULL,
  event_fields JSONB NOT NULL, source_revision TEXT,
  recurrence_identity TEXT, source_created_at TIMESTAMPTZ,
  source_updated_at TIMESTAMPTZ, last_seen_sync_id TEXT, updated_at TIMESTAMPTZ,
  PRIMARY KEY (subject_id, source, source_account_id, source_calendar_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_reminder_mirror (
  subject_id TEXT NOT NULL, source TEXT NOT NULL, source_account_id TEXT NOT NULL,
  source_list_id TEXT NOT NULL, source_reminder_id TEXT NOT NULL,
  reminder_fields JSONB NOT NULL, source_revision TEXT,
  source_created_at TIMESTAMPTZ, source_updated_at TIMESTAMPTZ,
  last_seen_sync_id TEXT, updated_at TIMESTAMPTZ,
  PRIMARY KEY (subject_id, source, source_account_id, source_list_id, source_reminder_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_retraction (
  subject_id TEXT NOT NULL, signal TEXT NOT NULL, source TEXT NOT NULL,
  source_event_id TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (subject_id, signal, source, source_event_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_sync_state (
  subject_id TEXT NOT NULL, source TEXT NOT NULL, collection_kind TEXT NOT NULL,
  sync_cursor TEXT, coverage_start TIMESTAMPTZ, coverage_end TIMESTAMPTZ,
  snapshot_kind TEXT, last_attempted_at TIMESTAMPTZ,
  last_successful_sync_at TIMESTAMPTZ, last_error_code TEXT,
  PRIMARY KEY (subject_id, source, collection_kind)
);

CREATE TABLE IF NOT EXISTS perceptkit_shadow_divergence (
  subject_id TEXT NOT NULL, signal TEXT NOT NULL, field TEXT NOT NULL,
  verdict TEXT NOT NULL, occurrences BIGINT NOT NULL DEFAULT 0,
  first_seen_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL,
  last_live TEXT, last_kit TEXT, last_report_id TEXT, note TEXT,
  last_skew_sec DOUBLE PRECISION, max_skew_sec DOUBLE PRECISION,
  last_live_at TIMESTAMPTZ, last_kit_at TIMESTAMPTZ,
  PRIMARY KEY (subject_id, signal, field, verdict)
);
"""


TABLES = (
    "perceptkit_ingest_receipt", "perceptkit_observation", "perceptkit_current",
    "perceptkit_daily_aggregate", "perceptkit_active_aggregate_generation",
    "perceptkit_aggregate_generation", "perceptkit_dedupe_identity",
    "perceptkit_conflict", "perceptkit_rule_state", "perceptkit_event_outbox",
    "perceptkit_wake_receipt", "perceptkit_calendar_mirror",
    "perceptkit_reminder_mirror", "perceptkit_sync_state",
    "perceptkit_shadow_divergence", "perceptkit_retraction",
)

TRUNCATE = "TRUNCATE " + ", ".join(TABLES) + " CASCADE;"

__all__ = ["DDL", "TRUNCATE", "TABLES"]
