"""V2 worker integration: native tool-loop turns, lane dispatch, and failures.

The tests keep real jobs/runtime state and effect-outbox persistence while
stubbing enclave-bound reads, capability execution, and provider responses.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from provider_types import ToolExchange
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import compaction as v2_compaction
from model_api_runtime.v2 import cursor as v2_cursor
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import prompt_frontier as v2_prompt_frontier
from model_api_runtime.v2 import worker


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table(monkeypatch):
    """claim_next_job() is a GLOBAL work-queue claim (by design it doesn't filter
    by user_id — see jobs_store.claim_next_job docstring). A pending job left
    behind by another test module (e.g. test_v2_jobs_store.py, which runs
    alphabetically before this file in a full suite) would otherwise get
    claimed here instead of the row a given test just enqueued. Truncate
    before each test so claim_next_job only ever sees this test's own row —
    mirrors the identical fixture in test_v2_jobs_store.py."""
    # Exact successor/enqueue assertions predate the independent profile lane.
    monkeypatch.setenv("FEEDLING_V2_PROFILE_ENABLED", "0")
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", False)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="",
    prompt_cache_route_fingerprint="feedling-v2-route-test")


def test_prompt_frontier_failures_use_stable_content_free_status_codes():
    unconfigured = v2_prompt_frontier.PromptContextLimitUnconfigured(
        provider="private-provider",
        model="private-model",
    )
    exhausted = v2_prompt_frontier.PromptFrontierExhausted(
        required_tokens=9_000,
        input_budget_tokens=8_000,
        context_window_tokens=12_000,
        required_components=("message_context",),
        limit_source="audited_family",
    )
    unaudited_exhausted = v2_prompt_frontier.PromptFrontierExhausted(
        required_tokens=9_000,
        input_budget_tokens=8_000,
        context_window_tokens=12_000,
        required_components=("message_context",),
        limit_source="unaudited_default",
    )

    assert worker._safe_failure_code("turn_failed", unconfigured) == (
        "turn_failed:prompt_context_limit_unconfigured"
    )
    assert "private" not in worker._safe_failure_code("turn_failed", unconfigured)
    assert worker._safe_failure_code("turn_failed", exhausted) == (
        "turn_failed:prompt_frontier_exhausted"
    )
    assert worker._turn_failure_error_class(unconfigured) == "unknown"
    assert worker._turn_failure_error_class(exhausted) == "context_overflow"
    assert worker._turn_failure_error_class(unaudited_exhausted) == "unknown"
    assert worker._turn_failure_error_class(
        worker.TurnError("prompt_coverage_incomplete:reply_empty")
    ) == "unknown"
    assert worker._safe_failure_code(
        "turn_failed", worker.v2_tool_loop.ProviderEmptyReply("empty_reply")
    ) == "turn_failed:empty_reply"
    assert worker._provider_health_error_class(
        worker.v2_tool_loop.ProviderEmptyReply("empty_reply")
    ) == "provider_empty_reply"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            provider_client.ProviderError("credit balance exhausted", status_code=402),
            "quota_insufficient",
        ),
        (
            provider_client.ProviderError("bad key", status_code=401),
            "auth_invalid",
        ),
        (
            provider_client.ProviderError("too many requests", status_code=429),
            "rate_limited",
        ),
        (
            provider_client.ProviderError("relay unavailable", status_code=503),
            "upstream_unavailable",
        ),
        (
            provider_client.ProviderError(
                "provider_http_400: Failed to deserialize the JSON body into "
                "the target type: messages[0]: unknown variant `image_url`, "
                "expected `text` at line 1 column 295",
                status_code=400,
            ),
            "vision_model_required",
        ),
        (
            provider_client.ProviderError(
                "provider_http_404: No endpoints found that support image input",
                status_code=404,
            ),
            "vision_model_required",
        ),
        # 2026-08-07: 空回复属于模型/provider 行为，统一归 provider。
        (worker.TurnError("empty_reply"), "provider_empty_reply"),
        (
            worker.v2_tool_loop.ProviderEmptyReply("empty_reply"),
            "provider_empty_reply",
        ),
        (RuntimeError("opaque internal failure"), "unknown"),
    ],
)
def test_v2_turn_failure_classification_uses_shared_notice_vocabulary(exc, expected):
    assert worker._turn_failure_error_class(exc) == expected


class _FakeCapResult:
    def __init__(self, data=None, ok=True):
        self._data = data or {}
        self._ok = ok

    def to_dict(self):
        return {"ok": self._ok, "data": self._data, "error": None, "trace": {}, "warnings": []}


def _reply_effect_dispatch(user_id):
    """Test-local production-shaped sink for the chat lane's `reply` effect_type
    only (this file's chat-turn tests never enqueue memory/identity/schedule
    effects — those are covered in tests/test_v2_effect_sinks.py). Mirrors
    `serve_worker._sink_reply`'s real write (`worker._write_encrypted_reply`)
    without pulling in serve_worker's hosted-adjacent wiring.
    `worker._write_encrypted_reply` is looked up via the module attribute at
    call time, so a test that monkeypatches it (most already do) still sees
    its own stub take effect here."""
    def dispatch(effect_type, payload):
        if effect_type == "reply":
            worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
        elif effect_type == "cursor":
            # Mirror serve_worker._sink_cursor: persist the advanced seq reply
            # cursor into the model_api_runtime blob (D5). Simplified — no
            # effect_sink_claim (the reply branch above skips it too).
            db.patch_blob_strict(
                user_id, "model_api_runtime", {v2_cursor.CURSOR_KEY: int(payload["new_seq"])})
    return dispatch


def _apply_effects(user_id):
    """`TurnDeps.apply_pending_effects` test wiring (Task 7 / spec C6): drains
    this turn's PR A outbox through `_reply_effect_dispatch` above — the same
    seam `on_reply` calls mid-loop for an intermediate bubble and process_job
    calls again at end-of-turn."""
    return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_reply_effect_dispatch(user_id))


def _script_provider(monkeypatch, responses):
    """Monkeypatch `provider_client.chat_completion_async` — what
    `tool_loop.run_tool_loop` calls once per round — to return the given
    scripted results in order. Each `responses[i]` is a dict `{"reply": str,
    "tool_calls": [...], "usage": {...}|None}` matching the wire contract
    `provider_types.ProviderResponse.from_result` reads. Returns the list of
    captured provider call records so a test can
    assert what the model actually saw each round (e.g. that a prior round's
    tool observation was folded in)."""
    it = iter(responses)
    calls = []

    async def _fake(config, messages, *, tools=None, **kwargs):
        calls.append({"messages": messages, "tools": tools, **kwargs})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _text_round(text, *, prompt_tokens=1, completion_tokens=1):
    """A scripted terminal round: no tool_calls -> `run_tool_loop` treats `text`
    as the final reply (Global Constraints: "no-tool-call plain text is the
    final reply")."""
    return {"reply": text, "tool_calls": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _tool_round(*tool_calls, prompt_tokens=1, completion_tokens=1):
    """A scripted non-terminal round: one or more tool_calls (built via `_tc`
    below), no plain-text bubble (accompanying text would be preamble, not a
    reply — Global Constraints)."""
    return {"reply": "", "tool_calls": list(tool_calls),
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _tc(call_id, name, **args):
    return {"id": call_id, "name": name, "args": args}


def _deps(*, messages, provider=None, token="rt-enclave"):
    provider = provider if provider is not None else (_BYOK, {})
    return worker.TurnDeps(
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: provider,
        mint_enclave_token=lambda uid: token,
        apply_pending_effects=_apply_effects,
    )


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


class _CountingSemaphore(asyncio.Semaphore):
    """A real asyncio.Semaphore that also counts acquisitions, so a test can
    assert *which* code paths went through the shared enclave gate (spec §11
    R3) without having to mock away asyncio's own synchronization primitive."""

    def __init__(self, value=2):
        super().__init__(value)
        self.acquire_count = 0

    async def acquire(self):
        self.acquire_count += 1
        return await super().acquire()


def test_write_encrypted_reply_uses_strict_persistence(monkeypatch):
    calls = []

    class _Store:
        def append_chat(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"id": "reply-1"}

        def notify_chat_waiters(self):
            calls.append(("notify", {}))

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _raw: ({"id": "reply-1"}, None),
    )

    assert worker._write_encrypted_reply(_Store(), "hello") == {"id": "reply-1"}
    assert calls[0][1] == {"strict": True}
    assert calls[1] == ("notify", {})


def test_write_encrypted_reply_propagates_strict_database_failure(monkeypatch):
    class _Store:
        def append_chat(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

        def notify_chat_waiters(self):
            raise AssertionError("must not notify after a failed persistence")

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _raw: ({"id": "reply-2"}, None),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        worker._write_encrypted_reply(_Store(), "hello")


# ------------------------------------------------------------------
# process_job: the full turn body
# ------------------------------------------------------------------

def test_process_job_end_to_end_writes_reply_and_completes(monkeypatch):
    """Happy path (Task 7 / spec C6+C9a): a pending user message -> real coalesce ->
    ONE round-trip through the unified `tool_loop.run_tool_loop` (no tool_calls,
    plain text) -> the reply effect drained through the real outbox -> encrypted
    reply written -> job completed -> action_digest folded into runtime_state."""
    uid = "u_w_happy"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _script_provider(monkeypatch, [_text_round("MODEL REPLY")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}])
    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text, user_id=store.user_id) or {"id": "r1"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert written == {"text": "MODEL REPLY", "user_id": uid}
    row = _job_status(job_id)
    assert row[0] == "completed"
    state = jobs_store.get_runtime_state(uid)
    assert state.get("last_replied_ts") == 10.0
    assert "action_digest" in state  # non-sensitive digest only; no capability data leaked here


def test_chat_still_calls_provider_and_restores_unhealthy_state(monkeypatch):
    uid = "u_w_provider_health_chat_probe"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO provider_health (
              user_id, provider_state, last_provider_failure_at,
              last_provider_error_class, last_provider_error_blame, last_probe_at
            )
            VALUES (%s, 'needs_user_action', now() - interval '49 hours',
                    'quota_insufficient', 'user_provider', now())
            """,
            (uid,),
        )
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-provider-health-chat")
    calls = _script_provider(monkeypatch, [_text_round("RECOVERED")])
    deps = _deps(
        messages=[
            {
                "id": "m-provider-health",
                "ts": 10.0,
                "role": "user",
                "content": "I fixed the provider",
            }
        ]
    )
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda _store, _text: {"id": "r-provider-health"},
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert len(calls) == 1
    assert _job_status(job_id)[0] == "completed"
    with db.get_pool().connection() as conn:
        health = conn.execute(
            """
            SELECT provider_state, last_provider_success_at IS NOT NULL
            FROM provider_health
            WHERE user_id = %s
            """,
            (uid,),
        ).fetchone()
    assert health == ("ok", True)


def test_message_coalesced_during_provider_call_creates_successor(monkeypatch):
    """finish_chat_job's late-input successor creation lives entirely in
    jobs_store (input_generation vs observed_generation), independent of what
    drives the turn body — this just needs SOME provider call to happen mid-turn
    during which a concurrent enqueue_job bumps the row's input_generation."""
    uid = "u_w_late_successor"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-late")

    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda action_type, store, **kwargs: _FakeCapResult({}),
    )

    async def _fake(config, messages, *, tools=None, **_kwargs):
        same_id, coalesced = jobs_store.enqueue_job(uid, "chat", reason="late-B")
        assert (same_id, coalesced) == (job_id, True)
        return _text_round("reply to A")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})
    deps = _deps(messages=[{"id": "A", "ts": 10.0, "role": "user", "content": "first"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK,
        api_key=None, runtime_token="rt"))

    assert status == "completed"
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status,reason FROM agent_jobs WHERE user_id=%s ORDER BY id", (uid,)
        ).fetchall()
    assert rows[0][:2] == (job_id, "completed")
    assert rows[1][1:] == ("pending", "coalesced_followup")


