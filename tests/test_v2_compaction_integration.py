"""Integration test (Task 7): chat-turn over budget -> maintenance enqueue -> compaction
job -> summary populated + watermark advanced, end to end through real
jobs_store/DB primitives.

Hermetic (no live enclave, no live provider network call):
- `provider_client.chat_completion_async` (the chat turn's own LLM call, via
  the unified `tool_loop.run_tool_loop` — Task 7) is monkeypatched to a
  scripted terminal-text round — its correctness is covered exhaustively by
  test_v2_worker_tool_loop.py; this test only cares that the *dispatch*
  (summary+tail plumbing, over-budget enqueue) wires up.
- `provider_client.reliable_chat_completion_async` (the LLM injected into
  `v2_compaction.compact`) is monkeypatched to a deterministic fake bullet —
  `compaction.compact`'s own fold logic is covered by test_v2_compaction.py.
- `read_tail`/`read_summary`/`write_summary` are simple fakes that persist
  PLAINTEXT (not a real enclave-encrypted envelope — no live enclave in this
  test process) directly into the real `v2_conversation_summary` table via the
  real `jobs_store.get_summary_row`/`upsert_summary_row_cas` — so the CAS write,
  version increment, and watermark advance are all exercised for real, only the
  encrypt/decrypt hop is faked (mirrors the same pattern test_v2_worker.py uses
  for read_messages: real coalesce/jobs_store, faked crypto/LLM boundaries).
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
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import summary_frontier as v2_summary_frontier
from model_api_runtime.v2 import worker
from model_api_runtime.v2 import serve_worker as v2_serve_worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="")


class _FakeCapResult:
    def to_dict(self):
        return {"ok": True, "data": {}, "error": None, "trace": {}, "warnings": []}


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table(monkeypatch):
    # This file primarily specifies the provider-backed fallback path. Keep
    # those assertions stable when the CI matrix sets the rollout flag ON;
    # the deterministic integration test below opts back in explicitly.
    monkeypatch.setattr(worker, "_PROFILE_COVERAGE_DETERMINISTIC", False)
    # claim_next_job is a GLOBAL claim (no user_id filter, by design). A pending
    # job left behind by another test file (e.g. the D3 claim-reservation / wake
    # tests enqueue jobs for other users) would pollute this test's global claim
    # ordering and get picked up instead of this test's own chat job. Truncate the
    # whole table before each test here (mirrors tests/test_v2_jobs_store.py).
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


def _make_fake_conversation_deps(messages: list[dict]):
    """`messages`: the full plaintext conversation (both roles), any order.

    - `read_messages`: mirrors serve_worker._read_messages's shape closely
      enough for this test — everything after the last assistant message,
      user-role only (feeds v2.coalesce so the chat lane doesn't short-circuit
      on "no pending messages").
    - `read_tail`/`read_summary`/`write_summary`: see module docstring — real
      jobs_store-backed CAS, plaintext instead of a real envelope.
    """
    ordered = sorted(messages, key=lambda m: m["ts"])

    def _read_messages(uid):
        last_assistant = -1
        for i, m in enumerate(ordered):
            if m["role"] in ("assistant", "openclaw"):
                last_assistant = i
        return [m for m in ordered[last_assistant + 1:] if m["role"] == "user"]

    def _read_tail(uid, after_ts, limit):
        out = [m for m in ordered if m["ts"] > after_ts]
        return out[-limit:] if limit > 0 else []

    def _read_summary(uid):
        row = jobs_store.get_summary_row(uid)
        if row is None:
            return "", 0.0, 0
        env = row["summary_envelope"]
        if not env:
            return "", row["watermark_ts"], row["version"]
        return str(env.get("plaintext") or ""), row["watermark_ts"], row["version"]

    def _write_summary(
        uid, summary, watermark_ts, expected_version, watermark_seq=None,
    ):
        return jobs_store.upsert_summary_row_cas(
            uid, summary_envelope={"plaintext": summary},
            watermark_ts=watermark_ts, expected_version=expected_version,
            watermark_seq=watermark_seq,
        )

    return _read_messages, _read_tail, _read_summary, _write_summary


def test_chat_turn_over_budget_enqueues_maintenance_then_compaction_advances_watermark(monkeypatch):
    uid = "u_v2_compact_integ"
    conftest.seed_user(uid)
    _reset(uid)

    # > _TAIL_BUDGET messages, both roles, so the chat turn's read_tail is over
    # budget and triggers the best-effort maintenance enqueue. The LAST message
    # must be an unanswered `user` turn — otherwise `_read_messages` (mirroring
    # "everything after the last assistant reply") sees nothing pending and the
    # chat lane short-circuits before reaching the provider/effect-enqueue step.
    n = worker._TAIL_BUDGET + 8
    messages = [
        {"id": f"m{i}", "ts": float(i + 1), "role": "user" if i % 2 == 0 else "assistant",
         "content": f"turn {i}"}
        for i in range(n)
    ]
    messages.append({"id": f"m{n}", "ts": float(n + 1), "role": "user", "content": "final unanswered"})
    # Persist an encrypted-shaped source row for every plaintext test message.
    # The prompt adapters below stay fake, but compaction now runs beside the
    # real durable transcript so this integration test proves advancing the
    # summary watermark never changes source retention.
    for message in messages:
        db.chat_append_strict(
            uid,
            message["id"],
            message["ts"],
            {
                "id": message["id"],
                "role": message["role"],
                "body_ct": f"cipher-{message['id']}",
            },
            core_store.MAX_CHAT_MESSAGES,
        )
    durable_count_before = db.chat_count_strict(uid)
    read_messages, read_tail, read_summary, write_summary = _make_fake_conversation_deps(messages)

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult())

    async def _fake_chat_completion(config, messages, *, tools=None):
        # Terminal plain-text round (Task 7): no tool_calls -> that text IS the
        # chat turn's final reply through the unified tool_loop.run_tool_loop.
        return {"reply": "model reply", "tool_calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake_chat_completion)
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    async def _fake_llm(cfg, msgs, **kw):
        return {"reply": "- folded integration bullet"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)

    def _reply_effect_dispatch(user_id):
        def dispatch(effect_type, payload):
            if effect_type == "reply":
                worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
        return dispatch

    def _apply_effects(user_id):
        return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_reply_effect_dispatch(user_id))

    deps = worker.TurnDeps(
        read_messages=read_messages,
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_tail=read_tail,
        read_summary=read_summary,
        write_summary=write_summary,
        apply_pending_effects=_apply_effects,
    )

    # --- chat turn ---
    jobs_store.enqueue_job(uid, "chat")
    chat_job = jobs_store.claim_next_job("w-chat")
    status = asyncio.run(worker.process_job(
        chat_job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))
    assert status == "completed"

    # (a) the over-budget tail must have triggered a maintenance-lane enqueue.
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, lane FROM agent_jobs WHERE user_id=%s AND lane='maintenance'",
            (uid,),
        ).fetchone()
    assert row is not None, "expected a pending maintenance job after an over-budget chat turn"
    assert row[0] == "pending"
    assert jobs_store.get_summary_row(uid) is None  # not compacted yet — only enqueued

    # --- maintenance turn ---
    maint_job = jobs_store.claim_next_job("w-maint")
    assert maint_job["lane"] == "maintenance"
    status2 = asyncio.run(worker.process_job(
        maint_job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))
    assert status2 == "completed"

    # (b) get_summary_row is now populated: non-null envelope, version==1,
    # watermark advanced past the oldest folded batch.
    summary_row = jobs_store.get_summary_row(uid)
    assert summary_row is not None
    assert summary_row["summary_envelope"] is not None
    assert summary_row["summary_envelope"]["plaintext"] == "- folded integration bullet"
    assert summary_row["version"] == 1
    assert summary_row["watermark_ts"] > 0.0

    with db.get_pool().connection() as conn:
        maint_row = conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (maint_job["id"],)
        ).fetchone()
    assert maint_row[0] == "completed"

    # (c) a follow-up read_tail(user_id, new_watermark, cap) returns fewer rows
    # than a read from the very start — the oldest batch got folded away.
    full_tail = read_tail(uid, 0.0, 10_000)
    new_tail = read_tail(uid, summary_row["watermark_ts"], 10_000)
    assert len(new_tail) < len(full_tail)
    assert len(new_tail) == worker._TAIL_KEEP
    assert db.chat_count_strict(uid) == durable_count_before == len(messages)


# --- maintenance must not be blocked by its own optimisations ---------------
# Both cases below were observed wedging real prod users on 2026-07-29 after a
# V2 cutover: the backlog could not drain, and manually re-enqueuing
# maintenance did nothing (the watermark did not move across repeated rounds).


def _minimal_compaction_deps(messages: list[dict], *, with_frontier: bool):
    """Just enough deps for `_run_compaction`'s legacy (ts) write path."""
    _read_messages, _read_tail, _read_summary, _write_summary = (
        _make_fake_conversation_deps(messages)
    )
    ordered = sorted(messages, key=lambda m: m["ts"])

    def _read_compaction_tail(uid, after_ts, limit):
        out = [m for m in ordered if m["ts"] > after_ts]
        return out[:limit] if limit > 0 else []

    kwargs = dict(
        read_messages=_read_messages,
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_summary=_read_summary,
        read_tail=_read_tail,
        read_compaction_tail=_read_compaction_tail,
        write_summary=_write_summary,
    )
    if with_frontier:
        # Presence of this callback is what makes `_run_compaction` attempt a
        # checkpoint rebalance before folding.
        kwargs["read_summary_frontier"] = lambda uid: None
    return worker.TurnDeps(**kwargs)


