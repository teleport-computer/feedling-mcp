"""Hosted Runtime V2 PR C — Task 10: the P0 / @sxysun-gates-deploy acceptance subset
for the unified provider-native tool loop (spec 2026-07-13 PR-C plan, "### Task 10").

Each test below asserts a STRONG live-behavior property of the real loop (`tool_loop.
run_tool_loop` + `worker.process_job`/`executor.dispatch_tool_calls`), not a weak proxy:

  1. weak-model plain text     -> exactly 1 provider call and exactly 1 bubble.
  2. reply + web_search        -> intermediate bubble written BEFORE the terminal one
                                   (real chat_messages seq order), and a turn RE-DRIVE
                                   that re-enqueues the SAME effect_id produces NO
                                   duplicate bubble (PR A's ON CONFLICT DO NOTHING).
  3. mid-turn fold, no restart -> a second user message (B) becomes visible via
                                   `deps.read_messages_since` DURING round-1's tool
                                   dispatch; round-2's `build_messages` sees A + B +
                                   round-1's tool_results, no `asyncio.sleep` (no
                                   debounce), and the loop does not restart (round
                                   counter monotonic, exactly 2 provider calls).
  4. malicious-page write refusal -> `turn_authorization=False` (a purely-external
                                   round) refuses every WRITE_ACTIONS tool_call
                                   deterministically: no effect row lands in
                                   `db.effect_pending`, the underlying capability write
                                   fn is never invoked.
  5. 4 providers live loop      -> for each of the 4 wires (anthropic/gemini/
                                   openai-chat/openai-responses), a canned wire body is
                                   decoded via PR B's real codec (`provider_client.
                                   _decode_tool_calls_<wire>`) into `ToolCall`s, fed
                                   through the real `tool_loop.run_tool_loop` +
                                   `executor.dispatch_tool_calls`, and the resulting
                                   `ToolResult`s are proven to carry the RIGHT content
                                   for the RIGHT call_id (no cross-wire id/content
                                   mixups).

Style mirrors tests/test_v2_worker_tool_loop.py (Task 7) / tests/test_v2_wake_tool_loop.py
(Task 8): real jobs_store/core_store/effect_outbox against Postgres; the only boundaries
stubbed are `capabilities.registry.run_capability` (capability correctness has its own
test files) and `provider_client.chat_completion_async` (the LLM wire boundary the loop
calls once per round — scripted here to drive specific round shapes).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import effect_id as v2_effect_id
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import executor as v2_executor
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import tool_loop as v2_tool_loop
from model_api_runtime.v2 import worker
from provider_types import ToolCall, ToolExchange

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"),
    reason="needs PG",
)

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="")


# ---------------------------------------------------------------------------
# Shared harness (mirrors tests/test_v2_worker_tool_loop.py's own helpers).
# ---------------------------------------------------------------------------

class _FakeCapResult:
    def __init__(self, data=None, ok=True):
        self._data = data or {}
        self._ok = ok

    def to_dict(self):
        return {"ok": self._ok, "data": self._data, "error": None, "trace": {}, "warnings": []}


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """claim_next_job() is a global claim, not filtered by user_id — a stray row from
    another test module would otherwise get claimed here instead of this file's own row
    (same rationale as tests/test_v2_worker.py's identical fixture).

    The effect-outbox tables are cleaned for the same cross-module reason: a prior
    module (e.g. test_v2_p0_exactly_once) leaves pending/applied effect rows behind
    (its pg_clean only TRUNCATEs at each test's START, not end), and this file's
    per-uid ``_reset`` does not cover the sink-applied dedup guard — a stale
    ``v2_effect_sink_applied`` row can make a fresh reply effect look already-applied
    and silently drop the bubble. Truncating here makes the module order-independent."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE agent_jobs, v2_effect_outbox, v2_effect_sink_applied, "
            "v2_runtime_state CASCADE"
        )
    yield


def _patch_real_write(monkeypatch):
    """`worker._write_encrypted_reply`'s real envelope-build path needs a live enclave,
    unavailable in this test process (same rationale as every DB-backed V2 tool-loop
    test). This variant still performs a REAL `store.append_chat(..., strict=True)` DB
    write so bubble-read-back assertions below see genuine chat_messages rows in real
    seq order — only the encryption step itself is skipped."""
    def _real_write(store, text):
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test"}
        return store.append_chat("openclaw", "model_api", envelope, strict=True)

    monkeypatch.setattr(worker, "_write_encrypted_reply", _real_write)


def _reply_effect_dispatch(user_id):
    """Test-local production-shaped sink for the `reply` effect_type — mirrors
    `serve_worker._sink_reply`'s real write without pulling in hosted-adjacent wiring."""
    def dispatch(effect_type, payload):
        if effect_type == "reply":
            worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
    return dispatch


def _apply_effects(user_id):
    return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_reply_effect_dispatch(user_id))


