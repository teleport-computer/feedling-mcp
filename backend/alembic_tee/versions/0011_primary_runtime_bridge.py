"""Add Phase-4 runtime-local tables to the promoted TEE database.

Revision ID: 0011_primary_runtime_bridge
Revises: 0010_v2_chat_tail_anchor
Create Date: 2026-08-02

The shadow schema intentionally omitted short-lived staging tables.  Once the
TEE database becomes ``DATABASE_URL`` those tables are runtime dependencies and
must exist, even though their old RDS rows are drained rather than replicated.

``frame_envelopes`` is a temporary protocol bridge.  The shadow ``frames``
projection stores an enclave-storage-key R2 object and cannot reconstruct the
FrameEnvelope v1 response expected by current clients.  Phase 4 copies the
original envelope metadata/R2 pointer verbatim into this table; ``frames`` is
retained for the later protocol migration.
"""

from alembic import op


revision = "0011_primary_runtime_bridge"
down_revision = "0010_v2_chat_tail_anchor"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS storage_generation BIGINT NOT NULL DEFAULT 0;

ALTER TABLE genesis_import_jobs
    ADD COLUMN IF NOT EXISTS resident_consumer_id  TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS resident_claimed_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resident_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resident_attempts     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS worker_claimed_by     TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS worker_claimed_at     TIMESTAMPTZ;

ALTER TABLE v2_conversation_summary
    ADD COLUMN IF NOT EXISTS effective_batch_cap INTEGER;

CREATE INDEX IF NOT EXISTS chat_r2_cleanup_due_idx
    ON chat_r2_cleanup (next_attempt_at, created_at, body_key);
CREATE INDEX IF NOT EXISTS chat_r2_cleanup_user_due_idx
    ON chat_r2_cleanup (user_id, next_attempt_at, created_at, body_key);
CREATE INDEX IF NOT EXISTS chat_r2_inventory_due_idx
    ON chat_r2_lifecycle (inventory_next_attempt_at, updated_at, user_id)
    WHERE inventory_pending;
CREATE INDEX IF NOT EXISTS ix_chat_message_archive_user_cleared
    ON chat_message_archive (user_id, cleared_at, source_seq);
CREATE INDEX IF NOT EXISTS ix_chat_message_archive_user_msg
    ON chat_message_archive (user_id, msg_id);

