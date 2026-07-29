from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import provider_client
from core import store as core_store
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker


_BYOK = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="sk-test",
    base_url="",
)


def _reset(uid: str) -> None:
    conftest.seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM v2_conversation_summary_segments WHERE user_id=%s", (uid,)
        )
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))


def _messages(uid: str, count: int) -> list[dict]:
    out = []
    for index in range(count):
        message_id = f"{uid}-m{index}"
        db.chat_append_strict(
            uid,
            message_id,
            float(index + 1),
            {"id": message_id, "role": "user", "body_ct": f"ct-{index}"},
            core_store.MAX_CHAT_MESSAGES,
        )
        out.append(
            {
                "id": message_id,
                "seq": int(db.chat_seq_for_msg_id(uid, message_id)),
                "ts": float(index + 1),
                "role": "user",
                "content": f"message {index}",
            }
        )
    return out


def test_leaf_checkpoint_and_materialized_head_are_one_versioned_cas():
    uid = "u_summary_segment_cas"
    _reset(uid)
    messages = _messages(uid, 5)

    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- leaf one"},
        head_summary_envelope={"plaintext": "- leaf one"},
        start_seq=messages[0]["seq"],
        end_seq=messages[1]["seq"],
        source_message_count=2,
        watermark_ts=messages[1]["ts"],
        expected_version=0,
        previous_watermark_seq=0,
    )
    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- leaf two"},
        head_summary_envelope={"plaintext": "- leaf one\n- leaf two"},
        start_seq=messages[2]["seq"],
        end_seq=messages[3]["seq"],
        source_message_count=2,
        watermark_ts=messages[3]["ts"],
        expected_version=1,
        previous_watermark_seq=messages[1]["seq"],
    )
    state = jobs_store.get_summary_frontier_state(uid)
    child_ids = tuple(row["segment_id"] for row in state["segments"])
    assert tuple(state["materialized_segment_ids"]) == child_ids
    assert state["version"] == 2

    assert jobs_store.insert_summary_checkpoint(
        uid,
        summary_envelope={"plaintext": "- parent"},
        head_summary_envelope={"plaintext": "- parent"},
        level=1,
        start_seq=messages[0]["seq"],
        end_seq=messages[3]["seq"],
        source_message_count=4,
        child_segment_ids=child_ids,
        expected_version=2,
        expected_watermark_seq=messages[3]["seq"],
    )
    after = jobs_store.get_summary_frontier_state(uid)
    assert after["version"] == 3
    assert len(after["segments"]) == 1
    assert tuple(after["materialized_segment_ids"]) == (
        after["segments"][0]["segment_id"],
    )
    assert after["summary_envelope"] == {"plaintext": "- parent"}
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v2_conversation_summary_segments WHERE user_id=%s",
            (uid,),
        ).fetchone()[0] == 3  # children retained; no automatic GC

    # A leaf that read v2 before the checkpoint cannot overwrite its bounded
    # materialized head after waiting on the row lock. It loses before insert.
    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- stale leaf"},
        head_summary_envelope={"plaintext": "- stale full head"},
        start_seq=messages[4]["seq"],
        end_seq=messages[4]["seq"],
        source_message_count=1,
        watermark_ts=messages[4]["ts"],
        expected_version=2,
        previous_watermark_seq=messages[3]["seq"],
    ) is False
    assert jobs_store.get_summary_row(uid)["summary_envelope"] == {
        "plaintext": "- parent"
    }


