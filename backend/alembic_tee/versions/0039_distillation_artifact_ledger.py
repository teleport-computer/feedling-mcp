"""Add the distillation artifact-attempt ledger and Beijing-day cells.

Revision ID: 0039_distill_artifact_ledger
Revises: 0038_v2_wake_followup_marker
"""

from alembic import op


revision = "0039_distill_artifact_ledger"
down_revision = "0038_v2_wake_followup_marker"
branch_labels = None
depends_on = None


_UP = """
ALTER TABLE genesis_import_jobs
    ADD COLUMN IF NOT EXISTS failed_phase TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS distillation_artifact_attempts (
    attempt_id       TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    job_id           TEXT NOT NULL,
    flow             TEXT NOT NULL,
    distill_kind     TEXT NOT NULL,
    artifact         TEXT NOT NULL,
    access_path      TEXT NOT NULL,
    outcome          TEXT NOT NULL DEFAULT '',
    terminal_result  TEXT NOT NULL DEFAULT '',
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    CONSTRAINT distillation_attempt_flow CHECK
        (flow IN ('history_import', 'genesis')),
    CONSTRAINT distillation_attempt_kind CHECK
        (distill_kind IN ('onboarding', 'redistill')),
    CONSTRAINT distillation_attempt_artifact CHECK
        (artifact IN ('memory', 'identity', 'persona', 'voice', 'profile', 'greeting')),
    CONSTRAINT distillation_attempt_access_path CHECK
        (access_path IN ('self_hosted', 'resident_v1', 'apikey_v1', 'apikey_v2',
                         'unbound_no_route', 'hosted_unclassified_v1',
                         'v2_control_v1_source')),
    CONSTRAINT distillation_attempt_outcome CHECK
        (outcome = '' OR outcome ~ '^[a-z0-9_:-]{1,80}$'),
    CONSTRAINT distillation_attempt_terminal CHECK
        (terminal_result IN ('', 'succeeded', 'failed', 'no_write')),
    CONSTRAINT distillation_attempt_finished_shape CHECK
        ((finished_at IS NULL AND outcome = '' AND terminal_result = '') OR
         (finished_at IS NOT NULL AND outcome <> '' AND terminal_result <> ''))
);
CREATE INDEX IF NOT EXISTS distillation_attempts_finished_idx
    ON distillation_artifact_attempts (finished_at, access_path, distill_kind, artifact)
    WHERE finished_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS distillation_attempts_job_idx
    ON distillation_artifact_attempts (user_id, job_id, started_at);

CREATE TABLE IF NOT EXISTS distillation_artifact_daily_rollup (
    day              TEXT NOT NULL,
    access_path      TEXT NOT NULL,
    distill_kind     TEXT NOT NULL,
    artifact         TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    terminal_result  TEXT NOT NULL,
    attempts         BIGINT NOT NULL DEFAULT 0,
    frozen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, access_path, distill_kind, artifact, outcome, terminal_result),
    CONSTRAINT distillation_daily_day CHECK
        (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT distillation_daily_access_path CHECK
        (access_path IN ('self_hosted', 'resident_v1', 'apikey_v1', 'apikey_v2',
                         'unbound_no_route', 'hosted_unclassified_v1',
                         'v2_control_v1_source')),
    CONSTRAINT distillation_daily_kind CHECK
        (distill_kind IN ('onboarding', 'redistill')),
    CONSTRAINT distillation_daily_artifact CHECK
        (artifact IN ('memory', 'identity', 'persona', 'voice', 'profile', 'greeting')),
    CONSTRAINT distillation_daily_outcome CHECK
        (outcome ~ '^[a-z0-9_:-]{1,80}$'),
    CONSTRAINT distillation_daily_terminal CHECK
        (terminal_result IN ('succeeded', 'failed', 'no_write')),
    CONSTRAINT distillation_daily_attempts_positive CHECK (attempts > 0)
);

CREATE TABLE IF NOT EXISTS distillation_rollup_watermark (
    scope           TEXT PRIMARY KEY,
    effective_from  TEXT NOT NULL,
    through_day     TEXT NOT NULL,
    frozen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT distillation_watermark_scope CHECK (scope = 'artifact_attempts'),
    CONSTRAINT distillation_watermark_from CHECK
        (effective_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT distillation_watermark_through CHECK
        (through_day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
);
"""


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0039_distill_artifact_ledger"]'::jsonb)::text,
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
