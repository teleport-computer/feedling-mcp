"""P0 acceptance tests for Hosted Runtime V2 raw-history safety."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import provider_client
from core import store as core_store
from model_api_runtime.v2 import effect_id
from model_api_runtime.v2 import effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker
from model_api_runtime.v2 import worker as v2_worker

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 P0 history-safety tests require the PostgreSQL test fixture",
)

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="")


def _reset(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table(monkeypatch):
    # claim_next_job claims globally (no user_id filter) — a leftover pending
    # job from another test module could get claimed by this file's tests.
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


# ---------------------------------------------------------------------------
# P0 #1 — 5000+ messages with identical timestamps remain immutable source
# history independent of the hot-cache limit.
# ---------------------------------------------------------------------------

N = 5001  # Strictly beyond the former durable-row cap; do not reduce.


def test_5000_identical_ts_preserve_every_source_row():
    """A tiny hot-window hint cannot delete any raw source row."""
    uid = "u_p0hs_gc_5000"
    seed_user(uid)
    _reset(uid)

    # 1) Seed a few older messages with distinct timestamps.
    old_ts = 1000.0
    n_old = 5
    for i in range(n_old):
        db.chat_append_strict(uid, f"old{i:02d}", old_ts + i, {"role": "user", "n": i}, 1_000_000)

    # 2) Write N=5000+ messages sharing one timestamp via the strict path.
    #    max_messages is far below N so a plain
    #    count-only trim would otherwise nuke almost all of them.
    identical_ts = 2000.0
    max_messages = 500
    for i in range(N):
        db.chat_append_strict(
            uid,
            f"m{i:05d}",
            identical_ts,
            {"role": "user", "n": i, "body_ct": f"cipher-{i}"},
            max_messages,
        )

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT msg_id, seq, ts FROM chat_messages WHERE user_id=%s ORDER BY seq ASC",
            (uid,),
        ).fetchall()
    new_rows = [r for r in rows if r[0].startswith("m")]
    old_rows = [r for r in rows if r[0].startswith("old")]

    assert len(new_rows) == N, (
        f"expected all {N} identical-ts messages to survive the "
        f"trim despite max_messages={max_messages}, got {len(new_rows)}"
    )
    seen_ns = {r[0] for r in new_rows}
    assert seen_ns == {f"m{i:05d}" for i in range(N)}  # exact set, no gaps
    assert [row[0] for row in old_rows] == [f"old{i:02d}" for i in range(n_old)]
    assert all(row[1] > old_rows[-1][1] for row in new_rows)
    assert all(row[2] == identical_ts for row in new_rows)
    assert len(rows) == N + n_old > max_messages
    # Single-row durable reads remain available on both sides of the former
    # 5,000-row boundary even though every message shares one timestamp.
    for msg_id in ("m00000", "m02500", "m05000"):
        row = db.chat_get_strict(uid, msg_id)
        assert row is not None
        assert row["n"] == int(msg_id.removeprefix("m"))
        assert row["body_ct"] == f"cipher-{row['n']}"

    first_batch_seq = db.chat_seq_for_msg_id(uid, "m00000")
    assert first_batch_seq is not None
    cursor_seq = first_batch_seq - 1
    paged_ids: list[str] = []
    while True:
        page = db.chat_history_page_by_seq_strict(
            uid, after_seq=cursor_seq, limit=777)
        if not page:
            break
        paged_ids.extend(str(item["id"]) for item in page)
        cursor_seq = int(page[-1]["seq"])
    assert paged_ids == [f"m{i:05d}" for i in range(N)]


def test_kill_between_sink_write_and_status_flip_yields_exactly_one_reply():
    """Crash-domain recovery invariant expressed at the effect boundary (PR
    D's job/turn lifecycle) leaning on PR A's effect_id fence +
    `effect_outbox.apply_pending_effects` idempotency — see
    tests/test_v2_p0_exactly_once.py for the FULL crash-point matrix
    (before_write / after_write_before_status / after_status / write_error)
    against both a test-double dispatch and the real production
    `serve_worker.build_production_effect_dispatch`. This test does not
    duplicate that matrix; it ties the same machinery to a PR D-shaped job:
    an effect keyed to a REAL claimed V2 "chat" job (jobs_store.claim_next_job
    — the re-claim path PR D provides), a REAL underlying durable writer
    (`worker._write_encrypted_reply`, going through the real
    `db.chat_append_strict`), and asserts the strong end state: after the
    simulated kill AND the re-drive, there is EXACTLY ONE reply row in
    chat_messages for this user — not zero (lost) and not two (duplicated).
    """
    uid = "u_p0hs_killboundary"
    seed_user(uid)
    _reset(uid)

    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-p0-killboundary")
    assert job is not None and job["lane"] == "chat"

    g = db.get_runtime_generation(uid)
    eid = effect_id.derive(job_id=job["id"], effect_type="reply", ordinal=0)
    assert db.effect_enqueue(eid, uid, job["id"], "reply", g, {"text": "hi from kill-boundary test"}) is True

    calls = {"n": 0}

    def flaky_write(store, text):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate the crash: the durable write itself never completes on
            # the first attempt (crash BETWEEN the sink's write attempt and
            # the outbox's status flip — apply_pending_effects' transaction
            # rolls back, the row stays 'pending').
            raise RuntimeError("simulated crash before the durable write lands")
        envelope = {"v": 1, "body_ct": "ct", "nonce": "n", "K_user": "k_test"}
        return store.append_chat("openclaw", "model_api", envelope, strict=True)

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(v2_worker, "_write_encrypted_reply", flaky_write)

        # Real production dispatch — the actual `_sink_reply` claim-release
        # wrapper (see serve_worker.build_production_effect_dispatch).
        dispatch = serve_worker.build_production_effect_dispatch(uid)

        with _pytest.raises(RuntimeError):
            effect_outbox.apply_pending_effects(uid, dispatch=dispatch)

        with db.get_pool().connection() as conn:
            status_after_crash = conn.execute(
                "SELECT status FROM v2_effect_outbox WHERE effect_id=%s", (eid,),
            ).fetchone()[0]
        assert status_after_crash == "pending"  # never flipped — the crash simulation
        with db.get_pool().connection() as conn:
            n_after_crash = conn.execute(
                "SELECT count(*) FROM chat_messages WHERE user_id=%s", (uid,),
            ).fetchone()[0]
        assert n_after_crash == 0  # the failed first write left nothing durable

        # Re-drive: re-run apply_pending_effects over the still-pending row
        # (the recovery path — a resumed/re-claimed worker re-applies the
        # outbox exactly the way it would after a real process kill+restart).
        # Advance the durable retry clock instead of sleeping through the
        # production backoff window.
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_effect_outbox SET next_attempt_at=now() "
                "WHERE effect_id=%s AND status='pending'",
                (eid,),
            )
        res = effect_outbox.apply_pending_effects(uid, dispatch=dispatch)
        assert res == {"applied": 1, "discarded": 0}

    with db.get_pool().connection() as conn:
        status_final = conn.execute(
            "SELECT status FROM v2_effect_outbox WHERE effect_id=%s", (eid,),
        ).fetchone()[0]
    assert status_final == "applied"

    # THE strong property: exactly one reply bubble total, across both the
    # crashed attempt and the recovery re-drive.
    assert calls["n"] == 2  # one failed attempt + one that actually wrote
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT doc->>'role', doc->>'source' FROM chat_messages WHERE user_id=%s",
            (uid,),
        ).fetchall()
    assert rows == [("openclaw", "model_api")]  # exactly one reply, written once