# NOTE (Task 7 / spec C6): test_reply_envelope_failure_is_terminal_not_success was
# deleted here, not converted. Its premise no longer holds: process_job's chat
# branch never calls worker._write_encrypted_reply directly anymore (that call
# now lives behind the PR A effect outbox, in serve_worker._sink_reply, invoked
# via deps.apply_pending_effects). serve_worker._sink_reply does not currently
# check _write_encrypted_reply's None return (envelope-build failure) and
# re-raise, so an envelope failure today applies the effect as if it had
# succeeded (no exception, no mark_failed) -- a real behavior gap, but one that
# lives in serve_worker.py / the PR A sink layer, outside worker.py and outside
# this task's assigned scope. Flagged in the Task 7 report; belongs with
# serve_worker._sink_reply's own tests (tests/test_v2_effect_sinks.py), not here.


def test_process_job_acquires_enclave_semaphore_for_read_messages_and_prefetch(monkeypatch):
    """FIX 2 (spec §11 R3): the per-turn enclave_sem must bound EVERY enclave-bound
    call in a turn, not just provider-key decrypt (_run_turn) and executor
    capability calls (_run_one). Before this fix, _coalesce_inputs's call to
    deps.read_messages (per-message chat decrypt) and the two _cap_data prefetch
    calls (memory_index/perception_snapshot) ran unbounded -> N concurrent
    workers could hit the shared, capacity-bounded enclave without ever passing through
    the shared gate. Uses a real (counting) Semaphore, not a mock, so the
    assertion exercises actual async acquire/release semantics.

    Task 7: the old planner-lane memory_index/perception_snapshot prefetches
    are gone (the unified tool loop only fetches what the model actually asks
    for), so this now drives a 2-round script — an explicitly enabled
    `web_search` tool call, then a terminal reply — to exercise a THIRD
    enclave-bound call
    (`executor.dispatch_tool_calls`'s inline read, same `_run_one` capability
    path the old prefetches used) alongside `_coalesce_inputs`'s read_messages.
    Together with the per-round fold described below, that preserves the
    ">= 3 acquisitions" assertion.

    Task 7 FIX (BUG-2): the per-round fold ahead of round 1 (call_idx > 0) is
    now ALSO gated by the same enclave_sem (`_make_fold_new_messages` wraps its
    reader call in `async with enclave_sem` + `asyncio.to_thread`, exactly like
    `_coalesce_inputs`) — so the minimum acquisition count is 3."""
    uid = "u_w_semaphore"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    _script_provider(monkeypatch, [
        _tool_round(_tc("c1", "web_search", query="x")),
        _text_round("R"),
    ])
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    read_messages_calls = {"n": 0}

    def _read_messages(uid_):
        read_messages_calls["n"] += 1
        return [{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}]

    deps = worker.TurnDeps(
        read_messages=_read_messages,
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        apply_pending_effects=_apply_effects,
        web_tools_enabled=lambda uid_: True,
    )
    sem = _CountingSemaphore(2)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt",
        enclave_sem=sem,
    ))

    assert status == "completed"
    # 2 calls: the turn's initial coalesce, plus the Task 6 per-round fold ahead
    # of round 1 (call_idx > 0) — it falls back to the same `deps.read_messages`
    # reader since no `read_messages_since` is wired here (see
    # `_make_fold_new_messages`'s fallback, matching `_coalesce_inputs`'s own).
    assert read_messages_calls["n"] == 2
    # 1 acquisition for read_messages (_coalesce_inputs) + 1 for the web_search
    # tool call's capability dispatch + 1 for the per-round fold's own read
    # (BUG-2 fix: `_make_fold_new_messages` now
    # gates its reader call through the SAME enclave_sem `process_job` passed
    # in, instead of calling the reader directly with no semaphore), at minimum.
    assert sem.acquire_count >= 3


def test_coalesce_inputs_and_cap_data_tolerate_enclave_sem_none(monkeypatch):
    """Direct unit coverage of the `enclave_sem is None` guard added to the two
    newly-wrapped helpers (mirrors executor._run_one's tolerance): calling them
    with no semaphore at all — not even process_job's private direct-call default
    substitution — must not raise."""
    uid = "u_w_semaphore_none"
    conftest.seed_user(uid)
    store = core_store.get_store(uid)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
    )
    coalesced, seq_cursor, ts_cursor = asyncio.run(
        worker._coalesce_inputs(deps, uid, 0, enclave_sem=None))
    assert ts_cursor == 1.0  # max ts of coalesced (rollback last_replied_ts feed)
    assert seq_cursor == 0   # row carries no seq -> falls back to the since_seq default
    assert coalesced and coalesced[0]["content"] == "hi"

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({"x": 1}))
    data = asyncio.run(worker._cap_data(
        store, "memory_index", api_key=None, runtime_token="rt", enclave_sem=None))
    assert data == {"x": 1}


def test_process_job_empty_terminal_reply_marks_failed_no_filler(monkeypatch):
    """An empty terminal reply (no tool_calls, blank text) must mark the job
    failed without treating blank provider output as a successful model reply.
    Durable foreground failure visibility is tested at the terminal-outbox
    boundary; this direct unit has no stored parent message."""
    uid = "u_w_resperr"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _script_provider(monkeypatch, [_text_round("")])
    deps = _deps(messages=[{"id": "m1", "ts": 5.0, "role": "user", "content": "hi"}])
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert write_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert row[1] == "turn_failed:empty_reply"


def test_process_job_no_pending_messages_chat_lane_completes_without_provider_call(monkeypatch):
    """A chat-lane job that finds no coalesced pending messages (already answered
    by a racing job) must complete cleanly without invoking the provider."""
    uid = "u_w_nopending"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    provider_calls = {"n": 0}

    async def _unexpected_provider(*args, **kwargs):
        provider_calls["n"] += 1
        raise AssertionError("empty chat job must not call the provider")

    monkeypatch.setattr(provider_client, "chat_completion_async", _unexpected_provider)
    emitted = []
    wakes = []
    monkeypatch.setattr(worker, "_emit_status", lambda user_id, jid, kind: emitted.append(kind))
    monkeypatch.setattr(
        worker.core_wake_bus,
        "notify",
        lambda channel, user_id: wakes.append((channel, user_id)),
    )
    deps = _deps(messages=[])  # nothing pending -> coalesce_pending returns ([], 0.0)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert provider_calls["n"] == 0
    assert _job_status(job_id)[0] == "completed"
    assert emitted == ["processing", "done"]
    assert ("chat", uid) in wakes