def test_checkpoint_failure_must_not_block_the_fold(monkeypatch):
    """A failed checkpoint must cost the checkpoint, not the fold.

    `_run_compaction` rebalances the summary frontier BEFORE reading the tail.
    That call raising (`SummaryFrontierExhausted` when no safe roll-up batch
    can be formed) skips the entire fold below it, so the backlog can never
    drain — and because the roll-up input doesn't change, it raises again on
    every retry. usr_7f30 on prod sat at a frozen watermark through repeated
    manual re-enqueues for exactly this reason.

    A checkpoint only reduces how many nodes a later prompt must read; the
    fold is the actual work. Losing the former must not cost the latter.
    """
    uid = "u_v2_checkpoint_blocks_fold"
    conftest.seed_user(uid)
    _reset(uid)

    n = worker._TAIL_KEEP + 12
    messages = [
        {"id": f"m{i}", "ts": float(i + 1),
         "role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
        for i in range(n)
    ]

    async def _rebalance_always_exhausted(*args, **kwargs):
        raise v2_summary_frontier.SummaryFrontierExhausted(
            "fanout_run_exceeds_rollup_input"
        )

    monkeypatch.setattr(
        worker, "_rebalance_summary_frontier", _rebalance_always_exhausted
    )

    async def _fake_llm(_config, _messages, **_kwargs):
        return {"reply": "- folded bullet"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)

    deps = _minimal_compaction_deps(messages, with_frontier=True)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance", reason="compaction")
    job = jobs_store.claim_next_job("compaction-test")
    assert job is not None and job["id"] == job_id

    status = asyncio.run(worker._run_compaction(
        job_id, uid, deps, _BYOK, worker.ENCLAVE_SEMAPHORE,
        claimed_by=str(job["claimed_by"])))

    # The fold ran despite the checkpoint failing, and the watermark moved.
    row = jobs_store.get_summary_row(uid)
    assert status == "completed", status
    assert row is not None and row["watermark_ts"] > 0, row
    assert "folded bullet" in str((row["summary_envelope"] or {}).get("plaintext") or "")


def test_refused_fold_shrinks_instead_of_reporting_success(monkeypatch):
    """A refused fold must be retried smaller, not silently written off.

    `_run_compaction`'s no-op guard treats "the model returned nothing usable"
    as success: it marks the job completed with `status="ok"` and returns
    WITHOUT advancing the watermark, retrying, shrinking or chaining a
    follow-up. The backlog is then permanently stuck while every dashboard
    reports healthy maintenance.

    usr_90184 on prod: job 130 recorded `model_calls=1, status=ok` while
    `v2_conversation_summary.updated_at` stayed an hour old — a 7393-char fold
    request came back as 95 chars (too short to pass bullet validation),
    presumably a refusal on that batch's content. The inline catch-up already
    handles this by shrinking the batch and, at a batch of one, quarantining
    the row; maintenance had none of that.
    """
    uid = "u_v2_refused_fold_shrinks"
    conftest.seed_user(uid)
    _reset(uid)

    n = worker._TAIL_KEEP + 12
    messages = [
        {"id": f"m{i}", "ts": float(i + 1),
         "role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
        for i in range(n)
    ]

    sizes = []

    async def _refuse_big_batches(_config, msgs, **_kwargs):
        # `compact` renders the whole batch into one user turn; count the rows
        # it was asked to fold from the caller side instead.
        sizes.append(_refuse_big_batches.pending)
        if _refuse_big_batches.pending > 2:
            return {"reply": ""}          # refused: nothing usable came back
        return {"reply": "- folded small"}

    _refuse_big_batches.pending = 0

    real_prefix = worker._bounded_compaction_prefix

    def _spy_prefix(rows, **kwargs):
        out = real_prefix(rows, **kwargs)
        _refuse_big_batches.pending = len(out)
        return out

    monkeypatch.setattr(worker, "_bounded_compaction_prefix", _spy_prefix)
    monkeypatch.setattr(
        provider_client, "reliable_chat_completion_async", _refuse_big_batches
    )

    deps = _minimal_compaction_deps(messages, with_frontier=False)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance", reason="compaction")
    job = jobs_store.claim_next_job("compaction-test")
    assert job is not None and job["id"] == job_id

    status = asyncio.run(worker._run_compaction(
        job_id, uid, deps, _BYOK, worker.ENCLAVE_SEMAPHORE,
        claimed_by=str(job["claimed_by"])))

    row = jobs_store.get_summary_row(uid)
    # It shrank past the refusal threshold rather than declaring victory.
    assert max(sizes) > 2, sizes
    assert min(sizes) <= 2, sizes
    # And the watermark actually moved, which is the whole point of the job.
    assert row is not None and row["watermark_ts"] > 0, (status, row)


def test_legacy_compaction_reader_also_excludes_gc_able_rows(monkeypatch):
    """Both compaction readers must agree on what may be folded.

    A `verify_ping` row lives in chat_messages only until its probe completes.
    Folding one into an immutable leaf freezes a coverage claim the row will
    not honour, and `validate_canonical_frontier` then fails on EVERY later
    turn. The seq-based reader passes `exclude_synthetic_sources=True`; its
    ts-based sibling did not, leaving the same hole open for any caller that
    still reaches it.
    """
    from model_api_runtime.v2 import serve_worker as v2_serve_worker

    uid = "u_v2_legacy_reader_excludes"
    rows = [
        {"id": "a", "ts": 1.0, "role": "user", "source": "chat", "body_ct": "x",
         "K_enclave": "k"},
        {"id": "ping", "ts": 2.0, "role": "user", "source": "verify_ping",
         "body_ct": "x", "K_enclave": "k"},
        {"id": "b", "ts": 3.0, "role": "openclaw", "source": "model_api",
         "body_ct": "x", "K_enclave": "k"},
    ]

    class _Store:
        user_id = uid
        chat_messages = rows

        def reload_chat_strict(self):
            return rows

    monkeypatch.setattr(v2_serve_worker.core_store, "get_store", lambda _u: _Store())
    monkeypatch.setattr(
        v2_serve_worker, "_decrypt_chat_rows",
        lambda _uid, sel, **_kw: [dict(r) for r in sel],
    )

    folded = v2_serve_worker._read_compaction_tail(uid, 0.0, 10)
    assert [r["id"] for r in folded] == ["a", "b"], folded


def test_metadata_coverage_bounds_exclude_gc_able_rows_and_honor_limit():
    uid = "u_v2_metadata_coverage_bounds"
    conftest.seed_user(uid)
    _reset(uid)
    sources = [
        "model_api",
        "verify_ping",
        "model_api",
        "resident_maintenance",
        "model_api",
    ]
    for index, source in enumerate(sources):
        db.chat_append_strict(
            uid,
            f"bounds-{index}",
            float(index + 1),
            {
                "id": f"bounds-{index}",
                "role": "user",
                "source": source,
                "body_ct": f"cipher-{index}",
            },
            core_store.MAX_CHAT_MESSAGES,
        )
    eligible = db.chat_messages_after_seq(
        uid,
        0,
        limit=None,
        exclude_synthetic_sources=True,
    )

    assert [row["source"] for row in eligible] == [
        "model_api",
        "model_api",
        "model_api",
    ]
    assert db.chat_coverage_bounds_after_seq(uid, 0, limit=2) == (
        eligible[0]["seq"],
        eligible[1]["seq"],
        2,
    )
    assert db.chat_coverage_bounds_after_seq(
        uid,
        eligible[0]["seq"],
        limit=10,
        through_seq=eligible[1]["seq"],
    ) == (eligible[1]["seq"], eligible[1]["seq"], 1)


def test_deterministic_maintenance_large_backlog_has_zero_model_calls_and_bounded_frontier(
    monkeypatch,
):
    uid = "u_v2_deterministic_large_backlog"
    conftest.seed_user(uid)
    _reset(uid)
    total = 1_510
    with db.get_pool().connection() as conn:
        with conn.transaction():
            for index in range(total):
                conn.execute(
                    "INSERT INTO chat_messages (user_id,msg_id,ts,doc) "
                    "VALUES (%s,%s,%s,%s)",
                    (
                        uid,
                        f"det-{index}",
                        float(index + 1),
                        db.Jsonb({
                            "id": f"det-{index}",
                            "role": "user" if index % 2 == 0 else "openclaw",
                            "source": "model_api",
                            "body_ct": f"cipher-{index}",
                        }),
                    ),
                )

    monkeypatch.setattr(worker, "_PROFILE_COVERAGE_DETERMINISTIC", True)
    monkeypatch.setattr(worker, "_COMPACTION_BATCH", 200)
    monkeypatch.setattr(worker, "_SUMMARY_ROLLUP_FANOUT", 8)
    provider_calls = []

    async def _provider_must_not_run(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("deterministic coverage must not call provider")

    monkeypatch.setattr(
        provider_client,
        "reliable_chat_completion_async",
        _provider_must_not_run,
    )
    head_sizes = []

    def _append_segment(uid_, segment_text, **kwargs):
        head_text = str(kwargs["head_summary"])
        head_sizes.append(len(head_text))
        return jobs_store.append_summary_leaf_cas(
            uid_,
            summary_envelope={"plaintext": str(segment_text)},
            head_summary_envelope={"plaintext": head_text},
            start_seq=kwargs["start_seq"],
            end_seq=kwargs["end_seq"],
            source_message_count=kwargs["source_message_count"],
            watermark_ts=kwargs["watermark_ts"],
            expected_version=kwargs["expected_version"],
            previous_watermark_seq=kwargs["previous_watermark_seq"],
        )

    def _append_checkpoint(uid_, checkpoint_text, **kwargs):
        head_text = str(kwargs["head_summary"])
        head_sizes.append(len(head_text))
        return jobs_store.insert_summary_checkpoint(
            uid_,
            summary_envelope={"plaintext": str(checkpoint_text)},
            head_summary_envelope={"plaintext": head_text},
            level=kwargs["level"],
            start_seq=kwargs["start_seq"],
            end_seq=kwargs["end_seq"],
            source_message_count=kwargs["source_message_count"],
            child_segment_ids=kwargs["child_segment_ids"],
            coverage_kind=kwargs["coverage_kind"],
            legacy_opaque_through_seq=kwargs["legacy_opaque_through_seq"],
            expected_version=kwargs["expected_version"],
            expected_watermark_seq=kwargs["expected_watermark_seq"],
        )

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        runtime_mode_enabled=lambda _uid: True,
        read_summary_frontier_metadata=v2_serve_worker._read_summary_frontier_metadata,
        append_summary_segment=_append_segment,
        append_summary_checkpoint=_append_checkpoint,
    )
    expected_jobs = (
        total
        - worker._TAIL_KEEP
        + worker._COMPACTION_BATCH
        - 1
    ) // worker._COMPACTION_BATCH
    jobs_store.enqueue_job(uid, "maintenance", reason="deterministic-test")
    completed_jobs = 0
    while completed_jobs < expected_jobs + 2:
        job = jobs_store.claim_next_job("deterministic-worker")
        if job is None:
            break
        assert job["user_id"] == uid
        status = asyncio.run(
            worker._run_compaction(
                job["id"],
                uid,
                deps,
                _BYOK,
                asyncio.Semaphore(1),
                claimed_by=str(job["claimed_by"]),
            )
        )
        assert status == "completed"
        completed_jobs += 1

    state = jobs_store.get_summary_frontier_state(uid)
    assert state is not None
    assert state["watermark_seq"] > 0
    assert db.count_messages_after_seq(
        uid,
        state["watermark_seq"],
        exclude_synthetic_sources=True,
    ) == worker._TAIL_KEEP
    assert completed_jobs == expected_jobs
    assert provider_calls == []
    assert len(state["segments"]) <= worker._SUMMARY_FRONTIER_MAX_SEGMENTS
    assert any(int(row["level"]) > 0 for row in state["segments"])
    assert head_sizes and max(head_sizes) < 80


def test_a_shrunk_fold_is_remembered_by_the_next_job(monkeypatch):
    """The rung that worked must outlive the job that found it.

    Draining a backlog runs `_run_compaction` over and over via chained
    compaction_catchup jobs. `fold_limit` used to be a local starting at the
    full batch, so every one of those jobs re-paid a guaranteed refusal to
    rediscover the same limit — roughly 100 wasted provider calls to drain a
    1200-message backlog, billed to the user's own key.
    """
    uid = "u_v2_batch_cap_is_remembered"
    conftest.seed_user(uid)
    _reset(uid)

    n = worker._TAIL_KEEP + 40
    messages = [
        {"id": f"m{i}", "ts": float(i + 1),
         "role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
        for i in range(n)
    ]

    refusals: list[int] = []

    async def _refuse_large_batches(_config, sent_messages, **_kwargs):
        # Count the rows the fold actually sent this call.
        rendered = str(sent_messages[-1].get("content") if sent_messages else "")
        rows = rendered.count("\n") + 1
        refusals.append(rows)
        # Anything above 8 rows is refused; smaller folds succeed.
        return {"reply": "" if rows > 8 else "- folded bullet"}

    monkeypatch.setattr(
        provider_client, "reliable_chat_completion_async", _refuse_large_batches
    )

    def _run_one_job():
        deps = _minimal_compaction_deps(messages, with_frontier=False)
        job_id, _ = jobs_store.enqueue_job(uid, "maintenance", reason="compaction")
        job = jobs_store.claim_next_job("compaction-test")
        assert job is not None
        return asyncio.run(worker._run_compaction(
            job_id, uid, deps, _BYOK, worker.ENCLAVE_SEMAPHORE,
            claimed_by=str(job["claimed_by"])))

    _run_one_job()
    first_job_calls = len(refusals)
    assert first_job_calls > 1, "first job should have had to shrink at least once"

    # Well below the configured batch: the additive increase on the winning
    # fold nudges it back up, so this is not the exact rung that succeeded.
    stored = db.v2_effective_batch_cap(uid)
    assert stored is not None and stored < worker._COMPACTION_BATCH, stored

    refusals.clear()
    _run_one_job()

    # The second job starts from the remembered rung instead of the top, so it
    # does not repeat the first job's ladder of refusals.
    assert len(refusals) < first_job_calls, (first_job_calls, len(refusals))
