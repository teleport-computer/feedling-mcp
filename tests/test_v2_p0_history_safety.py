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
from model_api_runtime.v2 import compaction as v2_compaction
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
    # The compaction acceptance case in this legacy safety suite specifies the
    # provider-fold CAS/requeue path.
    monkeypatch.setattr(worker, "_PROFILE_COVERAGE_DETERMINISTIC", False)
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


def _seed_summary(uid: str, *, covers: int, messages: list[dict]) -> int:
    watermark_seq = messages[covers - 1]["seq"] if covers > 0 else 0
    watermark_ts = messages[covers - 1]["ts"] if covers > 0 else 0.0
    ok = jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={"plaintext": "- old"}, watermark_ts=watermark_ts,
        expected_version=0, watermark_seq=watermark_seq)
    assert ok, "seed CAS insert must land on a clean row"
    return watermark_seq


def _make_deps(messages: list[dict]):
    ordered = sorted(messages, key=lambda m: m["ts"])

    def _read_summary(uid):
        row = jobs_store.get_summary_row(uid)
        if row is None:
            return "", 0.0, 0
        env = row["summary_envelope"]
        if not env:
            return "", row["watermark_ts"], row["version"]
        return str(env.get("plaintext") or ""), row["watermark_ts"], row["version"]

    def _read_compaction_tail(uid, after_ts, limit):
        out = [m for m in ordered if m["ts"] > after_ts]
        return out[:limit] if limit > 0 else []

    def _read_tail(uid, after_ts, limit):
        out = [m for m in ordered if m["ts"] > after_ts]
        return out[-limit:] if limit > 0 else []

    def _write_summary(uid, summary, watermark_ts, expected_version, watermark_seq=None):
        return jobs_store.upsert_summary_row_cas(
            uid, summary_envelope={"plaintext": summary}, watermark_ts=watermark_ts,
            expected_version=expected_version, watermark_seq=watermark_seq)

    return _read_summary, _read_compaction_tail, _read_tail, _write_summary