def test_process_job_picks_up_concurrent_new_message_via_per_round_fold(monkeypatch):
    """Task 7 replaces the old outer replan state machine (v2_inval safe-point
    REPLAN/CONTINUE, re-coalesce, bounded re-planning) with `tool_loop`'s
    per-round fold: no restart, the turn just keeps going and picks up newly-
    visible user messages before each provider call after the first (Global
    Constraints "per-round fold, no debounce/restart"). This is the direct
    successor of the old `..._replans_on_concurrent_new_message_within_budget`
    test — same scenario (a second message arrives mid-turn), same durable
    outcome (last_replied_ts reflects BOTH messages), different mechanism."""
    uid = "u_w_replan"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc("c1", "web_search", query="x")),  # round 0: forces a round 1
        _text_round("R"),                                  # round 1: terminal reply
    ])

    # `deps.read_messages` (no read_messages_since wired) backs BOTH the turn's
    # initial coalesce AND the fold closure (same fallback `_coalesce_inputs`
    # itself uses) — so the 1st call (initial coalesce, before the loop starts)
    # sees only m1; the 2nd call (the fold ahead of round 1, call_idx>0) sees m2
    # arriving concurrently, mirroring "a message showed up mid-turn."
    feed = iter([
        [{"id": "m1", "ts": 10.0, "seq": 1, "role": "user", "content": "first"}],
        [{"id": "m1", "ts": 10.0, "seq": 1, "role": "user", "content": "first"},
         {"id": "m2", "ts": 20.0, "seq": 2, "role": "user", "content": "second"}],
    ])
    deps = worker.TurnDeps(
        read_messages=lambda uid: next(feed),
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        apply_pending_effects=_apply_effects,
    )

    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text) or {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == 2               # exactly the scripted 2 rounds, no restart
    # round 1's messages carry the folded-in second message's content.
    assert any(
        "second" in str(m.get("content", ""))
        for m in calls[1]["messages"]
        if isinstance(m, dict)
    )
    assert written["text"] == "R"
    assert _job_status(job_id)[0] == "completed"
    # The DURABLE seq reply cursor advances to the fold's max seq (2) — the turn
    # answered BOTH messages, so the next turn resumes at seq > 2, never re-
    # reading either. This is the source of truth in the seq world.
    from core import store as _cs
    assert v2_cursor.load_seq(_cs.get_store(uid)) == 2
    # last_replied_ts is dual-written (vestigial rollback cursor) and still
    # reflects both messages' max ts (20.0), tracked across the mid-turn fold.
    assert jobs_store.get_runtime_state(uid).get("last_replied_ts") == 20.0


def _status_events(uid):
    return jobs_store.list_status_events(uid, after_id=0, limit=100)


def test_final_drain_uncertain_delivery_surfaces_without_rewriting_completed_job(
    monkeypatch,
):
    """A generic effect can become uncertain only in the final drain.

    The final reply and job transition are already durable at that point, so
    the worker preserves ``completed`` while still emitting an error status
    and updating the hosted error hook with a sanitized stable code.
    """
    uid = "u_w_final_drain_uncertain"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _script_provider(monkeypatch, [_text_round("reply already durable")])
    monkeypatch.setattr(
        worker, "_write_encrypted_reply", lambda store, text: {"id": "r-final"}
    )

    apply_calls = {"n": 0}

    def apply_then_find_uncertain(user_id):
        apply_calls["n"] += 1
        if apply_calls["n"] == 1:
            return _apply_effects(user_id)  # immediate final-reply drain succeeds
        raise db.EffectDeliveryUncertainError(
            "raw target details must never reach the client"
        )

    recorded: list[tuple[str, str]] = []
    deps = _deps(
        messages=[{"id": "m1", "ts": 5.0, "role": "user", "content": "hi"}]
    )
    deps.apply_pending_effects = apply_then_find_uncertain
    deps.record_terminal_error = (
        lambda user_id, message: recorded.append((user_id, message))
    )

    result = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert result == "completed"
    assert _job_status(job_id)[0] == "completed"
    assert apply_calls["n"] == 2
    error_events = [e for e in _status_events(uid) if e["kind"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["job_id"] == job_id
    assert recorded[-1] == (uid, "effect_delivery_uncertain")
    assert all("raw target details" not in message for _uid, message in recorded)


def test_process_job_terminal_failure_emits_error_status_and_calls_callback(monkeypatch):
    """Task 3: a terminally-failed turn must surface, not just write invisible
    agent_jobs.last_error — an "error"-kind status event goes on the stream
    (iOS's poll surface) AND the injected TurnDeps.record_terminal_error
    callback fires with (user_id, message), so serve_worker can also patch
    hosted's last_runtime_error. The direct unit has no durable parent row, so
    the terminal reply sink acknowledges an empty frontier; linked encrypted
    failure bubbles are covered in test_v2_jobs_store."""
    uid = "u_w_terminalerr"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise RuntimeError("provider blew up")

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    settled = []
    monkeypatch.setattr(
        worker.db,
        "chat_settle_failed_input",
        lambda *args: settled.append(args) or True,
    )
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    recorded = []
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 5.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert write_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"

    events = _status_events(uid)
    error_events = [e for e in events if e["kind"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["job_id"] == job_id

    assert len(recorded) == 1
    rec_uid, rec_msg = recorded[0]
    assert rec_uid == uid
    assert rec_msg == "turn_failed:runtimeerror"
    assert row[1] == rec_msg
    assert "provider blew up" not in rec_msg
    assert settled == [(uid, "m1", rec_msg)]


def test_image_turn_unrelated_failure_keeps_original_failure_owner(monkeypatch):
    """An image must not make an unrelated internal failure look visual."""
    uid = "u_w_image_unrelated_failure"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise RuntimeError("unrelated internal failure")

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    recorded = []
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [{
            "id": "image-parent",
            "ts": 5.0,
            "role": "user",
            "content": "看看这张图",
            "has_image": True,
            "image_mime": "image/png",
        }],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        record_terminal_error=lambda user_id, message: recorded.append(
            (user_id, message)
        ),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert _job_status(job_id) == ("failed", "turn_failed:runtimeerror")
    assert recorded == [(uid, "turn_failed:runtimeerror")]


def test_image_turn_explicit_openrouter_rejection_keeps_vision_required_slug(
    monkeypatch,
):
    uid = "u_w_image_openrouter_unsupported"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError(
            "provider_http_404: No endpoints found that support image input",
            status_code=404,
        )

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [{
            "id": "image-parent",
            "ts": 5.0,
            "role": "user",
            "content": "What is in this image?",
            "has_image": True,
            "image_mime": "image/png",
        }],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
    ))

    assert status == "failed"
    with db.get_pool().connection() as conn:
        marker = conn.execute(
            "SELECT error_class FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert marker == ("vision_model_required",)


def test_process_job_terminal_failure_tolerates_missing_callback(monkeypatch):
    """record_terminal_error defaults to None (dependency boundary preserved for
    callers that don't supply it) — the failure path must not crash when it's
    absent, and must still emit the error status event."""
    uid = "u_w_terminalerr_nocb"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    deps = _deps(messages=[{"id": "m1", "ts": 5.0, "role": "user", "content": "hi"}])
    assert deps.record_terminal_error is None

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    events = _status_events(uid)
    assert any(e["kind"] == "error" for e in events)


def test_process_job_post_claim_kill_switch_failure_surfaces_terminal_error(monkeypatch):
    """An accepted chat can observe the live kill switch only after claim.
    When this worker owns the failed transition, that terminal result must be
    visible through both the status stream and hosted last_runtime_error hook."""
    uid = "u_w_post_claim_halted"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(worker.kill_switch, "turns_halted", lambda: True)
    recorded = []
    deps = worker.TurnDeps(
        read_messages=lambda uid_: (_ for _ in ()).throw(
            AssertionError("halted turn must not read input")),
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert _job_status(job_id) == ("failed", "turns_halted")
    error_events = [e for e in _status_events(uid) if e["kind"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["job_id"] == job_id
    assert recorded == [(uid, "turns_halted")]


def test_process_job_post_claim_runtime_mode_change_surfaces_terminal_error(monkeypatch):
    """A user rollback racing an already-claimed chat has the same visible
    terminal contract as other owned chat failures; it cannot fail silently."""
    uid = "u_w_post_claim_mode_changed"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(worker.kill_switch, "turns_halted", lambda: False)
    recorded = []
    deps = worker.TurnDeps(
        read_messages=lambda uid_: (_ for _ in ()).throw(
            AssertionError("rolled-back turn must not read input")),
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
        runtime_mode_enabled=lambda user_id: False,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert _job_status(job_id) == ("failed", "runtime_mode_changed")
    error_events = [e for e in _status_events(uid) if e["kind"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["job_id"] == job_id
    assert recorded == [(uid, "runtime_mode_changed")]


def test_run_turn_provider_resolve_failure_emits_error_status_and_callback(monkeypatch):
    """The early (pre-process_job) provider-resolve failure path in _run_turn is
    the SECOND terminal-failure site — it must surface the same way, using
    user_id only (no `store` binding is available there)."""
    uid = "u_w_terminalerr_early"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat", trace_id="m-provider")
    job = jobs_store.claim_next_job("w")

    def _boom(*a, **k):
        raise AssertionError("must not run past provider resolution failure")

    recorded = []
    settled = []
    monkeypatch.setattr(
        worker.db,
        "chat_settle_failed_input",
        lambda *args: settled.append(args) or True,
    )
    deps = worker.TurnDeps(
        read_messages=_boom,
        resolve_provider=lambda uid_: (None, {"error": "model_api_key_decrypt_failed"}),
        mint_enclave_token=_boom,
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
    )
    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "failed"
    events = _status_events(uid)
    error_events = [e for e in events if e["kind"] == "error"]
    assert len(error_events) == 1
    assert len(recorded) == 1
    rec_uid, rec_msg = recorded[0]
    assert rec_uid == uid
    assert rec_msg == "provider_unavailable"
    assert "model_api_key_decrypt_failed" not in rec_msg
    assert settled == [(uid, "m-provider", "provider_unavailable")]


def test_run_turn_maintenance_bypasses_provider_and_runtime_token(monkeypatch):
    uid = "u_w_maint_metadata_only"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance", reason="compaction")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(jobs_store, "get_summary_row", lambda _uid: None)

    async def _unsummarized(_uid, watermark_seq, **_kwargs):
        return worker._TAIL_KEEP + 1 if watermark_seq == 0 else 0

    monkeypatch.setattr(worker, "_unsummarized_count", _unsummarized)
    monkeypatch.setattr(
        worker.db,
        "chat_coverage_bounds_after_seq",
        lambda *_args, **_kwargs: (1, 1, 1),
    )
    monkeypatch.setattr(
        worker.db,
        "count_messages_after_seq",
        lambda *_args, **_kwargs: 1,
    )

    writes = []

    def _append(_uid, segment_text, **kwargs):
        writes.append((segment_text, kwargs))
        return True

    def _provider_boom(_uid):
        raise AssertionError("maintenance resolved provider")

    def _token_boom(_uid):
        raise AssertionError("maintenance minted runtime token")

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=_provider_boom,
        mint_enclave_token=_token_boom,
        append_summary_segment=_append,
        read_summary_frontier_metadata=lambda _uid: None,
        runtime_mode_enabled=lambda _uid: True,
    )

    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "completed"
    assert _job_status(job_id)[0] == "completed"
    assert writes == []


def test_run_turn_heartbeat_resolve_failure_is_silent_no_user_error(monkeypatch):
    """Weak wake lanes remain silent when provider resolution fails.

    Scheduled is intentionally excluded: a due reminder has a delivery
    obligation and gets its own visible failure result.
    """
    uid = "u_w_heartbeat_resolve_fail"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    def _boom(*a, **k):
        raise AssertionError("must not run past provider resolution failure")

    recorded = []
    deps = worker.TurnDeps(
        read_messages=_boom,
        resolve_provider=lambda uid_: (None, {"error": "model_api_key_decrypt_failed"}),
        mint_enclave_token=_boom,
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
    )
    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "failed"
    # SILENT: no "error"-kind status event, no record_terminal_error callback.
    assert [e for e in _status_events(uid) if e["kind"] == "error"] == []
    assert recorded == []


def test_run_turn_scheduled_resolve_failure_queues_visible_result(monkeypatch):
    uid = "u_w_scheduled_resolve_fail"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    job = jobs_store.claim_next_job("w")
    surfaced = []
    monkeypatch.setattr(
        worker,
        "_surface_terminal_error",
        lambda deps, user_id, failed_job_id, code: surfaced.append(
            (user_id, failed_job_id, code)
        ),
    )

    deps = worker.TurnDeps(
        read_messages=lambda *_args: [],
        resolve_provider=lambda _uid: (None, {"error": "key_decrypt_failed"}),
        mint_enclave_token=lambda *_args: "must-not-run",
    )
    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "failed"
    assert surfaced == [(uid, job_id, "provider_unavailable")]
    with db.get_pool().connection() as conn:
        marker = conn.execute(
            "SELECT error_code,reply_frontier_seq,reply_parent_message_id "
            "FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert marker == ("provider_unavailable", None, None)


# ------------------------------------------------------------------
# _run_turn: single BYOK decrypt per turn
# ------------------------------------------------------------------

def test_run_turn_resolves_provider_exactly_once_even_across_a_replan(monkeypatch):
    """Single-decrypt-per-turn invariant: resolve_provider (the BYOK decrypt) must
    be called exactly once per turn, even when the turn internally spans
    multiple tool-loop rounds (Task 7: the old outer replan is gone, replaced by
    per-round fold — see test_process_job_picks_up_concurrent_new_message_via_per_round_fold —
    but the single-decrypt invariant must hold across that too)."""
    uid = "u_w_singledecrypt"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    _script_provider(monkeypatch, [
        _tool_round(_tc("c1", "web_search", query="x")),
        _text_round("R"),
    ])
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    feed = iter([
        [{"id": "m1", "ts": 1.0, "role": "user", "content": "a"}],
        [{"id": "m1", "ts": 1.0, "role": "user", "content": "a"},
         {"id": "m2", "ts": 2.0, "role": "user", "content": "b"}],
    ])
    resolve_calls = {"n": 0}

    def _resolve(uid_):
        resolve_calls["n"] += 1
        return _BYOK, {}

    deps = worker.TurnDeps(
        read_messages=lambda uid: next(feed),
        resolve_provider=_resolve,
        mint_enclave_token=lambda uid: "rt",
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "completed"
    assert resolve_calls["n"] == 1


def test_run_turn_fails_when_provider_unresolved_and_never_enters_process_job(monkeypatch):
    """resolve_provider returning (None, {"error": ...}) must mark the job failed
    and never touch read_messages/provider execution (no wasted work, no filler)."""
    uid = "u_w_noprovider"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    def _boom(*a, **k):
        raise AssertionError("process_job must not run when provider resolution fails")

    deps = worker.TurnDeps(
        read_messages=_boom,
        resolve_provider=lambda uid: (None, {"error": "model_api_key_decrypt_failed"}),
        mint_enclave_token=_boom,
    )
    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "failed"
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert row[1] == "provider_unavailable"


# ------------------------------------------------------------------
# run_worker_loop: claim loop, graceful drain, per-slot fault isolation
# ------------------------------------------------------------------

def _ok_deps(rec, *, messages=None):
    if messages is None:
        messages = [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    return worker.TurnDeps(
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        apply_pending_effects=_apply_effects,
    )


def _patch_loop_boundaries(monkeypatch, rec, *, reply="model reply"):
    _script_provider(monkeypatch, [_text_round(reply)])
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: rec.setdefault("replies", []).append((store.user_id, text)) or {"id": "r1"})


def test_run_worker_loop_drains_pending_then_stops(monkeypatch):
    uid = "u_w_4"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    rec = {}
    _patch_loop_boundaries(monkeypatch, rec)
    stop = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(worker.run_worker_loop(
            "w-loop", max_workers=1, poll_interval=0.02, stop_event=stop, deps=_ok_deps(rec),
        ))
        for _ in range(200):
            with db.get_pool().connection() as conn:
                st = conn.execute(
                    "SELECT status FROM agent_jobs WHERE user_id=%s", (uid,)
                ).fetchone()
            if st and st[0] == "completed":
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_driver())
    assert rec.get("replies") == [(uid, "model reply")]


def test_run_worker_loop_survives_transient_claim_error(monkeypatch):
    """Robustness fix: a transient exception raised inside a slot's per-iteration
    work (claim_next_job here, standing in for any DB hiccup around claim/
    mark_running) must not propagate out of _slot_loop and crash run_worker_loop.
    The slot logs it and continues; the very next poll re-claims and completes
    the job normally."""
    uid = "u_w_5"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    rec = {}
    _patch_loop_boundaries(monkeypatch, rec)
    stop = asyncio.Event()
    calls = {"n": 0}
    orig_claim = jobs_store.claim_next_job

    def _flaky_claim(worker_id, lanes=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db error")
        return orig_claim(worker_id, lanes=lanes)

    monkeypatch.setattr(jobs_store, "claim_next_job", _flaky_claim)

    async def _driver():
        task = asyncio.create_task(worker.run_worker_loop(
            "w-loop2", max_workers=1, poll_interval=0.02, stop_event=stop, deps=_ok_deps(rec),
        ))
        for _ in range(300):
            with db.get_pool().connection() as conn:
                st = conn.execute(
                    "SELECT status FROM agent_jobs WHERE user_id=%s", (uid,)
                ).fetchone()
            if st and st[0] == "completed":
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(_driver())
    assert calls["n"] >= 2  # first raised, a later call actually claimed
    assert rec.get("replies") == [(uid, "model reply")]


def test_slot_exception_path_backs_off_on_persistent_failure(monkeypatch):
    """Verify that when claim_next_job persistently fails (e.g., DB outage),
    the exception handler waits poll_interval before retrying, rather than
    hot-looping and flooding logs/connection pool."""
    rec = {}
    stop = asyncio.Event()
    calls = {"n": 0}

    def _always_fail(worker_id, lanes=None):
        calls["n"] += 1
        raise RuntimeError("persistent db outage")

    monkeypatch.setattr(jobs_store, "claim_next_job", _always_fail)

    async def _driver():
        task = asyncio.create_task(worker.run_worker_loop(
            "w-backoff", max_workers=1, poll_interval=0.05, stop_event=stop, deps=_ok_deps(rec),
        ))
        # Let it run for ~0.1s (enough for 2-3 poll_interval cycles if backing off,
        # but would be ~20+ attempts if hot-looping).
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_driver())
    # With backoff: ~2-3 attempts in 0.1s (0.05s poll_interval + overhead).
    # Without backoff: would be 20+ attempts.
    assert calls["n"] <= 4, f"Too many attempts ({calls['n']}) suggests hot-loop without backoff"


def test_slot_recovery_failure_does_not_kill_slot(monkeypatch):
    stop = asyncio.Event()
    claim_calls = {"n": 0}

    def _claim(worker_id, lanes=None):
        claim_calls["n"] += 1
        if claim_calls["n"] == 1:
            return {"id": 1, "user_id": "u", "lane": "chat",
                    "claimed_by": worker_id}
        stop.set()
        return None

    async def _explode(_job, _deps):
        raise RuntimeError("turn exploded")

    monkeypatch.setattr(jobs_store, "claim_next_job", _claim)
    monkeypatch.setattr(jobs_store, "mark_failed", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db still down")))
    monkeypatch.setattr(worker, "_run_turn", _explode)

    asyncio.run(worker._slot_loop(
        "w-recovery", poll_interval=0.001, stop_event=stop, deps=_ok_deps({})))

    assert claim_calls["n"] >= 2


def test_run_worker_loop_propagates_unexpected_slot_exit(monkeypatch):
    async def _broken_slot(worker_id, **kwargs):
        raise AssertionError(f"broken {worker_id}")

    monkeypatch.setattr(worker, "_slot_loop", _broken_slot)
    stop = asyncio.Event()

    with pytest.raises(AssertionError, match="broken w-supervise#0"):
        asyncio.run(worker.run_worker_loop(
            "w-supervise", max_workers=1, poll_interval=0.01,
            stop_event=stop, deps=_ok_deps({})))

    assert stop.is_set()


def test_bounded_gates_exist():
    assert isinstance(worker.MAX_READ_ACTION_PARALLELISM, int)
    assert not hasattr(worker, "ENCLAVE_SEMAPHORE")
    assert isinstance(worker._new_direct_enclave_gate(), asyncio.Semaphore)


def test_retired_max_workers_env_does_not_affect_worker_import():
    backend = str(Path(__file__).parent.parent / "backend")
    env = os.environ.copy()
    env["FEEDLING_V2_MAX_WORKERS"] = "retired-invalid-value"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, {backend!r}); "
                "from model_api_runtime.v2 import worker; "
                "print(worker._capture_provider_guard_pool_size())"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "nope"])
def test_positive_worker_integer_settings_fail_closed(monkeypatch, raw):
    monkeypatch.setenv("TEST_V2_POSITIVE_INT", raw)
    with pytest.raises(RuntimeError, match="positive integer"):
        worker._positive_int_env("TEST_V2_POSITIVE_INT", "1")


def test_positive_worker_integer_setting_accepts_value(monkeypatch):
    monkeypatch.setenv("TEST_V2_POSITIVE_INT", "3")
    assert worker._positive_int_env("TEST_V2_POSITIVE_INT", "1") == 3


# ------------------------------------------------------------------
# D3 Task 5: lane reservation wiring — _reserved_lane_slots() picks the
# per-slot lane allowlist that run_worker_loop hands to each _slot_loop.
# ------------------------------------------------------------------

def test_reserved_lane_slots_explicit_reserved_count():
    assert worker._reserved_lane_slots(4, 2) == [
        {"chat", "manual_wake"}, {"chat", "manual_wake"}, None, None,
    ]


def test_reserved_lane_slots_default_is_half_rounded_down():
    result = worker._reserved_lane_slots(4, None)
    assert result == [{"chat", "manual_wake"}, {"chat", "manual_wake"}, None, None]


def test_reserved_lane_slots_single_worker_stays_unrestricted():
    assert worker._reserved_lane_slots(1, None) == [None]


def test_reserved_lane_slots_reserved_clamped_to_leave_generic_worker():
    assert worker._reserved_lane_slots(3, 99) == [
        {"chat", "manual_wake"}, {"chat", "manual_wake"}, None,
    ]


def test_reserved_lane_slots_reserved_zero_means_all_unrestricted():
    assert worker._reserved_lane_slots(3, 0) == [None, None, None]


# ------------------------------------------------------------------
# Worker dispatch by lane — legacy plaintext tail callbacks must not revive
# semantic prompt construction or semantic compaction scheduling.
# ------------------------------------------------------------------

def test_process_job_legacy_tail_cannot_drive_prompt_or_compaction(monkeypatch):
    """A legacy plaintext tail cannot revive semantic prompt/compaction logic."""
    uid = "u_w_compact_enqueue"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    big_tail = [
        {"id": f"m{i}", "ts": float(i), "role": "user" if i % 2 == 0 else "openclaw", "content": f"msg {i}"}
        for i in range(worker._TAIL_BUDGET + 5)
    ]
    calls = _script_provider(monkeypatch, [_text_round("REPLY")])

    enqueue_calls = []
    orig_enqueue = jobs_store.enqueue_job

    def _spy_enqueue(user_id_, lane, *, reason=None, **k):
        enqueue_calls.append((user_id_, lane, reason))
        return orig_enqueue(user_id_, lane, reason=reason, **k)

    monkeypatch.setattr(jobs_store, "enqueue_job", _spy_enqueue)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: big_tail,
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == 1
    joined = " ".join(str(m.get("content", "")) for m in calls[0]["messages"])
    assert "prior summary" not in joined
    assert "msg 0" not in joined
    assert (uid, "maintenance", "compaction") not in enqueue_calls


def test_process_job_reply_skips_compaction_enqueue_when_under_budget(monkeypatch):
    """Symmetric negative case: a short tail (under `_TAIL_BUDGET`) must NOT
    trigger a maintenance enqueue."""
    uid = "u_w_compact_skip"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})
    _script_provider(monkeypatch, [_text_round("REPLY")])

    enqueue_calls = []
    orig_enqueue = jobs_store.enqueue_job

    def _spy_enqueue(user_id_, lane, *, reason=None, **k):
        enqueue_calls.append((user_id_, lane, reason))
        return orig_enqueue(user_id_, lane, reason=reason, **k)

    monkeypatch.setattr(jobs_store, "enqueue_job", _spy_enqueue)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert enqueue_calls == []


def test_chat_uses_recent_profile_context_without_compact_dependencies(monkeypatch):
    uid = "u_w_recent_profile_chat"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("conversation compact dependency was touched")

    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 20)
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda *a, **k: _FakeCapResult({}),
    )
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda store, text: {"id": "r"},
    )
    calls = _script_provider(monkeypatch, [_text_round("REPLY")])
    recent_calls = []
    recent_rows = [
        {
            "id": "m10",
            "seq": 10,
            "ts": 10.0,
            "role": "user",
            "content": "older user turn",
            "_genuine_user": True,
        },
        {
            "id": "m11",
            "seq": 11,
            "ts": 11.0,
            "role": "assistant",
            "content": "older assistant reply",
            "_genuine_user": False,
        },
        {
            "id": "m20",
            "seq": 20,
            "ts": 20.0,
            "role": "user",
            "content": "active user turn",
            "_genuine_user": True,
        },
    ]
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [recent_rows[-1]],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        read_summary_with_seq=forbidden,
        read_tail_after_seq=forbidden,
        read_recent_turns=lambda user_id, max_turns, row_cap, **kwargs: (
            recent_calls.append((user_id, max_turns, row_cap, kwargs))
            or {"rows": recent_rows, "source_truncated": False}
        ),
        select_profile_for_turn=lambda _uid, **_kwargs: (
            worker.v2_profile_store.ProfilePromptSelection(
                memory="remembered relationship",
                user="preferred interaction style",
                state="last_good",
            )
        ),
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert recent_calls == [
        (
            uid,
            40,
            worker._RECENT_TURN_ROW_CAP,
            {"through_seq": 20},
        )
    ]
    rendered = "\n".join(
        str(message.get("content") or "") for message in calls[0]["messages"]
    )
    assert "remembered relationship" in rendered
    assert "preferred interaction style" in rendered
    assert "older user turn" in rendered
    assert "older assistant reply" in rendered
    assert "active user turn" in rendered


def test_semantic_compaction_api_is_removed():
    assert not hasattr(v2_compaction, "compact")


# ------------------------------------------------------------------
# spec B5 (Hosted Runtime V2 PR B, Task 8): the old per-call metric callback is
# superseded by a per-job `TurnMetrics` whole-turn accumulator that upserts
# ONE idempotent `v2_turn_metrics` row per job_id at the turn's single terminal
# point — success AND every mark_failed path (not just responder success).
# These tests assert the real DB row rather than a compatibility callback.
# ------------------------------------------------------------------

def test_process_job_records_whole_turn_metric_after_successful_respond(monkeypatch):
    uid = "u_w_turnmetric"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    _script_provider(monkeypatch, [_text_round("REPLY", prompt_tokens=42, completion_tokens=8)])

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT user_id, lane, prompt_tokens, completion_tokens, model_calls, failed, status, latency_ms "
            "FROM v2_turn_metrics WHERE job_id=%s", (job_id,)).fetchone()
    assert row is not None
    assert row[0] == uid
    assert row[1] == "chat"
    assert row[2] == 42
    assert row[3] == 8
    assert row[4] == 1  # exactly one round-trip through the unified tool loop
    assert row[5] is False
    assert row[6] == "ok"
    assert row[7] >= 0


def test_process_job_records_failed_whole_turn_metric_on_provider_error(monkeypatch):
    """Inverted from the old per-call semantics: a failed turn (ResponderError)
    now DOES get a whole-turn metric row — spec B5 explicitly covers failed
    turns, not just successful ones (that's the point of a turn-level failure
    rate the load-test gate can read)."""
    uid = "u_w_turnmetric_err"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    _script_provider(monkeypatch, [_text_round("")])  # empty terminal text -> no-filler failure

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT failed, status FROM v2_turn_metrics WHERE job_id=%s", (job_id,)).fetchone()
    assert row is not None
    assert row[0] is True
    assert row[1] == "turn_failed:empty_reply"


def test_run_wake_records_whole_turn_metric_on_success(monkeypatch):
    """PR B review finding: `_run_wake` makes a real provider BYOK call (this IS
    the lane that burns idle-user tokens on heartbeat/scheduled wakes) but a
    SUCCESSFUL wake used to never flush a `v2_turn_metrics` row at all —
    `process_job` returns early at lane dispatch (`return await
    _run_wake(...)`), so its own success-path flush was never reached, and
    `_run_wake` itself never called `tm.add_call`/`tm.flush(failed=False, ...)`.
    Mirrors the chat-lane usage plumbing in
    `test_process_job_records_whole_turn_metric_after_successful_respond` above
    — since Task 8, `add_usage=tm.add_call` is wired straight into
    `tool_loop.run_tool_loop`, the same mechanism for both lanes, so a
    successful wake now records real prompt/completion tokens, not just a bare
    model_calls count."""
    uid = "u_w_wake_turnmetric"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    _script_provider(monkeypatch, [
        _text_round("hey, thinking of you", prompt_tokens=17, completion_tokens=4)])
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 1)
    monkeypatch.setattr(worker.db, "chat_seqs_after_seq", lambda *_a, **_k: [1])

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        has_genuine_user_history=lambda _uid: True,
        read_summary_with_seq=lambda _uid: ("", 0.0, 0, 0),
        read_tail_after_seq=lambda *_a, **_k: [
            {"id": "m1", "seq": 1, "ts": 1.0, "role": "user", "content": "hi"}
        ],
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT user_id, lane, prompt_tokens, completion_tokens, model_calls, failed, status "
            "FROM v2_turn_metrics WHERE job_id=%s", (job_id,)).fetchone()
    assert row is not None
    assert row[0] == uid
    assert row[1] == "heartbeat"
    assert row[2] == 17          # real usage, surfaced via the scripted round's usage
    assert row[3] == 4
    assert row[4] == 1           # exactly one model call
    assert row[5] is False
    assert row[6] == "ok"


