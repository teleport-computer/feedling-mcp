"""Generation-fenced effect outbox: enqueue idempotency + fenced apply (spec A4)."""
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
    reason="DB-backed V2 effect outbox tests require the PostgreSQL test fixture",
)


@pytest.fixture
def pg_clean():
    """Truncate the tables this module's tests touch so rows from one test
    (or another module sharing the session-scoped DB) never leak into the
    next: a leftover v2_runtime_state row would let a later test's
    db.get_runtime_generation lazy-init see a stale generation instead of
    starting fresh at 1, and a leftover v2_effect_outbox row would pollute
    effect_pending() for a reused user_id."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_effect_outbox, v2_runtime_state, agent_jobs, user_blobs CASCADE"
        )
    yield


def test_enqueue_is_idempotent_on_effect_id(pg_clean):
    seed_user("u_ob1")
    eid = effect_id.derive(job_id=1, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(eid, "u_ob1", 1, "reply", 1, {"text": "hi"}) is True
    assert db.effect_enqueue(eid, "u_ob1", 1, "reply", 1, {"text": "DUP"}) is False
    pend = db.effect_pending("u_ob1")
    assert len(pend) == 1 and pend[0]["payload"]["text"] == "hi"


def test_apply_dispatches_when_generation_matches(pg_clean):
    seed_user("u_ob2")
    db.get_runtime_generation("u_ob2")  # init at 1
    eid = effect_id.derive(job_id=2, effect_type="reply", ordinal=0)
    db.effect_enqueue(eid, "u_ob2", 2, "reply", 1, {"text": "keep"})
    seen = []
    res = effect_outbox.apply_pending_effects("u_ob2", dispatch=lambda t, p: seen.append((t, p)))
    assert res == {"applied": 1, "discarded": 0}
    # Subset assertion (not full-dict equality): the applier annotates the
    # dispatched payload with the row's effect_id (Task 6 / spec A6) so sinks
    # can claim it for exactly-once — that key is additive, not part of the
    # caller's original payload.
    assert len(seen) == 1
    etype, payload = seen[0]
    assert etype == "reply"
    assert payload["text"] == "keep"
    assert payload["effect_id"] == eid
    assert db.effect_pending("u_ob2") == []


def test_apply_discards_stale_generation_without_dispatch(pg_clean):
    seed_user("u_ob3")
    db.get_runtime_generation("u_ob3")  # 1
    eid = effect_id.derive(job_id=3, effect_type="memory", ordinal=0)
    db.effect_enqueue(eid, "u_ob3", 3, "memory", 1, {"card": "x"})
    # cut over -> generation 3; the pinned-at-1 effect must be discarded, NOT dispatched
    db.advance_runtime_state("u_ob3", from_state="resident", to_state="draining")
    db.advance_runtime_state("u_ob3", from_state="draining", to_state="v2")
    seen = []
    res = effect_outbox.apply_pending_effects("u_ob3", dispatch=lambda t, p: seen.append((t, p)))
    assert res == {"applied": 0, "discarded": 1}
    assert seen == []


def test_apply_is_rerunnable_after_partial(pg_clean):
    # A second apply pass over already-applied rows is a no-op (idempotent applier).
    seed_user("u_ob4")
    db.get_runtime_generation("u_ob4")
    eid = effect_id.derive(job_id=4, effect_type="status", ordinal=0)
    db.effect_enqueue(eid, "u_ob4", 4, "status", 1, {"k": "v"})
    n = []
    effect_outbox.apply_pending_effects("u_ob4", dispatch=lambda t, p: n.append(1))
    effect_outbox.apply_pending_effects("u_ob4", dispatch=lambda t, p: n.append(1))
    assert n == [1]  # dispatched exactly once
