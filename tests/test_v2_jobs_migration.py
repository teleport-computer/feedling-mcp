"""0014 迁移落地：四张 V2 表 + single-flight 唯一索引真的存在且生效。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory


def _seed_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def test_v2_tables_exist():
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN "
            "('agent_jobs','agent_action_queue','agent_status_events','runtime_state')"
        ).fetchall()
    names = {r[0] for r in rows}
    assert names == {"agent_jobs", "agent_action_queue", "agent_status_events", "runtime_state"}


def test_v2_job_liveness_columns_exist():
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='agent_jobs' "
            "AND column_name IN ('input_generation','lease_expires_at','queue_deadline_at')"
        ).fetchall()
    assert {row[0] for row in rows} == {
        "input_generation", "lease_expires_at", "queue_deadline_at",
    }


def test_migration_graph_preserves_deployed_v2_history_and_merges_profiles():
    backend = Path(__file__).parent.parent / "backend"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(cfg)

    assert script.get_revision("0014_hosted_runtime_v2").down_revision == (
        "0013_genesis_resident_claim"
    )
    assert set(script.get_revision("0021_merge_v2_profiles").down_revision) == {
        "0020_v2_heartbeat_kind",
        "0014_model_api_profiles",
    }
    # Head advanced by Hosted Runtime V2 PR A (effect foundation): 0025 runtime
    # generation, 0026 job expected_generation, 0027 effect outbox, 0028 effect
    # sink-applied dedup guard, chained after 0024_v2_worker_capacity. PR B's
    # B5 (idempotent whole-turn metric) chains 0029 after that: v2_turn_metrics
    # gains model_calls/retries/failed/status + UNIQUE(job_id). PR D's D4 (live
    # kill switch) chains 0030 after that: single-row v2_runtime_control table.
    # D5 summary coverage chains 0031 after that:
    # v2_conversation_summary gains a watermark_seq column alongside the
    # existing watermark_ts. Finally, rebasing feat/hosted-runtime-v2 onto test
    # joins the tee-pg shadow chain (0015_tee_sync_runs → 0016_tee_sync_table_
    # failures, which branched off 0014_model_api_profiles) with this V2 chain via
    # a no-op merge migration 0032_merge_tee_v2. The deployed Runtime V2
    # lineage then adds the seq cursor/effect order at 0033 and its forward
    # repair at 0034. In parallel, test extended the tee lineage through 0017
    # and 0018, then joined it back to 0032 at 0033_merge_tee_reconcile. Since
    # both 0033 histories are deployed, 0035 merges their two heads without
    # reparenting either lineage.
    assert set(script.get_revision("0032_merge_tee_v2").down_revision) == {
        "0016_tee_sync_table_failures",
        "0031_v2_summary_watermark_seq",
    }
    assert set(script.get_revision("0033_merge_tee_reconcile").down_revision) == {
        "0032_merge_tee_v2",
        "0018_tee_reconcile_cursors",
    }
    assert script.get_revision("0033_v2_seq_cursor_effect_order").down_revision == (
        "0032_merge_tee_v2"
    )
    assert script.get_revision("0034_v2_legacy_sink_reconcile").down_revision == (
        "0033_v2_seq_cursor_effect_order"
    )
    assert set(script.get_revision("0035_merge_v2_tee_reconcile").down_revision) == {
        "0033_merge_tee_reconcile",
        "0034_v2_legacy_sink_reconcile",
    }
    assert script.get_revision("0036_chat_r2_lifecycle").down_revision == (
        "0035_merge_v2_tee_reconcile"
    )
    assert script.get_revision("0037_v2_terminal_failure_outbox").down_revision == (
        "0036_chat_r2_lifecycle"
    )
    assert script.get_revision("0038_v2_prompt_cache_metrics").down_revision == (
        "0037_v2_terminal_failure_outbox"
    )
    assert script.get_current_head() == "0038_v2_prompt_cache_metrics"


def test_prompt_cache_metric_columns_and_recent_window_index_exist():
    with db.get_pool().connection() as conn:
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='v2_turn_metrics' AND column_name IN "
            "('provider','model','cache_read_tokens','cache_write_tokens',"
            "'cache_miss_tokens','usage_reported_calls','cache_reported_calls',"
            "'cache_route_fingerprint')"
        ).fetchall()
        indexes = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='v2_turn_metrics'"
        ).fetchall()
    assert {row[0] for row in columns} == {
        "provider",
        "model",
        "cache_route_fingerprint",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_miss_tokens",
        "usage_reported_calls",
        "cache_reported_calls",
    }
    assert "ix_v2_turn_metrics_lane_created_at" in {row[0] for row in indexes}
    assert "ix_v2_turn_metrics_cache_proof" in {row[0] for row in indexes}


def test_terminal_failure_outbox_schema_and_error_event_idempotency_guard_exist():
    with db.get_pool().connection() as conn:
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='v2_terminal_failure_outbox'"
        ).fetchall()
        indexes = conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename IN ('v2_terminal_failure_outbox','agent_status_events')"
        ).fetchall()
    assert {
        "job_id",
        "user_id",
        "error_code",
        "target_route_id",
        "target_route_updated_at",
        "status_delivered_at",
        "runtime_error_delivered_at",
        "status_next_attempt_at",
        "runtime_error_next_attempt_at",
    }.issubset({row[0] for row in columns})
    assert {
        "v2_terminal_failure_status_pending_idx",
        "v2_terminal_failure_runtime_pending_idx",
        "ux_agent_status_events_job_error",
    }.issubset({row[0] for row in indexes})


def test_singleflight_unique_index_enforced():
    _seed_user("u_mig_1")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status) VALUES ('u_mig_1','chat','pending')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO agent_jobs (user_id, lane, status) VALUES ('u_mig_1','chat','pending')"
            )
    # cleanup so the shared session DB stays clean for later modules
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id='u_mig_1'")
