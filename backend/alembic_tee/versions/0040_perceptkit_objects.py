"""Create the PerceptKit logical storage objects.

Revision ID: 0040_perceptkit_objects
Revises: 0039_distill_artifact_ledger

PerceptKit defines logical objects and port semantics; choosing tables and
indexes is the host's job. These are that choice.

``_UP`` is byte-identical to the paired revision on the other chain, and a
test asserts it also equals ``perception.perceptkit_adapter.schema.DDL``.
The adapter and its conformance tests run against that constant, so a drift
between it and this migration would mean the tables the tests exercise are
not the tables production creates. Everything is IF NOT EXISTS, so
re-running is safe.

This migration only creates tables. Nothing reads or writes them yet.
"""

from alembic import op


revision = "0040_perceptkit_objects"
down_revision = "0039_distill_artifact_ledger"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS perceptkit_ingest_receipt (
  subject_id      TEXT        NOT NULL,
  producer        TEXT        NOT NULL,
  report_id       TEXT        NOT NULL,
  payload_digest  TEXT        NOT NULL,
  received_at     TIMESTAMPTZ NOT NULL,
  status          TEXT        NOT NULL,
  PRIMARY KEY (subject_id, producer, report_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_observation (
  subject_id            TEXT        NOT NULL,
  observation_id        TEXT        NOT NULL,
  signal                TEXT        NOT NULL,
  signal_schema_version INT         NOT NULL,
  source                TEXT        NOT NULL,
  occurred_at           TIMESTAMPTZ NOT NULL,
  received_at           TIMESTAMPTZ NOT NULL,
  availability          TEXT        NOT NULL,
  effective_local_date  DATE        NOT NULL,
  typed_value           JSONB,
  timezone              TEXT,
  source_event_id       TEXT,
  source_revision       TEXT,
  created_at            TIMESTAMPTZ,
  PRIMARY KEY (subject_id, observation_id)
);

-- Timeline reads go by (subject, signal, occurred_at); retention sweeps scan
-- occurred_at.
CREATE INDEX IF NOT EXISTS perceptkit_observation_timeline
  ON perceptkit_observation (subject_id, signal, occurred_at, observation_id);

CREATE TABLE IF NOT EXISTS perceptkit_current (
  subject_id            TEXT        NOT NULL,
  signal                TEXT        NOT NULL,
  dimension_key         TEXT        NOT NULL,
  typed_value           JSONB,
  availability          TEXT        NOT NULL,
  observed_at           TIMESTAMPTZ NOT NULL,
  received_at           TIMESTAMPTZ NOT NULL,
  expires_at            TIMESTAMPTZ,
  source_observation_id TEXT,
  source_revision       TEXT,
  version               INT         NOT NULL DEFAULT 0,
  content_digest        TEXT,
  PRIMARY KEY (subject_id, signal, dimension_key)
);

CREATE TABLE IF NOT EXISTS perceptkit_daily_aggregate (
  subject_id           TEXT        NOT NULL,
  signal               TEXT        NOT NULL,
  local_date           DATE        NOT NULL,
  aggregation_kind     TEXT        NOT NULL,
  aggregation_version  INT         NOT NULL,
  typed_aggregate      JSONB       NOT NULL,
  timezone_attribution TEXT,
  source_coverage      JSONB       NOT NULL DEFAULT '{}'::jsonb,
  updated_at           TIMESTAMPTZ,
  PRIMARY KEY (subject_id, signal, local_date, aggregation_kind, aggregation_version)
);

-- When details expire but aggregates are permanent, this has to outlive the
-- details. A separate table is what makes a detail sweep physically unable to
-- touch it.
CREATE TABLE IF NOT EXISTS perceptkit_dedupe_identity (
  subject_id   TEXT        NOT NULL,
  signal       TEXT        NOT NULL,
  source       TEXT        NOT NULL,
  digest          TEXT        NOT NULL,
  first_applied_at TIMESTAMPTZ NOT NULL,
  -- Which permanent aggregate this identity guards. A retention sweep reads it
  -- to know this row is not yet removable.
  aggregate_scope TEXT,
  retain_until    TIMESTAMPTZ,
  PRIMARY KEY (subject_id, signal, source, digest)
);

CREATE TABLE IF NOT EXISTS perceptkit_rule_state (
  subject_id    TEXT  NOT NULL,
  definition_id TEXT  NOT NULL,
  scope_key     TEXT  NOT NULL,
  state         JSONB NOT NULL,
  PRIMARY KEY (subject_id, definition_id, scope_key)
);

CREATE TABLE IF NOT EXISTS perceptkit_event_outbox (
  event_id           TEXT        PRIMARY KEY,
  subject_id         TEXT        NOT NULL,
  definition_id      TEXT        NOT NULL,
  definition_version INT         NOT NULL,
  event_type         TEXT        NOT NULL,
  occurred_at        TIMESTAMPTZ NOT NULL,
  detected_at        TIMESTAMPTZ NOT NULL,
  delivery_state     TEXT        NOT NULL,
  attempt_count      INT         NOT NULL DEFAULT 0,
  fact_snapshot      JSONB       NOT NULL DEFAULT '{}'::jsonb,
  next_attempt_at    TIMESTAMPTZ,
  claim_token        TEXT,
  claimed_by         TEXT,
  claim_expires_at   TIMESTAMPTZ
);

-- How a worker picks up work: by state and due time. Ordering within one
-- subject does not matter.
CREATE INDEX IF NOT EXISTS perceptkit_event_outbox_claimable
  ON perceptkit_event_outbox (delivery_state, next_attempt_at)
  WHERE delivery_state IN ('pending', 'claimed');

CREATE TABLE IF NOT EXISTS perceptkit_wake_receipt (
  event_id    TEXT        NOT NULL,
  attempt_id  TEXT        NOT NULL,
  status      TEXT        NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  runtime_ref TEXT,
  reason      TEXT,
  PRIMARY KEY (event_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_calendar_mirror (
  subject_id          TEXT        NOT NULL,
  source_account_id   TEXT        NOT NULL,
  source_calendar_id  TEXT        NOT NULL,
  source_event_id     TEXT        NOT NULL,
  event_fields        JSONB       NOT NULL,
  source_revision     TEXT,
  recurrence_identity TEXT,
  source_created_at   TIMESTAMPTZ,
  source_updated_at   TIMESTAMPTZ,
  last_seen_sync_id   TEXT,
  updated_at          TIMESTAMPTZ,
  PRIMARY KEY (subject_id, source_account_id, source_calendar_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_reminder_mirror (
  subject_id         TEXT        NOT NULL,
  source_account_id  TEXT        NOT NULL,
  source_list_id     TEXT        NOT NULL,
  source_reminder_id TEXT        NOT NULL,
  reminder_fields    JSONB       NOT NULL,
  source_revision    TEXT,
  source_created_at  TIMESTAMPTZ,
  source_updated_at  TIMESTAMPTZ,
  last_seen_sync_id  TEXT,
  updated_at         TIMESTAMPTZ,
  PRIMARY KEY (subject_id, source_account_id, source_list_id, source_reminder_id)
);

CREATE TABLE IF NOT EXISTS perceptkit_sync_state (
  subject_id             TEXT        NOT NULL,
  source                 TEXT        NOT NULL,
  collection_kind        TEXT        NOT NULL,
  last_sync_id           TEXT,
  last_successful_sync_at TIMESTAMPTZ,
  coverage_start         TIMESTAMPTZ,
  coverage_end           TIMESTAMPTZ,
  cursor                 TEXT,
  PRIMARY KEY (subject_id, source, collection_kind)
);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Deliberately not implemented. These tables hold user perception data, so
    # an automated downgrade that drops them turns a rollback into data loss.
    # Removing them is a deliberate, manual operation.
    pass