def _script_provider(monkeypatch, responses):
    it = iter(responses)
    calls = []

    async def _fake(config, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _text_round(text, *, prompt_tokens=1, completion_tokens=1):
    return {"reply": text, "tool_calls": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _tool_round(*tool_calls, prompt_tokens=1, completion_tokens=1):
    return {"reply": "", "tool_calls": list(tool_calls),
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _tc(call_id, name, **args):
    return {"id": call_id, "name": name, "args": args}


def _deps(*, messages, token="rt-enclave"):
    return worker.TurnDeps(
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: token,
        apply_pending_effects=_apply_effects,
    )


def _bubbles(uid):
    """Real chat_messages rows for this user's model-authored replies, in DB `seq`
    (identity-column, `ORDER BY seq ASC` — see db.py's chat-load query) order, i.e. real
    write order — role="openclaw"/source="model_api" is exactly what
    `worker._write_encrypted_reply` always writes."""
    store = core_store.get_store(uid)
    store.reload()
    return [m for m in store.chat_messages if m.get("role") == "openclaw" and m.get("source") == "model_api"]


def _job_status_row(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


# ---------------------------------------------------------------------------
# P0 #1: weak-model plain text -> exactly 1 provider call and exactly 1 bubble.
# ---------------------------------------------------------------------------

def test_p0_weak_model_plain_text_one_call_one_bubble_no_dispatch_tiering(monkeypatch):
    uid = "u_p0_weak_plain"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("hello from the weak model")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == 1                       # STRONG: exactly one provider call, no
                                                   # forced second responder round-trip.
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1                      # STRONG: exactly one reply bubble.
    assert bubbles[0]["body_ct"] == "hello from the weak model"
    assert _job_status_row(job_id)[0] == "completed"


# ---------------------------------------------------------------------------
# P0 #2: reply(我看看哈) + web_search, exactly-once on retry.
# ---------------------------------------------------------------------------

def test_p0_reply_then_web_search_ordering_and_exactly_once_replay(monkeypatch):
    uid = "u_p0_reply_websearch"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(
        cap_registry, "run_capability",
        lambda action_type, store, **k: _FakeCapResult({"snippet": "search result"}))
    _patch_real_write(monkeypatch)
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc("r1", "reply", text="我看看哈"), _tc("s1", "web_search", query="x")),
        _text_round("final answer"),
    ])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == 2
    # Round 1 carries the native assistant call and call-id-matched observation.
    exchanges = [m for m in calls[1]["messages"] if isinstance(m, ToolExchange)]
    assert len(exchanges) == 1
    assert "search result" in " ".join(r.content for r in exchanges[0].results)

    bubbles = _bubbles(uid)
    assert len(bubbles) == 2
    # STRONG: two distinct bubbles, in real chat_messages seq/write order — the
    # intermediate `reply` bubble landed BEFORE the terminal one, not batched to
    # end-of-turn (C6: drained immediately mid-loop).
    assert [b["body_ct"] for b in bubbles] == ["我看看哈", "final answer"]

    # STRONG: exactly-once replay. Reconstruct the FIRST reply effect's deterministic
    # effect_id (ordinal 0 -- the intermediate `reply` tool call was this turn's first
    # enqueue_effect call, `reply` is folded before `web_search`'s dispatch) and
    # re-drive enqueue+drain exactly as a retried/re-enqueued turn would.
    gen = db.get_runtime_generation(uid)
    eid = v2_effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    replay_id = v2_effect_outbox.enqueue_effect(
        job_id=job_id, user_id=uid, effect_type="reply", ordinal=0,
        expected_generation=gen, payload={"text": "我看看哈"})
    assert replay_id == eid  # same deterministic id -> ON CONFLICT DO NOTHING, no new row
    result = _apply_effects(uid)
    assert result == {"applied": 0, "discarded": 0}  # already applied -> not in the pending set

    bubbles_after = _bubbles(uid)
    assert len(bubbles_after) == 2  # STRONG: NO duplicate bubble after the re-drive.


# ---------------------------------------------------------------------------
# P0 #3: mid-turn fold, no restart. A second user message B becomes visible
# (via a fake `deps.read_messages_since`) DURING round-1's tool dispatch;
# round-2's build_messages must see A + B + round-1's tool_results, with no
# debounce sleep and no loop restart.
# ---------------------------------------------------------------------------

def test_p0_mid_turn_fold_no_restart_no_debounce(monkeypatch):
    uid = "u_p0_midturn_fold"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    msg_a = {"id": "mA", "ts": 10.0, "role": "user", "content": "gate-message-A-unique"}
    msg_b = {"id": "mB", "ts": 20.0, "role": "user", "content": "gate-message-B-unique"}
    live_rows = {"rows": [msg_a]}  # grows to [msg_a, msg_b] mid-turn, simulating B's commit.

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        assert action_type == "memory_search"
        # Message B "arrives" (becomes visible to the enclave-decrypt reader) exactly
        # while round-1's tool dispatch is in flight — before round-2's fold.
        live_rows["rows"] = [msg_a, msg_b]
        return _FakeCapResult({"snippet": "search-result-unique"})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc("m1", "memory_search", query="x")),
        _text_round("done"),
    ])

    # No asyncio.sleep anywhere in the unified loop (Global Constraints: "no
    # time-debounce; no loop restart") -- a real debounce regression would call it.
    sleep_calls = []

    async def _boom_sleep(*a, **k):
        sleep_calls.append(a)
        raise AssertionError(f"unexpected asyncio.sleep during the tool loop: {a!r}")

    monkeypatch.setattr(asyncio, "sleep", _boom_sleep)

    # Capture the real LoopOutcome (process_job discards it) to assert the round
    # counter directly, proving "no restart" rather than inferring it.
    orig_run_tool_loop = v2_tool_loop.run_tool_loop
    captured = {}

    async def _spy_run_tool_loop(**kwargs):
        outcome = await orig_run_tool_loop(**kwargs)
        captured["outcome"] = outcome
        return outcome

    monkeypatch.setattr(v2_tool_loop, "run_tool_loop", _spy_run_tool_loop)

    deps = worker.TurnDeps(
        read_messages=lambda uid: list(live_rows["rows"]),
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt-enclave",
        apply_pending_effects=_apply_effects,
        read_messages_since=lambda uid, since_ts: list(live_rows["rows"]),
        read_summary=lambda uid: ("", 0.0, 0),
        read_tail=lambda uid, watermark, limit: [msg_a],  # D1 tail captured once at loop entry
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert sleep_calls == []                     # STRONG: no debounce sleep occurred.
    assert len(calls) == 2                        # STRONG: no restart -- a restart would
                                                    # need a 3rd scripted provider call
                                                    # (round 0 replayed), which would
                                                    # StopIteration and fail the turn.
    outcome = captured["outcome"]
    assert outcome.rounds == 2                     # STRONG: round counter monotonic, 2 not 3+.
    assert outcome.stop_reason == "final_text"

    round1_joined = " ".join(
        str(m.get("content", ""))
        for m in calls[1]["messages"]
        if isinstance(m, dict)
    )
    assert "gate-message-A-unique" in round1_joined     # A: base tail context.
    assert "gate-message-B-unique" in round1_joined     # B: folded in mid-turn, no restart.
    exchange = next(m for m in calls[1]["messages"] if isinstance(m, ToolExchange))
    assert "search-result-unique" in " ".join(r.content for r in exchange.results)

    assert _bubbles(uid)[0]["body_ct"] == "done"


# ---------------------------------------------------------------------------
# P0 #4: malicious-page write refusal. A purely-external round
# (`turn_authorization=False`) whose model emits memory_write/identity_patch/
# schedule_wake must be refused deterministically: no effect enqueued, no
# durable write, capability write fn never invoked.
# ---------------------------------------------------------------------------

def test_p0_malicious_page_write_refusal_no_effect_no_capability_call(monkeypatch):
    uid = "u_p0_write_refusal"
    conftest.seed_user(uid)
    _reset(uid)

    write_fn_called = []

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        write_fn_called.append(action_type)
        raise AssertionError("capability write fn must never run for a refused write")

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)

    enqueue_calls = []

    def _enqueue_write_effect(tc):
        enqueue_calls.append(tc)
        raise AssertionError("enqueue_write_effect must never be called for a refused write")

    class _Store:
        user_id = uid

    tool_calls = [
        ToolCall(id="w1", name="memory_write", args={"actions": [{
            "op": "add",
            "summary": "injected",
            "content": "injected",
        }]}),
        ToolCall(id="w2", name="identity_patch", args={"patch": {"persona": "evil"}}),
        # Keep this tool call schema-valid so the test isolates the provenance
        # refusal instead of the dispatcher's argument-validation refusal.
        ToolCall(id="w3", name="schedule_wake", args={"at": "now"}),
    ]

    results = asyncio.run(v2_executor.dispatch_tool_calls(
        tool_calls, store=_Store(), api_key="k", runtime_token="rt",
        enclave_sem=asyncio.Semaphore(4),
        turn_authorization=False,  # a purely-external round: no user/wake origin
        enqueue_write_effect=_enqueue_write_effect,
    ))

    assert len(results) == 3
    for r in results:
        assert "authorization" in r.content   # provenance.write_gate's deterministic refusal
    assert enqueue_calls == []                 # STRONG: never enqueued.
    assert write_fn_called == []                # STRONG: capability write fn never invoked.
    # STRONG: the durable outbox has NO memory/identity/schedule effect for this user.
    assert db.effect_pending(uid) == []


