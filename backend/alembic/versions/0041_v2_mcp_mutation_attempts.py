"""Durable Runtime V2 chat-mutation recovery frontiers.

Revision ID: 0041_v2_mcp_mutation_attempts
Revises: 0040_genesis_worker_claim
"""

from alembic import op
from sqlalchemy import text


revision = "0041_v2_mcp_mutation_attempts"
down_revision = "0040_genesis_worker_claim"
branch_labels = None
depends_on = None


# Reserved, content-free identifiers for the conservative barrier attached to
# a chat turn that was already owned by a pre-0041 worker at migration time.
# Ordinary MCP attempts use SHA-256(call_id/tool_name), so these domain-separated
# values cannot collide with a real call except by a cryptographic collision.
LEGACY_ACTIVE_CALL_KEY = (
    "5b29c62a0143d7b788b1dd2e1b2995310e5e8ed60d12290c4c483cf3a41d515e"
)
LEGACY_ACTIVE_TOOL_FINGERPRINT = (
    "437b890469973f1b39d9acb6f98fa1ef87de005ddc2cd1cdf0456a5a5cb202af"
)


_SCHEMA_UP = f"""
-- Wait for every old pending->claimed writer that started before this
-- migration, then keep later claim writers out until the protocol trigger and
-- conservative barriers are durable.  A pre-0041 UPDATE that was already
-- waiting resumes only after commit and is rejected by the new trigger.
LOCK TABLE agent_jobs IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE IF NOT EXISTS v2_mcp_mutation_attempts (
  job_id BIGINT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  input_frontier_seq BIGINT NOT NULL,
  call_key CHAR(64) NOT NULL,
  tool_fingerprint CHAR(64) NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  resolved_at TIMESTAMPTZ,
  outcome TEXT,
  PRIMARY KEY (job_id, call_key),
  CONSTRAINT ck_v2_mcp_mutation_frontier
    CHECK (input_frontier_seq >= 0),
  CONSTRAINT ck_v2_mcp_mutation_outcome
    CHECK (outcome IS NULL OR outcome IN ('known', 'unknown'))
);

CREATE INDEX IF NOT EXISTS ix_v2_mcp_mutation_user_frontier
  ON v2_mcp_mutation_attempts (user_id, input_frontier_seq);

ALTER TABLE v2_effect_outbox
  ADD COLUMN IF NOT EXISTS input_frontier_seq BIGINT;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='v2_effect_outbox'::regclass
      AND conname='ck_v2_effect_input_frontier'
  ) THEN
    ALTER TABLE v2_effect_outbox
      ADD CONSTRAINT ck_v2_effect_input_frontier
      CHECK (input_frontier_seq IS NULL OR input_frontier_seq >= 0) NOT VALID;
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION v2_fill_effect_input_frontier()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- New fenced reply rows must be invisible to a pre-0041 applier.  That
  -- applier only selects status='pending' and does not understand the source /
  -- input fence, so letting it seize one would split reply, cursor, job, and
  -- outbox publication back into separate commits during a rolling deploy.
  IF NEW.status = 'pending'
     AND NEW.effect_type IN (
       'reply_final_fenced_v1',
       'reply_terminal_fenced_v1',
       'reply_intermediate_fenced_v1'
     )
  THEN
    NEW.status := 'pending_fenced_v1';
  END IF;

  IF NEW.input_frontier_seq IS NULL
     AND NEW.effect_type IN (
       'memory_encrypted_v1',
       'identity_encrypted_v1',
       'schedule_encrypted_v1'
     )
     AND EXISTS (
       SELECT 1 FROM agent_jobs job
       WHERE job.id=NEW.job_id AND job.lane='chat'
     )
  THEN
    SELECT COALESCE(MAX(chat.seq), 0)
      INTO NEW.input_frontier_seq
    FROM chat_messages chat
    WHERE chat.user_id=NEW.user_id
      AND chat.doc->>'role' IN ('user','human');
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_v2_fill_effect_input_frontier
  ON v2_effect_outbox;
CREATE TRIGGER trg_v2_fill_effect_input_frontier
BEFORE INSERT ON v2_effect_outbox
FOR EACH ROW EXECUTE FUNCTION v2_fill_effect_input_frontier();

CREATE OR REPLACE FUNCTION v2_guard_agent_job_claim_0041()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  newest_user_seq BIGINT;
BEGIN
  -- Old images use the same status='pending' queue, but they do not record or
  -- consult mutation barriers.  Keep producer compatibility while making their
  -- pending->claimed UPDATE affect zero rows.  Current workers opt in for only
  -- the surrounding claim transaction via SET LOCAL/set_config(..., true).
  IF OLD.status='pending'
     AND NEW.status='claimed'
     AND current_setting('feedling.v2_worker_protocol', true)
           IS DISTINCT FROM '0041'
  THEN
    RETURN NULL;
  END IF;

  -- A pre-0041 worker may already own this chat job.  A late user message
  -- increments input_generation in the same transaction that persists the
  -- message.  Raise its reserved barrier through that newly visible frontier
  -- so a crash cannot replay any part of the expanded turn with write tools.
  IF OLD.lane='chat'
     AND OLD.status IN ('claimed','running')
     AND NEW.status IN ('claimed','running')
     AND COALESCE(NEW.input_generation,0) > COALESCE(OLD.input_generation,0)
  THEN
    SELECT COALESCE(MAX(chat.seq), 0)
      INTO newest_user_seq
    FROM chat_messages chat
    WHERE chat.user_id=OLD.user_id
      AND chat.doc->>'role' IN ('user','human');

    UPDATE v2_mcp_mutation_attempts attempt
    SET input_frontier_seq=GREATEST(
          attempt.input_frontier_seq,
          newest_user_seq
        )
    WHERE attempt.job_id=OLD.id
      AND attempt.call_key='{LEGACY_ACTIVE_CALL_KEY}';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_v2_guard_agent_job_claim_0041 ON agent_jobs;
CREATE TRIGGER trg_v2_guard_agent_job_claim_0041
BEFORE UPDATE ON agent_jobs
FOR EACH ROW EXECUTE FUNCTION v2_guard_agent_job_claim_0041();
"""


