"""D5 stable-seq reply cursor wiring: the V2 chat reply-read boundary must be
keyed on ``chat_messages.seq`` (monotonic identity column), never wall-clock
``ts``.

The bug this fixes (observed on the pre env as a ``reason=reconcile`` no-op
loop / "no reply"): a user message whose ``ts`` is <= the durable
``last_replied_ts`` cursor is skipped FOREVER by the ts boundary — either
because two messages share an identical ``ts`` (the second is dropped by the
``ts <= since`` compare) or because a later-arriving message carries an earlier
client-assigned ``ts`` than an already-answered one. ``seq`` has no such gap:
it is assigned in insertion order, so "answered up to seq N, read seq > N"
never loses or re-answers a message.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import cursor as v2_cursor
from model_api_runtime.v2 import serve_worker
from core import enclave as core_enclave

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 reply-cursor tests require the PostgreSQL test fixture",
)


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE chat_messages CASCADE")
    yield


def _enc_doc(mid: str, ts: float, role: str = "user") -> dict:
    # Minimal envelope that passes _read_messages' body_ct + K_enclave gate.
    return {"id": mid, "ts": ts, "role": role, "content_type": "text",
            "body_ct": f"ct-{mid}", "nonce": "n", "K_user": "ku", "K_enclave": "ke"}


def _stub_enclave(monkeypatch):
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda uid: "rt")
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda row, *a, **k: f"text-{row['id']}".encode())


def test_read_messages_is_keyed_on_seq_not_ts(pg_clean, monkeypatch):
    """Two user messages sharing an identical ts: after answering the first
    (cursor = its seq), the SECOND must still be delivered — the ts boundary
    (``ts <= 100``) would drop it, the seq boundary keeps it."""
    seed_user("u_rc_tie")
    db.chat_append("u_rc_tie", "m1", 100.0, _enc_doc("m1", 100.0), 5000)
    db.chat_append("u_rc_tie", "m2", 100.0, _enc_doc("m2", 100.0), 5000)
    s1 = db.chat_messages_after_seq("u_rc_tie", 0, limit=10)[0]["seq"]
    _stub_enclave(monkeypatch)

    out = serve_worker._read_messages("u_rc_tie", after_seq=s1)

    assert [m["id"] for m in out] == ["m2"]
    assert out[0]["seq"] == s1 + 1
    assert out[0]["content"] == "text-m2"


def test_read_messages_carries_seq_for_cursor_advance(pg_clean, monkeypatch):
    seed_user("u_rc_seq")
    db.chat_append("u_rc_seq", "a", 5.0, _enc_doc("a", 5.0), 5000)
    db.chat_append("u_rc_seq", "b", 6.0, _enc_doc("b", 6.0), 5000)
    _stub_enclave(monkeypatch)

    out = serve_worker._read_messages("u_rc_seq", after_seq=0)

    assert [(m["id"], m["seq"]) for m in out] == [
        ("a", db.chat_messages_after_seq("u_rc_seq", 0, limit=10)[0]["seq"]),
        ("b", db.chat_messages_after_seq("u_rc_seq", 0, limit=10)[1]["seq"]),
    ]


def test_coalesce_passes_seq_through():
    """coalesce must forward each kept row's ``seq`` so the caller can advance
    the durable seq cursor to the max answered seq."""
    rows = [
        {"id": "m1", "role": "user", "ts": 101.0, "seq": 7, "content": "A"},
        {"id": "m2", "role": "user", "ts": 102.0, "seq": 9, "content": "B"},
    ]
    coalesced, _cursor = v2_coalesce.coalesce_pending(rows, since_ts=0.0)
    assert [(m["id"], m["seq"]) for m in coalesced] == [("m1", 7), ("m2", 9)]


def test_zero_reply_cursor_does_not_reapply_legacy_timestamp_boundary(pg_clean):
    """Runtime reads must never reconstruct a seq cursor from last_replied_ts.

    Migration 0033 owns the conservative one-time conversion.  A runtime
    ``<=`` fallback can hide a same-timestamp user message forever, so a zero
    seq remains zero and safely replays instead of silently losing input.
    """
    seed_user("u_rc_boot")
    db.chat_append("u_rc_boot", "u1", 100.0, _enc_doc("u1", 100.0), 5000)
    db.chat_append("u_rc_boot", "r1", 101.0, _enc_doc("r1", 101.0, role="openclaw"), 5000)

    class _Store:
        user_id = "u_rc_boot"

    # No v2_reply_cursor_seq in the blob. Even if a legacy timestamp exists in
    # runtime state, the authoritative seq cursor remains conservative.
    assert v2_cursor.load_seq(_Store()) == 0