def test_prompt_coverage_after_catchup_no_message_falls_in_gap(monkeypatch):
    """A gap: watermark far behind (covers only the first 5 of 80 messages)
    and a bounded tail window (tail_limit=10, so >tail_cap=75 messages sit
    after the watermark) — before catch-up, messages with seq strictly
    between the watermark and the tail's start would be SILENTLY DROPPED
    (not summarized, not in the bounded tail; this is the exact D6 hole).

    Strong property: after running `_ensure_prompt_coverage`, EVERY message
    with seq > (the now-advanced) watermark_seq is verified present, by id,
    in the freshly-read tail — a single set-membership check across all 80
    messages, not a spot check. To prove this isn't vacuous, the test first
    positively demonstrates the pre-catch-up gap exists (some messages are
    provably in neither the old covered range nor the old tail) — so the
    post-catch-up all-covered assertion is closing a hole shown to be real,
    not asserting a property that held trivially from the start.
    """
    uid = "u_p0hs_prompt_gap"
    conftest.seed_user(uid)
    _reset(uid)

    n, tail_limit = 80, 10
    messages = _seed_messages(uid, n)
    seeded_watermark_seq = _seed_summary(uid, covers=5, messages=messages)

    read_summary, read_compaction_tail, read_tail, write_summary = _make_deps(messages)

    # --- Demonstrate the gap is REAL before catch-up (non-vacuity check) ---
    max_seq = messages[-1]["seq"]
    old_tail = read_tail(uid, messages[4]["ts"], tail_limit)  # watermark_ts of the seeded row
    old_tail_ids = {m["id"] for m in old_tail}
    gap_messages = [
        m for m in messages
        if m["seq"] > seeded_watermark_seq and m["id"] not in old_tail_ids
    ]
    assert len(gap_messages) > 0, "test setup must produce a real pre-catch-up gap"
    assert asyncio.run(worker._prompt_coverage_gap(
        uid, watermark_seq=seeded_watermark_seq, tail_limit=tail_limit))

    # --- Fold via a fake LLM (append a marker per call, no real provider) ---
    compact_calls = []

    async def _fake_compact(
        *, provider_config, current_summary, old_messages, llm, usage_out=None,
        reject_out=None,
    ):
        compact_calls.append(list(old_messages))
        return (current_summary + "\n- folded").strip()

    monkeypatch.setattr(v2_compaction, "compact", _fake_compact)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_summary=read_summary,
        read_compaction_tail=read_compaction_tail,
        read_tail=read_tail,
        write_summary=write_summary,
    )

    watermark_seq, returned_max_seq = asyncio.run(worker._ensure_prompt_coverage(
        uid, deps, provider_config=_BYOK, enclave_sem=None, tail_limit=tail_limit))

    assert len(compact_calls) >= 1  # catch-up actually ran, not a no-op
    assert returned_max_seq == max_seq
    assert not asyncio.run(worker._prompt_coverage_gap(
        uid, watermark_seq=watermark_seq, tail_limit=tail_limit))

    # Independent re-derivation from the DB agrees (Task 9's watermark_seq
    # column, not merely the in-process return value).
    row = jobs_store.get_summary_row(uid)
    assert row["watermark_seq"] == watermark_seq
    assert row["watermark_seq"] > seeded_watermark_seq

    # THE strong property: EVERY one of the 80 messages ends up either
    # summarized (seq <= the now-advanced watermark_seq) or present, by id,
    # in the re-read tail — never neither (that "neither" state is exactly
    # the D6 silent-drop hole this task closes). Checked across the whole
    # set, not sampled. (In this scenario `_ensure_prompt_coverage` folds the
    # entire gap in one inline pass — see its docstring, "covering the ENTIRE
    # gap" — so every one of the `gap_messages` proven missing from the OLD
    # tail above lands on the "summarized" side; the membership check still
    # covers the "or in the tail" branch generally, since it is evaluated for
    # every message, not assumed.)
    new_tail = read_tail(uid, row["watermark_ts"], tail_limit)
    covered_ids = {m["id"] for m in new_tail}
    for m in messages:
        summarized = m["seq"] <= watermark_seq
        in_tail = m["id"] in covered_ids
        assert summarized or in_tail, (
            f"message seq={m['seq']} id={m['id']} fell in the gap: neither "
            f"summarized (seq<=watermark_seq={watermark_seq}) nor in the tail"
        )
    # The messages proven to be in the pre-catch-up gap are, in particular,
    # now on the "summarized" side (folded, not silently lost).
    for m in gap_messages:
        assert m["seq"] <= watermark_seq

    # And nothing with seq <= watermark_seq is left dangling outside the
    # summary either — the post-assembly hard assertion independently agrees.
    asyncio.run(worker._assert_prompt_covers(uid, tail_limit))


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

    def _boom_compact(*a, **k):
        raise AssertionError(
            "compaction.compact must NOT run on this false-gap scenario -- "
            "the old seq-arithmetic code would have run it on every "
            "multi-user turn")

    monkeypatch.setattr(v2_compaction, "compact", _boom_compact)

    # User-scoped reader (mirrors production `serve_worker._read_tail_window`,
    # which always reads from `core_store.get_store(user_id)` -- B's
    # interleaved rows are never visible to A's reader at all).
    a_ordered = sorted(a_messages, key=lambda m: m["ts"])

    def _read_tail_a(uid_, after_ts, limit):
        out = [m for m in a_ordered if m["ts"] > after_ts]
        return out[-limit:] if limit > 0 else []

    def _read_compaction_tail_a(uid_, after_ts, limit):
        out = [m for m in a_ordered if m["ts"] > after_ts]
        return out[:limit] if limit > 0 else []

    def _read_summary_a(uid_):
        row = jobs_store.get_summary_row(uid_)
        if row is None:
            return "", 0.0, 0
        env = row["summary_envelope"]
        if not env:
            return "", row["watermark_ts"], row["version"]
        return str(env.get("plaintext") or ""), row["watermark_ts"], row["version"]

    def _write_summary_a(uid_, summary, watermark_ts_, expected_version, watermark_seq=None):
        return jobs_store.upsert_summary_row_cas(
            uid_, summary_envelope={"plaintext": summary}, watermark_ts=watermark_ts_,
            expected_version=expected_version, watermark_seq=watermark_seq)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_summary=_read_summary_a,
        read_compaction_tail=_read_compaction_tail_a,
        read_tail=_read_tail_a,
        write_summary=_write_summary_a,
    )

    returned_watermark_seq, returned_max_seq = asyncio.run(worker._ensure_prompt_coverage(
        uid_a, deps, provider_config=_BYOK, enclave_sem=None, tail_limit=tail_limit))

    # No catch-up ran (the boom guard would have raised) and the watermark
    # is unchanged.
    assert returned_watermark_seq == watermark_seq
    assert returned_max_seq == a_own_max_seq

    # The assembled tail for A is NON-EMPTY and contains exactly A's 10 real
    # recent messages -- under the OLD code, the false gap would have driven
    # a needless inline `compact()` collapsing the verbatim tail's role in
    # the prompt (or, in the general case, spinning through `max_retries`
    # no-op-fold attempts and raising ResponderError
    # ("prompt_coverage_incomplete") on a turn that never had a real gap).
    tail = _read_tail_a(uid_a, watermark_ts, tail_limit)
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