def test_run_wake_weak_wake_still_records_whole_turn_metric_with_call_counted(monkeypatch):
    """An empty terminal reply is a SUCCESSFUL weak-wake sleep, but it's still a
    REAL, billed provider call — the metric row must count it, not silently
    drop to model_calls=0 the way a pre-fix failed wake used to."""
    uid = "u_w_wake_turnmetric_weak"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    _script_provider(monkeypatch, [_text_round("", prompt_tokens=9, completion_tokens=0)])
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 1)
    monkeypatch.setattr(worker.db, "chat_seqs_after_seq", lambda *_a, **_k: [1])

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        has_genuine_user_history=lambda _uid: True,
        read_summary_with_seq=lambda _uid: ("", 0.0, 0, 0),
        read_tail_after_seq=lambda *_a, **_k: [
            {"id": "m1", "seq": 1, "ts": 1.0, "role": "user", "content": "hi"}
        ],
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT prompt_tokens, model_calls, failed, status "
            "FROM v2_turn_metrics WHERE job_id=%s", (job_id,)).fetchone()
    assert row is not None
    assert row[0] == 9
    assert row[1] == 1
    assert row[2] is False
    assert row[3] == "ok"


def test_retired_maintenance_records_whole_turn_metric(monkeypatch):
    """The content-free tombstone records success without any model usage."""
    uid = "u_w_compaction_turnmetric"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(jobs_store, "get_summary_row", lambda _uid: None)

    async def _unsummarized(_uid, _watermark_seq):
        return worker._TAIL_KEEP + 1

    monkeypatch.setattr(worker, "_unsummarized_count", _unsummarized)
    monkeypatch.setattr(
        worker.db,
        "chat_coverage_bounds_after_seq",
        lambda *_args, **_kwargs: (1, 1, 1),
    )
    monkeypatch.setattr(
        worker.db,
        "count_messages_after_seq",
        lambda *_args, **_kwargs: 1,
    )
    writes = {"n": 0}

    def _append(*_args, **_kwargs):
        writes["n"] += 1
        return True

    def _provider_boom(_uid):
        raise AssertionError("maintenance resolved provider")

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=_provider_boom,
        mint_enclave_token=lambda _uid: (_ for _ in ()).throw(
            AssertionError("maintenance minted runtime token")
        ),
        append_summary_segment=_append,
        runtime_mode_enabled=lambda _uid: True,
    )

    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "completed"
    assert writes["n"] == 0
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT lane, prompt_tokens, completion_tokens, model_calls, failed, status, "
            "cache_read_tokens, cache_write_tokens, cache_miss_tokens, "
            "usage_reported_calls, cache_reported_calls, provider, model, "
            "cache_route_fingerprint "
            "FROM v2_turn_metrics WHERE job_id=%s", (job_id,)).fetchone()
    assert row is not None
    assert row[0] == "maintenance"
    assert row[1] is None and row[2] is None
    assert row[3] == 0
    assert row[4] is False
    assert row[5] == "maintenance_retired"
    assert row[6:11] == (None, None, None, 0, 0)
    assert row[11:] == (None, None, None)