@pytest.mark.parametrize("translated_watermark", [0, 900])
def test_legacy_blob_with_no_retained_rows_seeds_and_unary_checkpoints(
    translated_watermark: int,
):
    uid = f"u_summary_legacy_{translated_watermark}"
    _reset(uid)
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "- " + "legacy " * 10_000},
        watermark_ts=1.0,
        expected_version=0,
        watermark_seq=translated_watermark,
    )
    assert jobs_store.seed_legacy_summary_segment(
        uid,
        expected_version=1,
        translated_watermark_seq=translated_watermark,
    )
    seeded = jobs_store.get_summary_frontier_state(uid)
    assert seeded["version"] == 2
    assert len(seeded["segments"]) == 1
    legacy = seeded["segments"][0]
    assert legacy["coverage_kind"] == "legacy_opaque"
    assert legacy["source_message_count"] == 0
    assert legacy["legacy_opaque_through_seq"] == translated_watermark
    assert tuple(seeded["materialized_segment_ids"]) == (legacy["segment_id"],)
    if translated_watermark == 0:
        # Once the zero boundary is bound to an opaque segment it is exact.
        # A later backdated row must not reactivate legacy ts->seq translation.
        db.chat_append_strict(
            uid,
            f"{uid}-late",
            0.5,
            {"id": f"{uid}-late", "role": "user", "body_ct": "late"},
            core_store.MAX_CHAT_MESSAGES,
        )
        assert jobs_store.get_summary_row(uid)["watermark_seq"] == 0
        assert jobs_store.get_summary_frontier_state(uid)["watermark_seq"] == 0

    assert jobs_store.insert_summary_checkpoint(
        uid,
        summary_envelope={"plaintext": "- bounded legacy checkpoint"},
        head_summary_envelope={"plaintext": "- bounded legacy checkpoint"},
        level=1,
        start_seq=0,
        end_seq=translated_watermark,
        source_message_count=0,
        child_segment_ids=(legacy["segment_id"],),
        expected_version=2,
        expected_watermark_seq=translated_watermark,
        coverage_kind="legacy_opaque",
        legacy_opaque_through_seq=translated_watermark,
    )
    bounded = jobs_store.get_summary_frontier_state(uid)
    assert bounded["version"] == 3
    assert bounded["summary_envelope"] == {
        "plaintext": "- bounded legacy checkpoint"
    }


def test_inline_prompt_catchup_appends_immutable_leaf_not_legacy_blob(monkeypatch):
    uid = "u_summary_inline_segment"
    _reset(uid)
    messages = _messages(uid, 12)
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "- legacy first row"},
        watermark_ts=messages[0]["ts"],
        expected_version=0,
        watermark_seq=messages[0]["seq"],
    )

    def _read_summary(user_id):
        row = jobs_store.get_summary_row(user_id)
        return (
            str((row["summary_envelope"] or {}).get("plaintext") or ""),
            row["watermark_ts"],
            row["version"],
            row["watermark_seq"],
        )

    def _read_oldest(user_id, after_seq, limit):
        return [row for row in messages if row["seq"] > after_seq][:limit]

    def _append(user_id, segment, **kwargs):
        current = kwargs.pop("current_summary")
        return jobs_store.append_summary_leaf_cas(
            user_id,
            summary_envelope={"plaintext": segment},
            head_summary_envelope={
                "plaintext": current.rstrip() + "\n" + segment.strip()
            },
            **kwargs,
        )

    legacy_write_calls = []
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        read_summary_with_seq=_read_summary,
        read_compaction_tail_after_seq=_read_oldest,
        read_tail_after_seq=_read_oldest,
        write_summary=lambda *args, **kwargs: legacy_write_calls.append((args, kwargs)),
        append_summary_segment=_append,
    )

    async def _fake_llm(_config, _messages, **_kwargs):
        return {"reply": "- immutable catchup leaf"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)
    asyncio.run(
        worker._ensure_prompt_coverage(
            uid,
            deps,
            provider_config=_BYOK,
            enclave_sem=asyncio.Semaphore(1),
            tail_limit=2,
            catchup_deadline_sec=5,
        )
    )
    assert legacy_write_calls == []
    state = jobs_store.get_summary_frontier_state(uid)
    assert state["watermark_seq"] == messages[-3]["seq"]
    assert [row["coverage_kind"] for row in state["segments"]] == [
        "legacy_opaque",
        "exact",
    ]
    assert len(state["materialized_segment_ids"]) == 2


