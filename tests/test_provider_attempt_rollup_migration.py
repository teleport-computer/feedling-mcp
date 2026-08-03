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
        assert "id" not in dimension_columns

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
        assert "id" not in membership_columns

        for table in (
            "llm_usage_daily_attempt_dimensions",
            "llm_usage_daily_call_memberships",
        ):
            assert conn.execute(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_schema='public' AND table_name=%s "
                "AND constraint_type='PRIMARY KEY'",
                (table,),
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT count(*) FROM pg_class seq "
                "JOIN pg_depend dep ON dep.objid=seq.oid "
                "JOIN pg_class tbl ON tbl.oid=dep.refobjid "
                "JOIN pg_namespace ns ON ns.oid=tbl.relnamespace "
                "WHERE seq.relkind='S' AND ns.nspname='public' AND tbl.relname=%s",
                (table,),
            ).fetchone() == (0,)

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
            "retention_pending_from",
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


def test_0077_stale_started_partial_index_is_exact_and_query_uses_it():
    migration = _migration_module()
    name = "ix_llm_provider_attempts_stale_started"
    assert name in migration._CONCURRENT_INDEXES
    with db.get_pool().connection() as conn:
        assert migration._index_validity(name, conn) is True
    uid = "u_attempt_stale_index"
    _seed_user(uid)
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,call_id,"
                "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
                "resolved_provider,requested_model,resolved_model,transport,started_at,state,"
                "outcome,error_class,source,completeness,revision) "
                "SELECT '00000000-0000-5000-8000-'||lpad(to_hex(n),12,'0'),%s,'chat',"
                "'stale-call-'||n,1,1,'initial','asked','served','asked-model','served-model',"
                "'responses',now()-interval '2 hours'-(n*interval '1 second'),'started',"
                "'unknown','none','runtime_recorder','started_only',0 "
                "FROM generate_series(1,3000) n",
                (uid,),
            )
            conn.execute("ANALYZE llm_provider_attempts")
            plan = conn.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
                "SELECT attempt_id FROM llm_provider_attempts "
                "WHERE source='runtime_recorder' AND state='started' "
                "AND finished_at IS NULL AND possibly_billed=false "
                "AND started_at<now()-interval '60 seconds' "
                "ORDER BY started_at,attempt_id LIMIT 10"
            ).fetchone()[0][0]

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        assert any(
            node.get("Index Name") == name for node in nodes(plan["Plan"])
        )
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_0077_retention_orphan_index_is_exact_and_query_uses_it():
    migration = _migration_module()
    name = "ix_llm_provider_attempts_retention_started"
    assert name in migration._CONCURRENT_INDEXES
    with db.get_pool().connection() as conn:
        assert migration._index_validity(name, conn) is True
    uid = "u_attempt_retention_index"
    _seed_user(uid)
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,call_id,"
                "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
                "resolved_provider,requested_model,resolved_model,transport,started_at,state,"
                "outcome,error_class,source,completeness,revision) "
                "SELECT '30000000-0000-5000-8000-'||lpad(to_hex(n),12,'0'),%s,'chat',"
                "'retention-call-'||n,1,1,'initial','asked','served','asked-model',"
                "'served-model','responses',now()-interval '500 days'"
                "-(n*interval '1 second'),'completed','succeeded','none',"
                "'runtime_recorder','complete',1 FROM generate_series(1,3000) n",
                (uid,),
            )
            conn.execute("ANALYZE llm_provider_attempts")
            plan = conn.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
                "SELECT attempt_id FROM llm_provider_attempts "
                "WHERE source='runtime_recorder' AND started_at<now()-interval '400 days' "
                "ORDER BY started_at,attempt_id LIMIT 10"
            ).fetchone()[0][0]

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        assert any(
            node.get("Index Name") == name for node in nodes(plan["Plan"])
        )
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


