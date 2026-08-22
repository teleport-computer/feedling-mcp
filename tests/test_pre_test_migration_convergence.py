import os
import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql


ROOT = Path(__file__).parent.parent


def _scripts(tree: str) -> ScriptDirectory:
    ini = "alembic.ini" if tree == "alembic" else "alembic_tee/alembic.ini"
    cfg = Config(str(ROOT / "backend" / ini))
    cfg.set_main_option("script_location", str(ROOT / "backend" / tree))
    return ScriptDirectory.from_config(cfg)


def _database_url(base: str, database: str) -> str:
    prefix, _, _ = base.rpartition("/")
    return f"{prefix}/{database}"


def test_rds_pre_and_test_heads_converge():
    script = _scripts("alembic")
    assert script.get_heads() == ["0098_chat_change_events"]
    assert (
        script.get_revision("0098_chat_change_events").down_revision
        == "0097_v2_job_recovery_events"
    )
    assert (
        script.get_revision("0097_v2_job_recovery_events").down_revision
        == "0096_trace_write_stats_health"
    )
    assert (
        script.get_revision("0096_trace_write_stats_health").down_revision
        == "0095_trace_write_stats"
    )
    assert (
        script.get_revision("0095_trace_write_stats").down_revision
        == "0094_chat_daily_rollup"
    )
    assert (
        script.get_revision("0094_chat_daily_rollup").down_revision
        == "0093_lane_rollup_voice"
    )
    assert (
        script.get_revision("0093_lane_rollup_voice").down_revision
        == "0092_lane_rollup_safe_ts"
    )
    assert (
        script.get_revision("0092_lane_rollup_safe_ts").down_revision
        == "0091_lane_daily_rollup"
    )
    assert (
        script.get_revision("0091_lane_daily_rollup").down_revision
        == "0090_merge_wake_outcomes"
    )
    assert set(
        script.get_revision("0090_merge_wake_outcomes").down_revision
    ) == {
        "0089_merge_pre_test_agent_jobs",
        "0089_v2_wake_outcomes",
    }
    assert set(
        script.get_revision("0089_merge_pre_test_agent_jobs").down_revision
    ) == {
        "0088_merge_pre_test_heads",
        "0088_agent_jobs_available_at",
    }
    assert set(script.get_revision("0088_merge_pre_test_heads").down_revision) == {
        "0086_merge_voice_wake",
        "0087_v2_first_chat_activation",
    }


def test_tee_chain_carries_test_runtime_schema():
    script = _scripts("alembic_tee")
    assert script.get_heads() == ["0034_chat_poll_index"]
    assert (
        script.get_revision("0034_chat_poll_index").down_revision
        == "0033_trace_events"
    )
    assert (
        script.get_revision("0033_trace_events").down_revision
        == "0032_v2_job_recovery_events"
    )
    assert (
        script.get_revision("0032_v2_job_recovery_events").down_revision
        == "0031_merge_voice_primary"
    )
    assert set(
        script.get_revision("0031_merge_voice_primary").down_revision
    ) == {
        "0029_plaintext_shadow_merge",
        "0030_voice_call_sessions_primary",
    }
    assert set(
        script.get_revision("0029_plaintext_shadow_merge").down_revision
    ) == {
        "0028_trace_write_stats_health",
        "0027_plaintext_shadow_gates",
    }
    assert (
        script.get_revision("0027_plaintext_shadow_gates").down_revision
        == "0026_plaintext_shadow_control"
    )
    assert (
        script.get_revision("0026_plaintext_shadow_control").down_revision
        == "0025_lane_rollup_voice"
    )
    assert (
        script.get_revision("0028_trace_write_stats_health").down_revision
        == "0027_trace_write_stats"
    )
    assert (
        script.get_revision("0027_trace_write_stats").down_revision
        == "0026_chat_daily_rollup"
    )
    assert (
        script.get_revision("0026_chat_daily_rollup").down_revision
        == "0025_lane_rollup_voice"
    )
    assert (
        script.get_revision("0030_voice_call_sessions_primary").down_revision
        == "0025_lane_rollup_voice"
    )
    assert (
        script.get_revision("0025_lane_rollup_voice").down_revision
        == "0024_lane_rollup_safe_ts"
    )
    assert (
        script.get_revision("0024_lane_rollup_safe_ts").down_revision
        == "0023_lane_daily_rollup"
    )
    assert (
        script.get_revision("0023_lane_daily_rollup").down_revision
        == "0022_v2_wake_outcomes"
    )
    assert (
        script.get_revision("0022_v2_wake_outcomes").down_revision
        == "0021_agent_jobs_available_at"
    )
    assert (
        script.get_revision("0021_agent_jobs_available_at").down_revision
        == "0020_v2_first_chat_activation"
    )
    assert (
        script.get_revision("0020_v2_first_chat_activation").down_revision
        == "0019_v2_worker_pool_heartbeats"
    )
    assert (
        script.get_revision("0019_v2_worker_pool_heartbeats").down_revision
        == "0018_v2_wake_shadow_decisions"
    )
    assert (
        script.get_revision("0018_v2_wake_shadow_decisions").down_revision
        == "0017_voice_primary_alignment"
    )