# --- quarantine ✕ canonical frontier ----------------------------------------
# `validate_canonical_frontier` is the real gate every later turn passes
# through (`serve_worker._summary_metadata_frontier`), and it is strictly
# stronger than "the watermark moved": it re-proves the first source seq, the
# strict non-overlap of every segment, and the sum of exact source counts.
# The quarantine path WRITES a segment, so it can violate those witnesses in
# ways a watermark assertion cannot see — the first attempt at it did exactly
# that, stamping a segment with a seq belonging to no row of this user and
# breaking every subsequent turn with `v2_summary_frontier_integrity_error`
# (strictly worse than the stall it was fixing). These tests run the real
# quarantine and then hand the resulting rows to the real validator.


def _messages_with_seq_gaps(uid: str, count: int) -> list[dict]:
    """`_messages`, but with another user's rows interleaved.

    `chat_messages.seq` is a table-wide IDENTITY shared by every user, so in
    production one user's consecutive rows are almost never consecutive seqs.
    A test that seeds one user in isolation gets seq == index + 1 by accident,
    which silently makes any seq DERIVED from the watermark (`watermark + 1`,
    `index`-based arithmetic) look correct. Interleaving a second writer
    reproduces the real shape and makes such derivations fail loudly.
    """
    other = f"{uid}_noise"
    conftest.seed_user(other)
    out = []
    for index in range(count):
        noise_id = f"{other}-n{index}"
        db.chat_append_strict(
            other,
            noise_id,
            float(index + 1),
            {"id": noise_id, "role": "user", "body_ct": "noise"},
            core_store.MAX_CHAT_MESSAGES,
        )
        message_id = f"{uid}-m{index}"
        db.chat_append_strict(
            uid,
            message_id,
            float(index + 1),
            {"id": message_id, "role": "user", "body_ct": f"ct-{index}"},
            core_store.MAX_CHAT_MESSAGES,
        )
        out.append(
            {
                "id": message_id,
                "seq": int(db.chat_seq_for_msg_id(uid, message_id)),
                "ts": float(index + 1),
                "role": "user",
                "content": f"message {index}",
            }
        )
    # Guard the guard: if these ever came out consecutive the interleaving
    # stopped working and these tests silently lost their teeth.
    seqs = [row["seq"] for row in out]
    assert all(b - a > 1 for a, b in zip(seqs, seqs[1:])), seqs
    return out


def _seq_aware_deps(uid: str, messages: list[dict], *, tail_limit: int = 2):
    """seq-callback deps writing REAL immutable segments (mirrors
    `test_inline_prompt_catchup_appends_immutable_leaf_not_legacy_blob`)."""

    def _read_summary(user_id):
        row = jobs_store.get_summary_row(user_id)
        return (
            str((row["summary_envelope"] or {}).get("plaintext") or ""),
            row["watermark_ts"],
            row["version"],
            row["watermark_seq"],
        )

    def _read_oldest(user_id, after_seq, limit):
        return [row for row in messages if row["seq"] > after_seq][:limit]

    def _append(user_id, segment, **kwargs):
        current = kwargs.pop("current_summary")
        return jobs_store.append_summary_leaf_cas(
            user_id,
            summary_envelope={"plaintext": segment},
            head_summary_envelope={
                "plaintext": current.rstrip() + "\n" + segment.strip()
            },
            **kwargs,
        )

    return worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        read_summary_with_seq=_read_summary,
        read_compaction_tail_after_seq=_read_oldest,
        read_tail_after_seq=_read_oldest,
        append_summary_segment=_append,
    )


def _assert_frontier_passes_production_validator(uid: str):
    """Run the exact validation every later turn runs, and return the cover."""
    from model_api_runtime.v2 import serve_worker as v2_serve_worker

    state = jobs_store.get_summary_frontier_state(uid)
    assert state is not None, "quarantine must leave a readable frontier"
    return v2_serve_worker._summary_metadata_frontier(state)