def test_turn_metrics_keep_unknown_usage_nullable_and_count_coverage(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        jobs_store,
        "record_whole_turn_metric",
        lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs),
    )
    tm = worker.TurnMetrics(job_id=123, user_id="u", lane="chat")
    tm.bind_provider(_BYOK)
    tm.add_call(None)
    tm.add_call({
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": None,
        "cache_miss_tokens": 100,
    })
    tm.add_call({
        "prompt_tokens": 50,
        "completion_tokens": None,
        "cache_read_tokens": 40,
        "cache_write_tokens": None,
        "cache_miss_tokens": 10,
    })

    tm.flush(failed=False, status="ok")

    assert captured["args"] == (123, "u", "chat")
    assert captured["kwargs"]["prompt_tokens"] == 150
    assert captured["kwargs"]["completion_tokens"] == 10
    assert captured["kwargs"]["cache_read_tokens"] == 40
    assert captured["kwargs"]["cache_write_tokens"] is None
    assert captured["kwargs"]["cache_miss_tokens"] == 110
    assert captured["kwargs"]["usage_reported_calls"] == 2
    assert captured["kwargs"]["cache_reported_calls"] == 2
    assert captured["kwargs"]["provider"] == "anthropic"
    assert captured["kwargs"]["model"] == "claude-sonnet-4-test"
    assert captured["kwargs"]["cache_route_fingerprint"] == "feedling-v2-route-test"


