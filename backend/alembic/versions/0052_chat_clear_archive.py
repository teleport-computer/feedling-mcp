"""Retain encrypted chat source rows across Clear Chat.

Revision ID: 0052_chat_clear_archive
Revises: 0051_v2_capture_batches
"""

from alembic import op


revision = "0052_chat_clear_archive"
down_revision = "0051_v2_capture_batches"
branch_labels = None
depends_on = None


_UP = r"""
CREATE TABLE IF NOT EXISTS chat_message_archive (
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  source_seq BIGINT NOT NULL,
  msg_id TEXT NOT NULL,
  ts DOUBLE PRECISION NOT NULL,
  doc JSONB NOT NULL,
  storage_generation BIGINT NOT NULL DEFAULT 0,
  clear_generation BIGINT NOT NULL,
  cleared_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, source_seq),
  CONSTRAINT ck_chat_message_archive_source_seq CHECK (source_seq > 0),
  CONSTRAINT ck_chat_message_archive_storage_generation
    CHECK (storage_generation >= 0),
  CONSTRAINT ck_chat_message_archive_clear_generation
    CHECK (clear_generation > 0)
);

CREATE INDEX IF NOT EXISTS ix_chat_message_archive_user_cleared
  ON chat_message_archive (user_id, cleared_at, source_seq);
CREATE INDEX IF NOT EXISTS ix_chat_message_archive_user_msg
  ON chat_message_archive (user_id, msg_id);

-- Clear Chat copies a row into the encrypted archive before deleting the live
-- row.  In that one case the R2 ciphertext is still referenced and must not be
-- retired.  Ordinary single-message deletion and replacement keep the prior
-- cleanup semantics.
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

CREATE OR REPLACE FUNCTION reject_archived_chat_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'archived chat rows are immutable';
END;
$$;

DROP TRIGGER IF EXISTS chat_message_archive_immutable
  ON chat_message_archive;
CREATE TRIGGER chat_message_archive_immutable
BEFORE UPDATE ON chat_message_archive
FOR EACH ROW EXECUTE FUNCTION reject_archived_chat_update();
"""


_DOWN = r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM chat_message_archive LIMIT 1) THEN
    RAISE EXCEPTION
      'cannot downgrade 0052_chat_clear_archive while retained chat rows exist';
  END IF;
END;
$$;

DROP TRIGGER IF EXISTS chat_message_archive_immutable
  ON chat_message_archive;
DROP FUNCTION IF EXISTS reject_archived_chat_update();
DROP TRIGGER IF EXISTS chat_message_archive_retire_r2_body
  ON chat_message_archive;
DROP FUNCTION IF EXISTS enqueue_retired_archived_chat_r2_body();

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

DROP TABLE IF EXISTS chat_message_archive;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
