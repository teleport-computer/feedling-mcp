"""Summary watermark sequence plumbing and migration graph coverage.

Migration 0031 introduced ``watermark_seq``. Migration 0033 and the V2 worker
now also move the reply/turn boundary from ``last_replied_ts`` to the durable
``v2_reply_cursor_seq``; this module remains focused on summary coverage.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import provider_client
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from model_api_runtime.v2 import compaction as v2_compaction
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="sk-user-byok",
    base_url="",
)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    # claim_next_job() is a GLOBAL claim (no user_id filter, by design) — see
    # the identical fixture in test_v2_worker.py / test_v2_compaction_integration.py.
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
    assert script.get_current_head() == "0062_v2_failure_reply"
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


def test_upsert_and_read_summary_round_trip_watermark_seq():
    uid = "u_wmseq_roundtrip"
    conftest.seed_user(uid)
    _reset(uid)

    # First build (expected_version=0): INSERT branch, explicit watermark_seq.
    ok = jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "s1"},
        watermark_ts=10.0,
        expected_version=0,
        watermark_seq=7,
    )
    assert ok
    row = jobs_store.get_summary_row(uid)
    assert row["watermark_ts"] == 10.0
    assert row["watermark_seq"] == 7
    assert row["version"] == 1

    # CAS version semantics unchanged: wrong expected_version -> False, no write.
    bad = jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "s2"},
        watermark_ts=20.0,
        expected_version=99,
        watermark_seq=8,
    )
    assert bad is False
    row2 = jobs_store.get_summary_row(uid)
    assert row2["watermark_seq"] == 7  # unchanged by the lost CAS

    # Correct CAS advance, new watermark_seq, same UPDATE as the ts advance.
    ok2 = jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "s2"},
        watermark_ts=20.0,
        expected_version=1,
        watermark_seq=15,
    )
    assert ok2
    row3 = jobs_store.get_summary_row(uid)
    assert row3["watermark_ts"] == 20.0
    assert row3["watermark_seq"] == 15
    assert row3["version"] == 2

    # Omitting watermark_seq on a subsequent CAS preserves the prior value
    # (COALESCE back-compat path in jobs_store) rather than clobbering it to 0
    # — legacy callers that don't know about watermark_seq yet must not regress
    # a value a previous compaction already wrote.
    ok3 = jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={"plaintext": "s3"}, watermark_ts=30.0, expected_version=2
    )
    assert ok3
    row4 = jobs_store.get_summary_row(uid)
    assert row4["watermark_ts"] == 30.0
    assert row4["watermark_seq"] == 15  # preserved, not zeroed


def test_get_summary_row_lazily_translates_legacy_zero_watermark_seq():
    """A pre-0031 row (watermark_seq at the migration default 0, but a real
    watermark_ts) must read back a real translated seq, not a bare 0 — this is
    the back-compat path `get_summary_row` runs via `db.seq_for_watermark_ts`."""
    uid = "u_wmseq_legacy_row"
    conftest.seed_user(uid)
    _reset(uid)
    db.chat_append(uid, "m0", 5.0, {"id": "m0", "role": "user", "content": "a"}, 1000)
    seq0 = db.chat_seq_for_msg_id(uid, "m0")
    db.chat_append(uid, "m1", 15.0, {"id": "m1", "role": "user", "content": "b"}, 1000)

    # Write a summary row the OLD way: no watermark_seq kwarg at all (mirrors a
    # pre-0031 caller / a row written before this task existed).
    ok = jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "legacy"},
        watermark_ts=10.0,
        expected_version=0,
    )
    assert ok
    row = jobs_store.get_summary_row(uid)
    assert (
        row["watermark_seq"] == seq0
    )  # translated: MAX(seq) WHERE ts < 10.0 == m0's seq


# ---------------------------------------------------------------------------
# db.seq_for_watermark_ts: strictly-less boundary, including same-ts ties
# ---------------------------------------------------------------------------


def test_seq_for_watermark_ts_strictly_less_boundary():
    uid = "u_wmseq_boundary"
    conftest.seed_user(uid)
    _reset(uid)
    # m_below at ts=99; m_tie1/m_tie2 share ts=100 (a same-ts tie); m_above at ts=101.
    db.chat_append(
        uid, "m_below", 99.0, {"id": "m_below", "role": "user", "content": "a"}, 1000
    )
    seq_below = db.chat_seq_for_msg_id(uid, "m_below")
    db.chat_append(
        uid, "m_tie1", 100.0, {"id": "m_tie1", "role": "user", "content": "b"}, 1000
    )
    seq_tie1 = db.chat_seq_for_msg_id(uid, "m_tie1")
    db.chat_append(
        uid, "m_tie2", 100.0, {"id": "m_tie2", "role": "user", "content": "c"}, 1000
    )
    seq_tie2 = db.chat_seq_for_msg_id(uid, "m_tie2")
    db.chat_append(
        uid, "m_above", 101.0, {"id": "m_above", "role": "user", "content": "d"}, 1000
    )
    seq_above = db.chat_seq_for_msg_id(uid, "m_above")
    assert seq_below < seq_tie1 < seq_tie2 < seq_above

    # watermark_ts=100.0 (AT the tie): strictly-less excludes BOTH tie rows —
    # a row exactly at the boundary ts is never counted as covered, since a
    # ts-only watermark can't tell which same-ts row was actually folded.
    got = db.seq_for_watermark_ts(uid, 100.0)
    assert got == seq_below

    # watermark_ts=101.0 (past the tie): both tie rows now included, m_above excluded.
    got2 = db.seq_for_watermark_ts(uid, 101.0)
    assert got2 == seq_tie2

    # No row strictly before ts=0 -> covers nothing.
    assert db.seq_for_watermark_ts(uid, 0.0) == 0


def test_chat_seq_for_msg_id_exact_lookup_and_missing():
    uid = "u_wmseq_by_id"
    conftest.seed_user(uid)
    _reset(uid)
    db.chat_append(uid, "m0", 1.0, {"id": "m0", "role": "user", "content": "a"}, 1000)
    seq0 = db.chat_seq_for_msg_id(uid, "m0")
    assert isinstance(seq0, int) and seq0 > 0
    assert db.chat_seq_for_msg_id(uid, "does_not_exist") is None


# ---------------------------------------------------------------------------
# worker._run_compaction: advances watermark_seq to the last-compacted row's
# real (DB-assigned) seq, atomically alongside watermark_ts
# ---------------------------------------------------------------------------


def test_run_compaction_advances_watermark_seq_to_last_compacted_row(monkeypatch):
    """Seeds REAL chat_messages rows (so each has a real, distinct DB-assigned
    seq) over the compaction budget, drives a real maintenance-lane compaction
    job through `worker.process_job`, and asserts the summary row's
    watermark_seq lands on the EXACT seq of the last-folded row — mirrors
    `test_v2_worker.py::test_run_compaction_folds_oldest_batch_and_advances_watermark`
    but for the new seq watermark."""
    uid = "u_wmseq_compaction"
    conftest.seed_user(uid)
    _reset(uid)

    n = worker._TAIL_KEEP + 6
    tail_rows = []
    for i in range(n):
        mid = f"m{i}"
        db.chat_append(
            uid, mid, float(i), {"id": mid, "role": "user", "content": f"msg {i}"}, 1000
        )
        tail_rows.append(
            {"id": mid, "ts": float(i), "role": "user", "content": f"msg {i}"}
        )

    old_count = n - worker._TAIL_KEEP
    assert old_count > 0
    expected_last_seq = db.chat_seq_for_msg_id(uid, f"m{old_count - 1}")
    assert expected_last_seq is not None
    # Distinct, monotonic seqs (sanity: the fixture actually produced what the
    # test claims to be exercising, not an accidental tie).
    all_seqs = [db.chat_seq_for_msg_id(uid, f"m{i}") for i in range(n)]
    assert all_seqs == sorted(all_seqs)
    assert len(set(all_seqs)) == n

    job_id, _ = jobs_store.enqueue_job(uid, "maintenance")
    job = jobs_store.claim_next_job("w")

    async def _fake_compact(
        *,
        provider_config,
        current_summary,
        old_messages,
        llm,
        usage_out=None,
    ):
        return current_summary + "\n- new"

    monkeypatch.setattr(v2_compaction, "compact", _fake_compact)

    def _write_summary(
        uid_, summary, watermark_ts, expected_version, watermark_seq=None
    ):
        return jobs_store.upsert_summary_row_cas(
            uid_,
            summary_envelope={"plaintext": summary},
            watermark_ts=watermark_ts,
            expected_version=expected_version,
            watermark_seq=watermark_seq,
        )

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: tail_rows,
        read_summary=lambda uid_: ("- old", 0.0, 0),
        write_summary=_write_summary,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    summary_row = jobs_store.get_summary_row(uid)
    assert summary_row["watermark_ts"] == float(old_count - 1)
    assert summary_row["watermark_seq"] == expected_last_seq
    assert _job_status(job_id)[0] == "completed"


def test_run_compaction_without_id_in_tail_row_leaves_seq_watermark_unadvanced(
    monkeypatch,
):
    """Degenerate fallback: a tail row dict with neither "seq" nor "id" (only
    ever a synthetic test double — every real row carries "id") must not crash
    the CAS write; the ts watermark still advances, watermark_seq is left at
    its prior value (0 on first build) rather than a guessed value."""
    uid = "u_wmseq_no_id"
    conftest.seed_user(uid)
    _reset(uid)

    tail = [
        {"ts": float(i), "role": "user", "content": f"msg {i}"}
        for i in range(worker._TAIL_KEEP + 6)
    ]

    async def _fake_compact(
        *,
        provider_config,
        current_summary,
        old_messages,
        llm,
        usage_out=None,
    ):
        return current_summary + "\n- new"

    monkeypatch.setattr(v2_compaction, "compact", _fake_compact)

    def _write_summary(
        uid_, summary, watermark_ts, expected_version, watermark_seq=None
    ):
        return jobs_store.upsert_summary_row_cas(
            uid_,
            summary_envelope={"plaintext": summary},
            watermark_ts=watermark_ts,
            expected_version=expected_version,
            watermark_seq=watermark_seq,
        )

    job_id, _ = jobs_store.enqueue_job(uid, "maintenance")
    job = jobs_store.claim_next_job("w")
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: tail,
        read_summary=lambda uid_: ("- old", 0.0, 0),
        write_summary=_write_summary,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    old_count = len(tail) - worker._TAIL_KEEP
    summary_row = jobs_store.get_summary_row(uid)
    assert summary_row["watermark_ts"] == tail[old_count - 1]["ts"]
    assert summary_row["watermark_seq"] == 0


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    return row
