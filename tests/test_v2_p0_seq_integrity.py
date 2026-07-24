"""P0 fault-injection: history integrity under identical timestamps.

Proves that ``seq`` (a real monotonic identity-column counter), not ``ts``
(wall-clock, which can collide under concurrent/rapid writers), is the total
order chat history is read back in. 5000 messages sharing ONE ts must come
back complete and in exact insertion order, and a cursor advanced to
chat_max_seq must lose nothing and surface nothing spurious on resume.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 P0 tests require the PostgreSQL test fixture",
)

N = 5000  # P0 is specifically about scale under identical ts — do not reduce.


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_effect_outbox, v2_runtime_state, v2_effect_sink_applied, "
            "agent_jobs, chat_messages, user_blobs CASCADE"
        )
    yield


def test_seq_total_order_survives_identical_ts(pg_clean):
    uid = "u_p0seq"
    seed_user(uid)
    ts = 1_700_000_000.0

    for i in range(N):
        db.chat_append_strict(uid, f"m{i:05d}", ts, {"role": "user", "n": i}, 0)

    # chat_messages_after_seq returns each stored doc with `seq` merged in
    # (see db.chat_messages_after_seq docstring) — limit must cover all N.
    rows = db.chat_messages_after_seq(uid, 0, limit=N + 10)
    assert len(rows) == N
    ns = [r["n"] for r in rows]
    assert ns == list(range(N))  # strictly increasing seq == insertion order

    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == N  # all distinct despite the shared ts

    mx = db.chat_max_seq(uid)
    assert mx == rows[-1]["seq"]

    # Cursor round-trip: advancing to chat_max_seq must return nothing (no
    # spurious re-delivery) ...
    assert db.chat_messages_after_seq(uid, mx, limit=N + 10) == []

    # ... and appending exactly one more message (same ts) must surface
    # exactly that one new row on resume — no gap, no duplicate.
    db.chat_append_strict(uid, "m_extra", ts, {"role": "user", "n": N}, 0)
    resumed = db.chat_messages_after_seq(uid, mx, limit=N + 10)
    assert len(resumed) == 1
    assert resumed[0]["n"] == N
    assert resumed[0]["seq"] > mx