def test_turn_metrics_ignore_malformed_or_bigint_overflow_usage():
    tm = worker.TurnMetrics(job_id=123, user_id="u", lane="chat")
    tm.add_call({
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "cache_read_tokens": 4,
    })
    tm.add_call({
        "prompt_tokens": float("inf"),
        "completion_tokens": (1 << 63),
        "cache_read_tokens": True,
    })

    assert tm.prompt_tokens == 10
    assert tm.completion_tokens == 2
    assert tm.cache_read_tokens == 4


# ------------------------------------------------------------------
# Unified-loop regression coverage: one _TURN_MAX_LLM_CALLS budget spans the
# complete chronological provider-native turn.
# ------------------------------------------------------------------

def test_chat_turn_always_replies_even_when_model_only_calls_tools(monkeypatch):
    """BUG-4 structural successor (Task 7): in the old json_planner pipeline, a
    plan that never asked for `final_response` could silently swallow the turn
    (fixed back then by forcing `wants_reply=True` for the chat lane). In the
    unified tool loop this can no longer happen BY CONSTRUCTION:
    `tool_loop.run_tool_loop`'s last round keeps referenced schemas but sets
    `tool_choice=none`, so a model that just keeps calling tools every round
    still gets forced to a real reply at the `_TURN_MAX_LLM_CALLS`
    budget — never a placeholder, never a silent swallow."""
    uid = "u_w_loop_bug4"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    n = worker._TURN_MAX_LLM_CALLS
    script = [_tool_round(_tc(f"c{i}", "memory_index")) for i in range(n - 1)]
    script.append(_text_round("MODEL REPLY"))
    calls = _script_provider(monkeypatch, script)

    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}])
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r1"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == n            # ran to the budget, no early silent stop
    assert {spec.name for spec in calls[-1]["tools"]} == {"memory_index"}
    assert calls[-1]["tool_choice"] == "none"
    assert written.get("text") == "MODEL REPLY"       # forced reply at the cap — no silent swallow