def test_tee_migrations_reuse_the_rds_contract_sql():
    rds = _scripts("alembic")
    tee = _scripts("alembic_tee")
    assert (
        tee.get_revision("0018_v2_wake_shadow_decisions").module._SCHEMA_UP
        == rds.get_revision("0085_v2_wake_shadow_decisions").module._SCHEMA_UP
    )
    assert (
        tee.get_revision("0019_v2_worker_pool_heartbeats").module._UP
        == rds.get_revision("0086_v2_worker_pool_heartbeats").module._UP
    )
    assert (
        tee.get_revision("0020_v2_first_chat_activation").module._BACKFILL_SQL
        == rds.get_revision("0087_v2_first_chat_activation").module._BACKFILL_SQL
    )
    assert (
        tee.get_revision("0021_agent_jobs_available_at").module._UP
        == rds.get_revision("0088_agent_jobs_available_at").module._UP
    )
    assert (
        tee.get_revision("0022_v2_wake_outcomes").module._UP
        == rds.get_revision("0089_v2_wake_outcomes").module._UP
    )
    # Alembic revision modules cannot import each other (names start with a
    # digit), so the two chains hold separate copies of the voice DDL. This is
    # the guard that keeps them one contract: edit either side alone and CI
    # fails here rather than the columns silently diverging between the RDS
    # and TEE databases — and test已把 TEE 提为 primary, so a divergence there
    # is what the freezer would actually run against.
    assert (
        tee.get_revision("0025_lane_rollup_voice").module._UP
        == rds.get_revision("0093_lane_rollup_voice").module._UP
    )
    assert (
        tee.get_revision("0026_chat_daily_rollup").module._UP
        == rds.get_revision("0094_chat_daily_rollup").module._UP
    )
    assert (
        tee.get_revision("0027_trace_write_stats").module._UP
        == rds.get_revision("0095_trace_write_stats").module._UP
    )
    assert (
        tee.get_revision("0028_trace_write_stats_health").module._UP
        == rds.get_revision("0096_trace_write_stats_health").module._UP
    )
    assert (
        tee.get_revision("0030_voice_call_sessions_primary").module._UP
        == rds.get_revision("0081_voice_call_sessions").module._UP
    )
    assert (
        tee.get_revision("0032_v2_job_recovery_events").module._UP
        == rds.get_revision("0097_v2_job_recovery_events").module._UP
    )
    tee_chat = tee.get_revision("0034_chat_poll_index").module
    rds_chat = rds.get_revision("0098_chat_change_events").module
    assert tee_chat._SCHEMA_UP == rds_chat._SCHEMA_UP
    assert tee_chat._CAPTURE_UP == rds_chat._CAPTURE_UP
    assert tee_chat._POLL_INDEX == rds_chat._POLL_INDEX


def test_tee_0029_upgrades_to_voice_merge_head(monkeypatch):
    """A live TEST-shaped database must converge without duplicate voice DDL."""
    admin_url = os.environ.get(
        "FEEDLING_TEST_PG",
        "postgresql://postgres:test@127.0.0.1:55432/postgres",
    )
    database = f"tee_voice_merge_{uuid.uuid4().hex[:10]}"
    database_url = _database_url(admin_url, database)
    cfg = Config(str(ROOT / "backend/alembic_tee/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend/alembic_tee"))

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

    monkeypatch.setenv("TEE_MIGRATION_DATABASE_URL", database_url)
    try:
        command.upgrade(cfg, "0029_plaintext_shadow_merge")
        with psycopg.connect(database_url, autocommit=True) as conn:
            assert conn.execute(
                "SELECT to_regclass('public.voice_call_sessions')"
            ).fetchone() == (None,)
            conn.execute(
                "INSERT INTO plaintext_shadow_restore_evidence "
                "(restored_at,source_backup_at,schema_head,verifier_digest,"
                "backup_artifact_digest,target_fingerprint,target_capacity_bytes,"
                "target_connection_limit,ha_verified,attestation_key_fingerprint,"
                "attestation_signature_digest,operator_id,expires_at) VALUES "
                "(now() - interval '1 hour',now() - interval '2 hours',"
                "'0029_plaintext_shadow_merge','sha256:test','sha256:backup',"
                "'target:test',1,1,true,'key:test','signature:test',"
                "'test-operator',now() + interval '1 day')"
            )
            conn.execute(
                "INSERT INTO server_config(key,value) VALUES "
                "('phase4_primary_prepared',convert_to(%s,'UTF8')) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                ('{"prepared":true,"tee_heads":["0029_plaintext_shadow_merge"]}',),
            )

        command.upgrade(cfg, "head")
        with psycopg.connect(database_url, autocommit=True) as conn:
            assert conn.execute(
                "SELECT version_num FROM alembic_tee_version"
            ).fetchall() == [("0034_chat_poll_index",)]
            assert conn.execute(
                "SELECT to_regclass('public.voice_call_sessions')"
            ).fetchone() == ("voice_call_sessions",)
            assert conn.execute(
                "SELECT count(*) FROM plaintext_shadow_restore_evidence"
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT convert_from(value,'UTF8')::jsonb->'tee_heads' "
                "FROM server_config WHERE key='phase4_primary_prepared'"
            ).fetchone() == (["0034_chat_poll_index"],)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s",
                (database,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database)
                )
            )
