"""Summary watermark sequence plumbing and migration graph coverage.

Migration 0031 introduced ``watermark_seq``. Migration 0033 and the V2 worker
now also move the reply/turn boundary from ``last_replied_ts`` to the durable
``v2_reply_cursor_seq``; this module remains focused on summary coverage.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from model_api_runtime.v2 import jobs_store


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table(monkeypatch):
    # claim_next_job() is a GLOBAL claim (no user_id filter, by design).
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_head_and_watermark_seq_column():
    backend = Path(__file__).parent.parent / "backend"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    # 0032 joins the original tee-pg and V2 lineages. The deployed Runtime V2
    # path continues through 0033/0034 while test's later tee extension is
    # independently joined to 0032 by 0033_merge_tee_reconcile. 0035 merges
    # those valid deployed heads without rewriting either history.
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
    # 0039 no-op-merges test's tee-shadow extension (0019_tee_reconcile_state)
    # into the V2 head when pre is rebased onto test — see test_v2_jobs_migration.
    assert set(script.get_revision("0039_merge_tee_recon_state").down_revision) == {
        "0038_v2_prompt_cache_metrics",
        "0019_tee_reconcile_state",
    }
    # 0040 (genesis serve-worker claim attribution) chains linearly off 0039;
    # 0041 installs mutation attempts, 0042 adds the V2 workspace, 0043 adds
    # encrypted trajectories, and 0044 registers encrypted workspace batches.
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
    assert script.get_revision("0047_model_route_context_window").down_revision == (
        "0046_v2_summary_segments"
    )
    assert script.get_revision("0048_v2_turn_metrics_user_fk").down_revision == (
        "0047_model_route_context_window"
    )
    # pre chain off 0049.
    assert script.get_revision("0050_v2_web_halted_columns").down_revision == (
        "0049_merge_test_pre_heads"
    )
    assert script.get_revision("0051_web_settings_backfill").down_revision == (
        "0050_v2_web_halted_columns"
    )
    # 0052 restores the V1 supervisor tables 0045 dropped (dual-runtime
    # coexistence) and chains linearly off 0051.
    assert script.get_revision("0052_dual_runtime_coexistence").down_revision == (
        "0051_web_settings_backfill"
    )
    # Runtime-V2 lifecycle-closure chain off the same 0049 ancestor.
    assert script.get_revision("0050_v2_trajectory_access_audit").down_revision == (
        "0049_merge_test_pre_heads"
    )
    assert script.get_revision("0051_v2_capture_batches").down_revision == (
        "0050_v2_trajectory_access_audit"
    )
    assert script.get_revision("0052_chat_clear_archive").down_revision == (
        "0051_v2_capture_batches"
    )
    assert script.get_revision("0055_capture_applied_check").down_revision == (
        "0054_merge_pre_v2_heads"
    )
    assert script.get_revision("0056_agent_jobs_hb_idx").down_revision == (
        "0055_capture_applied_check"
    )
    assert script.get_revision("0057_provider_health").down_revision == (
        "0056_agent_jobs_hb_idx"
    )
    # 0058 adds provider_usage_halted off the 0057_provider_health head.
    assert script.get_revision("0058_provider_usage_halted").down_revision == (
        "0057_provider_health"
    )
    # 刻意不钉死 head 的具体 revision 名：链每合入一个 migration 就要回来改这行，
    # 而"当前 head 叫什么"本身没有约束价值——真正要防的是**链分叉成多头**
    # （多头会让部署时的 alembic upgrade head 失败，本仓库有过多头事故）。
    # get_current_head() 在多头时本就会抛错，这里再显式断言一次单头，失败信息更直白。
    # 2026-07-27：本行原钉死 "0058_provider_usage_halted"，被 0059/0060/0061 合入后
    # 撞红；合 test 时又见它被挪到 "0062_v2_failure_reply"——同一类维护滞后，
    # 正是拆掉它的理由。
    assert len(script.get_heads()) == 1, f"迁移链分叉成多头：{script.get_heads()}"
    assert script.get_revision("0031_v2_summary_watermark_seq").down_revision == (
        "0030_v2_runtime_control"
    )
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT column_name, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name='v2_conversation_summary' AND column_name='watermark_seq'"
        ).fetchall()
    assert len(rows) == 1
    _name, is_nullable, default = rows[0]
    assert is_nullable == "NO"
    assert default is not None and "0" in default


# ---------------------------------------------------------------------------
# upsert_summary_row_cas / get_summary_row: watermark_seq round trip + CAS
# semantics unchanged
# ---------------------------------------------------------------------------



def test_chat_seq_for_msg_id_exact_lookup_and_missing():
    uid = "u_wmseq_by_id"
    conftest.seed_user(uid)
    _reset(uid)
    db.chat_append(uid, "m0", 1.0, {"id": "m0", "role": "user", "content": "a"}, 1000)
    seq0 = db.chat_seq_for_msg_id(uid, "m0")
    assert isinstance(seq0, int) and seq0 > 0
    assert db.chat_seq_for_msg_id(uid, "does_not_exist") is None