def test_quarantined_segment_still_passes_the_canonical_frontier_validator(
    monkeypatch
):
    """The invariant the seq bug broke, checked by the real validator.

    Asserting on `watermark_seq` alone would have passed even while the
    frontier was corrupt: the watermark did advance, it just advanced to a seq
    no row of this user owned, so `frontier_start_mismatch` fired on the next
    turn instead.
    """
    uid = "u_summary_quarantine_frontier"
    _reset(uid)
    messages = _messages_with_seq_gaps(uid, 12)
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "- legacy first row"},
        watermark_ts=messages[0]["ts"],
        expected_version=0,
        watermark_seq=messages[0]["seq"],
    )
    blocking_seq = messages[1]["seq"]

    # The oldest unsummarized row alone blows the whole batch's char budget —
    # the usr_7f30… shape, the one path that reaches quarantine directly.
    monkeypatch.setattr(worker, "_COMPACTION_BATCH_CHARS", 50)
    monkeypatch.setattr(
        worker,
        "_compaction_message_chars",
        lambda m: 5_000 if m["seq"] == blocking_seq else 10,
    )

    async def _fake_llm(_config, _messages, **_kwargs):
        return {"reply": "- folded leaf"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)

    watermark_seq, _max_seq = asyncio.run(
        worker._ensure_prompt_coverage(
            uid,
            _seq_aware_deps(uid, messages),
            provider_config=_BYOK,
            enclave_sem=asyncio.Semaphore(1),
            tail_limit=2,
            catchup_deadline_sec=10,
        )
    )

    assert watermark_seq > blocking_seq, "must have moved past the blocking row"
    validated = _assert_frontier_passes_production_validator(uid)

    # The quarantined row is covered by exactly one 1-row `exact` segment
    # carrying its OWN seq — not a seq invented from the reader's row.
    quarantine_segments = [
        seg for seg in validated
        if seg.coverage_kind == "exact"
        and seg.start_seq == seg.end_seq == blocking_seq
    ]
    assert len(quarantine_segments) == 1, [
        (s.segment_id, s.start_seq, s.end_seq, s.source_message_count)
        for s in validated
    ]
    assert quarantine_segments[0].source_message_count == 1


def test_frontier_stays_valid_across_several_quarantines(monkeypatch):
    """Consecutive unfoldable rows must each get their own honest segment.

    One quarantine landing correctly does not prove the next one does: the
    witnesses are cumulative (`covered_source_count` sums across every
    segment, and each segment must start strictly after the previous one
    ends), so an off-by-one that a single quarantine hides shows up once they
    chain.
    """
    uid = "u_summary_quarantine_chain"
    _reset(uid)
    messages = _messages_with_seq_gaps(uid, 12)
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "- legacy first row"},
        watermark_ts=messages[0]["ts"],
        expected_version=0,
        watermark_seq=messages[0]["seq"],
    )
    # Three consecutive oversized rows right behind the watermark.
    blocking = {m["seq"] for m in messages[1:4]}

    monkeypatch.setattr(worker, "_COMPACTION_BATCH_CHARS", 50)
    monkeypatch.setattr(
        worker,
        "_compaction_message_chars",
        lambda m: 5_000 if m["seq"] in blocking else 10,
    )

    async def _fake_llm(_config, _messages, **_kwargs):
        return {"reply": "- folded leaf"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)

    watermark_seq, _max_seq = asyncio.run(
        worker._ensure_prompt_coverage(
            uid,
            _seq_aware_deps(uid, messages),
            provider_config=_BYOK,
            enclave_sem=asyncio.Semaphore(1),
            tail_limit=2,
            catchup_deadline_sec=10,
        )
    )

    assert watermark_seq > max(blocking)
    validated = _assert_frontier_passes_production_validator(uid)

    # Each blocked row got its own 1-row segment; none was skipped silently.
    quarantined_seqs = {
        seg.start_seq for seg in validated
        if seg.start_seq == seg.end_seq and seg.source_message_count == 1
        and seg.coverage_kind == "exact"
    }
    assert blocking <= quarantined_seqs, (blocking, quarantined_seqs)


