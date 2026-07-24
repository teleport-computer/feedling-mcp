"""D6 / Task 10 (Hosted Runtime V2 PR D): prompt invariant — every message with
``seq > watermark_seq`` must appear verbatim in the tail a turn hands the
model. Today's bug (before this task): ``serve_worker._read_tail`` only ever
returns the newest ``_TAIL_HARD_CAP`` messages after the summary watermark
(``result[-limit:]``) — if compaction falls more than ``tail_limit`` messages
behind, the messages strictly between the watermark and the tail's start seq
are SILENTLY DROPPED: not summarized, not in the tail. ``worker.
_ensure_prompt_coverage`` closes that hole with a SYNCHRONOUS catch-up
compaction run inline before the prompt is assembled; ``worker.
_assert_prompt_covers``/``_assert_prompt_covers_seq`` are the post-assembly
hard invariant check.

Style mirrors ``tests/test_v2_compaction_integration.py``: real
jobs_store-backed CAS (``jobs_store.get_summary_row``/
``upsert_summary_row_cas``) with a plaintext-shaped envelope (no live
enclave in this test process) + REAL ``chat_messages`` rows via
``core_store.get_store(uid).append_chat(..., strict=True)`` (needed here,
unlike that file, because the gap-detection math itself reads real seq via
``db.chat_max_seq``/``db.chat_seq_for_msg_id``, not just ts-windowed fakes).

IMPORTANT: ``chat_messages.seq`` is ``BIGINT GENERATED ALWAYS AS IDENTITY`` —
a single identity sequence shared by the WHOLE TABLE, not reset per user (see
migration 0001_baseline.py). A message's seq is therefore NOT its 1-based
index within one user's own history; every helper below looks the real seq
up via ``db.chat_seq_for_msg_id`` after inserting, never assumes seq==index+1.
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
from core import store as core_store
from model_api_runtime.v2 import compaction as v2_compaction
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="")


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


def _seed_messages(uid: str, n: int) -> list[dict]:
    """Write `n` REAL chat_messages rows (alternating user/assistant, plaintext
    stuffed straight into body_ct — no live enclave in this test process,
    mirrors test_v2_worker_tool_loop.py's `_patch_real_write`). Returns the
    list of `{"id","ts","role","content","seq"}` dicts in insertion order,
    each carrying its REAL (globally-identity-assigned, see module docstring)
    `seq` looked up via `db.chat_seq_for_msg_id` right after the insert."""
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


def _seed_summary(uid: str, *, covers: int, messages: list[dict], summary: str = "- old") -> int:
    """CAS-insert a v2_conversation_summary row (expected_version==0 -> first
    build) whose watermark covers the first `covers` messages (0 -> never
    compacted). watermark_seq/watermark_ts are the REAL values off
    `messages[covers - 1]` (see module docstring on why this can't be a bare
    index). Returns the seeded watermark_seq."""
    watermark_seq = messages[covers - 1]["seq"] if covers > 0 else 0
    watermark_ts = messages[covers - 1]["ts"] if covers > 0 else 0.0
    ok = jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={"plaintext": summary}, watermark_ts=watermark_ts,
        expected_version=0, watermark_seq=watermark_seq)
    assert ok, "seed CAS insert must land on a clean row"
    return watermark_seq


def _make_deps(messages: list[dict]):
    """read_summary/read_compaction_tail/read_tail/write_summary wired to the
    real jobs_store CAS (plaintext envelope), windowed by ts over the
    in-memory `messages` list — mirrors
    test_v2_compaction_integration.py's `_make_fake_conversation_deps`, plus
    passing `seq` through (so `_ensure_prompt_coverage`'s `old[-1].get("seq")`
    fast path is exercised directly instead of falling back to
    `db.chat_seq_for_msg_id`)."""
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


def test_ensure_prompt_coverage_runs_inline_catchup_and_closes_gap(monkeypatch):
    """Watermark far behind (covers only the first 5 of 25 messages) with a
    small `tail_limit` (10) -> a real gap: messages 6..14 are neither
    summarized nor inside the newest-10 tail window (16..25). Running
    `_ensure_prompt_coverage` must run compaction.compact INLINE
    (synchronously, awaited by this very call — not merely enqueue a
    background maintenance job) and advance watermark_seq far enough that no
    gap remains afterward."""
    uid = "u_prompt_inv_gap"
    conftest.seed_user(uid)
    _reset(uid)

    n, tail_limit = 25, 10
    messages = _seed_messages(uid, n)
    seeded_watermark_seq = _seed_summary(uid, covers=5, messages=messages)

    compact_calls = []

    async def _fake_compact(
        *, provider_config, current_summary, old_messages, llm, usage_out=None,
    ):
        compact_calls.append(list(old_messages))
        return (current_summary + "\n- folded").strip()

    monkeypatch.setattr(v2_compaction, "compact", _fake_compact)

    enqueue_calls = []
    orig_enqueue = jobs_store.enqueue_job
    monkeypatch.setattr(
        jobs_store, "enqueue_job",
        lambda *a, **k: enqueue_calls.append((a, k)) or orig_enqueue(*a, **k))

    read_summary, read_compaction_tail, read_tail, write_summary = _make_deps(messages)
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_summary=read_summary,
        read_compaction_tail=read_compaction_tail,
        read_tail=read_tail,
        write_summary=write_summary,
    )

    watermark_seq, max_seq = asyncio.run(worker._ensure_prompt_coverage(
        uid, deps, provider_config=_BYOK, enclave_sem=None, tail_limit=tail_limit))

    # Compaction ran INLINE during this very call — not merely enqueued.
    assert len(compact_calls) >= 1
    assert enqueue_calls == []  # no background job requested — the catch-up was synchronous

    assert max_seq == messages[-1]["seq"]
    assert not asyncio.run(worker._prompt_coverage_gap(
        uid, watermark_seq=watermark_seq, tail_limit=tail_limit))  # no gap left

    # Independent re-derivation from the DB agrees.
    row = jobs_store.get_summary_row(uid)
    assert row["watermark_seq"] == watermark_seq
    assert row["watermark_seq"] > seeded_watermark_seq  # actually advanced, not a no-op

    # The re-read tail (same reader, same tail_limit) now covers every
    # message with seq > watermark_seq: nothing in the gap is missing.
    new_tail = read_tail(uid, row["watermark_ts"], tail_limit)
    covered_ids = {m["id"] for m in new_tail}
    for m in messages:
        if m["seq"] > row["watermark_seq"]:
            assert m["id"] in covered_ids, f"message seq={m['seq']} missing from tail after catch-up"

    # Post-assembly hard assertion must now pass cleanly (no raise).
    asyncio.run(worker._assert_prompt_covers(uid, tail_limit))


def test_prompt_catchup_allows_many_bounded_batches_and_renews_lease(monkeypatch):
    """A healthy backlog can require far more than the no-progress retry budget.

    Every provider request must receive an oldest-first prefix bounded by both
    message count and rendered chars; successful watermark movement resets the
    retry counter, and the active job lease is renewed before the next batch.
    """
    uid = "u_prompt_inv_many_batches"
    conftest.seed_user(uid)
    _reset(uid)

    n, covers, tail_limit = 30, 2, 4
    messages = _seed_messages(uid, n)
    _seed_summary(uid, covers=covers, messages=messages)
    monkeypatch.setattr(worker, "_COMPACTION_BATCH", 3)
    monkeypatch.setattr(worker, "_COMPACTION_BATCH_CHARS", 34)

    compact_calls: list[list[dict]] = []

    async def _fake_compact(
        *, provider_config, current_summary, old_messages, llm, usage_out=None,
    ):
        compact_calls.append(list(old_messages))
        return (current_summary + f"\n- folded-{len(compact_calls)}").strip()

    monkeypatch.setattr(v2_compaction, "compact", _fake_compact)
    progress_events = []
    monkeypatch.setattr(worker, "_report_turn_progress", progress_events.append)

    lease_calls = []

    def _renew(job_id, claimed_by, *, ttl_sec):
        lease_calls.append((job_id, claimed_by, ttl_sec))
        return True

    monkeypatch.setattr(jobs_store, "renew_job_lease", _renew)

    read_summary, read_compaction_tail, read_tail, write_summary = _make_deps(messages)
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_summary=read_summary,
        read_compaction_tail=read_compaction_tail,
        read_tail=read_tail,
        write_summary=write_summary,
    )

    watermark_seq, max_seq = asyncio.run(worker._ensure_prompt_coverage(
        uid,
        deps,
        provider_config=_BYOK,
        enclave_sem=None,
        tail_limit=tail_limit,
        job_id="job-catchup",
        claimed_by="worker-catchup",
    ))

    assert len(compact_calls) > 3  # successful batches do not consume retry budget
    assert progress_events.count("prompt_catchup_batch_start") == len(compact_calls)
    assert progress_events.count("prompt_catchup_batch_complete") == len(compact_calls)
    assert progress_events.count("prompt_catchup_watermark_write") == len(compact_calls)
    assert len(lease_calls) == len(compact_calls)
    assert all(call[:2] == ("job-catchup", "worker-catchup") for call in lease_calls)
    assert all(len(batch) <= worker._COMPACTION_BATCH for batch in compact_calls)
    assert all(
        sum(worker._compaction_message_chars(message) for message in batch)
        <= worker._COMPACTION_BATCH_CHARS
        for batch in compact_calls
    )

    folded = [message for batch in compact_calls for message in batch]
    expected = messages[covers:n - tail_limit]
    assert [message["seq"] for message in folded] == [message["seq"] for message in expected]
    assert watermark_seq == expected[-1]["seq"]
    assert max_seq == messages[-1]["seq"]
    assert not asyncio.run(worker._prompt_coverage_gap(
        uid, watermark_seq=watermark_seq, tail_limit=tail_limit))


def test_ensure_prompt_coverage_no_gap_is_a_noop(monkeypatch):
    """Watermark already covers everything the tail window won't reach -> the
    fast path must return WITHOUT ever calling compaction.compact or writing
    a new summary version."""
    uid = "u_prompt_inv_nogap"
    conftest.seed_user(uid)
    _reset(uid)

    n, tail_limit = 12, 10
    messages = _seed_messages(uid, n)
    max_seq = messages[-1]["seq"]
    # Exact boundary: leave exactly `tail_limit` messages unsummarized (count
    # == tail_limit, not > tail_limit) — the watermark covers everything else.
    covers = n - tail_limit
    seeded_watermark_seq = _seed_summary(uid, covers=covers, messages=messages)
    assert seeded_watermark_seq == messages[covers - 1]["seq"]

    def _boom_compact(*a, **k):
        raise AssertionError("compaction.compact must not be called on the no-gap fast path")

    monkeypatch.setattr(v2_compaction, "compact", _boom_compact)

    read_summary, read_compaction_tail, read_tail, write_summary = _make_deps(messages)
    write_calls = []
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_summary=read_summary,
        read_compaction_tail=read_compaction_tail,
        read_tail=read_tail,
        write_summary=lambda *a, **k: write_calls.append((a, k)) or write_summary(*a, **k),
    )

    watermark_seq, ret_max_seq = asyncio.run(worker._ensure_prompt_coverage(
        uid, deps, provider_config=_BYOK, enclave_sem=None, tail_limit=tail_limit))

    assert ret_max_seq == max_seq
    assert watermark_seq == seeded_watermark_seq
    assert write_calls == []  # summary untouched — genuinely a no-op

    # Post-assembly hard assertion agrees: no raise.
    asyncio.run(worker._assert_prompt_covers(uid, tail_limit))


def test_prompt_coverage_hole_that_survives_the_retry_budget_raises(monkeypatch):
    """Construct a gap the bounded retry cannot close: compaction.compact
    always produces a genuine no-op fold (unchanged summary text), so
    write_summary's CAS never actually advances watermark_seq. After the
    bounded retries are exhausted, `_ensure_prompt_coverage` must raise
    ResponderError rather than silently proceeding with a coverage hole."""
    uid = "u_prompt_inv_stuck"
    conftest.seed_user(uid)
    _reset(uid)

    n, tail_limit = 25, 10
    messages = _seed_messages(uid, n)
    seeded_watermark_seq = _seed_summary(uid, covers=5, messages=messages, summary="- old")

    async def _noop_compact(
        *, provider_config, current_summary, old_messages, llm, usage_out=None,
    ):
        return current_summary  # unchanged -> watermark never advances

    monkeypatch.setattr(v2_compaction, "compact", _noop_compact)

    read_summary, read_compaction_tail, read_tail, write_summary = _make_deps(messages)
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_summary=read_summary,
        read_compaction_tail=read_compaction_tail,
        read_tail=read_tail,
        write_summary=write_summary,
    )

    with pytest.raises(worker.TurnError, match="prompt_coverage_incomplete"):
        asyncio.run(worker._ensure_prompt_coverage(
            uid, deps, provider_config=_BYOK, enclave_sem=None, tail_limit=tail_limit,
            max_retries=3))

    # The watermark genuinely never moved (a no-op fold correctly declines to
    # advance it — mirrors _run_compaction's own no-op handling) — a coverage
    # hole DOES remain, and the hard assertion must independently agree and
    # raise too, not silently pass.
    row = jobs_store.get_summary_row(uid)
    assert row["watermark_seq"] == seeded_watermark_seq
    with pytest.raises(worker.TurnError, match="prompt_coverage_incomplete"):
        asyncio.run(worker._assert_prompt_covers(uid, tail_limit))


def test_gap_from_count_pure_boundary_cases():
    """Pure/sync core of D6 gap detection (``_gap_from_count``): exercise the
    exact boundary — unsummarized count == tail_limit passes (no gap), one
    more raises the gap — with no DB I/O. This replaces the OLD seq-
    arithmetic boundary test (``_tail_start_seq``/``max_seq``-based), which
    is exactly the false-gap-producing math this task removes: a pure count
    comparison has no "seq span" concept left to get wrong across users."""
    tail_limit = 10

    assert not worker._gap_from_count(tail_limit, tail_limit)  # count == cap: no gap
    assert worker._gap_from_count(tail_limit + 1, tail_limit)  # one more: a real gap

    # No unsummarized messages at all -> never a gap, regardless of tail_limit.
    assert not worker._gap_from_count(0, tail_limit)


def test_assert_prompt_covers_seq_db_boundary(monkeypatch):
    """DB-backed counterpart of the pure boundary test above, exercised
    through the actual async ``_assert_prompt_covers_seq`` (now necessarily
    I/O-bound — see ``_gap_from_count``'s docstring for why per-user gap
    detection can no longer be pure seq arithmetic): a watermark leaving
    exactly ``tail_limit`` unsummarized messages must NOT raise; a watermark
    one message further back (``tail_limit + 1`` unsummarized) MUST raise."""
    uid = "u_prompt_inv_boundary"
    conftest.seed_user(uid)
    _reset(uid)

    n, tail_limit = 15, 10
    messages = _seed_messages(uid, n)

    # Exact boundary: count == tail_limit -> no raise.
    watermark_at_cap = messages[n - tail_limit - 1]["seq"]
    asyncio.run(worker._assert_prompt_covers_seq(
        uid, watermark_seq=watermark_at_cap, tail_limit=tail_limit))

    # One message further back: count == tail_limit + 1 -> raises.
    watermark_over_cap = messages[n - tail_limit - 2]["seq"]
    with pytest.raises(worker.TurnError, match="prompt_coverage_incomplete"):
        asyncio.run(worker._assert_prompt_covers_seq(
            uid, watermark_seq=watermark_over_cap, tail_limit=tail_limit))

    # No messages past the watermark at all -> never a gap.
    asyncio.run(worker._assert_prompt_covers_seq(
        uid, watermark_seq=messages[-1]["seq"], tail_limit=tail_limit))


def test_prompt_coverage_gap_false_positive_under_multiuser_interleaving(monkeypatch):
    """THE regression test for the critical D6 bug: ``chat_messages.seq`` is a
    TABLE-WIDE identity counter shared by every user (migration
    0001_baseline.py), so in production other users' inserts interleave with
    this user's. Seed user A's messages INTERLEAVED with user B's — A ends up
    with exactly ``tail_limit`` (10) unsummarized messages (no real gap), but
    those 10 messages are spread across a GLOBAL seq span far larger than 10
    (B's interleaved inserts sit in between), because 6 of B's messages are
    inserted after each of A's last 10.

    Under the OLD seq-arithmetic code (``watermark_seq < max_seq - tail_limit
    + 1 - 1``, using the GLOBAL ``max_seq``), this span alone would trip a
    FALSE gap: ``max_seq`` reflects B's latest insert, not A's, so
    ``max_seq - watermark_seq`` vastly overstates how many of A's OWN
    messages are actually unsummarized. That false gap would run a needless
    synchronous catch-up compaction (asserted here NOT to happen), and
    ``_read_tail_window``'s ``candidates[-limit:]`` slice (already correctly
    scoped to A's own rows in production, unlike the flawed gap check) would
    still hand back a NON-EMPTY tail of A's 10 recent messages either way —
    the real symptom in production is the WASTEFUL/lossy inline compaction
    happening at all, which this test's ``compact`` boom-guard catches.

    Under the FIXED count-based code, no gap is detected (A's own count ==
    tail_limit, not more), so ``_ensure_prompt_coverage`` returns via the
    fast no-op path and the assembled tail is A's 10 real messages,
    non-empty."""
    uid_a = "u_prompt_inv_interleave_a"
    uid_b = "u_prompt_inv_interleave_b"
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

    # Phase 1: 5 "old" A messages (to be covered by the watermark), each
    # trailed by 2 B messages -- interleaving starts immediately, not just
    # after the watermark, so the watermark's own seq is already non-trivial
    # relative to A's message index.
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

    # Phase 2: exactly `tail_limit` (10) more A messages, each trailed by 6 B
    # messages -- inflates the GLOBAL seq span between the watermark and
    # max_seq to 10 * 7 = 70, far past tail_limit=10, while A's OWN
    # unsummarized count stays exactly 10.
    for i in range(5, 5 + tail_limit):
        a_messages.append(_seed_one(uid_a, i, "user" if i % 2 == 0 else "openclaw"))
        for j in range(6):
            _seed_one(uid_b, 1000 + 6 * i + j, "user" if j % 2 == 0 else "openclaw")

    # `db.chat_max_seq` IS user-scoped (filters WHERE user_id=%s) -- this is
    # A's own most recent row's seq, exactly what the OLD buggy
    # `_ensure_prompt_coverage` fed into `_prompt_coverage_gap` as `max_seq`.
    # Because `seq` is a TABLE-WIDE identity counter, A's own last row's
    # NUMERIC seq value is still inflated by every one of B's interleaved
    # inserts that landed before it -- that's the whole bug: a per-user MAX
    # seq is not evidence of a per-user CONTIGUOUS seq range.
    a_own_max_seq = db.chat_max_seq(uid_a)
    assert a_own_max_seq == a_messages[-1]["seq"]

    # --- Non-vacuity: the OLD seq-arithmetic condition WOULD have found a
    # false gap here (span from watermark to A's own max seq exceeds
    # tail_limit by a wide margin, purely from B's interleaved inserts sitting
    # in between), even though A's real unsummarized count is exactly at the
    # (non-gap) boundary. ---
    assert a_own_max_seq - watermark_seq > 60
    real_unsummarized_count = db.count_messages_after_seq(uid_a, watermark_seq)
    assert real_unsummarized_count == tail_limit  # exactly at the cap -> NOT a gap

    # --- The FIXED count-based check must NOT see a gap. ---
    assert not asyncio.run(worker._prompt_coverage_gap(
        uid_a, watermark_seq=watermark_seq, tail_limit=tail_limit))

    def _boom_compact(*a, **k):
        raise AssertionError(
            "compaction.compact must NOT run: this is a false-gap scenario, "
            "not a real one -- the old seq-arithmetic code would have called "
            "this needlessly on every multi-user turn")

    monkeypatch.setattr(v2_compaction, "compact", _boom_compact)

    # User-scoped reader (mirrors production `_read_tail_window`, which reads
    # from `core_store.get_store(user_id)` -- ALWAYS scoped to one user, B's
    # interleaved rows are never visible to A's reader in the first place).
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

    def _write_summary_a(uid_, summary, watermark_ts, expected_version, watermark_seq=None):
        return jobs_store.upsert_summary_row_cas(
            uid_, summary_envelope={"plaintext": summary}, watermark_ts=watermark_ts,
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

    # No catch-up ran (the `_boom_compact` guard would have raised
    # AssertionError if it had) and the watermark is unchanged.
    assert returned_watermark_seq == watermark_seq
    assert returned_max_seq == a_own_max_seq

    # The assembled tail for A is NON-EMPTY and contains exactly A's 10
    # recent unsummarized messages -- the real, correct outcome. Under the
    # OLD code, the false gap would have driven a needless inline
    # `compact()` call collapsing this tail's role in the prompt (or worse,
    # spun through `max_retries` no-op-fold attempts and raised
    # ResponderError("prompt_coverage_incomplete") on a turn that had no
    # real gap at all).
    tail = _read_tail_a(uid_a, watermark_ts, tail_limit)
    assert len(tail) == tail_limit
    tail_ids = {m["id"] for m in tail}
    expected_ids = {m["id"] for m in a_messages if m["seq"] > watermark_seq}
    assert tail_ids == expected_ids

    # The post-assembly hard assertion agrees: no raise, no gap.
    asyncio.run(worker._assert_prompt_covers(uid_a, tail_limit))


def test_process_job_chat_turn_runs_inline_catchup_before_replying(monkeypatch):
    """End-to-end through `process_job` (not calling `_ensure_prompt_coverage`
    directly): a chat turn whose compaction backlog has a real gap must run
    the catch-up compaction INLINE before the provider call, and the reply
    must go out normally afterward — the turn is not failed by having a gap,
    only by failing to CLOSE it."""
    uid = "u_prompt_inv_chat_e2e"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))

    n, tail_limit = 25, 10
    messages = _seed_messages(uid, n)
    seeded_watermark_seq = _seed_summary(uid, covers=5, messages=messages)
    monkeypatch.setattr(worker, "_TAIL_HARD_CAP", tail_limit)

    async def _fake_compact(
        *, provider_config, current_summary, old_messages, llm, usage_out=None,
    ):
        return (current_summary + "\n- folded").strip()

    monkeypatch.setattr(v2_compaction, "compact", _fake_compact)

    from capabilities import registry as cap_registry
    from model_api_runtime.v2 import effect_outbox as v2_effect_outbox

    class _FakeCapResult:
        def to_dict(self):
            return {"ok": True, "data": {}, "error": None, "trace": {}, "warnings": []}

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult())
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    async def _fake_chat_completion(config, msgs, *, tools=None):
        return {"reply": "model reply", "tool_calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake_chat_completion)

    def _reply_effect_dispatch(user_id):
        def dispatch(effect_type, payload):
            if effect_type == "reply":
                worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
        return dispatch

    def _apply_effects(user_id):
        return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_reply_effect_dispatch(user_id))

    read_summary, read_compaction_tail, read_tail, write_summary = _make_deps(messages)
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "new1", "ts": messages[-1]["ts"] + 1.0,
                                      "role": "user", "content": "final unanswered"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_summary=read_summary,
        read_compaction_tail=read_compaction_tail,
        read_tail=read_tail,
        write_summary=write_summary,
        apply_pending_effects=_apply_effects,
    )

    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-e2e")
    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    row = jobs_store.get_summary_row(uid)
    # The gap was closed INLINE this turn (before the provider call ever
    # ran), not just left for the best-effort background enqueue.
    assert row is not None and row["watermark_seq"] > seeded_watermark_seq
