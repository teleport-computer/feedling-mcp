"""Deletion-safe daily rollups for canonical provider attempts.

Revision ID: 0077_llm_usage_attempt_rollups
Revises: 0076_llm_provider_attempts
"""

from alembic import op


revision = "0077_llm_usage_attempt_rollups"
down_revision = "0076_llm_provider_attempts"
branch_labels = None
depends_on = None


_TOKEN_KINDS = (
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
    "cache_miss",
)


def _token_columns() -> str:
    return ",\n  ".join(
        f"{kind}_tokens_sum BIGINT NOT NULL DEFAULT 0,\n"
        f"  {kind}_tokens_known_count BIGINT NOT NULL DEFAULT 0"
        for kind in _TOKEN_KINDS
    )


def _token_checks() -> str:
    return " AND\n    ".join(
        f"{kind}_tokens_known_count >= 0 AND "
        f"{kind}_tokens_known_count <= attempts"
        for kind in _TOKEN_KINDS
    )


_SCHEMA_UP = f"""
CREATE OR REPLACE FUNCTION llm_ttft_samples_are_sorted(samples double precision[])
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT COALESCE(bool_and(
    samples[i] IS NOT NULL
    AND samples[i] >= 0
    AND samples[i] <> 'NaN'::double precision
    AND abs(samples[i]) <> 'Infinity'::double precision
    AND (i = 1 OR samples[i - 1] <= samples[i])
  ), true)
  FROM generate_subscripts(samples, 1) AS i
$$;

CREATE TABLE IF NOT EXISTS llm_usage_daily_attempt_dimensions (
  local_day DATE NOT NULL,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  cohort_lane TEXT NOT NULL,
  requested_provider TEXT NOT NULL,
  requested_model TEXT NOT NULL,
  resolved_provider TEXT NOT NULL,
  resolved_model TEXT NOT NULL,
  effective_usage_known BOOLEAN NOT NULL,
  cost_kind TEXT NOT NULL,
  currency TEXT,
  attempts BIGINT NOT NULL DEFAULT 0,
  retry_attempts BIGINT NOT NULL DEFAULT 0,
  failover_attempts BIGINT NOT NULL DEFAULT 0,
  failed_attempts BIGINT NOT NULL DEFAULT 0,
  possibly_billed_attempts BIGINT NOT NULL DEFAULT 0,
  {_token_columns()},
  authoritative_cost_attempts BIGINT NOT NULL DEFAULT 0,
  estimated_cost_attempts BIGINT NOT NULL DEFAULT 0,
  unknown_cost_attempts BIGINT NOT NULL DEFAULT 0,
  cost_amount NUMERIC(28,8) NOT NULL DEFAULT 0,
  ttft_samples DOUBLE PRECISION[] NOT NULL DEFAULT '{{}}'::DOUBLE PRECISION[],
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ux_llm_usage_daily_attempt_dimensions_grain
    UNIQUE NULLS NOT DISTINCT (
      local_day,user_id,cohort_lane,requested_provider,requested_model,
      resolved_provider,resolved_model,effective_usage_known,cost_kind,currency
    ),
  CONSTRAINT ck_llm_usage_daily_attempt_dimensions_identity CHECK (
    cohort_lane ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,63}}$' AND
    requested_provider ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{{0,79}}$' AND
    resolved_provider ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{{0,79}}$' AND
    requested_model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,159}}$' AND
    resolved_model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,159}}$'
  ),
  CONSTRAINT ck_llm_usage_daily_attempt_dimensions_cost CHECK (
    cost_kind IN ('authoritative','estimated','unknown') AND
    authoritative_cost_attempts >= 0 AND
    estimated_cost_attempts >= 0 AND
    unknown_cost_attempts >= 0 AND
    authoritative_cost_attempts + estimated_cost_attempts
      + unknown_cost_attempts = attempts AND
    ((cost_kind = 'authoritative' AND authoritative_cost_attempts = attempts
      AND estimated_cost_attempts = 0 AND unknown_cost_attempts = 0
      AND (currency IS NULL OR currency ~ '^[A-Z]{{3}}$'))
     OR (cost_kind = 'estimated' AND estimated_cost_attempts = attempts
      AND authoritative_cost_attempts = 0 AND unknown_cost_attempts = 0
      AND cost_amount >= 0 AND currency ~ '^[A-Z]{{3}}$')
     OR (cost_kind = 'unknown' AND unknown_cost_attempts = attempts
      AND authoritative_cost_attempts = 0 AND estimated_cost_attempts = 0
      AND currency IS NULL AND cost_amount = 0))
  ),
  CONSTRAINT ck_llm_usage_daily_attempt_dimensions_counts CHECK (
    attempts >= 0 AND retry_attempts >= 0 AND retry_attempts <= attempts AND
    failover_attempts >= 0 AND failover_attempts <= attempts AND
    failed_attempts >= 0 AND failed_attempts <= attempts AND
    possibly_billed_attempts >= 0 AND possibly_billed_attempts <= attempts AND
    {_token_checks()} AND
    cardinality(ttft_samples) <= attempts AND
    llm_ttft_samples_are_sorted(ttft_samples)
  )
);

CREATE TABLE IF NOT EXISTS llm_usage_daily_call_memberships (
  local_day DATE NOT NULL,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  cohort_lane TEXT NOT NULL,
  call_id TEXT NOT NULL,
  requested_provider TEXT NOT NULL,
  requested_model TEXT NOT NULL,
  resolved_provider TEXT NOT NULL,
  resolved_model TEXT NOT NULL,
  effective_usage_known BOOLEAN NOT NULL,
  missing_outer_ordinals BIGINT NOT NULL DEFAULT 0,
  missing_inner_ordinals BIGINT NOT NULL DEFAULT 0,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ux_llm_usage_daily_call_memberships_grain UNIQUE (
    local_day,user_id,cohort_lane,call_id,requested_provider,requested_model,
    resolved_provider,resolved_model,effective_usage_known
  ),
  CONSTRAINT ck_llm_usage_daily_call_memberships_identity CHECK (
    cohort_lane ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,63}}$' AND
    call_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,159}}$' AND
    requested_provider ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{{0,79}}$' AND
    resolved_provider ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{{0,79}}$' AND
    requested_model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,159}}$' AND
    resolved_model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,159}}$'
  ),
  CONSTRAINT ck_llm_usage_daily_call_memberships_one_based_gaps CHECK (
    missing_outer_ordinals >= 0 AND missing_inner_ordinals >= 0
  )
);

CREATE TABLE IF NOT EXISTS llm_usage_rollup_dirty_days (
  rollup_name TEXT NOT NULL,
  local_day DATE NOT NULL,
  reason TEXT NOT NULL,
  generation BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (rollup_name,local_day),
  CONSTRAINT ck_llm_usage_rollup_dirty_days_name CHECK (
    rollup_name ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'
  ),
  CONSTRAINT ck_llm_usage_rollup_dirty_days_reason CHECK (
    reason ~ '^[a-z][a-z0-9_:-]{{0,63}}$'
  ),
  CONSTRAINT ck_llm_usage_rollup_dirty_days_generation CHECK (generation >= 0)
);

ALTER TABLE llm_usage_rollup_watermarks
  ADD COLUMN IF NOT EXISTS attempt_updated_at TIMESTAMPTZ NOT NULL
    DEFAULT 'epoch'::timestamptz,
  ADD COLUMN IF NOT EXISTS attempt_updated_id TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS turn_metric_updated_at TIMESTAMPTZ NOT NULL
    DEFAULT 'epoch'::timestamptz,
  ADD COLUMN IF NOT EXISTS turn_metric_id BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS rate_card_created_at TIMESTAMPTZ NOT NULL
    DEFAULT 'epoch'::timestamptz,
  ADD COLUMN IF NOT EXISTS rate_card_provider TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS rate_card_model TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS rate_card_version TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS bootstrap_complete BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS completed_through_day DATE,
  ADD COLUMN IF NOT EXISTS retained_from DATE,
  ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='ck_llm_usage_rollup_watermarks_followup_cursors'
      AND conrelid='llm_usage_rollup_watermarks'::regclass
  ) THEN
    ALTER TABLE llm_usage_rollup_watermarks ADD CONSTRAINT
      ck_llm_usage_rollup_watermarks_followup_cursors CHECK (
        turn_metric_id >= 0 AND version >= 0 AND
        (attempt_updated_id = '' OR attempt_updated_id ~
          '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$') AND
        ((rate_card_provider = '' AND rate_card_model = '' AND rate_card_version = '')
         OR (rate_card_provider ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{{0,79}}$'
          AND rate_card_model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,159}}$'
          AND rate_card_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'))
      );
  END IF;
END $$;

-- A cursor row contains only the post-update turn day.  Preserve both sides of
-- a rare created_at correction atomically at the source; otherwise the old
-- derived day could never be discovered.  This writes only the small, existing
-- current-RDS dirty queue and adds no service or provider-path dependency.
CREATE OR REPLACE FUNCTION mark_attempt_rollup_turn_day_move()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  current_generation BIGINT;
  old_day DATE;
  new_day DATE;
BEGIN
  IF OLD.created_at IS NOT DISTINCT FROM NEW.created_at THEN
    RETURN NEW;
  END IF;
  old_day := (OLD.created_at AT TIME ZONE 'Asia/Shanghai')::date;
  new_day := (NEW.created_at AT TIME ZONE 'Asia/Shanghai')::date;
  IF old_day = new_day THEN
    RETURN NEW;
  END IF;
  SELECT replay_generation INTO current_generation
    FROM llm_usage_rollup_watermarks
    WHERE rollup_name='hosted_v2_attempt_usage';
  current_generation := coalesce(current_generation,0);
  INSERT INTO llm_usage_rollup_dirty_days
    (rollup_name,local_day,reason,generation,created_at,updated_at)
  VALUES
    ('hosted_v2_attempt_usage',
     old_day,
     'turn_day_move',current_generation,now(),now()),
    ('hosted_v2_attempt_usage',
     new_day,
     'turn_day_move',current_generation,now(),now())
  ON CONFLICT (rollup_name,local_day) DO UPDATE SET
    reason=EXCLUDED.reason,
    generation=greatest(llm_usage_rollup_dirty_days.generation,EXCLUDED.generation),
    updated_at=EXCLUDED.updated_at;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_v2_turn_metrics_attempt_rollup_day_move
  ON v2_turn_metrics;
CREATE TRIGGER trg_v2_turn_metrics_attempt_rollup_day_move
  AFTER UPDATE OF created_at ON v2_turn_metrics
  FOR EACH ROW EXECUTE FUNCTION mark_attempt_rollup_turn_day_move();
"""


