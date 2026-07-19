"""0014 迁移落地：四张 V2 表 + single-flight 唯一索引真的存在且生效。"""

import inspect
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


def _migration_0041_module():
    backend = Path(__file__).parent.parent / "backend"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    return (
        ScriptDirectory.from_config(cfg)
        .get_revision("0041_v2_mcp_mutation_attempts")
        .module
    )


def test_v2_tables_exist():
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN "
            "('agent_jobs','agent_action_queue','agent_status_events','runtime_state')"
        ).fetchall()
    names = {r[0] for r in rows}
    assert names == {
        "agent_jobs",
        "agent_action_queue",
        "agent_status_events",
        "runtime_state",
    }


def test_v2_job_liveness_columns_exist():
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='agent_jobs' "
            "AND column_name IN ('input_generation','lease_expires_at','queue_deadline_at')"
        ).fetchall()
    assert {row[0] for row in rows} == {
        "input_generation",
        "lease_expires_at",
        "queue_deadline_at",
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
    # Rebasing pre onto test picked up test's tee-shadow extension
    # 0019_tee_reconcile_state (forked off 0018_tee_reconcile_cursors, already
    # joined to the V2 lineage at 0033/0035). 0039 no-op-merges it into the V2
    # head without reparenting either deployed lineage.
    assert set(script.get_revision("0039_merge_tee_recon_state").down_revision) == {
        "0038_v2_prompt_cache_metrics",
        "0019_tee_reconcile_state",
    }
    # 0040 chains linearly off 0039 (genesis serve-worker claim attribution for
    # deploy-orphan reclaim), followed by 0041 mutation attempts, 0042
    # workspace, 0043 encrypted trajectories, and 0044 workspace batches.
    assert (
        script.get_revision("0040_genesis_worker_claim").down_revision
        == "0039_merge_tee_recon_state"
    )
    assert script.get_revision("0041_v2_mcp_mutation_attempts").down_revision == (
        "0040_genesis_worker_claim"
    )
    assert script.get_revision("0042_v2_workspace_foundation").down_revision == (
        "0041_v2_mcp_mutation_attempts"
    )
    assert script.get_revision("0043_v2_encrypted_trajectories").down_revision == (
        "0042_v2_workspace_foundation"
    )
    assert script.get_revision("0044_v2_workspace_batches").down_revision == (
        "0043_v2_encrypted_trajectories"
    )
    assert script.get_revision("0045_drop_retired_supervisor").down_revision == (
        "0044_v2_workspace_batches"
    )
    assert script.get_revision("0046_v2_summary_segments").down_revision == (
        "0045_drop_retired_supervisor"
    )
    assert script.get_current_head() == "0046_v2_summary_segments"


def test_0046_segmented_summary_schema_is_immutable_and_head_is_bound():
    with db.get_pool().connection() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='v2_conversation_summary_segments'"
            ).fetchall()
        }
        head_column = conn.execute(
            "SELECT is_nullable,column_default FROM information_schema.columns "
            "WHERE table_name='v2_conversation_summary' "
            "AND column_name='materialized_segment_ids'"
        ).fetchone()
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND "
                "tgrelid IN ('v2_conversation_summary'::regclass,"
                "'v2_conversation_summary_segments'::regclass)"
            ).fetchall()
        }
    assert {
        "segment_id",
        "coverage_kind",
        "level",
        "start_seq",
        "end_seq",
        "source_message_count",
        "legacy_opaque_through_seq",
        "child_segment_ids",
        "summary_envelope",
    } <= columns
    assert head_column is not None and head_column[0] == "NO"
    assert "trg_v2_summary_segments_immutable" in triggers
    assert "trg_v2_segmented_summary_head" in triggers
    assert "trg_v2_summary_head_delete_segments" in triggers


def test_0041_indexes_and_validated_frontier_constraint_exist():
    with db.get_pool().connection() as conn:
        indexes = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='v2_effect_outbox'"
        ).fetchall()
        constraint = conn.execute(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conrelid='v2_effect_outbox'::regclass "
            "AND conname='ck_v2_effect_input_frontier'"
        ).fetchone()
        enqueue_seq_index = conn.execute(
            "SELECT indisunique FROM pg_index "
            "WHERE indexrelid='v2_effect_outbox_enqueue_seq_unique'::regclass"
        ).fetchone()
    names = {row[0] for row in indexes}
    assert "ix_v2_effect_user_frontier" in names
    assert "ix_v2_effect_dispatch_pending_v0041" in names
    assert constraint == (True,)
    # 0041's keyset backfill advances on enqueue_seq and therefore relies on
    # the uniqueness installed by 0033 and retained/repaired by 0034.
    assert enqueue_seq_index == (True,)


