"""P0 acceptance tests for Hosted Runtime V2 PR D, Half-B (history safety).

Task 11 of docs/superpowers/plans/2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety.md.
Builds on Tasks 6-10 (all already in this working tree):

  - Task 6  (worker._run_compaction CAS-loss requeue)   — see test_v2_compaction_cas_requeue.py
  - Task 7  (db.reconcile_unenqueued_v2_messages)        — see test_v2_reconcile_sweeper.py (not exercised here)
  - Durable retention (append limits never delete source rows) — see test_v2_gc_coverage_gate.py
  - Task 9  (v2_conversation_summary.watermark_seq, migration 0031)
  - Task 10 (worker._ensure_prompt_coverage / _assert_prompt_covers prompt invariant) — see test_v2_prompt_invariant.py

Each test below asserts a STRONG, non-vacuous property (documented per-test on
why it can't pass by accident) rather than merely exercising the code path.
"""
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
# P0 #1 — 5000+ messages, identical ts, compaction watermark -> immutable
# source history independent of the hot-cache limit.
# ---------------------------------------------------------------------------

N = 5001  # Strictly beyond the former durable-row cap; do not reduce.


def test_5000_identical_ts_and_summary_watermark_preserve_every_source_row():
    """A full summary watermark and a tiny hot-window hint cannot delete any
    raw row, including covered rows and a 5000-message same-timestamp batch."""
    uid = "u_p0hs_gc_5000"
    seed_user(uid)
    _reset(uid)

    # 1) A few OLD, covered messages (distinct ts, strictly before the
    #    watermark) — the "no summary row yet" state means nothing is
    #    trimmed while these are written (fail-safe: no proof of coverage).
    old_ts = 1000.0
    n_old = 5
    for i in range(n_old):
        db.chat_append_strict(uid, f"old{i:02d}", old_ts + i, {"role": "user", "n": i}, 1_000_000)

    # 2) A LOW watermark: watermark_ts sits just above the old batch, so it
    #    covers exactly the old messages (ts 1000..1004 < 1005) and nothing
    #    from the identical-ts batch about to be written (ts=2000 >= 1005).
    watermark_ts = old_ts + n_old  # 1005.0
    ok = jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={"plaintext": "- old stuff"}, watermark_ts=watermark_ts,
        expected_version=0,
    )
    assert ok, "seed CAS insert must land on a clean row"
    summary_row = jobs_store.get_summary_row(uid)
    assert summary_row is not None
    watermark_seq = summary_row["watermark_seq"]
    # Sanity: the lazy ts->seq back-compat translation (Task 9) placed the
    # watermark exactly at the last old message, not beyond it.
    old_last_seq = db.chat_seq_for_msg_id(uid, f"old{n_old - 1:02d}")
    assert watermark_seq == old_last_seq

    # 3) N=5000+ messages, ALL sharing ONE identical ts, via the V2 strict
    #    path — coverage_gated=True. max_messages is far below N so a plain
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
        f"expected all {N} uncovered identical-ts messages to survive the "
        f"trim despite max_messages={max_messages}, got {len(new_rows)}"
    )
    seen_ns = {r[0] for r in new_rows}
    assert seen_ns == {f"m{i:05d}" for i in range(N)}  # exact set, no gaps
    for _msg_id, seq, ts in new_rows:
        assert seq > watermark_seq
        assert ts >= watermark_ts

    assert [row[0] for row in old_rows] == [f"old{i:02d}" for i in range(n_old)]
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


# ---------------------------------------------------------------------------
# P0 #2 — prompt coverage after catch-up (Task 10's synchronous catch-up
# compaction closes the "silently dropped between watermark and tail" hole).
# ---------------------------------------------------------------------------


def _seed_messages(uid: str, n: int) -> list[dict]:
    """Write `n` REAL chat_messages rows (alternating user/assistant) and
    return their real, globally-assigned seq (chat_messages.seq is a table-
    wide IDENTITY sequence, not per-user — see test_v2_prompt_invariant.py's
    module docstring)."""
    store = core_store.get_store(uid)
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "openclaw"
        text = f"msg {i}"
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test", "id": f"{uid}-m{i}"}
        row = store.append_chat(role, "user_message" if role == "user" else "model_api",
                                 envelope, strict=True)
        seq = db.chat_seq_for_msg_id(uid, row["id"])
        assert seq is not None
        out.append({"id": row["id"], "ts": row["ts"],
                     "role": "assistant" if role == "openclaw" else "user",
                     "content": text, "seq": seq})
    return out