_CONCURRENT_INDEXES = {
    "ix_llm_provider_attempts_updated_id":
        "CREATE INDEX CONCURRENTLY ix_llm_provider_attempts_updated_id "
        "ON public.llm_provider_attempts (updated_at,attempt_id) INCLUDE (job_id) "
        "WHERE source='runtime_recorder'",
    "ix_llm_rate_cards_created_identity":
        "CREATE INDEX CONCURRENTLY ix_llm_rate_cards_created_identity "
        "ON public.llm_rate_cards (created_at,provider,model,version) INCLUDE (effective_at)",
    "ix_llm_usage_daily_attempt_dimensions_user":
        "CREATE INDEX CONCURRENTLY ix_llm_usage_daily_attempt_dimensions_user "
        "ON public.llm_usage_daily_attempt_dimensions (user_id,local_day)",
    "ix_llm_usage_daily_attempt_dimensions_resolved":
        "CREATE INDEX CONCURRENTLY ix_llm_usage_daily_attempt_dimensions_resolved "
        "ON public.llm_usage_daily_attempt_dimensions "
        "(local_day,resolved_provider,resolved_model,user_id,cohort_lane) "
        "INCLUDE (requested_provider,requested_model,effective_usage_known,cost_kind,currency)",
    "ix_llm_usage_daily_call_memberships_user":
        "CREATE INDEX CONCURRENTLY ix_llm_usage_daily_call_memberships_user "
        "ON public.llm_usage_daily_call_memberships (user_id,local_day)",
    "ix_llm_usage_daily_call_memberships_resolved":
        "CREATE INDEX CONCURRENTLY ix_llm_usage_daily_call_memberships_resolved "
        "ON public.llm_usage_daily_call_memberships "
        "(local_day,resolved_provider,resolved_model,user_id,cohort_lane) "
        "INCLUDE (call_id,requested_provider,requested_model,effective_usage_known,"
        "missing_outer_ordinals,missing_inner_ordinals)",
    "ix_llm_usage_daily_call_memberships_cohort":
        "CREATE INDEX CONCURRENTLY ix_llm_usage_daily_call_memberships_cohort "
        "ON public.llm_usage_daily_call_memberships (local_day,user_id,cohort_lane) "
        "INCLUDE (call_id,requested_provider,requested_model,resolved_provider,"
        "resolved_model,effective_usage_known,missing_outer_ordinals,missing_inner_ordinals)",
}


