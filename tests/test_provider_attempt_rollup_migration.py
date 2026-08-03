"""0077 attempt-rollup schema contracts for exact, deletion-safe reporting."""

import os
import sys
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402


def _alembic_config() -> Config:
    backend = Path(__file__).parent.parent / "backend"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    return cfg


def _migration_module():
    return (
        ScriptDirectory.from_config(_alembic_config())
        .get_revision("0077_llm_usage_attempt_rollups")
        .module
    )


def _seed_user(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id,created_at,doc) VALUES (%s,'','{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def test_0077_installs_exact_attempt_rollup_grains_and_durable_cursors():
    """Missing grains/cursors would make the reconciler lossy or non-resumable."""
    migration = _migration_module()
    assert migration.down_revision == "0076_llm_provider_attempts"

    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0077_llm_usage_attempt_rollups",
        )
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=ANY(%s)",
                (
                    [
                        "llm_usage_daily_attempt_dimensions",
                        "llm_usage_daily_call_memberships",
                        "llm_usage_rollup_dirty_days",
                    ],
                ),
            ).fetchall()
        }
        assert tables == {
            "llm_usage_daily_attempt_dimensions",
            "llm_usage_daily_call_memberships",
            "llm_usage_rollup_dirty_days",
        }

        dimension_columns = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT column_name,data_type FROM information_schema.columns "
                "WHERE table_name='llm_usage_daily_attempt_dimensions'"
            ).fetchall()
        }
        assert {
            "local_day",
            "user_id",
            "cohort_lane",
            "requested_provider",
            "requested_model",
            "resolved_provider",
            "resolved_model",
            "effective_usage_known",
            "cost_kind",
            "currency",
            "attempts",
            "retry_attempts",
            "failover_attempts",
            "failed_attempts",
            "possibly_billed_attempts",
            "input_tokens_sum",
            "input_tokens_known_count",
            "output_tokens_sum",
            "output_tokens_known_count",
            "reasoning_tokens_sum",
            "reasoning_tokens_known_count",
            "cache_read_tokens_sum",
            "cache_read_tokens_known_count",
            "cache_write_tokens_sum",
            "cache_write_tokens_known_count",
            "cache_miss_tokens_sum",
            "cache_miss_tokens_known_count",
            "authoritative_cost_attempts",
            "estimated_cost_attempts",
            "unknown_cost_attempts",
            "cost_amount",
            "ttft_samples",
            "refreshed_at",
        } <= set(dimension_columns)
        assert dimension_columns["ttft_samples"] == "ARRAY"

        membership_columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='llm_usage_daily_call_memberships'"
            ).fetchall()
        }
        assert {
            "local_day",
            "user_id",
            "cohort_lane",
            "call_id",
            "requested_provider",
            "requested_model",
            "resolved_provider",
            "resolved_model",
            "effective_usage_known",
            "missing_outer_ordinals",
            "missing_inner_ordinals",
            "refreshed_at",
        } <= membership_columns

        watermark_columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='llm_usage_rollup_watermarks'"
            ).fetchall()
        }
        assert {
            "attempt_updated_at",
            "attempt_updated_id",
            "attempt_id",
            "late_correction_id",
            "turn_metric_updated_at",
            "turn_metric_id",
            "rate_card_created_at",
            "rate_card_provider",
            "rate_card_model",
            "rate_card_version",
            "replay_generation",
            "bootstrap_complete",
            "completed_through_day",
            "retained_from",
            "version",
        } <= watermark_columns