def test_second_round_receives_first_round_native_tool_exchange(monkeypatch):
    """Successor of the old planner-prior_action_results test: in the unified
    loop, a read tool's result is fed back as grounding context to the NEXT
    round's `build_messages` call (Global Constraints "reads run parallel,
    content fed back"), not replayed as a wire-level tool round-trip."""
    uid = "u_w_loop_prior"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(
        cap_registry, "run_capability",
        lambda action_type, store, **k: _FakeCapResult({"marker": "OBSERVED_MEMORY_INDEX"}))
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc("c1", "memory_index")),
        _text_round("R"),
    ])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}])
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == 2
    assert not any(isinstance(m, ToolExchange) for m in calls[0]["messages"])
    exchanges = [m for m in calls[1]["messages"] if isinstance(m, ToolExchange)]
    assert len(exchanges) == 1
    assert "OBSERVED_MEMORY_INDEX" in " ".join(r.content for r in exchanges[0].results)


def test_turn_llm_call_budget_binds_even_with_continuous_new_input(monkeypatch):
    """The `_TURN_MAX_LLM_CALLS` budget (spec §6: bounds runaway BYOK spend) must
    hold even when new user messages keep arriving mid-turn (the per-round
    fold — Task 6/7's replacement for the old outer replan mechanism) and the
    model keeps asking for tools every round: the turn must still terminate at
    EXACTLY the budget, forced to plain text on the last round
    (`tool_choice=none`), never running away past it."""
    uid = "u_w_loop_budget"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    n = worker._TURN_MAX_LLM_CALLS
    script = [_tool_round(_tc(f"c{i}", "memory_index")) for i in range(n - 1)]
    script.append(_text_round("R"))
    calls = _script_provider(monkeypatch, script)

    # A new message keeps "arriving" — every read returns one more row than the
    # last, so the fold ahead of every round after the first sees something new,
    # mirroring a talkative user who never lets the turn go quiet.
    counter = {"n": 0}

    def _read_messages(uid_):
        counter["n"] += 1
        return [{"id": f"m{i}", "ts": float(i), "role": "user", "content": f"msg{i}"}
                for i in range(1, counter["n"] + 1)]

    deps = worker.TurnDeps(
        read_messages=_read_messages,
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        apply_pending_effects=_apply_effects,
    )
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == n
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT model_calls FROM v2_turn_metrics WHERE job_id=%s", (job_id,)).fetchone()
    assert row[0] == n


def test_unhandled_lane_never_writes_a_bubble_and_fails_loudly_in_the_db(monkeypatch):
    """An unhandled lane must NOT take the chat path (no chat bubble, no user-visible error
    chip) and must fail loudly in the DB with `unhandled_lane:<lane>`.

    Task 3 note: this test used to enqueue `capture`, then `screen_watch` — both are now
    real handled lanes (memory extraction via `_run_extraction`; screen_watch wake via
    `_run_wake`). To keep exercising the genuine unhandled branch we use a lane that is NOT
    in `jobs_store.LANES`. `enqueue_job` validates against LANES, so we INSERT the bogus-lane
    row directly (chosen over monkeypatching LANES, so the Python-side guard stays real for
    every other test)."""
    uid = "u_w_unhandled_lane"
    conftest.seed_user(uid)
    _reset(uid)
    bogus_lane = "bogus_lane"  # not in jobs_store.LANES → genuinely unregistered
    assert bogus_lane not in jobs_store.LANES
    with db.get_pool().connection() as conn:
        job_id = conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, priority) "
            "VALUES (%s, %s, 'pending', 0) RETURNING id", (uid, bogus_lane)).fetchone()[0]
    job = jobs_store.claim_next_job("w")

    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r"})
    emitted = []
    monkeypatch.setattr(worker, "_emit_status",
                        lambda uid_, jid, status, **kw: emitted.append(status))

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert written == {}                       # no chat bubble
    assert "error" not in emitted              # no user-visible error chip
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert row[1].startswith("unhandled_lane:")