def test_0042_workspace_tables_and_mutation_frontier_are_installed():
    with db.get_pool().connection() as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('v2_workspace_entries','v2_sandbox_usage_events')"
        ).fetchall()
        function_source = conn.execute(
            "SELECT pg_get_functiondef('v2_fill_effect_input_frontier()'::regprocedure)"
        ).fetchone()[0]
        usage_columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='v2_sandbox_usage_events'"
            ).fetchall()
        }
    assert {row[0] for row in tables} == {
        "v2_workspace_entries",
        "v2_sandbox_usage_events",
    }
    assert "workspace_encrypted_v1" in function_source
    assert {"released_at", "duration_ms", "outcome"} <= usage_columns


def test_0044_workspace_batch_mutation_frontier_is_installed():
    with db.get_pool().connection() as conn:
        function_source = conn.execute(
            "SELECT pg_get_functiondef("
            "'v2_fill_effect_input_frontier()'::regprocedure)"
        ).fetchone()[0]
    assert "workspace_batch_encrypted_v1" in function_source


def test_0041_claim_gate_is_installed_and_backfill_runs_after_ddl_commit():
    migration = _migration_0041_module()
    with db.get_pool().connection() as conn:
        trigger = conn.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid='agent_jobs'::regclass AND NOT tgisinternal "
            "AND tgname='trg_v2_guard_agent_job_claim_0041'"
        ).fetchone()
    assert trigger == ("trg_v2_guard_agent_job_claim_0041",)

    # Structural lock regression: the transaction that takes ALTER/claim-writer
    # locks contains no historical outbox UPDATE. The keyset backfill starts
    # only inside the autocommit block, after those locks have been committed.
    assert "UPDATE v2_effect_outbox effect" not in migration._SCHEMA_UP
    assert "ALTER TABLE v2_effect_outbox" not in (
        migration._BACKFILL_EFFECT_FRONTIERS_BATCH
    )
    upgrade_source = inspect.getsource(migration.upgrade)
    assert upgrade_source.index("autocommit_block") < upgrade_source.index(
        "_backfill_effect_frontiers()"
    )