_SEED_LEGACY_ACTIVE_BARRIERS = f"""
-- Any claimed/running chat row can belong to an old worker that has already
-- sent (or may still send) an unlogged remote mutation.  One reserved unknown
-- attempt conservatively protects its entire currently visible input frontier.
INSERT INTO v2_mcp_mutation_attempts
  (job_id,user_id,input_frontier_seq,call_key,tool_fingerprint,
   resolved_at,outcome)
SELECT job.id,
       job.user_id,
       COALESCE(
         MAX(chat.seq) FILTER (
           WHERE chat.doc->>'role' IN ('user','human')
         ),
         0
       ),
       '{LEGACY_ACTIVE_CALL_KEY}',
       '{LEGACY_ACTIVE_TOOL_FINGERPRINT}',
       clock_timestamp(),
       'unknown'
FROM agent_jobs job
LEFT JOIN chat_messages chat ON chat.user_id=job.user_id
WHERE job.lane='chat' AND job.status IN ('claimed','running')
GROUP BY job.id,job.user_id
ON CONFLICT (job_id,call_key) DO UPDATE
SET input_frontier_seq=GREATEST(
      v2_mcp_mutation_attempts.input_frontier_seq,
      EXCLUDED.input_frontier_seq
    ),
    outcome='unknown',
    resolved_at=COALESCE(
      v2_mcp_mutation_attempts.resolved_at,
      EXCLUDED.resolved_at
    );
"""