def test_prompt_coverage_no_false_gap_under_multiuser_seq_interleaving(monkeypatch):
    """THE critical-bug regression test (P0, real Postgres): ``chat_messages.
    seq`` is a TABLE-WIDE ``BIGINT GENERATED ALWAYS AS IDENTITY`` counter
    shared by ALL users (migration 0001_baseline.py), not reset or scoped per
    user. In production, other users' concurrent inserts interleave with any
    one active user's own messages, so that user's own recent messages are
    NOT contiguous in the global seq space — they can span far more seq
    UNITS than the count of messages the user actually sent.

    The OLD (buggy) ``worker._tail_start_seq``/``_prompt_coverage_gap`` did
    pure seq arithmetic (``watermark_seq < max_seq - tail_limit + 1 - 1``)
    treating that seq SPAN as if it were this user's own unsummarized
    message COUNT. Under multi-user interleaving that span is inflated by
    every other user's inserts sitting in between, so the OLD code found a
    FALSE gap on ~every multi-user turn — triggering a needless synchronous
    BYOK catch-up compaction before every reply (collapsing the verbatim
    tail to a bare summary, a severe quality regression, and occasionally
    failing the turn outright with ``ResponderError
    ("prompt_coverage_incomplete")`` once the bounded retries hit a
    no-op fold).

    This test seeds user A's messages INTERLEAVED with user B's (via the
    real V2 chat-append path, so ``chat_messages.seq`` is the real,
    globally-shared identity value — never assumed to equal a 1-based
    index), leaving A with exactly 10 (<= tail_limit) unsummarized messages
    whose GLOBAL seq span is nonetheless > 60. It asserts:
      1. Non-vacuity — the old seq-arithmetic condition WOULD have flagged a
         gap here (the span exceeds tail_limit by a wide margin).
      2. The FIXED count-based ``_prompt_coverage_gap``/
         ``_ensure_prompt_coverage`` find NO gap (``compact`` is never
         called — enforced via a boom guard, not just "wasn't observed to
         be called").
      3. The assembled tail for A is NON-EMPTY and contains exactly A's 10
         real recent messages (the exact failure mode this task closes: an
         empty tail because a false gap ran a needless catch-up fold).
    """
    uid_a = "u_p0hs_interleave_a"
    uid_b = "u_p0hs_interleave_b"
    conftest.seed_user(uid_a)
    conftest.seed_user(uid_b)
    _reset(uid_a)
    _reset(uid_b)

    tail_limit = 10

    def _seed_one(uid, i, role):
        store = core_store.get_store(uid)
        text = f"{uid} msg {i}"
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test",
                    "id": f"{uid}-m{i}"}
        row = store.append_chat(role, "user_message" if role == "user" else "model_api",
                                 envelope, strict=True)
        seq = db.chat_seq_for_msg_id(uid, row["id"])
        assert seq is not None
        return {"id": row["id"], "ts": row["ts"],
                "role": "assistant" if role == "openclaw" else "user",
                "content": text, "seq": seq}

    # 5 "old" A messages (to be covered by the watermark), each trailed by 2
    # B messages -- interleaving starts immediately.
    a_messages: list[dict] = []
    for i in range(5):
        a_messages.append(_seed_one(uid_a, i, "user" if i % 2 == 0 else "openclaw"))
        _seed_one(uid_b, 2 * i, "user")
        _seed_one(uid_b, 2 * i + 1, "openclaw")

    watermark_seq = a_messages[-1]["seq"]
    watermark_ts = a_messages[-1]["ts"]
    ok = jobs_store.upsert_summary_row_cas(
        uid_a, summary_envelope={"plaintext": "- old"}, watermark_ts=watermark_ts,
        expected_version=0, watermark_seq=watermark_seq)
    assert ok

    # Exactly `tail_limit` (10) more A messages, each trailed by 6 B messages
    # -- inflates the seq span between the watermark and A's own latest
    # message to 10 * 7 = 70, far past tail_limit=10, while A's OWN
    # unsummarized count stays exactly 10.
    for i in range(5, 5 + tail_limit):
        a_messages.append(_seed_one(uid_a, i, "user" if i % 2 == 0 else "openclaw"))
        for j in range(6):
            _seed_one(uid_b, 1000 + 6 * i + j, "user" if j % 2 == 0 else "openclaw")

    # `db.chat_max_seq` IS user-scoped (WHERE user_id=%s) -- this is exactly
    # what the OLD `_ensure_prompt_coverage` fed into `_prompt_coverage_gap`
    # as `max_seq`. Its numeric value is still inflated by B's interleaved
    # inserts because `seq` is a table-wide identity counter.
    a_own_max_seq = db.chat_max_seq(uid_a)
    assert a_own_max_seq == a_messages[-1]["seq"]

    # --- Non-vacuity: the OLD seq-arithmetic condition would have found a
    # false gap here. ---
    assert a_own_max_seq - watermark_seq > 60
    assert db.count_messages_after_seq(uid_a, watermark_seq) == tail_limit  # real count: no gap

    # --- The FIXED count-based check must NOT see a gap. ---
    assert not asyncio.run(worker._prompt_coverage_gap(
        uid_a, watermark_seq=watermark_seq, tail_limit=tail_limit))

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        append_summary_segment=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("no-gap path must not append coverage")
        ),
    )

    returned_watermark_seq, returned_max_seq = asyncio.run(
        worker._ensure_prompt_coverage(
            uid_a,
            deps,
            enclave_sem=None,
            tail_limit=tail_limit,
        )
    )

    assert returned_watermark_seq == watermark_seq
    assert returned_max_seq == a_own_max_seq

    # The assembled tail for A is NON-EMPTY and contains exactly A's 10 real
    # recent messages -- under the OLD code, the false gap would have driven
    # a needless inline `compact()` collapsing the verbatim tail's role in
    # the prompt (or, in the general case, spinning through `max_retries`
    # no-op-fold attempts and raising ResponderError
    # ("prompt_coverage_incomplete") on a turn that never had a real gap).
    tail = [m for m in a_messages if m["seq"] > watermark_seq]
    assert len(tail) == tail_limit
    assert tail  # non-empty -- the exact D6-regression failure mode
    tail_ids = {m["id"] for m in tail}
    expected_ids = {m["id"] for m in a_messages if m["seq"] > watermark_seq}
    assert tail_ids == expected_ids

    # The post-assembly hard assertion agrees: no raise, no gap.
    asyncio.run(worker._assert_prompt_covers(uid_a, tail_limit))


# ---------------------------------------------------------------------------
# P0 #3 — compaction CAS-loss requeues, never permanently abandons a
# still-over-budget tail (Task 6).
# ---------------------------------------------------------------------------



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