def _make_tail_deps(n: int):
    messages = [{"id": f"m{i}", "ts": float(i), "content": f"turn {i}"} for i in range(1, n + 1)]

    def _read_tail(uid, after_ts, limit):
        out = [m for m in messages if m["ts"] > after_ts]
        return out[-limit:] if limit > 0 else []

    def _read_summary(uid):
        row = jobs_store.get_summary_row(uid)
        if row is None:
            return "", 0.0, 0
        env = row["summary_envelope"]
        if not env:
            return "", row["watermark_ts"], row["version"]
        return str(env.get("plaintext") or ""), row["watermark_ts"], row["version"]

    def _write_summary(uid, summary, watermark_ts, expected_version):
        return jobs_store.upsert_summary_row_cas(
            uid, summary_envelope={"plaintext": summary},
            watermark_ts=watermark_ts, expected_version=expected_version)

    return _read_tail, _read_summary, _write_summary


def test_compaction_cas_loss_requeues_never_permanently_abandons_tail(monkeypatch):
    """`worker._run_compaction` with `jobs_store.upsert_summary_row_cas`
    monkeypatched to unconditionally return False (every attempt loses the
    race) must still leave a fresh 'maintenance' job PENDING afterward — the
    over-budget tail is never silently abandoned, it is guaranteed another
    shot at coverage.

    Strong property: not just "a job got enqueued" (which could trivially be
    satisfied by enqueuing unconditionally on every failure, masking a bug
    where non-CAS failures also wrongly retry forever) — this test also
    drives the requeued job through a SECOND `_run_compaction` attempt (this
    time with a real, unpatched CAS) and asserts it actually SUCCEEDS and
    advances the watermark, proving the retry loop terminates in real
    coverage rather than spinning as an infinite-requeue mirage.
    """
    uid = "u_p0hs_cas_requeue"
    conftest.seed_user(uid)
    _reset(uid)

    read_tail, read_summary, write_summary = _make_tail_deps(worker._TAIL_KEEP + 5)

    async def _fake_llm(cfg, msgs, **kw):
        return {"reply": "- folded bullet"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)
    monkeypatch.setattr(jobs_store, "upsert_summary_row_cas", lambda *a, **k: False)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_tail=read_tail,
        read_summary=read_summary,
        write_summary=write_summary,
    )

    jobs_store.enqueue_job(uid, "maintenance")
    job1 = jobs_store.claim_next_job("w-p0-maint")
    assert job1 is not None and job1["lane"] == "maintenance"
    sem = asyncio.Semaphore(1)
    status = asyncio.run(worker._run_compaction(
        job1["id"], uid, deps, _BYOK, sem, claimed_by=job1["claimed_by"]))
    assert status == "failed"

    with db.get_pool().connection() as conn:
        pending = conn.execute(
            "SELECT id, status, reason FROM agent_jobs "
            "WHERE user_id=%s AND lane='maintenance' AND status='pending'",
            (uid,),
        ).fetchall()
    assert len(pending) == 1, f"expected exactly one fresh pending maintenance job, got {pending}"
    assert pending[0][2] == "cas_lost_retry"
    assert jobs_store.get_summary_row(uid) is None  # the lost CAS write never landed

    # Un-patch the CAS and drive the requeued job to a real completion — the
    # requeue must eventually terminate in actual coverage, not just exist.
    monkeypatch.undo()
    monkeypatch.setattr(worker, "_PROFILE_COVERAGE_DETERMINISTIC", False)
    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)
    job2 = jobs_store.claim_next_job("w-p0-maint-2")
    assert job2 is not None and job2["id"] == pending[0][0]
    status2 = asyncio.run(worker._run_compaction(
        job2["id"], uid, deps, _BYOK, sem, claimed_by=job2["claimed_by"]))
    assert status2 == "completed"
    summary_row = jobs_store.get_summary_row(uid)
    assert summary_row is not None and summary_row["version"] == 1


# ---------------------------------------------------------------------------
# P0 #4 — kill at a durable-effect boundary -> exactly one reply/effect.
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
