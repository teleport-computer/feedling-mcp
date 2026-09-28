"""PerceptKit v0.10 durable storage contract.

Revision ID: 0116_perceptkit_v010_storage
Revises: 0115_outbox_source_fact

Legacy aggregate rows are retained as audit evidence in explicit incomplete
generations.  No active pointer is inferred from row presence or max(version):
an operator-owned v0.10 rebuild must publish a complete generation.
"""
from alembic import op


revision = "0116_perceptkit_v010_storage"
down_revision = "0115_outbox_source_fact"
branch_labels = None
depends_on = None


_UP = r"""
ALTER TABLE perceptkit_ingest_receipt ADD COLUMN IF NOT EXISTS error_code TEXT;
ALTER TABLE perceptkit_ingest_receipt ADD COLUMN IF NOT EXISTS observations_applied INT NOT NULL DEFAULT 0;

ALTER TABLE perceptkit_observation ADD COLUMN IF NOT EXISTS source_revision_value JSONB;
UPDATE perceptkit_observation SET source_revision_value=to_jsonb(source_revision)
 WHERE source_revision_value IS NULL AND source_revision IS NOT NULL;
ALTER TABLE perceptkit_observation ADD COLUMN IF NOT EXISTS source_units JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE perceptkit_observation ADD COLUMN IF NOT EXISTS source_values JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE perceptkit_observation ADD COLUMN IF NOT EXISTS timezone_source TEXT NOT NULL DEFAULT 'legacy_unknown';

ALTER TABLE perceptkit_current ADD COLUMN IF NOT EXISTS source_revision_value JSONB;
UPDATE perceptkit_current SET source_revision_value=to_jsonb(source_revision)
 WHERE source_revision_value IS NULL AND source_revision IS NOT NULL;
ALTER TABLE perceptkit_current ADD COLUMN IF NOT EXISTS timezone TEXT;
ALTER TABLE perceptkit_current ADD COLUMN IF NOT EXISTS timezone_source TEXT NOT NULL DEFAULT 'legacy_unknown';
ALTER TABLE perceptkit_current ADD COLUMN IF NOT EXISTS source_units JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE perceptkit_current ADD COLUMN IF NOT EXISTS source_values JSONB NOT NULL DEFAULT '{}'::jsonb;

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
      (subject_id, signal, aggregation_kind, generation_id) ON DELETE CASCADE
);

ALTER TABLE perceptkit_daily_aggregate ADD COLUMN IF NOT EXISTS generation_id TEXT;
UPDATE perceptkit_daily_aggregate
 SET generation_id='legacy-v' || aggregation_version::text
 WHERE generation_id IS NULL;
ALTER TABLE perceptkit_daily_aggregate ALTER COLUMN generation_id SET NOT NULL;
ALTER TABLE perceptkit_daily_aggregate ADD COLUMN IF NOT EXISTS completeness TEXT NOT NULL DEFAULT 'complete';
ALTER TABLE perceptkit_daily_aggregate ADD COLUMN IF NOT EXISTS incomplete_reasons JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE perceptkit_daily_aggregate ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 0;
ALTER TABLE perceptkit_daily_aggregate DROP CONSTRAINT IF EXISTS perceptkit_daily_aggregate_pkey;
ALTER TABLE perceptkit_daily_aggregate ADD PRIMARY KEY
  (subject_id, signal, local_date, aggregation_kind, aggregation_version, generation_id);

INSERT INTO perceptkit_aggregate_generation
 (subject_id, signal, aggregation_kind, generation_id, aggregation_version,
  requested_start_date, requested_end_date, status, completeness,
  accounted_dates, incomplete_dates, incomplete_reasons, created_at, updated_at)
SELECT subject_id, signal, aggregation_kind, generation_id, aggregation_version,
       min(local_date), max(local_date), 'incomplete', 'incomplete',
       to_jsonb(array_agg(DISTINCT local_date ORDER BY local_date)),
       to_jsonb(ARRAY(
         SELECT d::date FROM generate_series(min(local_date), max(local_date), interval '1 day') d
       )),
       '["legacy_coverage_unverified"]'::jsonb, min(updated_at), max(updated_at)
  FROM perceptkit_daily_aggregate
 GROUP BY subject_id, signal, aggregation_kind, generation_id, aggregation_version
ON CONFLICT DO NOTHING;
UPDATE perceptkit_daily_aggregate
 SET completeness='incomplete',
     incomplete_reasons='["legacy_coverage_unverified"]'::jsonb
 WHERE generation_id LIKE 'legacy-v%';
ALTER TABLE perceptkit_daily_aggregate
  ADD CONSTRAINT perceptkit_daily_aggregate_generation_fk
  FOREIGN KEY (subject_id, signal, aggregation_kind, generation_id)
  REFERENCES perceptkit_aggregate_generation
    (subject_id, signal, aggregation_kind, generation_id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS perceptkit_daily_aggregate_window
  ON perceptkit_daily_aggregate
    (subject_id, signal, aggregation_kind, local_date, aggregation_version, generation_id);

ALTER TABLE perceptkit_dedupe_identity ADD COLUMN IF NOT EXISTS fact_key TEXT;
ALTER TABLE perceptkit_dedupe_identity ADD COLUMN IF NOT EXISTS source_revision_value JSONB;
ALTER TABLE perceptkit_dedupe_identity ADD COLUMN IF NOT EXISTS semantic_digest TEXT;
ALTER TABLE perceptkit_dedupe_identity ADD COLUMN IF NOT EXISTS legacy_content_digest TEXT;
ALTER TABLE perceptkit_dedupe_identity ADD COLUMN IF NOT EXISTS effective_local_date DATE;
ALTER TABLE perceptkit_dedupe_identity ADD COLUMN IF NOT EXISTS dimension_key TEXT;
CREATE INDEX IF NOT EXISTS perceptkit_identity_fact
  ON perceptkit_dedupe_identity (subject_id, signal, source, fact_key);
CREATE INDEX IF NOT EXISTS perceptkit_identity_legacy
  ON perceptkit_dedupe_identity (subject_id, signal, source) WHERE fact_key IS NULL;

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

ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS dedupe_key TEXT;
ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS budget_reservation_id TEXT;
ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS fact_dependencies JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS fact_dependencies_complete BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS dispatch_started_at TIMESTAMPTZ;
ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS invalidation_reason TEXT;
ALTER TABLE perceptkit_event_outbox ADD COLUMN IF NOT EXISTS signal TEXT NOT NULL DEFAULT '';
DROP INDEX IF EXISTS perceptkit_event_outbox_source;
CREATE INDEX IF NOT EXISTS perceptkit_event_outbox_source
  ON perceptkit_event_outbox (subject_id, signal, source, source_event_id);
CREATE INDEX IF NOT EXISTS perceptkit_event_outbox_subject_event
  ON perceptkit_event_outbox (subject_id, event_id);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Deliberately preserve v0.10 audit and completeness evidence. Dropping it
    # would relabel unverifiable legacy rows as ordinary complete history.
    pass