_BACKFILL_EFFECT_FRONTIERS_BATCH = """
WITH batch AS MATERIALIZED (
  SELECT effect.effect_id,effect.user_id,effect.enqueue_seq
  FROM v2_effect_outbox effect
  JOIN agent_jobs job ON job.id=effect.job_id AND job.lane='chat'
  WHERE effect.effect_type IN (
    'memory_encrypted_v1',
    'identity_encrypted_v1',
    'schedule_encrypted_v1'
  )
    AND effect.input_frontier_seq IS NULL
    AND effect.enqueue_seq > :after_enqueue_seq
  ORDER BY effect.enqueue_seq
  LIMIT :batch_size
), affected_users AS MATERIALIZED (
  SELECT DISTINCT batch.user_id FROM batch
), user_frontiers AS MATERIALIZED (
  SELECT affected.user_id,
         COALESCE(
           MAX(chat.seq) FILTER (
             WHERE chat.doc->>'role' IN ('user','human')
           ),
           0
         ) AS input_frontier_seq
  FROM affected_users affected
  LEFT JOIN chat_messages chat ON chat.user_id=affected.user_id
  GROUP BY affected.user_id
), updated AS (
  UPDATE v2_effect_outbox effect
  SET input_frontier_seq=frontier.input_frontier_seq
  FROM batch,user_frontiers frontier
  WHERE effect.effect_id=batch.effect_id
    AND frontier.user_id=batch.user_id
    AND effect.input_frontier_seq IS NULL
  RETURNING effect.enqueue_seq
)
-- Advance by the selected keyset, not only rows UPDATE happened to return. A
-- concurrent user deletion may remove a selected effect after the snapshot;
-- that row no longer needs a barrier, but it must not make us stop before
-- scanning higher enqueue_seq values. Migration 0033/0034 guarantees a UNIQUE
-- enqueue_seq index, so there are no ties for this cursor to skip.
SELECT MAX(batch.enqueue_seq) AS scanned_through,
       COUNT(updated.enqueue_seq) AS updated_count
FROM batch
LEFT JOIN updated ON updated.enqueue_seq=batch.enqueue_seq;
"""


# Offline SQL generation cannot consume UPDATE ... RETURNING to drive a Python
# keyset loop.  Emit one grouped statement there; live migrations use bounded,
# independently committed batches below.
_BACKFILL_EFFECT_FRONTIERS_ALL = """
WITH affected_users AS MATERIALIZED (
  SELECT DISTINCT effect.user_id
  FROM v2_effect_outbox effect
  JOIN agent_jobs job ON job.id=effect.job_id AND job.lane='chat'
  WHERE effect.effect_type IN (
    'memory_encrypted_v1',
    'identity_encrypted_v1',
    'schedule_encrypted_v1'
  )
    AND effect.input_frontier_seq IS NULL
), user_frontiers AS MATERIALIZED (
  SELECT affected.user_id,
         COALESCE(
           MAX(chat.seq) FILTER (
             WHERE chat.doc->>'role' IN ('user','human')
           ),
           0
         ) AS input_frontier_seq
  FROM affected_users affected
  LEFT JOIN chat_messages chat ON chat.user_id=affected.user_id
  GROUP BY affected.user_id
)
UPDATE v2_effect_outbox effect
SET input_frontier_seq=frontier.input_frontier_seq
FROM agent_jobs job,user_frontiers frontier
WHERE effect.job_id=job.id
  AND job.lane='chat'
  AND frontier.user_id=effect.user_id
  AND effect.effect_type IN (
    'memory_encrypted_v1',
    'identity_encrypted_v1',
    'schedule_encrypted_v1'
  )
  AND effect.input_frontier_seq IS NULL;
"""


_VALIDATE_EFFECT_FRONTIER = """
ALTER TABLE v2_effect_outbox
  VALIDATE CONSTRAINT ck_v2_effect_input_frontier
"""


_DROP_CONCURRENT_EFFECT_FRONTIER_INDEX = (
    "DROP INDEX CONCURRENTLY IF EXISTS ix_v2_effect_user_frontier"
)
_CREATE_CONCURRENT_EFFECT_FRONTIER_INDEX = """
CREATE INDEX CONCURRENTLY ix_v2_effect_user_frontier
  ON v2_effect_outbox (user_id, input_frontier_seq)
  WHERE input_frontier_seq IS NOT NULL
"""