@pytest.mark.parametrize(
    "name",
    [
        "ix_llm_provider_attempts_stale_started",
        "ix_llm_provider_attempts_retention_started",
    ],
)
def test_0077_repairs_wrong_partial_index_and_refuses_unrelated_owner(name):
    cfg = _alembic_config()
    try:
        command.downgrade(cfg, "0076_llm_provider_attempts")
        migration = _migration_module()
        with db.get_pool().connection() as conn:
            conn.execute(migration._SCHEMA_UP)
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(
                f"CREATE INDEX CONCURRENTLY {name} ON llm_provider_attempts (call_id)"
            )
        command.upgrade(cfg, "head")
        with db.get_pool().connection() as conn:
            assert _migration_module()._index_validity(name, conn) is True

        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(f"DROP INDEX CONCURRENTLY public.{name}")
            conn.execute(f"CREATE INDEX CONCURRENTLY {name} ON public.users (user_id)")
        with pytest.raises(RuntimeError, match="another relation"):
            command.downgrade(cfg, "0076_llm_provider_attempts")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT tbl.relname FROM pg_class idx "
                "JOIN pg_index pi ON pi.indexrelid=idx.oid "
                "JOIN pg_class tbl ON tbl.oid=pi.indrelid "
                "WHERE idx.relname=%s", (name,),
            ).fetchone() == ("users",)
    finally:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
        command.downgrade(cfg, "0076_llm_provider_attempts")
        command.upgrade(cfg, "head")


def test_0077_schema_phase_restart_does_not_create_surrogate_sequences():
    """Replaying the committed schema phase must preserve natural-grain storage."""
    migration = _migration_module()
    with db.get_pool().connection() as conn:
        conn.execute(migration._SCHEMA_UP)
        assert conn.execute(
            "SELECT count(*) FROM pg_class seq "
            "JOIN pg_depend dep ON dep.objid=seq.oid "
            "JOIN pg_class tbl ON tbl.oid=dep.refobjid "
            "JOIN pg_namespace ns ON ns.oid=tbl.relnamespace "
            "WHERE seq.relkind='S' AND ns.nspname='public' "
            "AND tbl.relname=ANY(%s)",
            ([
                "llm_usage_daily_attempt_dimensions",
                "llm_usage_daily_call_memberships",
            ],),
        ).fetchone() == (0,)


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


def test_0077_upgrade_refuses_same_name_index_in_another_schema():
    """Index recovery must resolve public explicitly, never through search_path."""
    cfg = _alembic_config()
    name = "ix_llm_usage_daily_call_memberships_resolved"
    try:
        command.downgrade(cfg, "0076_llm_provider_attempts")
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute("CREATE SCHEMA rollup_shadow")
            conn.execute("CREATE TABLE rollup_shadow.memberships (call_id text)")
            conn.execute(
                f"CREATE INDEX {name} ON rollup_shadow.memberships (call_id)"
            )
        with pytest.raises(RuntimeError, match="another schema"):
            command.upgrade(cfg, "head")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT ns.nspname,tbl.relname FROM pg_class idx "
                "JOIN pg_namespace ns ON ns.oid=idx.relnamespace "
                "JOIN pg_index pi ON pi.indexrelid=idx.oid "
                "JOIN pg_class tbl ON tbl.oid=pi.indrelid "
                "WHERE idx.relname=%s",
                (name,),
            ).fetchall() == [("rollup_shadow", "memberships")]
    finally:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS rollup_shadow CASCADE")
        command.upgrade(cfg, "head")


def test_0077_downgrade_refuses_same_name_index_on_another_relation():
    """Downgrade preflights ownership before deleting any concurrent index."""
    cfg = _alembic_config()
    name = "ix_llm_usage_daily_call_memberships_resolved"
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(f"DROP INDEX CONCURRENTLY public.{name}")
            conn.execute(f"CREATE INDEX CONCURRENTLY {name} ON public.users (user_id)")
        with pytest.raises(RuntimeError, match="another relation"):
            command.downgrade(cfg, "0076_llm_provider_attempts")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT tbl.relname FROM pg_class idx "
                "JOIN pg_index pi ON pi.indexrelid=idx.oid "
                "JOIN pg_class tbl ON tbl.oid=pi.indrelid "
                "JOIN pg_namespace ns ON ns.oid=idx.relnamespace "
                "WHERE ns.nspname='public' AND idx.relname=%s",
                (name,),
            ).fetchone() == ("users",)
    finally:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
        command.downgrade(cfg, "0076_llm_provider_attempts")
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
            "retention_pending_from",
            "version",
        } & columns
    finally:
        command.upgrade(cfg, "head")
