"""Fence chat-body generations and durably queue retired R2 objects.

Revision ID: 0036_chat_r2_lifecycle
Revises: 0035_merge_v2_tee_reconcile
"""

from alembic import op


revision = "0036_chat_r2_lifecycle"
down_revision = "0035_merge_v2_tee_reconcile"
branch_labels = None
depends_on = None


_UP = r"""
-- This row is deliberately NOT a child of users.  It is both the lock that
-- serializes append/clear and the durable proof that older object generations
-- are retired.  Account deletion must not erase that proof before R2 cleanup.
CREATE TABLE IF NOT EXISTS chat_r2_lifecycle (
    user_id                   TEXT PRIMARY KEY,
    generation                BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    inventory_pending         BOOLEAN NOT NULL DEFAULT FALSE,
    inventory_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    inventory_attempt_count   INTEGER NOT NULL DEFAULT 0
                              CHECK (inventory_attempt_count >= 0),
    inventory_last_error      TEXT NOT NULL DEFAULT '',
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO chat_r2_lifecycle (user_id)
SELECT DISTINCT user_id FROM chat_messages
ON CONFLICT (user_id) DO NOTHING;

ALTER TABLE chat_messages
  ADD COLUMN IF NOT EXISTS storage_generation BIGINT NOT NULL DEFAULT 0;

-- No users/chat FK on purpose: a row is useful precisely after its source row
-- (and possibly its user) has committed deletion.
CREATE TABLE IF NOT EXISTS chat_r2_cleanup (
    body_key        TEXT PRIMARY KEY CHECK (body_key <> ''),
    user_id         TEXT NOT NULL,
    generation      BIGINT,
    reason          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS chat_r2_cleanup_due_idx
  ON chat_r2_cleanup (next_attempt_at, created_at, body_key);
CREATE INDEX IF NOT EXISTS chat_r2_cleanup_user_due_idx
  ON chat_r2_cleanup (user_id, next_attempt_at, created_at, body_key);
CREATE INDEX IF NOT EXISTS chat_r2_inventory_due_idx
  ON chat_r2_lifecycle (inventory_next_attempt_at, updated_at, user_id)
  WHERE inventory_pending;

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

DROP TRIGGER IF EXISTS chat_messages_retire_r2_body ON chat_messages;
CREATE TRIGGER chat_messages_retire_r2_body
AFTER DELETE OR UPDATE OF doc ON chat_messages
FOR EACH ROW EXECUTE FUNCTION enqueue_retired_chat_r2_body();
"""


_DOWN = r"""
DROP TRIGGER IF EXISTS chat_messages_retire_r2_body ON chat_messages;
DROP FUNCTION IF EXISTS enqueue_retired_chat_r2_body();
DROP INDEX IF EXISTS chat_r2_inventory_due_idx;
DROP INDEX IF EXISTS chat_r2_cleanup_user_due_idx;
DROP INDEX IF EXISTS chat_r2_cleanup_due_idx;
DROP TABLE IF EXISTS chat_r2_cleanup;
ALTER TABLE chat_messages DROP COLUMN IF EXISTS storage_generation;
DROP TABLE IF EXISTS chat_r2_lifecycle;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
