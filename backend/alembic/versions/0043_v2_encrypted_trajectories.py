"""Encrypted Runtime V2 trajectories and failure-review queue.

Revision ID: 0043_v2_encrypted_trajectories
Revises: 0042_v2_workspace_foundation
"""

from alembic import op


revision = "0043_v2_encrypted_trajectories"
down_revision = "0042_v2_workspace_foundation"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE IF NOT EXISTS v2_trajectory_streams (
  job_id BIGINT PRIMARY KEY REFERENCES agent_jobs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  next_event_index INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT ck_v2_trajectory_next_index CHECK (next_event_index >= 0),
  CONSTRAINT ux_v2_trajectory_stream_job_user UNIQUE (job_id, user_id)
);

CREATE TABLE IF NOT EXISTS v2_trajectory_events (
  job_id BIGINT NOT NULL,
  user_id TEXT NOT NULL,
  event_index INTEGER NOT NULL,
  event_kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_envelope JSONB NOT NULL,
  payload_bytes INTEGER NOT NULL,
  truncated BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (job_id, event_index),
  CONSTRAINT fk_v2_trajectory_event_stream
    FOREIGN KEY (job_id, user_id)
    REFERENCES v2_trajectory_streams(job_id, user_id) ON DELETE CASCADE,
  CONSTRAINT ux_v2_trajectory_event_idempotency
    UNIQUE (job_id, idempotency_key),
  CONSTRAINT ck_v2_trajectory_event_index CHECK (event_index >= 0),
  CONSTRAINT ck_v2_trajectory_event_kind
    CHECK (event_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
  CONSTRAINT ck_v2_trajectory_idempotency
    CHECK (length(idempotency_key) BETWEEN 1 AND 96),
  CONSTRAINT ck_v2_trajectory_payload_bytes
    CHECK (payload_bytes BETWEEN 1 AND 1048576),
  CONSTRAINT ck_v2_trajectory_envelope
    CHECK (
      jsonb_typeof(payload_envelope) = 'object'
      AND payload_envelope ? 'body_ct'
      AND payload_envelope ? 'nonce'
      AND payload_envelope ? 'K_user'
      AND payload_envelope ? 'K_enclave'
      AND payload_envelope ? 'owner_user_id'
      AND payload_envelope ? 'id'
      AND payload_envelope ? 'v'
      AND payload_envelope ? 'visibility'
      AND jsonb_typeof(payload_envelope->'v') = 'number'
      AND jsonb_typeof(payload_envelope->'id') = 'string'
      AND jsonb_typeof(payload_envelope->'owner_user_id') = 'string'
      AND jsonb_typeof(payload_envelope->'visibility') = 'string'
      AND jsonb_typeof(payload_envelope->'body_ct') = 'string'
      AND jsonb_typeof(payload_envelope->'nonce') = 'string'
      AND jsonb_typeof(payload_envelope->'K_user') = 'string'
      AND jsonb_typeof(payload_envelope->'K_enclave') = 'string'
      AND payload_envelope->>'owner_user_id' = user_id
      AND payload_envelope->>'visibility' = 'shared'
      AND length(payload_envelope->>'id') > 0
      AND length(payload_envelope->>'body_ct') > 0
      AND length(payload_envelope->>'nonce') > 0
      AND length(payload_envelope->>'K_user') > 0
      AND length(payload_envelope->>'K_enclave') > 0
      AND payload_envelope - ARRAY[
        'v','id','owner_user_id','visibility','body_ct','nonce','K_user',
        'K_enclave','enclave_pk_fpr','content_pk_fpr'
      ]::text[] = '{}'::jsonb
    )
);

CREATE INDEX IF NOT EXISTS ix_v2_trajectory_events_user_created
  ON v2_trajectory_events (user_id, created_at DESC);

-- Event rows are append-only. Explicit account/job deletion is still allowed
-- through the foreign-key cascades, but no writer can rewrite captured history.
CREATE OR REPLACE FUNCTION v2_reject_trajectory_event_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'v2 trajectory events are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_v2_trajectory_event_immutable
  ON v2_trajectory_events;
CREATE TRIGGER trg_v2_trajectory_event_immutable
BEFORE UPDATE ON v2_trajectory_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_trajectory_event_update();

CREATE TABLE IF NOT EXISTS v2_trajectory_reviews (
  source_job_id BIGINT PRIMARY KEY REFERENCES agent_jobs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  claimed_by_job_id BIGINT REFERENCES agent_jobs(id) ON DELETE SET NULL,
  review_envelope JSONB,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  CONSTRAINT ck_v2_trajectory_review_status
    CHECK (status IN ('pending','running','completed','failed')),
  CONSTRAINT ck_v2_trajectory_review_attempts
    CHECK (attempt_count BETWEEN 0 AND 3),
  CONSTRAINT ck_v2_trajectory_review_envelope
    CHECK (
      review_envelope IS NULL OR (
        jsonb_typeof(review_envelope) = 'object'
        AND review_envelope ? 'body_ct'
        AND review_envelope ? 'nonce'
        AND review_envelope ? 'K_user'
        AND review_envelope ? 'K_enclave'
        AND review_envelope ? 'owner_user_id'
        AND review_envelope ? 'id'
        AND review_envelope ? 'v'
        AND review_envelope ? 'visibility'
        AND jsonb_typeof(review_envelope->'v') = 'number'
        AND jsonb_typeof(review_envelope->'id') = 'string'
        AND jsonb_typeof(review_envelope->'owner_user_id') = 'string'
        AND jsonb_typeof(review_envelope->'visibility') = 'string'
        AND jsonb_typeof(review_envelope->'body_ct') = 'string'
        AND jsonb_typeof(review_envelope->'nonce') = 'string'
        AND jsonb_typeof(review_envelope->'K_user') = 'string'
        AND jsonb_typeof(review_envelope->'K_enclave') = 'string'
        AND review_envelope->>'owner_user_id' = user_id
        AND review_envelope->>'visibility' = 'shared'
        AND length(review_envelope->>'id') > 0
        AND length(review_envelope->>'body_ct') > 0
        AND length(review_envelope->>'nonce') > 0
        AND length(review_envelope->>'K_user') > 0
        AND length(review_envelope->>'K_enclave') > 0
        AND review_envelope - ARRAY[
          'v','id','owner_user_id','visibility','body_ct','nonce','K_user',
          'K_enclave','enclave_pk_fpr','content_pk_fpr'
        ]::text[] = '{}'::jsonb
      )
    )
);

CREATE INDEX IF NOT EXISTS ix_v2_trajectory_reviews_pending
  ON v2_trajectory_reviews (user_id, created_at, source_job_id)
  WHERE status='pending';

CREATE INDEX IF NOT EXISTS ix_v2_trajectory_reviews_claimed
  ON v2_trajectory_reviews (claimed_by_job_id)
  WHERE status='running';

CREATE INDEX IF NOT EXISTS ix_v2_trajectory_reviews_active
  ON v2_trajectory_reviews (status)
  WHERE status IN ('pending','running');
"""


_DOWN = """
DROP TABLE IF EXISTS v2_trajectory_reviews;
DROP TRIGGER IF EXISTS trg_v2_trajectory_event_immutable
  ON v2_trajectory_events;
DROP FUNCTION IF EXISTS v2_reject_trajectory_event_update();
DROP TABLE IF EXISTS v2_trajectory_events;
DROP TABLE IF EXISTS v2_trajectory_streams;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