# --- chaos: the frontier must survive ANY interleaving of failures ----------
# Each failure mode has its own escalation (refusal → shrink, oversized row →
# quarantine, provider timeout → shrink-then-fail), and each was verified in
# isolation. The dangerous case is the COMBINATION: shrink, then hit an
# oversized row, then time out at the smaller cap, then recover. There is one
# property that must hold across every such path, including the ones that end
# in an exception: whatever is on disk when catch-up stops is a valid
# canonical frontier. A stalled catch-up is recoverable; a corrupt frontier
# fails every later turn forever (see the seq bug above).


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 7, 8])
def test_frontier_survives_any_mix_of_fold_failures(monkeypatch, seed):
    import random

    uid = f"u_summary_chaos_{seed}"
    _reset(uid)
    messages = _messages_with_seq_gaps(uid, 16)
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "- legacy first row"},
        watermark_ts=messages[0]["ts"],
        expected_version=0,
        watermark_seq=messages[0]["seq"],
    )

    rng = random.Random(seed)
    # Some rows are individually unfoldable (R2-offloaded giants); the rest
    # fold or fail per the script below.
    oversized = {
        row["seq"] for row in messages[1:] if rng.random() < 0.2
    }
    monkeypatch.setattr(worker, "_COMPACTION_BATCH_CHARS", 500)
    monkeypatch.setattr(
        worker,
        "_compaction_message_chars",
        lambda m: 5_000 if m["seq"] in oversized else 10,
    )

    calls = {"n": 0}

    async def _chaotic_fold(
        *, provider_config, old_messages, llm, usage_out=None, reject_out=None,
        current_summary=None,
    ):
        calls["n"] += 1
        roll = rng.random()
        if roll < 0.25:
            # Provider never answered (compaction's own 60s timeout).
            raise provider_client.ProviderError("read timeout", status_code=None)
        if roll < 0.45:
            # Answered, but the reply failed bullet validation.
            if reject_out is not None:
                reject_out("line_not_bullet:0")
            return "" if current_summary is None else current_summary
        return "- folded leaf"

    monkeypatch.setattr(
        worker.v2_compaction, "compact_segment",
        lambda **kw: _chaotic_fold(current_summary=None, **kw),
    )

    before = jobs_store.get_summary_frontier_state(uid)["watermark_seq"]
    try:
        asyncio.run(
            worker._ensure_prompt_coverage(
                uid,
                _seq_aware_deps(uid, messages),
                provider_config=_BYOK,
                enclave_sem=asyncio.Semaphore(1),
                tail_limit=2,
                catchup_deadline_sec=10,
            )
        )
    except (worker.TurnError, provider_client.ProviderError):
        # A catch-up that gives up is allowed; a corrupt frontier is not.
        pass

    # Coverage guard: an oversized head row is quarantined BEFORE any provider
    # call, so a future change that made every row look oversized would leave
    # this test silently exercising only the quarantine path while still
    # passing. The provider must actually have been reached.
    assert calls["n"] > 0, "chaos never reached the provider — test lost its teeth"

    # THE invariant, on whatever state we ended up in.
    from model_api_runtime.v2 import serve_worker as v2_serve_worker

    state = jobs_store.get_summary_frontier_state(uid)
    validated = v2_serve_worker._summary_metadata_frontier(state)

    # The watermark never moves backwards, and never past the newest row.
    assert state["watermark_seq"] >= before
    assert state["watermark_seq"] <= messages[-1]["seq"]
    # Every claimed seq belongs to a real row of THIS user.
    owned = {row["seq"] for row in messages}
    for seg in validated:
        if seg.coverage_kind == "legacy_opaque":
            continue
        assert seg.start_seq in owned, (seg.segment_id, seg.start_seq, sorted(owned))
        assert seg.end_seq in owned, (seg.segment_id, seg.end_seq, sorted(owned))
    # Anything written off was written off ONE row at a time.
    for seg in validated:
        if seg.coverage_kind == "exact" and seg.source_message_count == 1:
            assert seg.start_seq == seg.end_seq