_INDEX_TARGETS = {
    "ix_llm_provider_attempts_updated_id": (
        "llm_provider_attempts",
        ("updated_at", "attempt_id"),
        ("job_id",),
        "source = 'runtime_recorder'::text",
    ),
    "ix_llm_rate_cards_created_identity": (
        "llm_rate_cards",
        ("created_at", "provider", "model", "version"),
        ("effective_at",),
        "",
    ),
    "ix_llm_usage_daily_attempt_dimensions_user": (
        "llm_usage_daily_attempt_dimensions", ("user_id", "local_day"), (), "",
    ),
    "ix_llm_usage_daily_attempt_dimensions_resolved": (
        "llm_usage_daily_attempt_dimensions",
        ("local_day", "resolved_provider", "resolved_model", "user_id", "cohort_lane"),
        ("requested_provider", "requested_model", "effective_usage_known", "cost_kind", "currency"),
        "",
    ),
    "ix_llm_usage_daily_call_memberships_user": (
        "llm_usage_daily_call_memberships", ("user_id", "local_day"), (), "",
    ),
    "ix_llm_usage_daily_call_memberships_resolved": (
        "llm_usage_daily_call_memberships",
        ("local_day", "resolved_provider", "resolved_model", "user_id", "cohort_lane"),
        ("call_id", "requested_provider", "requested_model", "effective_usage_known", "missing_outer_ordinals", "missing_inner_ordinals"),
        "",
    ),
    "ix_llm_usage_daily_call_memberships_cohort": (
        "llm_usage_daily_call_memberships",
        ("local_day", "user_id", "cohort_lane"),
        ("call_id", "requested_provider", "requested_model", "resolved_provider", "resolved_model", "effective_usage_known", "missing_outer_ordinals", "missing_inner_ordinals"),
        "",
    ),
}