_DROP_CONCURRENT_DISPATCH_INDEX = (
    "DROP INDEX CONCURRENTLY IF EXISTS ix_v2_effect_dispatch_pending_v0041"
)
_CREATE_CONCURRENT_DISPATCH_INDEX = """
CREATE INDEX CONCURRENTLY ix_v2_effect_dispatch_pending_v0041
  ON v2_effect_outbox (user_id, enqueue_seq)
  WHERE status IN ('pending', 'pending_fenced_v1')
"""


_DOWN_GUARD = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM v2_effect_outbox effect
    WHERE effect.status='pending_fenced_v1'
      AND effect.effect_type IN (
        'reply_final_fenced_v1',
        'reply_terminal_fenced_v1',
        'reply_intermediate_fenced_v1'
      )
  ) THEN
    RAISE EXCEPTION
      'cannot downgrade 0041: fenced reply effects are still pending'
      USING ERRCODE='55000';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM (
      SELECT attempt.user_id,attempt.input_frontier_seq
      FROM v2_mcp_mutation_attempts attempt
      UNION ALL
      SELECT effect.user_id,effect.input_frontier_seq
      FROM v2_effect_outbox effect
      WHERE effect.input_frontier_seq IS NOT NULL
    ) frontier
    LEFT JOIN user_blobs profile
      ON profile.user_id=frontier.user_id
     AND profile.kind='model_api_runtime'
    WHERE frontier.input_frontier_seq > CASE
      WHEN COALESCE(profile.doc->>'v2_reply_cursor_seq','0')
           ~ '^[0-9]{1,18}$'
      THEN COALESCE(profile.doc->>'v2_reply_cursor_seq','0')::bigint
      ELSE 0
    END
  ) THEN
    RAISE EXCEPTION
      'cannot downgrade 0041: mutation frontier exceeds durable reply cursor'
      USING ERRCODE='55000';
  END IF;
END
$$;
"""


_DOWN_UNSUPPORTED = """
DO $$
BEGIN
  RAISE EXCEPTION
    'cannot downgrade 0041: worker protocol and replay evidence are permanent'
    USING ERRCODE='55000';
END
$$;
"""


def _backfill_effect_frontiers() -> None:
    context = op.get_context()
    if context.as_sql:
        op.execute(_BACKFILL_EFFECT_FRONTIERS_ALL)
        return

    bind = op.get_bind()
    after_enqueue_seq = -1
    while True:
        progress = bind.execute(
            text(_BACKFILL_EFFECT_FRONTIERS_BATCH),
            {
                "after_enqueue_seq": after_enqueue_seq,
                "batch_size": 2000,
            },
        ).fetchone()
        if progress is None or progress[0] is None:
            return
        after_enqueue_seq = int(progress[0])


def upgrade() -> None:
    # The explicit agent_jobs lock makes trigger installation + active-job
    # barrier seeding one short cutover transaction. Entering the autocommit
    # block commits it and releases both that writer lock and the outbox ALTER
    # lock before any historical-row scan begins.
    op.execute(_SCHEMA_UP)
    op.execute(_SEED_LEGACY_ACTIVE_BARRIERS)
    with op.get_context().autocommit_block():
        _backfill_effect_frontiers()
        op.execute(_VALIDATE_EFFECT_FRONTIER)
        op.execute(_DROP_CONCURRENT_EFFECT_FRONTIER_INDEX)
        op.execute(_CREATE_CONCURRENT_EFFECT_FRONTIER_INDEX)
        op.execute(_DROP_CONCURRENT_DISPATCH_INDEX)
        op.execute(_CREATE_CONCURRENT_DISPATCH_INDEX)


def downgrade() -> None:
    # 0041 is a one-way safety boundary. Keep the two actionable diagnostics,
    # then fail even on a clean ledger; no destructive statement follows the
    # checks, so a concurrent writer cannot race evidence deletion.
    op.execute(_DOWN_GUARD)
    op.execute(_DOWN_UNSUPPORTED)
