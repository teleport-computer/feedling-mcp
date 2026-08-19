"""Add the content-free plaintext-shadow replication control plane.

Revision ID: 0026_plaintext_shadow_control
Revises: 0025_lane_rollup_voice

The same TEE migration chain is used by the promoted primary and the new
plaintext target.  This revision therefore creates the trigger *function* and
control tables but intentionally installs no per-table triggers.  Operators
install those only on the authoritative primary after Gate 1.
"""

from alembic import op


revision = "0026_plaintext_shadow_control"
down_revision = "0025_lane_rollup_voice"
branch_labels = None
depends_on = None


_UP = r"""
CREATE SEQUENCE IF NOT EXISTS plaintext_shadow_generation_seq AS BIGINT;

CREATE TABLE IF NOT EXISTS plaintext_shadow_dirty_keys (
    table_name       TEXT        NOT NULL,
    key_json         JSONB       NOT NULL,
    operation        TEXT        NOT NULL
        CONSTRAINT plaintext_shadow_dirty_operation
        CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    generation       BIGINT      NOT NULL
        DEFAULT nextval('plaintext_shadow_generation_seq'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts         INTEGER     NOT NULL DEFAULT 0
        CONSTRAINT plaintext_shadow_dirty_attempts_nonneg CHECK (attempts >= 0),
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error_slug  TEXT,
    quarantined_at   TIMESTAMPTZ,
    PRIMARY KEY (table_name, key_json)
);

CREATE INDEX IF NOT EXISTS plaintext_shadow_dirty_ready_idx
    ON plaintext_shadow_dirty_keys (next_attempt_at, generation)
    WHERE quarantined_at IS NULL;

CREATE TABLE IF NOT EXISTS plaintext_shadow_sync_runs (
    id                      BIGSERIAL PRIMARY KEY,
    ran_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms             BIGINT,
    applied                 BIGINT NOT NULL DEFAULT 0,
    deleted                 BIGINT NOT NULL DEFAULT 0,
    retried                 BIGINT NOT NULL DEFAULT 0,
    quarantined             BIGINT NOT NULL DEFAULT 0,
    pending                 BIGINT NOT NULL DEFAULT 0,
    oldest_pending_seconds  DOUBLE PRECISION,
    target_ok               BOOLEAN NOT NULL DEFAULT FALSE,
    target_probe_ms         DOUBLE PRECISION,
    verify_ok               BOOLEAN,
    table_metrics           JSONB NOT NULL DEFAULT '{}'::jsonb
        CONSTRAINT plaintext_shadow_table_metrics_object
        CHECK (jsonb_typeof(table_metrics) = 'object')
);

CREATE INDEX IF NOT EXISTS plaintext_shadow_sync_runs_ran_at_idx
    ON plaintext_shadow_sync_runs (ran_at DESC);

CREATE TABLE IF NOT EXISTS plaintext_shadow_restore_evidence (
    id                BIGSERIAL PRIMARY KEY,
    restored_at       TIMESTAMPTZ NOT NULL,
    source_backup_at  TIMESTAMPTZ NOT NULL,
    schema_head       TEXT        NOT NULL,
    verifier_digest   TEXT        NOT NULL,
    operator_id       TEXT        NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT plaintext_shadow_restore_time_order
        CHECK (source_backup_at <= restored_at AND restored_at <= recorded_at),
    CONSTRAINT plaintext_shadow_restore_expiry
        CHECK (expires_at > restored_at),
    CONSTRAINT plaintext_shadow_restore_scalars_nonempty
        CHECK (schema_head <> '' AND verifier_digest <> '' AND operator_id <> '')
);

CREATE OR REPLACE FUNCTION feedling_capture_plaintext_shadow_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_row JSONB;
    dirty_key  JSONB := '{}'::jsonb;
    key_name   TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        source_row := to_jsonb(OLD);
    ELSE
        source_row := to_jsonb(NEW);
    END IF;

    -- Zero arguments deliberately means a table-level marker.  Some legacy
    -- tables use an authentication token or object-storage key as their
    -- primary key; copying that value into the control plane would leak a
    -- credential.  Their consumer performs one table reconcile instead.
    IF TG_NARGS > 0 THEN
        FOREACH key_name IN ARRAY TG_ARGV LOOP
            IF NOT source_row ? key_name OR source_row->key_name = 'null'::jsonb THEN
                RAISE EXCEPTION 'plaintext shadow capture key column is missing or null: %', key_name;
            END IF;
            dirty_key := dirty_key || jsonb_build_object(key_name, source_row->key_name);
        END LOOP;
    END IF;

    INSERT INTO plaintext_shadow_dirty_keys
        (table_name, key_json, operation, generation, created_at, attempts,
         next_attempt_at, last_error_slug, quarantined_at)
    VALUES
        (TG_TABLE_NAME, dirty_key, TG_OP,
         nextval('plaintext_shadow_generation_seq'), now(), 0, now(), NULL, NULL)
    ON CONFLICT (table_name, key_json) DO UPDATE SET
        operation = EXCLUDED.operation,
        generation = nextval('plaintext_shadow_generation_seq'),
        created_at = now(),
        attempts = 0,
        next_attempt_at = now(),
        last_error_slug = NULL,
        quarantined_at = NULL;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
"""


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0026_plaintext_shadow_control"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