def test_0041_exact_pre_worker_claim_update_is_fenced():
    uid = "u_0041_old_claim"
    _seed_user(uid)
    try:
        with db.get_pool().connection() as conn:
            job_id = conn.execute(
                "INSERT INTO agent_jobs "
                "(user_id,lane,status,queue_deadline_at) "
                "VALUES (%s,'chat','pending',clock_timestamp()+interval '2 minutes') "
                "RETURNING id",
                (uid,),
            ).fetchone()[0]
            with conn.transaction():
                # This is the pending->claimed UPDATE issued by origin/pre: it
                # has no transaction-local 0041 protocol marker. The BEFORE
                # trigger must return NULL, so UPDATE ... RETURNING yields no row.
                claimed = conn.execute(
                    "UPDATE agent_jobs SET status='claimed', claimed_by=%s, "
                    "claimed_at=now(), expected_runtime_generation="
                    "COALESCE(expected_runtime_generation,%s), "
                    "lease_expires_at=now()+make_interval(secs => %s), "
                    "deadline_at=now()+make_interval(secs => %s) "
                    "WHERE id=%s RETURNING id",
                    ("pre-0041-worker", 1, 300.0, 300.0, job_id),
                ).fetchone()
            row = conn.execute(
                "SELECT status,claimed_by,claimed_at FROM agent_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        assert claimed is None
        assert row == ("pending", None, None)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


@pytest.mark.parametrize("status", ["claimed", "running"])
def test_0041_seeds_and_raises_legacy_active_job_frontier(status):
    uid = f"u_0041_legacy_active_{status}"
    migration = _migration_0041_module()
    _seed_user(uid)
    try:
        db.chat_append_strict(
            uid,
            "legacy-input-1",
            1.0,
            {"id": "legacy-input-1", "role": "user", "body_ct": "ct-1"},
            5000,
        )
        first_seq = db.chat_seq_for_msg_id(uid, "legacy-input-1")
        with db.get_pool().connection() as conn:
            job_id = conn.execute(
                "INSERT INTO agent_jobs "
                "(user_id,lane,status,claimed_by,input_generation,"
                " lease_expires_at,deadline_at) "
                "VALUES (%s,'chat',%s,'pre-0041-worker',0,"
                " clock_timestamp()+interval '5 minutes',"
                " clock_timestamp()+interval '5 minutes') RETURNING id",
                (uid, status),
            ).fetchone()[0]
            conn.execute(migration._SEED_LEGACY_ACTIVE_BARRIERS)
            seeded = conn.execute(
                "SELECT input_frontier_seq,call_key,tool_fingerprint,outcome "
                "FROM v2_mcp_mutation_attempts WHERE job_id=%s",
                (job_id,),
            ).fetchone()
        assert seeded == (
            first_seq,
            migration.LEGACY_ACTIVE_CALL_KEY,
            migration.LEGACY_ACTIVE_TOOL_FINGERPRINT,
            "unknown",
        )

        # Mirror chat_append_and_enqueue's ordering: the new user row is visible
        # in the same transaction before input_generation increments. The
        # agent_jobs trigger must lift the synthetic barrier through that row.
        db.chat_append_strict(
            uid,
            "legacy-input-2",
            2.0,
            {"id": "legacy-input-2", "role": "user", "body_ct": "ct-2"},
            5000,
        )
        second_seq = db.chat_seq_for_msg_id(uid, "legacy-input-2")
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE agent_jobs SET input_generation=input_generation+1 WHERE id=%s",
                (job_id,),
            )
            raised = conn.execute(
                "SELECT input_frontier_seq FROM v2_mcp_mutation_attempts "
                "WHERE job_id=%s AND call_key=%s",
                (job_id, migration.LEGACY_ACTIVE_CALL_KEY),
            ).fetchone()[0]
        assert second_seq > first_seq
        assert raised == second_seq
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_0041_downgrade_refuses_pending_fenced_reply():
    uid = "u_0041_down_fenced"
    _seed_user(uid)
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_effect_outbox "
                "(effect_id,user_id,effect_type,expected_generation,payload) "
                "VALUES ('e_0041_down_fenced',%s,'reply_final_fenced_v1',1,'{}')",
                (uid,),
            )
            status = conn.execute(
                "SELECT status FROM v2_effect_outbox "
                "WHERE effect_id='e_0041_down_fenced'"
            ).fetchone()[0]
            assert status == "pending_fenced_v1"
            with pytest.raises(
                psycopg.errors.ObjectNotInPrerequisiteState,
                match="fenced reply effects are still pending",
            ):
                with conn.transaction():
                    conn.execute(_migration_0041_module()._DOWN_GUARD)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_0041_downgrade_refuses_frontier_ahead_of_reply_cursor():
    uid = "u_0041_down_frontier"
    _seed_user(uid)
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO user_blobs (user_id,kind,doc) "
                "VALUES (%s,'model_api_runtime','{\"v2_reply_cursor_seq\": 3}')",
                (uid,),
            )
            job_id = conn.execute(
                "INSERT INTO agent_jobs (user_id,lane,status) "
                "VALUES (%s,'chat','completed') RETURNING id",
                (uid,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO v2_effect_outbox "
                "(effect_id,user_id,job_id,effect_type,expected_generation,payload,"
                " status,input_frontier_seq) "
                "VALUES ('e_0041_down_frontier',%s,%s,'memory_encrypted_v1',1,"
                " '{}','applied',4)",
                (uid, job_id),
            )
            with pytest.raises(
                psycopg.errors.ObjectNotInPrerequisiteState,
                match="mutation frontier exceeds durable reply cursor",
            ):
                with conn.transaction():
                    conn.execute(_migration_0041_module()._DOWN_GUARD)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_0041_downgrade_is_unconditionally_unsupported_after_diagnostics():
    migration = _migration_0041_module()
    with db.get_pool().connection() as conn:
        # Keep the cleanup and intentional exception in one transaction. The
        # exception rolls the deletes back, while proving the complete guard
        # block parses and executes before downgrade reaches its permanent
        # unsupported boundary.
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState,
            match="worker protocol and replay evidence are permanent",
        ):
            with conn.transaction():
                conn.execute("DELETE FROM v2_mcp_mutation_attempts")
                conn.execute("DELETE FROM v2_effect_outbox")
                conn.execute(migration._DOWN_GUARD)
                conn.execute(migration._DOWN_UNSUPPORTED)


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
        token_types = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='v2_turn_metrics' "
            "AND column_name IN ('prompt_tokens','completion_tokens')"
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
    assert dict(token_types) == {
        "prompt_tokens": "bigint",
        "completion_tokens": "bigint",
    }


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