# --- verify_ping / resident_maintenance synthetic rows must never enter the
# --- immutable summary coverage (found on test 2026-07-28) -----------------
# A `verify_ping`/`resident_maintenance` row IS stored in `chat_messages`
# (see core/store.py), but a verify_ping is DELETED once /v1/chat/verify_loop
# completes. If the compaction fold folds one into an immutable level-0 leaf,
# its seq becomes a permanent coverage claim; the moment the row is GC'd the
# canonical witness (a LIVE `COUNT`/`MIN` over `chat_messages`) stops matching
# the leaf's frozen `source_message_count`/`start_seq`, so
# `validate_canonical_frontier` raises `v2_summary_frontier_integrity_error` on
# EVERY later turn — the permanent multi-turn brick reproduced end to end. These
# rows must be invisible to the whole coverage machinery consistently: the fold
# reader, the gap counter, and BOTH the write-time and read-time witnesses.


def _append_row(uid: str, msg_id: str, ts: float, *, source: str | None = None) -> int:
    doc = {"id": msg_id, "role": "user", "body_ct": f"ct-{msg_id}"}
    if source is not None:
        doc["source"] = source
    db.chat_append_strict(uid, msg_id, ts, doc, core_store.MAX_CHAT_MESSAGES)
    seq = db.chat_seq_for_msg_id(uid, msg_id)
    assert seq is not None
    return int(seq)


def test_coverage_readers_exclude_synthetic_sources():
    """`chat_messages_after_seq`/`count_messages_after_seq` opt out of the two
    GC-able synthetic sources, so the fold never even sees a row that a later
    verify_loop will delete out from under an immutable leaf. The default stays
    all-inclusive for every non-coverage caller (reply coalescing, etc.)."""
    uid = "u_coverage_excl_synthetic_readers"
    _reset(uid)
    _append_row(uid, f"{uid}-r0", 1.0)
    _append_row(uid, f"{uid}-r1", 2.0)
    _append_row(uid, f"{uid}-vp", 3.0, source="verify_ping")
    _append_row(uid, f"{uid}-rm", 4.0, source="resident_maintenance")

    # Default behaviour is unchanged: callers that must see every row still do.
    assert len(db.chat_messages_after_seq(uid, 0)) == 4
    assert db.count_messages_after_seq(uid, 0) == 4

    # Coverage callers opt out of the synthetic rows.
    real = db.chat_messages_after_seq(uid, 0, exclude_synthetic_sources=True)
    assert [row["id"] for row in real] == [f"{uid}-r0", f"{uid}-r1"]
    assert db.count_messages_after_seq(uid, 0, exclude_synthetic_sources=True) == 2


def test_summary_leaf_survives_gc_of_a_folded_synthetic_row():
    """The permanent-brick repro, at the store layer.

    A leaf covering three REAL rows, with a `verify_ping` row interleaved in
    its seq range, must (a) be writable — the write-time witness counts only
    real rows, so `source_message_count=3` is accepted — and (b) keep passing
    the production validator BOTH while the synthetic row is present AND after
    verify_loop deletes it. Before the fix the write-time witness counted the
    synthetic row (n=4 != 3, so the leaf was refused); and had it been written
    the old way (folding the synthetic row in, count=4), the read witness would
    mismatch the instant the row was GC'd — the brick this test pins down."""
    uid = "u_summary_synthetic_row_gc"
    _reset(uid)
    r0 = _append_row(uid, f"{uid}-r0", 1.0)
    r1 = _append_row(uid, f"{uid}-r1", 2.0)
    _append_row(uid, f"{uid}-vp", 3.0, source="verify_ping")  # interleaved: r1 < vp < r2
    r2 = _append_row(uid, f"{uid}-r2", 4.0)
    assert r0 < r1 < r2

    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- folded three real rows"},
        start_seq=r0,
        end_seq=r2,
        source_message_count=3,
        watermark_ts=4.0,
        expected_version=0,
        previous_watermark_seq=0,
    ), "a leaf covering only the real rows must be writable despite an interleaved synthetic row"

    # Valid with the synthetic row still present (read witness excludes it)...
    _assert_frontier_passes_production_validator(uid)

    # ...verify_loop then deletes the synthetic ping...
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, f"{uid}-vp"),
        )

    # ...and the frontier is STILL valid: no v2_summary_frontier_integrity_error.
    validated = _assert_frontier_passes_production_validator(uid)
    covered = sum(
        seg.source_message_count for seg in validated if seg.coverage_kind == "exact"
    )
    assert covered == 3