def test_0077_rollup_rows_enforce_grain_bounds_sorted_ttft_and_one_based_gaps():
    """Duplicate grains, invalid counters, or unsorted samples corrupt exact totals."""
    uid = "u_attempt_rollup_constraints"
    _seed_user(uid)
    try:
        with db.get_pool().connection() as conn:
            dimension_insert = (
                "INSERT INTO llm_usage_daily_attempt_dimensions "
                "(local_day,user_id,cohort_lane,requested_provider,requested_model,"
                "resolved_provider,resolved_model,effective_usage_known,cost_kind,currency,"
                "attempts,input_tokens_sum,input_tokens_known_count,"
                "authoritative_cost_attempts,cost_amount,ttft_samples) "
                "VALUES ('2026-08-01',%s,'chat','openai','gpt-test','openai','gpt-test',"
                "true,'authoritative','USD',2,10,2,2,0.25,%s)"
            )
            conn.execute(dimension_insert, (uid, [1.0, 2.0]))
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(dimension_insert, (uid, [1.0, 2.0]))
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    dimension_insert.replace("2,10,2,2,0.25", "1,10,2,1,0.25"),
                    (uid, [1.0]),
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    dimension_insert.replace("'2026-08-01'", "'2026-08-02'"),
                    (uid, [2.0, 1.0]),
                )
            # Authoritative corrections are signed and may have no trustworthy
            # currency; the report preserves that as an unknown-currency bucket.
            conn.execute(
                dimension_insert.replace(
                    "'2026-08-01'", "'2026-08-03'"
                ).replace(
                    "'USD',2,10,2,2,0.25", "NULL,1,-5,1,1,-0.05"
                ),
                (uid, []),
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    dimension_insert.replace(
                        "'2026-08-01'", "'2026-08-04'"
                    ).replace(
                        "'authoritative','USD',2,10,2,2,0.25",
                        "'estimated','USD',1,0,0,0,-0.01",
                    ),
                    (uid, []),
                )

            membership_insert = (
                "INSERT INTO llm_usage_daily_call_memberships "
                "(local_day,user_id,cohort_lane,call_id,requested_provider,requested_model,"
                "resolved_provider,resolved_model,effective_usage_known,"
                "missing_outer_ordinals,missing_inner_ordinals) "
                "VALUES ('2026-08-01',%s,'chat','call-0077','openai','gpt-test',"
                "'openai','gpt-test',true,%s,%s)"
            )
            conn.execute(membership_insert, (uid, 0, 0))
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    membership_insert.replace("'call-0077'", "'call-0077-negative'"),
                    (uid, -1, 0),
                )
            # Gap counts are missing one-based ordinal positions, never a zero-based
            # ordinal value.  Zero is valid only as "no positions missing".
            assert conn.execute(
                "SELECT missing_outer_ordinals,missing_inner_ordinals "
                "FROM llm_usage_daily_call_memberships WHERE user_id=%s",
                (uid,),
            ).fetchone() == (0, 0)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_0077_user_rows_cascade_and_dirty_watermark_constraints_hold():
    """Account deletion must remove every derived row; cursors cannot regress below zero."""
    uid = "u_attempt_rollup_cascade"
    _seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_daily_attempt_dimensions "
            "(local_day,user_id,cohort_lane,requested_provider,requested_model,"
            "resolved_provider,resolved_model,effective_usage_known,cost_kind,"
            "attempts,unknown_cost_attempts) "
            "VALUES ('2026-08-03',%s,'chat','openai','gpt-test','openai','gpt-test',"
            "false,'unknown',1,1)",
            (uid,),
        )
        conn.execute(
            "INSERT INTO llm_usage_daily_call_memberships "
            "(local_day,user_id,cohort_lane,call_id,requested_provider,requested_model,"
            "resolved_provider,resolved_model,effective_usage_known) "
            "VALUES ('2026-08-03',%s,'chat','call-cascade','openai','gpt-test',"
            "'openai','gpt-test',false)",
            (uid,),
        )
        conn.execute(
            "INSERT INTO llm_usage_rollup_dirty_days "
            "(rollup_name,local_day,reason,generation) "
            "VALUES ('hosted-v2-attempts','2026-08-03','attempt_update',1)"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO llm_usage_rollup_dirty_days "
                "(rollup_name,local_day,reason,generation) "
                "VALUES ('bad','2026-08-04','attempt_update',-1)"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO llm_usage_rollup_watermarks "
                "(rollup_name,turn_metric_id) VALUES ('bad-cursor',-1)"
            )
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,attempt_id,attempt_updated_id) VALUES "
            "('independent-attempt-cursors',"
            "'11111111-1111-5111-8111-111111111111',"
            "'22222222-2222-5222-8222-222222222222')"
        )
        assert conn.execute(
            "SELECT attempt_id,attempt_updated_id FROM llm_usage_rollup_watermarks "
            "WHERE rollup_name='independent-attempt-cursors'"
        ).fetchone() == (
            "11111111-1111-5111-8111-111111111111",
            "22222222-2222-5222-8222-222222222222",
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO llm_usage_rollup_watermarks "
                "(rollup_name,attempt_updated_id) "
                "VALUES ('bad-updated-attempt-id','not-a-uuid')"
            )
        conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
        for table in (
            "llm_usage_daily_attempt_dimensions",
            "llm_usage_daily_call_memberships",
        ):
            assert conn.execute(
                f"SELECT count(*) FROM {table} WHERE user_id=%s", (uid,)
            ).fetchone() == (0,)