def _index_validity(name: str, bind=None) -> bool | None:
    relation, keys, includes, predicate = _INDEX_TARGETS[name]
    columns = keys + includes
    column_checks = " AND ".join(
        f"pg_get_indexdef(idx.indexrelid,{position},true)=%s"
        for position in range(1, len(columns) + 1)
    )
    bind = op.get_bind() if bind is None else bind
    execute = getattr(bind, "exec_driver_sql", bind.execute)
    rows = execute(
        "SELECT idx_ns.nspname,tbl_ns.nspname,tbl.relname,"
        "(idx.indisvalid AND am.amname='btree' AND NOT idx.indisunique "
        f"AND idx.indnkeyatts={len(keys)} AND idx.indnatts={len(columns)} "
        f"AND idx.indexprs IS NULL AND {column_checks} "
        "AND COALESCE(pg_get_expr(idx.indpred,idx.indrelid,true),'')=%s) "
        "FROM pg_class AS cls "
        "JOIN pg_namespace AS idx_ns ON idx_ns.oid=cls.relnamespace "
        "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
        "JOIN pg_class AS tbl ON tbl.oid=idx.indrelid "
        "JOIN pg_namespace AS tbl_ns ON tbl_ns.oid=tbl.relnamespace "
        "JOIN pg_am AS am ON am.oid=cls.relam "
        "WHERE cls.relkind='i' AND cls.relname=%s",
        (*columns, predicate, name),
    ).fetchall()
    if not rows:
        return None
    if any(row[0] != "public" or row[1] != "public" for row in rows):
        raise RuntimeError(
            f"{name} exists in another schema; refusing ambiguous index ownership"
        )
    if len(rows) != 1 or rows[0][2] != relation:
        raise RuntimeError(
            f"{name} exists on another relation; refusing to drop an unrelated index"
        )
    return bool(rows[0][3])


def upgrade() -> None:
    # Schema-only.  Existing Runtime V2 maintenance builds rows after deploy.
    op.execute(_SCHEMA_UP)
    with op.get_context().autocommit_block():
        for name, sql in _CONCURRENT_INDEXES.items():
            validity = _index_validity(name)
            if validity is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
            if validity is not True:
                op.execute(sql)


def downgrade() -> None:
    # Preflight every name before the first autocommit DROP.  This avoids a
    # partially destructive downgrade when a later name belongs to another
    # schema/relation.
    owned = {name: _index_validity(name) for name in _CONCURRENT_INDEXES}
    with op.get_context().autocommit_block():
        for name, validity in owned.items():
            if validity is not None:
                op.execute(f"DROP INDEX CONCURRENTLY public.{name}")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_v2_turn_metrics_attempt_rollup_day_move "
        "ON v2_turn_metrics"
    )
    op.execute("DROP FUNCTION IF EXISTS mark_attempt_rollup_turn_day_move()")
    op.execute("DROP TABLE IF EXISTS llm_usage_rollup_dirty_days")
    op.execute("DROP TABLE IF EXISTS llm_usage_daily_call_memberships")
    op.execute("DROP TABLE IF EXISTS llm_usage_daily_attempt_dimensions")
    op.execute(
        "ALTER TABLE llm_usage_rollup_watermarks "
        "DROP CONSTRAINT IF EXISTS ck_llm_usage_rollup_watermarks_followup_cursors, "
        "DROP COLUMN IF EXISTS attempt_updated_at, "
        "DROP COLUMN IF EXISTS attempt_updated_id, "
        "DROP COLUMN IF EXISTS turn_metric_updated_at, "
        "DROP COLUMN IF EXISTS turn_metric_id, "
        "DROP COLUMN IF EXISTS rate_card_created_at, "
        "DROP COLUMN IF EXISTS rate_card_provider, "
        "DROP COLUMN IF EXISTS rate_card_model, "
        "DROP COLUMN IF EXISTS rate_card_version, "
        "DROP COLUMN IF EXISTS bootstrap_complete, "
        "DROP COLUMN IF EXISTS completed_through_day, "
        "DROP COLUMN IF EXISTS retained_from, "
        "DROP COLUMN IF EXISTS version"
    )
    op.execute("DROP FUNCTION IF EXISTS llm_ttft_samples_are_sorted(double precision[])")