# ---------------------------------------------------------------------------
# P0 #5: 4 providers live loop. For each of the 4 wires, a canned wire body is
# decoded via PR B's real codec into ToolCalls and driven through the real
# tool_loop.run_tool_loop + executor.dispatch_tool_calls; assert 2 ToolResults
# come back keyed by the RIGHT call_id (no cross-wire mixups).
# ---------------------------------------------------------------------------

def _openai_chat_body():
    return {"choices": [{"message": {"tool_calls": [
        {"id": "call_a", "function": {"name": "memory_search", "arguments": '{"query": "tea"}'}},
        {"id": "call_b", "function": {"name": "web_search", "arguments": '{"query": "weather"}'}},
    ]}}]}


def _openai_responses_body():
    return {"output": [
        {"type": "function_call", "call_id": "fc_a", "name": "memory_search", "arguments": '{"query": "tea"}'},
        {"type": "function_call", "call_id": "fc_b", "name": "web_search", "arguments": '{"query": "weather"}'},
    ]}


def _anthropic_body():
    return {"content": [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "toolu_a", "name": "memory_search", "input": {"query": "tea"}},
        {"type": "tool_use", "id": "toolu_b", "name": "web_search", "input": {"query": "weather"}},
    ]}


def _gemini_body():
    return {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "memory_search", "args": {"query": "tea"}}},
        {"functionCall": {"name": "web_search", "args": {"query": "weather"}}},
    ]}}]}


