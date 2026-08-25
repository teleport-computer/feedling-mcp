"""Durable per-user chat versions and compact change events.

Revision ID: 0101_chat_change_events
Revises: 0100_lane_rollup_access_paths

PostgreSQL NOTIFY remains a best-effort latency hint.  These tables are the
replayable correctness source for workers that miss, duplicate, or reorder a
wake while retaining only a bounded chat hot window.
"""

from alembic import op


revision = "0101_chat_change_events"
down_revision = "0100_lane_rollup_access_paths"
branch_labels = None
depends_on = None


_POLL_INDEX = (
    "CREATE INDEX CONCURRENTLY ix_chat_messages_user_ts_seq "
    "ON chat_messages (user_id, ts, seq)"
)


_SCHEMA_UP = """
CREATE TABLE IF NOT EXISTS chat_change_state (
    user_id TEXT PRIMARY KEY
        REFERENCES users(user_id) ON DELETE CASCADE,
    version BIGINT NOT NULL CHECK (version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_change_events (
    user_id TEXT NOT NULL
        REFERENCES chat_change_state(user_id) ON DELETE CASCADE,
    version BIGINT NOT NULL CHECK (version > 0),
    operation TEXT NOT NULL
        CHECK (operation IN ('upsert', 'delete', 'reset')),
    message_ids TEXT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, version),
    CONSTRAINT ck_chat_change_event_shape CHECK (
        (operation = 'reset' AND cardinality(message_ids) = 0)
        OR (operation IN ('upsert', 'delete')
            AND cardinality(message_ids) BETWEEN 1 AND 64)
    )
);

CREATE INDEX IF NOT EXISTS ix_chat_change_events_created_at
    ON chat_change_events (created_at, user_id, version);
"""


_CAPTURE_UP = r"""
CREATE OR REPLACE FUNCTION feedling_record_chat_change(
    changed_user_id TEXT,
    changed_operation TEXT,
    changed_message_ids TEXT[]
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    next_version BIGINT;
BEGIN
    -- A users DELETE cascades through chat_messages after the parent row has
    -- disappeared.  Do not recreate child control rows or block account reset.
    IF changed_user_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM users WHERE user_id = changed_user_id
    ) THEN
        RETURN;
    END IF;

    INSERT INTO chat_change_state AS state (user_id, version, updated_at)
    VALUES (changed_user_id, 1, clock_timestamp())
    ON CONFLICT (user_id) DO UPDATE SET
        version = state.version + 1,
        updated_at = clock_timestamp()
    RETURNING version INTO next_version;

    INSERT INTO chat_change_events (
        user_id, version, operation, message_ids, created_at
    ) VALUES (
        changed_user_id,
        next_version,
        changed_operation,
        changed_message_ids,
        clock_timestamp()
    );

    PERFORM pg_notify(
        'feedling_wake',
        jsonb_build_object(
            'v', 2,
            'c', 'chat',
            'u', changed_user_id,
            'r', next_version
        )::text
    );
END;
$$;

CREATE OR REPLACE FUNCTION feedling_capture_chat_insert_changes()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    changed RECORD;
BEGIN
    FOR changed IN
        SELECT
            user_id,
            array_agg(DISTINCT msg_id ORDER BY msg_id)::TEXT[] AS message_ids
        FROM new_rows
        GROUP BY user_id
    LOOP
        IF cardinality(changed.message_ids) > 64 THEN
            PERFORM feedling_record_chat_change(
                changed.user_id, 'reset', ARRAY[]::TEXT[]
            );
        ELSE
            PERFORM feedling_record_chat_change(
                changed.user_id, 'upsert', changed.message_ids
            );
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION feedling_capture_chat_update_changes()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    changed RECORD;
BEGIN
    FOR changed IN
        WITH touched_users AS (
            SELECT user_id FROM old_rows
            UNION
            SELECT user_id FROM new_rows
        )
        SELECT
            touched.user_id,
            COALESCE((
                SELECT array_agg(DISTINCT msg_id ORDER BY msg_id)::TEXT[]
                FROM old_rows WHERE user_id = touched.user_id
            ), ARRAY[]::TEXT[]) AS old_ids,
            COALESCE((
                SELECT array_agg(DISTINCT msg_id ORDER BY msg_id)::TEXT[]
                FROM new_rows WHERE user_id = touched.user_id
            ), ARRAY[]::TEXT[]) AS new_ids
        FROM touched_users AS touched
    LOOP
        IF changed.old_ids IS DISTINCT FROM changed.new_ids
           OR cardinality(changed.new_ids) > 64 THEN
            PERFORM feedling_record_chat_change(
                changed.user_id, 'reset', ARRAY[]::TEXT[]
            );
        ELSE
            PERFORM feedling_record_chat_change(
                changed.user_id, 'upsert', changed.new_ids
            );
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION feedling_capture_chat_delete_changes()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    changed RECORD;
BEGIN
    FOR changed IN
        SELECT
            user_id,
            array_agg(DISTINCT msg_id ORDER BY msg_id)::TEXT[] AS message_ids
        FROM old_rows
        GROUP BY user_id
    LOOP
        IF cardinality(changed.message_ids) > 64 THEN
            PERFORM feedling_record_chat_change(
                changed.user_id, 'reset', ARRAY[]::TEXT[]
            );
        ELSE
            PERFORM feedling_record_chat_change(
                changed.user_id, 'delete', changed.message_ids
            );
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS chat_change_capture_insert ON chat_messages;
CREATE TRIGGER chat_change_capture_insert
AFTER INSERT ON chat_messages
REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION feedling_capture_chat_insert_changes();

DROP TRIGGER IF EXISTS chat_change_capture_update ON chat_messages;
CREATE TRIGGER chat_change_capture_update
AFTER UPDATE ON chat_messages
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION feedling_capture_chat_update_changes();

DROP TRIGGER IF EXISTS chat_change_capture_delete ON chat_messages;
CREATE TRIGGER chat_change_capture_delete
AFTER DELETE ON chat_messages
REFERENCING OLD TABLE AS old_rows
FOR EACH STATEMENT EXECUTE FUNCTION feedling_capture_chat_delete_changes();
"""


