"""Canonical, content-free provider-attempt accounting ledger.

Revision ID: 0076_llm_provider_attempts
Revises: 0075_v2_usage_rollup
"""

from alembic import op


revision = "0076_llm_provider_attempts"
down_revision = "0075_v2_usage_rollup"
branch_labels = None
depends_on = None


_SCHEMA_UP = """
CREATE TABLE IF NOT EXISTS llm_provider_attempts (
  attempt_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  installation_id TEXT,
  runtime TEXT,
  lane TEXT NOT NULL,
  job_id BIGINT,
  turn_id TEXT,
  round_id TEXT,
  call_id TEXT NOT NULL,
  outer_attempt_ordinal INTEGER NOT NULL,
  inner_attempt_ordinal INTEGER NOT NULL,
  retry_kind TEXT NOT NULL DEFAULT 'initial',
  requested_provider TEXT NOT NULL,
  resolved_provider TEXT NOT NULL,
  requested_model TEXT NOT NULL,
  resolved_model TEXT NOT NULL,
  transport TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  state TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT 'unknown',
  error_class TEXT NOT NULL DEFAULT 'none',
  provider_request_id TEXT,
  input_tokens BIGINT,
  output_tokens BIGINT,
  reasoning_tokens BIGINT,
  cache_read_tokens BIGINT,
  cache_write_tokens BIGINT,
  cache_miss_tokens BIGINT,
  usage_known BOOLEAN NOT NULL DEFAULT false,
  usage_unknown_reason TEXT,
  possibly_billed BOOLEAN NOT NULL DEFAULT false,
  latency_ms DOUBLE PRECISION,
  ttft_ms DOUBLE PRECISION,
  cost NUMERIC(20,8),
  currency TEXT,
  rate_card_version TEXT,
  source TEXT NOT NULL,
  completeness TEXT NOT NULL DEFAULT 'started_only',
  revision INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ux_llm_provider_attempts_logical_ordinal
    UNIQUE (call_id, outer_attempt_ordinal, inner_attempt_ordinal),
  CONSTRAINT ck_llm_provider_attempts_ordinals CHECK (
    outer_attempt_ordinal >= 0 AND inner_attempt_ordinal >= 0
  ),
  CONSTRAINT ck_llm_provider_attempts_lane CHECK (lane IN (
    'chat','heartbeat','scheduled','manual_wake','screen_watch','maintenance',
    'capture','dream','trajectory_review','unknown'
  )),
  CONSTRAINT ck_llm_provider_attempts_retry_kind CHECK (retry_kind IN (
    'initial','outer_retry','compatibility_retry','failover'
  )),
  CONSTRAINT ck_llm_provider_attempts_state CHECK (state IN ('started','completed')),
  CONSTRAINT ck_llm_provider_attempts_outcome CHECK (outcome IN (
    'unknown','succeeded','failed','timed_out','cancelled'
  )),
  CONSTRAINT ck_llm_provider_attempts_error_class CHECK (error_class IN (
    'none','authentication','authorization','rate_limit','timeout','network',
    'provider','protocol','validation','cancelled','unknown'
  )),
  CONSTRAINT ck_llm_provider_attempts_source CHECK (source IN (
    'runtime_recorder','legacy_best_effort'
  )),
  CONSTRAINT ck_llm_provider_attempts_completeness CHECK (completeness IN (
    'started_only','complete','usage_unknown','legacy_best_effort'
  )),
  CONSTRAINT ck_llm_provider_attempts_tokens_nonnegative CHECK (
    (input_tokens IS NULL OR input_tokens >= 0) AND
    (output_tokens IS NULL OR output_tokens >= 0) AND
    (reasoning_tokens IS NULL OR reasoning_tokens >= 0) AND
    (cache_read_tokens IS NULL OR cache_read_tokens >= 0) AND
    (cache_write_tokens IS NULL OR cache_write_tokens >= 0) AND
    (cache_miss_tokens IS NULL OR cache_miss_tokens >= 0)
  ),
  CONSTRAINT ck_llm_provider_attempts_measurements_nonnegative CHECK (
    (latency_ms IS NULL OR latency_ms >= 0) AND
    (ttft_ms IS NULL OR ttft_ms >= 0) AND
    (cost IS NULL OR cost >= 0) AND revision >= 0
  )
);

CREATE TABLE IF NOT EXISTS llm_provider_attempt_corrections (
  id BIGSERIAL PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES llm_provider_attempts(attempt_id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  input_tokens_delta BIGINT,
  output_tokens_delta BIGINT,
  reasoning_tokens_delta BIGINT,
  cache_read_tokens_delta BIGINT,
  cache_write_tokens_delta BIGINT,
  cache_miss_tokens_delta BIGINT,
  cost_delta NUMERIC(20,8),
  currency TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ux_llm_provider_attempt_corrections_revision UNIQUE (attempt_id, revision),
  CONSTRAINT ck_llm_provider_attempt_corrections_revision CHECK (revision > 0)
);

CREATE INDEX IF NOT EXISTS ix_llm_provider_attempt_corrections_attempt
  ON llm_provider_attempt_corrections (attempt_id, revision);
CREATE INDEX IF NOT EXISTS ix_llm_provider_attempt_corrections_user_id
  ON llm_provider_attempt_corrections (user_id);

CREATE TABLE IF NOT EXISTS llm_rate_cards (
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  version TEXT NOT NULL,
  currency TEXT NOT NULL,
  input_cost_per_million NUMERIC(20,8) NOT NULL DEFAULT 0,
  output_cost_per_million NUMERIC(20,8) NOT NULL DEFAULT 0,
  reasoning_cost_per_million NUMERIC(20,8) NOT NULL DEFAULT 0,
  cache_read_cost_per_million NUMERIC(20,8) NOT NULL DEFAULT 0,
  cache_write_cost_per_million NUMERIC(20,8) NOT NULL DEFAULT 0,
  cache_miss_cost_per_million NUMERIC(20,8) NOT NULL DEFAULT 0,
  effective_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, model, version),
  CONSTRAINT ux_llm_rate_cards_effective_at UNIQUE (provider, model, effective_at),
  CONSTRAINT ck_llm_rate_cards_nonnegative CHECK (
    input_cost_per_million >= 0 AND output_cost_per_million >= 0 AND
    reasoning_cost_per_million >= 0 AND cache_read_cost_per_million >= 0 AND
    cache_write_cost_per_million >= 0 AND cache_miss_cost_per_million >= 0
  )
);

CREATE TABLE IF NOT EXISTS llm_usage_rollup_watermarks (
  rollup_name TEXT PRIMARY KEY,
  attempt_finished_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch'::timestamptz,
  attempt_id TEXT NOT NULL DEFAULT '',
  late_correction_id BIGINT NOT NULL DEFAULT 0,
  replay_generation BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_llm_usage_rollup_watermarks_correction_cursor
    CHECK (late_correction_id >= 0),
  CONSTRAINT ck_llm_usage_rollup_watermarks_replay_generation
    CHECK (replay_generation >= 0)
);

CREATE OR REPLACE FUNCTION reject_llm_rate_card_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'llm_rate_cards are immutable by version';
END;
$$;
DROP TRIGGER IF EXISTS trg_llm_rate_cards_immutable ON llm_rate_cards;
CREATE TRIGGER trg_llm_rate_cards_immutable
  BEFORE UPDATE OR DELETE ON llm_rate_cards
  FOR EACH ROW EXECUTE FUNCTION reject_llm_rate_card_mutation();
"""


