"""P0 fault-injection: no cross-generation contamination (ABA).

Proves that effects pinned to an OLD generation are discarded — never
dispatched, never sink-claimed — after the runtime has moved on, EVEN THOUGH
the cutover state machine returns to a superficially identical value
(resident -> draining -> resident). The monotonic runtime_generation counter
(bumped on every valid transition by db.advance_runtime_state) is what makes
generation 3's "resident" distinct from generation 1's "resident" — a naive
state-only fence would let a stale generation-1 effect through once the state
machine cycles back, this fences on generation instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import effect_outbox, effect_id

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 P0 tests require the PostgreSQL test fixture",
)

ALL_EFFECT_TYPES = [
    "reply", "status", "cursor", "job", "memory", "identity", "schedule", "workspace",
]


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_effect_outbox, v2_runtime_state, v2_effect_sink_applied, "
            "agent_jobs, chat_messages, user_blobs CASCADE"
        )
    yield


def test_aba_stale_generation_effects_never_dispatched(pg_clean):
    uid = "u_p0aba"
    seed_user(uid)
    g = db.get_runtime_generation(uid)
    assert g == 1

    eids = []
    for i, etype in enumerate(ALL_EFFECT_TYPES):
        eid = effect_id.derive(job_id=7, effect_type=etype, ordinal=i)
        assert db.effect_enqueue(eid, uid, 7, etype, g, {"k": "v"}) is True
        eids.append(eid)

    # A -> B -> A': cycle the state machine back to a superficially identical
    # "resident" value, but at a NEW generation (3, not 1).
    g2 = db.advance_runtime_state(uid, from_state="resident", to_state="draining")
    assert g2 == 2
    g3 = db.advance_runtime_state(uid, from_state="draining", to_state="resident")
    assert g3 == 3
    assert db.get_runtime_generation(uid) == 3

    with db.get_pool().connection() as conn:
        state_row = conn.execute(
            "SELECT hosted_runtime_state FROM v2_runtime_state WHERE user_id=%s", (uid,)
        ).fetchone()
    assert state_row[0] == "resident"

    recording: list = []
    res = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda t, p: recording.append(t)
    )

    assert res == {"applied": 0, "discarded": len(ALL_EFFECT_TYPES)}
    assert recording == []  # dispatch never called for ANY stale effect

    # No durable sink write can have happened for any of them (nothing to
    # claim), and every row must be terminally 'discarded'.
    with db.get_pool().connection() as conn:
        claimed = conn.execute(
            "SELECT count(*) FROM v2_effect_sink_applied WHERE effect_id = ANY(%s)",
            (eids,),
        ).fetchone()[0]
        statuses = conn.execute(
            "SELECT effect_id, status FROM v2_effect_outbox WHERE effect_id = ANY(%s)",
            (eids,),
        ).fetchall()
    assert claimed == 0
    assert len(statuses) == len(ALL_EFFECT_TYPES)
    assert all(status == "discarded" for _eid, status in statuses)