def _poll_index_validity() -> bool | None:
    row = op.get_bind().exec_driver_sql(
        "SELECT (idx.indisvalid "
        "AND idx.indrelid='chat_messages'::regclass "
        "AND am.amname='btree' AND NOT idx.indisunique "
        "AND idx.indnkeyatts=3 AND idx.indnatts=3 "
        "AND idx.indexprs IS NULL AND idx.indpred IS NULL "
        "AND pg_get_indexdef(idx.indexrelid,1,true)='user_id' "
        "AND pg_get_indexdef(idx.indexrelid,2,true)='ts' "
        "AND pg_get_indexdef(idx.indexrelid,3,true)='seq') "
        "FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        "JOIN pg_am AS am ON am.oid=cls.relam "
        "WHERE cls.relkind='i' "
        "AND cls.relname='ix_chat_messages_user_ts_seq' "
        "AND pg_table_is_visible(cls.oid)"
    ).fetchone()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    op.execute(_SCHEMA_UP)
    validity = _poll_index_validity()
    with op.get_context().autocommit_block():
        if validity is False:
            op.execute(
                "DROP INDEX CONCURRENTLY IF EXISTS ix_chat_messages_user_ts_seq"
            )
        if validity is not True:
            op.execute(_POLL_INDEX)
    op.execute(_CAPTURE_UP)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS chat_change_capture_delete ON chat_messages;"
        "DROP TRIGGER IF EXISTS chat_change_capture_update ON chat_messages;"
        "DROP TRIGGER IF EXISTS chat_change_capture_insert ON chat_messages;"
        "DROP FUNCTION IF EXISTS feedling_capture_chat_delete_changes();"
        "DROP FUNCTION IF EXISTS feedling_capture_chat_update_changes();"
        "DROP FUNCTION IF EXISTS feedling_capture_chat_insert_changes();"
        "DROP FUNCTION IF EXISTS feedling_record_chat_change(TEXT,TEXT,TEXT[]);"
        "DROP TABLE IF EXISTS chat_change_events;"
        "DROP TABLE IF EXISTS chat_change_state;"
    )
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_chat_messages_user_ts_seq"
        )