_WIRES = {
    "anthropic": dict(
        decode=provider_client._decode_tool_calls_anthropic, body=_anthropic_body,
        provider_config=provider_client.ProviderConfig(
            provider="anthropic", model="claude-sonnet-4-test", api_key="k", base_url="")),
    "gemini": dict(
        decode=provider_client._decode_tool_calls_gemini, body=_gemini_body,
        provider_config=provider_client.ProviderConfig(
            provider="gemini", model="gemini-2-test", api_key="k", base_url="")),
    "openai_chat": dict(
        decode=provider_client._decode_tool_calls_openai_chat, body=_openai_chat_body,
        provider_config=provider_client.ProviderConfig(
            provider="deepseek", model="deepseek-chat", api_key="k", base_url="")),
    "openai_responses": dict(
        decode=provider_client._decode_tool_calls_openai_responses, body=_openai_responses_body,
        provider_config=provider_client.ProviderConfig(
            provider="openai", model="gpt-5", api_key="k", base_url="")),
}


@pytest.mark.parametrize("wire", ["anthropic", "gemini", "openai_chat", "openai_responses"])
def test_p0_four_providers_two_reads_dispatch_and_reply_by_call_id(monkeypatch, wire):
    spec = _WIRES[wire]
    # (a) decode a canned 2-tool-call wire body via PR B's real codec -> 2 ToolCalls
    # with the WIRE-SPECIFIC call_id shape (openai "call_a"/"call_b", openai-responses
    # "fc_a"/"fc_b", anthropic "toolu_a"/"toolu_b", gemini's synthetic "call_<i>_<name>").
    decoded = spec["decode"](spec["body"]())
    assert len(decoded) == 2
    tool_calls = [ToolCall(**d) for d in decoded]
    call_ids = [tc.id for tc in tool_calls]
    assert len(set(call_ids)) == 2
    id_by_name = {tc.name: tc.id for tc in tool_calls}
    assert set(id_by_name) == {"memory_search", "web_search"}

    ran = []

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        ran.append(action_type)
        return _FakeCapResult({"body": f"result-for-{action_type}"})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)

    class _Store:
        user_id = "u_p0_providers"

    async def _dispatch_tools(tcs):
        return await v2_executor.dispatch_tool_calls(
            tcs, store=_Store(), api_key="k", runtime_token="rt",
            enclave_sem=asyncio.Semaphore(4), turn_authorization=True,
            enqueue_write_effect=lambda tc: (_ for _ in ()).throw(
                AssertionError("no writes expected in this read-only round")))

    build_calls = []

    def _build_messages(transcript):
        build_calls.append(list(transcript))
        return [{"role": "user", "content": "turn"}]

    on_reply_calls = []

    async def _on_reply(text, *, final):
        on_reply_calls.append((text, final))

    async def _fold_new_messages():
        return []

    provider = iter([
        # (b) round 0: the model's response IS the decoded wire's 2 tool_calls.
        {"reply": "", "tool_calls": decoded, "usage": {}},
        {"reply": "done", "tool_calls": [], "usage": {}},
    ])

    async def _fake_chat_completion_async(config, messages, *, tools=None):
        return next(provider)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake_chat_completion_async)

    outcome = asyncio.run(v2_tool_loop.run_tool_loop(
        provider_config=spec["provider_config"], build_messages=_build_messages,
        dispatch_tools=_dispatch_tools, on_reply=_on_reply,
        fold_new_messages=_fold_new_messages, add_usage=lambda usage: None,
        max_calls=5,
    ))

    assert outcome.rounds == 2
    assert outcome.stop_reason == "final_text"
    assert on_reply_calls == [("done", True)]
    assert sorted(ran) == ["memory_search", "web_search"]

    # (c) STRONG: the 2 ToolResults dispatch_tools returned this round are exactly the
    # native ToolExchange build_messages saw for round 1, keyed by the RIGHT call_id --
    # memory_search's result under memory_search's id, web_search's under web_search's,
    # never swapped, regardless of the wire's id shape.
    exchange = build_calls[1][0]
    assert isinstance(exchange, ToolExchange)
    prior_results1 = exchange.results
    assert len(prior_results1) == 2
    by_call_id = {r.call_id: r.content for r in prior_results1}
    assert set(by_call_id) == set(call_ids)
    assert "result-for-memory_search" in by_call_id[id_by_name["memory_search"]]
    assert "result-for-web_search" in by_call_id[id_by_name["web_search"]]