_CONCURRENT_INDEXES = {
    "ix_llm_provider_attempts_user_started":
        "CREATE INDEX CONCURRENTLY ix_llm_provider_attempts_user_started "
        "ON llm_provider_attempts (user_id, started_at DESC)",
    "ix_llm_provider_attempts_finished":
        "CREATE INDEX CONCURRENTLY ix_llm_provider_attempts_finished "
        "ON llm_provider_attempts (finished_at DESC) "
        "WHERE finished_at IS NOT NULL",
    "ix_llm_provider_attempts_provider_model_finished":
        "CREATE INDEX CONCURRENTLY ix_llm_provider_attempts_provider_model_finished "
        "ON llm_provider_attempts (resolved_provider, resolved_model, finished_at DESC) "
        "WHERE finished_at IS NOT NULL",
    "ix_llm_provider_attempts_call":
        "CREATE INDEX CONCURRENTLY ix_llm_provider_attempts_call "
        "ON llm_provider_attempts (call_id, outer_attempt_ordinal, inner_attempt_ordinal)",
}


def _index_validity(name: str) -> bool | None:
    row = op.get_bind().exec_driver_sql(
        "SELECT idx.indisvalid FROM pg_class AS cls "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        "WHERE cls.relkind='i' AND cls.relname=%s AND pg_table_is_visible(cls.oid)",
        (name,),
    ).fetchone()
    return None if row is None else bool(row[0])


def upgrade() -> None:
    op.execute(_SCHEMA_UP)
    with op.get_context().autocommit_block():
        for name, sql in _CONCURRENT_INDEXES.items():
            if _index_validity(name) is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            if _index_validity(name) is not True:
                op.execute(sql)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _CONCURRENT_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS llm_usage_rollup_watermarks")
    op.execute("DROP TABLE IF EXISTS llm_rate_cards")
    op.execute("DROP TABLE IF EXISTS llm_provider_attempt_corrections")
    op.execute("DROP TABLE IF EXISTS llm_provider_attempts")
    op.execute("DROP FUNCTION IF EXISTS reject_llm_rate_card_mutation()")
