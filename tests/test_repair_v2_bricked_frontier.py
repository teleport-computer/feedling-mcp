"""Guard for scripts/repair_v2_bricked_summary_frontier.py.

The repair script DELETES summary rows, so it earns a test: seed a frontier
corrupted exactly the way the pre-ed3a275f fold corrupted them — an immutable
leaf whose source_message_count counts a verify_ping row that verify_loop then
GC'd — and prove the script (a) detects it via the real production validator
and (b) resets it, leaving the durable chat_messages ledger untouched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
from core import store as core_store
from model_api_runtime.v2 import jobs_store


def _load_script():
    path = Path(__file__).parent.parent / "scripts" / "repair_v2_bricked_summary_frontier.py"
    spec = importlib.util.spec_from_file_location("repair_v2_frontier_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reset(uid: str) -> None:
    conftest.seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM v2_conversation_summary_segments WHERE user_id=%s", (uid,)
        )
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))


def _append(uid: str, msg_id: str, ts: float, *, source: str | None = None) -> int:
    doc = {"id": msg_id, "role": "user", "body_ct": f"ct-{msg_id}"}
    if source is not None:
        doc["source"] = source
    db.chat_append_strict(uid, msg_id, ts, doc, core_store.MAX_CHAT_MESSAGES)
    seq = db.chat_seq_for_msg_id(uid, msg_id)
    assert seq is not None
    return int(seq)


def test_repair_detects_and_resets_a_synthetic_bricked_frontier():
    uid = "u_repair_bricked_frontier"
    _reset(uid)
    real_seq = _append(uid, f"{uid}-r0", 1.0)
    vp_seq = _append(uid, f"{uid}-vp", 2.0, source="verify_ping")
    assert vp_seq > real_seq

    # Reproduce the PRE-FIX corrupt leaf directly (the fixed append_summary_leaf_cas
    # would refuse it now): an exact leaf covering [real..verify_ping] whose
    # source_message_count (2) counts the synthetic row.
    with db.get_pool().connection() as conn:
        with conn.transaction():
            seg_id = conn.execute(
                "INSERT INTO v2_conversation_summary_segments "
                "(user_id,format_version,coverage_kind,level,start_seq,end_seq,"
                " source_message_count,legacy_opaque_through_seq,child_segment_ids,"
                " summary_envelope) "
                "VALUES (%s,1,'exact',0,%s,%s,2,0,'{}'::BIGINT[],%s) RETURNING segment_id",
                (uid, real_seq, vp_seq, jobs_store.Jsonb({"plaintext": "- corrupt"})),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO v2_conversation_summary "
                "(user_id,summary_envelope,watermark_ts,version,watermark_seq,"
                " materialized_segment_ids) VALUES (%s,%s,2.0,1,%s,%s)",
                (uid, jobs_store.Jsonb({"plaintext": "- corrupt"}), vp_seq, [seg_id]),
            )

    script = _load_script()

    # The post-fix witness excludes synthetic rows, so the over-counting leaf is
    # flagged as soon as it exists — even before verify_loop deletes the row —
    # which is exactly the desired proactive detection (this leaf WILL brick).
    assert script._bricked_detail(uid) == "source_count_mismatch"

    # verify_loop then GCs the verify_ping row; the frontier stays flagged.
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, f"{uid}-vp"),
        )
    detail = script._bricked_detail(uid)
    assert detail is not None, "GC'd-synthetic leaf must be flagged as bricked"

    segments, heads = script._reset_frontier(uid)
    assert (segments, heads) == (1, 1)

    # Reset cleared the corruption, and the durable ledger row survived.
    assert script._bricked_detail(uid) is None
    assert jobs_store.get_summary_frontier_state(uid) is None
    assert db.chat_seq_for_msg_id(uid, f"{uid}-r0") == real_seq