def test_chat_still_works_and_takes_the_chat_path(monkeypatch):
    """Guard against the dispatch fix accidentally starving the real chat turn."""
    uid = "u_w_chat_still_works"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    _script_provider(monkeypatch, [_text_round("REPLY")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert written == {"text": "REPLY"}


# ------------------------------------------------------------------
# Hosted Runtime V2 PR D Task 2: slot-driven progress_cb. Replaces the old
# free-running progress ticker (turn_child._progress_ticker) with a signal
# tied to REAL slot activity — claim, turn completion, and idle-poll wake —
# so the Task 3 watchdog can tell "event loop healthy, nothing to claim"
# apart from "all slots wedged mid-turn, event loop still spinning". Fully
# mocked (no real DB claim/turn), mirroring test_slot_recovery_failure_does_
# not_kill_slot's direct-_slot_loop style.
# ------------------------------------------------------------------

def test_slot_loop_progress_cb_called_on_claim_and_turn_completion(monkeypatch):
    stop = asyncio.Event()
    claim_calls = {"n": 0}
    turn_calls = {"n": 0}

    def _claim(worker_id, lanes=None):
        claim_calls["n"] += 1
        if claim_calls["n"] == 1:
            return {"id": 1, "user_id": "u", "lane": "chat", "claimed_by": worker_id}
        stop.set()
        return None

    async def _run_turn_stub(_job, _deps):
        turn_calls["n"] += 1

    monkeypatch.setattr(jobs_store, "claim_next_job", _claim)
    monkeypatch.setattr(worker, "_run_turn", _run_turn_stub)

    events = []

    asyncio.run(worker._slot_loop(
        "w-progress", poll_interval=0.001, stop_event=stop, deps=_ok_deps({}),
        slot_id="foreground-7", slot_generation="g7", progress_cb=events.append))

    assert claim_calls["n"] >= 2
    assert turn_calls["n"] == 1
    # (a) claim + (b) turn-completion signals, both tagged with this slot's id —
    # plus (c) an idle-poll signal once claim_next_job starts returning None.
    assert len(events) >= 2
    assert all(event.slot_id == "foreground-7" for event in events)
    assert all(event.slot_generation == "g7" for event in events)
    assert events[0].active_job == worker.slot_protocol.ActiveJobIdentity(
        1, "chat", "w-progress"
    )
    # (a) the claim signal must carry a non-None turn_start (hard-timeout fix);
    # (b)/(c) idle signals must carry None.
    claim_events = [event.turn_start for event in events if event.turn_start is not None]
    idle_events = [event.turn_start for event in events if event.turn_start is None]
    assert len(claim_events) == 2, "claimed and durable-completion share one turn"
    assert isinstance(claim_events[0], float) and claim_events[0] > 0
    assert len(idle_events) >= 1, "turn-completion and/or idle-poll signals report turn_start=None"


def test_slot_loop_progress_cb_exception_does_not_crash_loop(monkeypatch):
    """progress_cb must be cheap and never allowed to blow up the slot loop —
    a broken telemetry hook can't be allowed to take down job processing."""
    stop = asyncio.Event()
    calls = {"n": 0}

    def _claim(worker_id, lanes=None):
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()
        return None

    monkeypatch.setattr(jobs_store, "claim_next_job", _claim)

    def _boom(_progress):
        raise RuntimeError("boom")

    # Must not raise despite progress_cb always raising.
    asyncio.run(worker._slot_loop(
        "w-progress-boom", poll_interval=0.001, stop_event=stop, deps=_ok_deps({}),
        slot_id="foreground-0", slot_generation="g0", progress_cb=_boom))

    assert calls["n"] >= 3


def test_run_worker_loop_threads_progress_cb_with_slot_index(monkeypatch):
    """run_worker_loop must hand each _slot_loop its own index (slot_id) plus
    the same progress_cb — this is the wiring the turn_child progress pipe
    (and the eventual Task 3 watchdog) relies on to attribute a heartbeat to a
    specific slot."""
    stop = asyncio.Event()

    async def _fake_slot_loop(worker_id, *, poll_interval, stop_event, deps,
                               wake_event=None, lanes=None, slot_id="slot-0",
                               slot_generation="g0", progress_cb=None):
        if progress_cb is not None:
            progress_cb(worker.slot_protocol.SlotProgress(
                slot_id, slot_generation, 1.0, None, "idle", None))
        stop_event.set()

    monkeypatch.setattr(worker, "_slot_loop", _fake_slot_loop)
    events = []
    asyncio.run(worker.run_worker_loop(
        "w-thread", max_workers=2, poll_interval=0.01, stop_event=stop, deps=_ok_deps({}),
        progress_cb=events.append))
    assert sorted(event.slot_id for event in events) == ["slot-0", "slot-1"]


def test_slot_loop_progress_cb_reports_turn_start_at_claim_and_none_after_completion(monkeypatch):
    """Hard-timeout fix: `progress_cb`'s second positional arg must be a real
    (non-None) `time.monotonic()` value at the moment a job is claimed and the
    slot is about to enter `_run_turn` — that's the only signal
    `ChildSupervisor.poll_liveness()` has for `current_turn_age_sec`, the field
    that makes D2 watchdog clause (c) (per-turn hard timeout) live instead of
    dead code. Must go back to None immediately after the turn completes."""
    stop = asyncio.Event()
    claim_calls = {"n": 0}

    def _claim(worker_id, lanes=None):
        claim_calls["n"] += 1
        if claim_calls["n"] == 1:
            return {"id": 1, "user_id": "u", "lane": "chat", "claimed_by": worker_id}
        stop.set()
        return None

    async def _run_turn_stub(_job, _deps):
        pass

    monkeypatch.setattr(jobs_store, "claim_next_job", _claim)
    monkeypatch.setattr(worker, "_run_turn", _run_turn_stub)

    calls = []  # list of (slot_id, turn_start)

    asyncio.run(worker._slot_loop(
        "w-turnstart", poll_interval=0.001, stop_event=stop, deps=_ok_deps({}),
        slot_id="foreground-3", slot_generation="g3", progress_cb=calls.append))

    # First call: claim just happened, about to run the turn -> non-None turn_start.
    first_slot_id, first_turn_start = calls[0].slot_id, calls[0].turn_start
    assert first_slot_id == "foreground-3"
    assert calls[0].stage == "claimed"
    assert first_turn_start is not None
    assert isinstance(first_turn_start, float) and first_turn_start > 0

    # Second call: the turn just completed -> back to idle (turn_start=None).
    second_slot_id, second_turn_start = calls[1].slot_id, calls[1].turn_start
    assert second_slot_id == "foreground-3"
    assert calls[1].stage == "durable_completion"
    assert second_turn_start == first_turn_start
    assert calls[2].stage == "idle"
    assert calls[2].active_job is None


def test_slot_loop_in_turn_boundaries_refresh_same_turn_stall_clock(monkeypatch):
    """Deep helpers report progress through the slot-local context callback.

    Every in-turn boundary must carry the original turn_start (absolute clock
    stays fixed) while producing a fresh pipe message (stall clock refreshes).
    """
    stop = asyncio.Event()
    claims = {"n": 0}

    def _claim(worker_id, lanes=None):
        claims["n"] += 1
        if claims["n"] == 1:
            return {"id": 2, "user_id": "u", "lane": "chat",
                    "claimed_by": worker_id}
        stop.set()
        return None

    async def _run_turn_stub(_job, _deps):
        worker._report_turn_progress("provider_complete")
        worker._report_turn_progress("prompt_catchup_batch_complete")

    monkeypatch.setattr(jobs_store, "claim_next_job", _claim)
    monkeypatch.setattr(worker, "_run_turn", _run_turn_stub)
    events = []

    asyncio.run(worker._slot_loop(
        "w-in-turn-progress", poll_interval=0.001, stop_event=stop,
        deps=_ok_deps({}), slot_id="foreground-4", slot_generation="g4",
        progress_cb=events.append))

    active = [event for event in events if event.turn_start is not None]
    assert len(active) == 4  # claim, two boundaries, durable completion
    assert {event.slot_id for event in active} == {"foreground-4"}
    assert len({event.turn_start for event in active}) == 1
    assert {event.active_job for event in active} == {
        worker.slot_protocol.ActiveJobIdentity(2, "chat", "w-in-turn-progress")
    }
    assert [event.stage for event in active] == [
        "claimed",
        "provider_complete",
        "prompt_catchup_batch_complete",
        "durable_completion",
    ]
    assert any(event.turn_start is None for event in events)


def test_active_job_lease_keeper_renews_until_stopped(monkeypatch):
    calls = []

    def _renew(job_id, claimed_by, *, ttl_sec):
        calls.append((job_id, claimed_by, ttl_sec))
        return True

    monkeypatch.setattr(jobs_store, "renew_job_lease", _renew)

    async def _driver():
        stop = asyncio.Event()
        task = asyncio.create_task(worker._keep_active_job_lease(
            "job-long", "worker-long", stop, interval=0.005))
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.002)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert len(calls) >= 2
    assert all(call == ("job-long", "worker-long", jobs_store.RUNNING_TTL_SEC)
               for call in calls)


def test_metadata_compaction_refreshes_turn_progress(monkeypatch):
    progress = []
    monkeypatch.setattr(worker, "_report_turn_progress", progress.append)
    uid = "u_metadata_progress"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance")
    job = jobs_store.claim_next_job("worker-progress")
    monkeypatch.setattr(jobs_store, "get_summary_row", lambda _uid: None)

    async def _unsummarized(_uid, _watermark_seq):
        return worker._TAIL_KEEP + 1

    monkeypatch.setattr(worker, "_unsummarized_count", _unsummarized)
    monkeypatch.setattr(
        worker.db,
        "chat_coverage_bounds_after_seq",
        lambda *_args, **_kwargs: (1, 1, 1),
    )
    monkeypatch.setattr(
        worker.db,
        "count_messages_after_seq",
        lambda *_args, **_kwargs: 1,
    )
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        append_summary_segment=lambda *_args, **_kwargs: True,
    )

    result = asyncio.run(
        worker._run_compaction(
            job_id,
            uid,
            deps,
            None,
            claimed_by=job["claimed_by"],
        )
    )

    assert result == "completed"
    assert progress == [
        "deterministic_compaction_batch_start",
        "deterministic_compaction_batch_complete",
    ]


def test_chat_lane_still_takes_the_chat_path(monkeypatch):
    """Regression guard for the BUG-2 dispatch fix: the `lane != "chat"` bail-out must not
    starve the real interactive chat turn. Pairs with
    test_unhandled_lane_never_writes_a_bubble_and_fails_loudly_in_the_db."""
    uid = "u_w_chat_still_works"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    _script_provider(monkeypatch, [_text_round("REPLY")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert written == {"text": "REPLY"}


# --- 真实模型自称块进入回合 system 位（prod usr_6bb6…，2026-07-25）----------
# BYOK 路由切换后 agent 仍照记忆自称旧模型，根因是 V2 的 system prompt 从不携带
# 真实 provider/model。工厂函数级语义在 tests/test_v2_model_identity.py；这里锁住
# 两条面向用户的 lane 确实把当回合的 provider_config 传了下去。

_THIRD_PARTY = provider_client.ProviderConfig(
    provider="deepseek", model="deepseek-chat", api_key="sk-user-byok",
    base_url="https://api.deepseek.com")


def test_chat_turn_system_prompt_states_the_live_third_party_model(monkeypatch):
    uid = "u_w_identity_chat"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    calls = _script_provider(monkeypatch, [_text_round("REPLY")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "你是什么模型"}],
                 provider=(_THIRD_PARTY, {}))
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_THIRD_PARTY, api_key=None, runtime_token="rt"))

    assert status == "completed"
    system = calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "deepseek-chat" in system["content"]


def test_wake_turn_system_prompt_states_the_live_third_party_model(monkeypatch):
    uid = "u_w_identity_wake"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    calls = _script_provider(monkeypatch, [_text_round("hey")])
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 1)
    monkeypatch.setattr(worker.db, "chat_seqs_after_seq", lambda *_a, **_k: [1])
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_THIRD_PARTY, {}),
        mint_enclave_token=lambda uid_: "rt",
        has_genuine_user_history=lambda _uid: True,
        read_summary_with_seq=lambda _uid: ("", 0.0, 0, 0),
        read_tail_after_seq=lambda *_a, **_k: [
            {"id": "m1", "seq": 1, "ts": 1.0, "role": "user", "content": "hi"}
        ],
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_THIRD_PARTY, api_key=None, runtime_token="rt"))

    assert status == "completed"
    system = calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "deepseek-chat" in system["content"]


def test_official_route_chat_turn_also_pins_the_exact_model_id(monkeypatch):
    """官方直连同样注入：V2 没有 CLI 壳子，不注入时模型会报错版本（实测 anthropic
    自称 "Claude 3.5 Sonnet"、openai 自称 "GPT-5"）。"""
    uid = "u_w_identity_official"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    calls = _script_provider(monkeypatch, [_text_round("REPLY")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    system = calls[0]["messages"][0]["content"]
    assert _BYOK.model in system        # claude-sonnet-4-test，钉死精确型号
    assert "官方直连" in system          # 走官方文案，不是第三方那套
