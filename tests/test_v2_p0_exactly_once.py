"""P0 fault-injection: exactly-once across the durable-effect boundary.

Proves that a crash between a sink's durable write and the outbox applier's
status='applied' flip yields EXACTLY ONE sink write after recovery. The
applier (``effect_outbox.apply_pending_effects``) does the status flip in the
SAME transaction as the ``dispatch`` call (see effect_outbox.py); if
``dispatch`` raises, the transaction rolls back and the row stays 'pending' —
that IS the crash simulation, no production seam needed. Recovery re-runs
``apply_pending_effects`` over the still-pending row; the sink's own
``db.effect_sink_claim`` (a separate autocommit connection, spec A6) is what
makes the replay a no-op rather than a duplicate write.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import effect_outbox, effect_id, serve_worker
from model_api_runtime.v2 import worker as v2_worker

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 P0 tests require the PostgreSQL test fixture",
)


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_effect_outbox, v2_runtime_state, v2_effect_sink_applied, "
            "agent_jobs, chat_messages, user_blobs CASCADE"
        )
    yield


def _row_status(eid: str) -> str:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status FROM v2_effect_outbox WHERE effect_id=%s", (eid,)
        ).fetchone()
    assert row is not None, f"effect row {eid} vanished"
    return row[0]


def _make_dispatch(writes: list, *, crash_point: str):
    """Test double simulating a real durable sink. ``writes`` is a shared
    list; a successful claim+write appends 1 to it (that IS the sink write).
    Whether it raises (simulating a crash) depends on ``crash_point``:
      - "before_write": raise before claiming — nothing written.
      - "after_write_before_status": claim + count, THEN raise — write
        happened but the applier's status flip never runs.
      - "after_status": claim + count, return normally — no-crash baseline.
      - "write_error": mirrors the PRODUCTION claim-release wrapper every
        `serve_worker._sink_*` follows — claim, THEN the write itself raises
        (not a bare crash after a successful write); on the exception, undo
        the claim via `db.effect_sink_release` before re-raising, so a
        replay re-claims and re-writes instead of silently no-oping. Kept
        alongside `after_write_before_status` (whose write DID succeed) to
        keep this test double faithful to both fault shapes the sinks
        distinguish; the real-sink test below is the load-bearing one for
        this specific seam.
    """
    def dispatch(effect_type: str, payload: dict) -> None:
        if crash_point == "before_write":
            raise RuntimeError("simulated crash BEFORE sink write")
        eid = payload["effect_id"]
        if crash_point == "write_error":
            if not db.effect_sink_claim(eid):
                return
            try:
                raise RuntimeError("simulated sink WRITE failure (not a bare crash)")
            except RuntimeError:
                db.effect_sink_release(eid)
                raise
        if db.effect_sink_claim(eid):
            writes.append(1)
        if crash_point == "after_write_before_status":
            raise RuntimeError("simulated crash AFTER sink write, BEFORE status flip")
        # "after_status": fall through, return normally.

    return dispatch


@pytest.mark.parametrize(
    "crash_point",
    ["before_write", "after_write_before_status", "after_status", "write_error"],
)
def test_exactly_once_across_crash_recovery(pg_clean, crash_point):
    uid = f"u_p0once_{crash_point}"
    seed_user(uid)
    g = db.get_runtime_generation(uid)
    eid = effect_id.derive(job_id=1, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(eid, uid, 1, "reply", g, {"text": "hi"}) is True

    writes: list = []

    if crash_point == "after_status":
        # No-crash baseline: a single apply run applies cleanly.
        res = effect_outbox.apply_pending_effects(
            uid, dispatch=_make_dispatch(writes, crash_point=crash_point)
        )
        assert res == {"applied": 1, "discarded": 0}
        assert writes == [1]
        assert _row_status(eid) == "applied"
        return

    # First run: dispatch raises mid-way (or immediately). apply_pending_effects
    # must propagate the exception (no production catch/swallow), and the row
    # must NOT have been flipped to 'applied'.
    with pytest.raises(RuntimeError):
        effect_outbox.apply_pending_effects(
            uid, dispatch=_make_dispatch(writes, crash_point=crash_point)
        )
    assert _row_status(eid) == "pending"

    if crash_point in ("before_write", "write_error"):
        # "before_write": crash happened before the sink write occurred.
        # "write_error": claimed, then the write itself raised and the claim
        # was released — no successful write ever landed in `writes`.
        assert writes == []
    else:
        assert writes == [1]  # the write DID happen before the simulated crash

    # Recovery: re-run apply over the still-pending row with a non-crashing
    # dispatch (same claim+count logic).
    res = effect_outbox.apply_pending_effects(
        uid, dispatch=_make_dispatch(writes, crash_point="after_status")
    )
    assert res == {"applied": 1, "discarded": 0}
    assert _row_status(eid) == "applied"

    # The strong property: across BOTH runs, exactly one sink write total —
    # effect_sink_claim deduped the replay for the crash-mid-write case, and
    # the before-write case gets its (only) write on recovery.
    assert writes == [1]


def test_write_error_after_claim_releases_and_replay_completes_real_reply_sink(
    pg_clean, monkeypatch
):
    """The reviewer-flagged seam, driven through the REAL production sink —
    not the test double above. `serve_worker._sink_reply` (via
    `build_production_effect_dispatch`) claims the effect via
    `db.effect_sink_claim`, then the underlying durable writer
    (`model_api_runtime.v2.worker._write_encrypted_reply`) raises. Before this
    hardening, `effect_sink_claim`'s commit already landed on its own
    connection, so a replay would see the claim and no-op forever — the
    effect would be LOST even though the outbox row stays 'pending' (visible
    forever as an unresolved job, never actually delivered). Claim-release
    fixes this: the sink's except-clause calls `db.effect_sink_release` before
    re-raising, undoing the claim so a replay redoes the write.

    The underlying writer is swapped for a fake that raises on its first call
    and, on the second, performs a REAL durable write via
    `db.chat_append_strict` (through the real `UserStore.append_chat(...,
    strict=True)`) — the same DB call `_write_encrypted_reply` itself makes.
    Only the encryption envelope construction is stubbed (server stores
    ciphertext verbatim regardless of shape — see `append_chat`'s docstring),
    so this exercises the actual claim-release control flow and the actual
    chat_messages persistence, not just a plain Python side effect."""
    uid = "u_p0_writeerror_reply_sink"
    seed_user(uid)
    g = db.get_runtime_generation(uid)
    eid = effect_id.derive(job_id=1, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(eid, uid, 1, "reply", g, {"text": "hi from write-error test"}) is True

    calls = {"n": 0}

    def flaky_write(store, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient sink WRITE failure")
        # Real write path (mirrors _write_encrypted_reply minus crypto):
        # server stores ciphertext verbatim, so a fixed-shape envelope is a
        # faithful stand-in for a real one here.
        envelope = {"v": 1, "body_ct": "ct", "nonce": "n", "K_user": "k_test"}
        return store.append_chat("openclaw", "model_api", envelope, strict=True)

    monkeypatch.setattr(v2_worker, "_write_encrypted_reply", flaky_write)

    # Real production dispatch — the actual `_sink_reply` claim-release
    # wrapper, not a fake.
    dispatch = serve_worker.build_production_effect_dispatch(uid)

    # First apply: the underlying writer raises. apply_pending_effects must
    # propagate (no swallow) so the outbox row stays 'pending', AND the claim
    # must have been RELEASED -- proving claim-release, not just a bare
    # crash-after-claim that would strand the effect forever.
    with pytest.raises(RuntimeError):
        effect_outbox.apply_pending_effects(uid, dispatch=dispatch)
    assert _row_status(eid) == "pending"
    assert calls["n"] == 1
    with db.get_pool().connection() as conn:
        claimed_row = conn.execute(
            "SELECT 1 FROM v2_effect_sink_applied WHERE effect_id=%s", (eid,)
        ).fetchone()
    assert claimed_row is None, (
        "claim must be released after a write failure -- otherwise the "
        "replay below would no-op and the effect would be LOST"
    )
    with db.get_pool().connection() as conn:
        reply_count_after_failure = conn.execute(
            "SELECT count(*) FROM chat_messages WHERE user_id=%s", (uid,)
        ).fetchone()[0]
    assert reply_count_after_failure == 0  # failed write must not have persisted anything

    # Recovery: re-run apply over the still-pending row. The writer now
    # succeeds on its (second) call.
    res = effect_outbox.apply_pending_effects(uid, dispatch=dispatch)
    assert res == {"applied": 1, "discarded": 0}
    assert _row_status(eid) == "applied"

    # The underlying writer performed exactly ONE successful write (call #2;
    # call #1 raised and produced nothing durable).
    assert calls["n"] == 2
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT doc->>'role', doc->>'source' FROM chat_messages WHERE user_id=%s",
            (uid,),
        ).fetchall()
    assert rows == [("openclaw", "model_api")]  # exactly one reply exists, written once