def test_0077_concurrent_indexes_have_exact_recoverable_shapes():
    """Every cursor/report index must be valid and match its intended relation."""
    migration = _migration_module()
    with db.get_pool().connection() as conn:
        for name in migration._CONCURRENT_INDEXES:
            assert migration._index_validity(name, conn) is True
            assert conn.execute(
                "SELECT indisvalid FROM pg_index WHERE indexrelid=%s::regclass",
                (name,),
            ).fetchone() == (True,)


def test_0077_upgrade_repairs_same_relation_wrong_index_definition():
    """A valid but wrong same-table shell must be replaced, not accepted."""
    cfg = _alembic_config()
    name = "ix_llm_usage_daily_call_memberships_resolved"
    try:
        command.downgrade(cfg, "0076_llm_provider_attempts")
        migration = _migration_module()
        with db.get_pool().connection() as conn:
            conn.execute(migration._SCHEMA_UP)
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(
                f"CREATE INDEX CONCURRENTLY {name} "
                "ON llm_usage_daily_call_memberships (call_id)"
            )
        command.upgrade(cfg, "head")
        with db.get_pool().connection() as conn:
            assert _migration_module()._index_validity(name, conn) is True
    finally:
        command.upgrade(cfg, "head")


def test_0077_upgrade_refuses_same_name_index_on_another_relation():
    """Recovery must never drop an unrelated index that happens to share a name."""
    cfg = _alembic_config()
    name = "ix_llm_usage_daily_call_memberships_resolved"
    try:
        command.downgrade(cfg, "0076_llm_provider_attempts")
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(f"CREATE INDEX CONCURRENTLY {name} ON users (user_id)")
        with pytest.raises(RuntimeError, match="another relation"):
            command.upgrade(cfg, "head")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT indrelid::regclass::text FROM pg_index WHERE indexrelid=%s::regclass",
                (name,),
            ).fetchone() == ("users",)
    finally:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        command.upgrade(cfg, "head")


def test_0077_downgrade_removes_only_followup_watermark_columns():
    """Rolling back 0077 must preserve every cursor that shipped in 0076."""
    cfg = _alembic_config()
    try:
        command.downgrade(cfg, "0076_llm_provider_attempts")
        with db.get_pool().connection() as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='llm_usage_rollup_watermarks'"
                ).fetchall()
            }
        assert {
            "attempt_finished_at",
            "attempt_id",
            "late_correction_id",
            "replay_generation",
            "updated_at",
        } <= columns
        assert not {
            "attempt_updated_at",
            "attempt_updated_id",
            "turn_metric_updated_at",
            "turn_metric_id",
            "rate_card_created_at",
            "rate_card_provider",
            "rate_card_model",
            "rate_card_version",
            "bootstrap_complete",
            "completed_through_day",
            "retained_from",
            "version",
        } & columns
    finally:
        command.upgrade(cfg, "head")