CREATE TABLE IF NOT EXISTS frame_envelopes (
    user_id  TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    ts       DOUBLE PRECISION NOT NULL,
    doc      JSONB,
    env_meta JSONB,
    body_key TEXT,
    PRIMARY KEY (user_id, frame_id),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS frame_user_ts_idx
    ON frame_envelopes (user_id, ts);

CREATE TABLE IF NOT EXISTS genesis_import_chunks (
    user_id            TEXT NOT NULL,
    job_id             TEXT NOT NULL,
    seq                INTEGER NOT NULL,
    byte_start         BIGINT NOT NULL DEFAULT 0,
    byte_end           BIGINT NOT NULL DEFAULT 0,
    ciphertext_sha256  TEXT NOT NULL,
    content_sha256     TEXT NOT NULL DEFAULT '',
    aad                JSONB NOT NULL DEFAULT '{}'::jsonb,
    encrypted_body     BYTEA NOT NULL,
    size_bytes         INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'uploaded',
    attempts           INTEGER NOT NULL DEFAULT 0,
    map_output_ref     TEXT NOT NULL DEFAULT '',
    error              TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id, seq),
    FOREIGN KEY (user_id, job_id)
        REFERENCES genesis_import_jobs (user_id, job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS genesis_chunks_job_idx
    ON genesis_import_chunks (user_id, job_id, seq);

CREATE TABLE IF NOT EXISTS voice_turn_results (
    call_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    message_id TEXT NOT NULL DEFAULT '',
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (call_id, turn_id)
);
CREATE INDEX IF NOT EXISTS ix_voice_turn_results_expiry
    ON voice_turn_results(expires_at);

CREATE TABLE IF NOT EXISTS voice_turn_streams (
    call_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    segment INTEGER NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    text_len INTEGER NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    is_final BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (call_id, turn_id, segment)
);
CREATE INDEX IF NOT EXISTS ix_voice_turn_streams_expiry
    ON voice_turn_streams(expires_at);

-- These are runtime-primary triggers, intentionally created DISABLED so 0011
-- can land before the freeze without changing shadow behavior.  The offline
-- cutover tool enables them with the owner role only after all writers stop.
CREATE OR REPLACE FUNCTION enqueue_retired_chat_r2_body()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_key TEXT := NULLIF(OLD.doc->>'body_key', '');
    old_is_pointer BOOLEAN := (
        old_key IS NOT NULL
        AND (NOT (OLD.doc ? 'body_ct') OR OLD.doc->'body_ct' = 'null'::jsonb)
    );
    new_key TEXT;
    new_is_pointer BOOLEAN;
BEGIN
    IF NOT old_is_pointer THEN
        RETURN NULL;
    END IF;

    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM chat_message_archive archived
        WHERE archived.user_id = OLD.user_id
          AND archived.source_seq = OLD.seq
          AND archived.doc->>'body_key' = old_key
          AND (NOT (archived.doc ? 'body_ct')
               OR archived.doc->'body_ct' = 'null'::jsonb)
    ) THEN
        RETURN NULL;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        new_key := NULLIF(NEW.doc->>'body_key', '');
        new_is_pointer := (
            new_key IS NOT NULL
            AND (NOT (NEW.doc ? 'body_ct') OR NEW.doc->'body_ct' = 'null'::jsonb)
        );
        IF new_is_pointer AND new_key IS NOT DISTINCT FROM old_key THEN
            RETURN NULL;
        END IF;
    END IF;

    INSERT INTO chat_r2_cleanup (body_key, user_id, generation, reason)
    VALUES (
        old_key,
        OLD.user_id,
        OLD.storage_generation,
        CASE WHEN TG_OP = 'DELETE' THEN 'row_deleted' ELSE 'pointer_replaced' END
    )
    ON CONFLICT (body_key) DO UPDATE SET
        user_id = EXCLUDED.user_id,
        generation = EXCLUDED.generation,
        reason = EXCLUDED.reason,
        created_at = LEAST(chat_r2_cleanup.created_at, EXCLUDED.created_at),
        attempt_count = 0,
        last_attempt_at = NULL,
        next_attempt_at = now(),
        last_error = '';

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS chat_messages_retire_r2_body ON chat_messages;
CREATE TRIGGER chat_messages_retire_r2_body
AFTER DELETE OR UPDATE OF doc ON chat_messages
FOR EACH ROW EXECUTE FUNCTION enqueue_retired_chat_r2_body();
ALTER TABLE chat_messages DISABLE TRIGGER chat_messages_retire_r2_body;

CREATE OR REPLACE FUNCTION enqueue_retired_archived_chat_r2_body()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_key TEXT := NULLIF(OLD.doc->>'body_key', '');
    old_is_pointer BOOLEAN := (
        old_key IS NOT NULL
        AND (NOT (OLD.doc ? 'body_ct') OR OLD.doc->'body_ct' = 'null'::jsonb)
    );
BEGIN
    IF NOT old_is_pointer THEN
        RETURN NULL;
    END IF;
    INSERT INTO chat_r2_cleanup (body_key, user_id, generation, reason)
    VALUES (old_key, OLD.user_id, OLD.storage_generation, 'archive_row_deleted')
    ON CONFLICT (body_key) DO UPDATE SET
        user_id = EXCLUDED.user_id,
        generation = EXCLUDED.generation,
        reason = EXCLUDED.reason,
        created_at = LEAST(chat_r2_cleanup.created_at, EXCLUDED.created_at),
        attempt_count = 0,
        last_attempt_at = NULL,
        next_attempt_at = now(),
        last_error = '';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS chat_message_archive_retire_r2_body
    ON chat_message_archive;
CREATE TRIGGER chat_message_archive_retire_r2_body
AFTER DELETE ON chat_message_archive
FOR EACH ROW EXECUTE FUNCTION enqueue_retired_archived_chat_r2_body();
ALTER TABLE chat_message_archive
    DISABLE TRIGGER chat_message_archive_retire_r2_body;

CREATE OR REPLACE FUNCTION reject_archived_chat_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'archived chat rows are immutable';
END;
$$;

DROP TRIGGER IF EXISTS chat_message_archive_immutable ON chat_message_archive;
CREATE TRIGGER chat_message_archive_immutable
BEFORE UPDATE ON chat_message_archive
FOR EACH ROW EXECUTE FUNCTION reject_archived_chat_update();
ALTER TABLE chat_message_archive DISABLE TRIGGER chat_message_archive_immutable;
"""

_DOWN = """
DROP TRIGGER IF EXISTS chat_message_archive_immutable ON chat_message_archive;
DROP FUNCTION IF EXISTS reject_archived_chat_update();
DROP TRIGGER IF EXISTS chat_message_archive_retire_r2_body
    ON chat_message_archive;
DROP FUNCTION IF EXISTS enqueue_retired_archived_chat_r2_body();
DROP TRIGGER IF EXISTS chat_messages_retire_r2_body ON chat_messages;
DROP FUNCTION IF EXISTS enqueue_retired_chat_r2_body();
DROP TABLE IF EXISTS voice_turn_streams;
DROP TABLE IF EXISTS voice_turn_results;
DROP TABLE IF EXISTS genesis_import_chunks;
DROP TABLE IF EXISTS frame_envelopes;
DROP INDEX IF EXISTS ix_chat_message_archive_user_msg;
DROP INDEX IF EXISTS ix_chat_message_archive_user_cleared;
DROP INDEX IF EXISTS chat_r2_inventory_due_idx;
DROP INDEX IF EXISTS chat_r2_cleanup_user_due_idx;
DROP INDEX IF EXISTS chat_r2_cleanup_due_idx;
ALTER TABLE chat_messages DROP COLUMN IF EXISTS storage_generation;
ALTER TABLE genesis_import_jobs
    DROP COLUMN IF EXISTS worker_claimed_at,
    DROP COLUMN IF EXISTS worker_claimed_by,
    DROP COLUMN IF EXISTS resident_attempts,
    DROP COLUMN IF EXISTS resident_heartbeat_at,
    DROP COLUMN IF EXISTS resident_claimed_at,
    DROP COLUMN IF EXISTS resident_consumer_id;
ALTER TABLE v2_conversation_summary
    DROP COLUMN IF EXISTS effective_batch_cap;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
