"""worker._run_wake on the unified provider-native tool loop.

Style mirrors tests/test_v2_worker_tool_loop.py (Task 7's chat-lane sibling):
real jobs_store (real DB claim/mark_*), real core_store (real DB chat/reload),
real model_api_runtime.v2.coalesce/executor/effect_outbox/tool_loop; the only
boundary stubbed is `provider_client.chat_completion_async` (the LLM wire
`tool_loop.run_tool_loop` calls once per round — scripted here to drive
specific round shapes).

Key wake-specific differences from the chat lane, both asserted here:
- No required user message: `_run_wake` always seeds a fixed `_WAKE_NUDGE`
  user-role turn, so the old `ResponderError("no_user_messages")` guard has no
  analogue in the tool-loop version — a wake turn is valid even with an empty
  coalesce/fold.
- An empty terminal reply is NOT a no-filler failure here (unlike chat) — it's
  a legitimate "weak wake sleeps": the job still completes, with zero bubbles.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from provider_types import ToolCall, ToolExchange
from core import store as core_store
from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"),
    reason="needs PG",
)

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="")


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """Mirrors test_v2_worker.py's fixture: claim_next_job() is a global claim,
    not filtered by user_id, so a stray row from another test module would
    otherwise get claimed here instead of this file's own row."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _patch_real_write(monkeypatch):
    """Same rationale as test_v2_worker_tool_loop.py's identical helper:
    `worker._write_encrypted_reply`'s real envelope-build path needs a live
    enclave, unavailable in this test process. Still performs a REAL
    `store.append_chat(..., strict=True)` DB write so `_bubbles` below reads
    back genuine chat_messages rows."""
    def _real_write(store, text):
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test"}
        return store.append_chat("openclaw", "model_api", envelope, strict=True)

    monkeypatch.setattr(worker, "_write_encrypted_reply", _real_write)


def _patch_tool_effect_encryption(monkeypatch):
    """Provide a deterministic test envelope without a live enclave.

    The durable payload contains only base64 test ciphertext; the local sink
    adapter below decodes it solely so this integration test can keep asserting
    the authorized memory action that reached the sink.
    """
    def _fake_build(store, plaintext, *, item_id=None):
        return ({
            "id": item_id,
            "owner_user_id": store.user_id,
            "body_ct": base64.b64encode(plaintext).decode("ascii"),
        }, "")

    monkeypatch.setattr(worker.core_envelope, "_build_shared_envelope_for_store", _fake_build)


def _effect_dispatch(user_id, sink_calls):
    """Test-local production-shaped sink for BOTH `reply` (mirrors
    `serve_worker._sink_reply`) and `memory` (records the payload instead of
    performing a real memory-actions write — memory persistence correctness
    has its own test files; this file only cares whether the write tool_call
    made it INTO the outbox authorized, not refused by the provenance gate)."""
    def dispatch(effect_type, payload):
        logical_effect_type = {
            stored: logical
            for logical, stored in worker.ENCRYPTED_TOOL_EFFECT_TYPES.items()
        }.get(effect_type, effect_type)
        if "effect_envelope" in payload:
            envelope = payload["effect_envelope"]
            decoded = json.loads(base64.b64decode(envelope["body_ct"]).decode("utf-8"))
            decoded["effect_id"] = payload["effect_id"]
            payload = decoded
        sink_calls.append((logical_effect_type, payload))
        if logical_effect_type == "reply":
            worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
    return dispatch


def _apply_effects_factory(sink_calls):
    def _apply(user_id):
        return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_effect_dispatch(user_id, sink_calls))
    return _apply


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


def _wake_deps(*, tail=None, summary="", sink_calls=None, token="rt-enclave"):
    return worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: token,
        read_tail=lambda uid, after_ts, limit: list(tail if tail is not None else []),
        read_summary=lambda uid: (summary, 0.0, 0),
        apply_pending_effects=_apply_effects_factory(sink_calls if sink_calls is not None else []),
)


class _TrajectoryCapture:
    def __init__(self, events=None, scope=""):
        self.events = [] if events is None else events
        self.scope = scope

    def scoped(self, scope):
        return _TrajectoryCapture(self.events, str(scope))

    async def record(self, event_kind, payload):
        self.events.append((self.scope, event_kind, payload))
        return len(self.events) - 1

    async def record_best_effort(self, event_kind, payload):
        await self.record(event_kind, payload)
        return True


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


def _bubbles(uid):
    store = core_store.get_store(uid)
    store.reload()
    return [m for m in store.chat_messages if m.get("role") == "openclaw" and m.get("source") == "model_api"]


def _turn_metric_row(job_id):
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT model_calls, failed, status FROM v2_turn_metrics WHERE job_id=%s",
            (job_id,)).fetchone()
    return row


def _status_events(uid):
    return jobs_store.list_status_events(uid, after_id=0, limit=100)


# ------------------------------------------------------------------
# Terminal plain text -> exactly one proactive bubble via the effect outbox.
# ------------------------------------------------------------------

def test_wake_terminal_plain_text_writes_exactly_one_proactive_bubble(monkeypatch):
    uid = "u_wake_toolloop_happy"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("hey, thinking of you")])
    sink_calls = []
    deps = _wake_deps(tail=[], sink_calls=sink_calls)
    trajectory = _TrajectoryCapture()

    # Through process_job (not a direct _run_wake call) so a `TurnMetrics`
    # accumulator gets created and flushed, same as production's real
    # dispatch path (`_run_turn` -> `process_job` -> lane dispatch).
    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
        trajectory_recorder=trajectory,
    ))

    assert status == "completed"
    assert len(calls) == 1
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1
    assert bubbles[0]["body_ct"] == "hey, thinking of you"
    assert [c[0] for c in sink_calls] == ["reply"]
    reply_dispositions = [
        payload
        for _scope, kind, payload in trajectory.events
        if kind == "reply_effect_disposition"
    ]
    assert len(reply_dispositions) == 1
    assert reply_dispositions[0]["status"] == "applied_unverified"

    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[0] >= 1           # >=1 model call
    assert row[1] is False       # not failed
    assert row[2] == "ok"
    assert _job_status(job_id)[0] == "completed"


# ------------------------------------------------------------------
# Weak wake sleeps: empty terminal text is NOT a no-filler failure here.
# ------------------------------------------------------------------

def test_wake_empty_terminal_text_completes_with_zero_bubbles(monkeypatch):
    uid = "u_wake_toolloop_silence"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)
    _script_provider(monkeypatch, [_text_round("")])
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    sink_calls = []
    deps = _wake_deps(tail=[], sink_calls=sink_calls)
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, str(job["claimed_by"])))

    assert status == "completed"
    assert _bubbles(uid) == []
    assert sink_calls == []
    assert surface_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"
    assert not any(e["kind"] == "error" for e in _status_events(uid))


# ------------------------------------------------------------------
# No required user message: an empty tail still completes (the nudge alone
# satisfies build_turn_messages's "at least one non-system turn").
# ------------------------------------------------------------------

def test_wake_empty_tail_still_completes_no_no_user_messages_guard(monkeypatch):
    uid = "u_wake_toolloop_notail"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)
    seen = {}

    async def _fake(config, messages, *, tools=None):
        seen["messages"] = messages
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    deps = _wake_deps(tail=[])  # zero real tail rows: only the nudge should be present
    status = asyncio.run(worker._run_wake(
        job_id, uid, "scheduled", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, str(job["claimed_by"])))

    assert status == "completed"
    conversation_messages = [
        message
        for message in seen["messages"]
        if message.get("role") != "system"
        and not str(message.get("content") or "").startswith(
            v2_context.RUNTIME_CONTEXT_HEADER
        )
    ]
    assert len(conversation_messages) == 1
    assert conversation_messages[0]["role"] == "user"


# ------------------------------------------------------------------
# memory_write is AUTHORIZED (turn_authorization=True from wake), applied, and
# acknowledged with its durable disposition — never refused by provenance.
# ------------------------------------------------------------------

def test_wake_memory_write_is_authorized_applied_and_not_refused(monkeypatch):
    uid = "u_wake_toolloop_memwrite"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)
    _patch_tool_effect_encryption(monkeypatch)
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc(
            "w1", "memory_write",
            actions=[{
                "op": "add",
                "summary": "likes tea",
                "content": "likes tea",
            }],
        )),
        _text_round(""),
    ])
    sink_calls = []
    deps = _wake_deps(tail=[], sink_calls=sink_calls)
    trajectory = _TrajectoryCapture()

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "manual_wake",
        deps,
        _BYOK,
        worker.ENCLAVE_SEMAPHORE,
        str(job["claimed_by"]),
        trajectory_recorder=trajectory,
    ))

    assert status == "completed"
    assert len(calls) == 2
    # Round 1 carries the native assistant call and call-id-matched write result.
    exchanges = [m for m in calls[1]["messages"] if isinstance(m, ToolExchange)]
    assert len(exchanges) == 1
    round1_results = " ".join(r.content for r in exchanges[0].results)
    assert "ok: memory_write applied" in round1_results
    assert "refused" not in round1_results
    tool_results = [
        payload
        for _scope, kind, payload in trajectory.events
        if kind == "tool_call_result" and payload.get("call_id") == "w1"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["effect"]["status"] == "applied"

    memory_sinks = [p for (t, p) in sink_calls if t == "memory"]
    assert len(memory_sinks) == 1
    # The raw model action ({"op":"add","summary":...,"content":...}) is translated into the server
    # memory-action shape (worker._memory_tool_actions) — no envelope, nested
    # plaintext memory dict — so the plaintext write path builds the E2E envelope.
    # NOT passed through raw (which memory_core.actions rejects with 400).
    assert memory_sinks[0]["actions"] == [{
        "type": "memory.add",
        "memory": {"summary": "likes tea", "content": "likes tea", "bucket": "", "threads": []},
        "reason": "Written by the agent via the memory_write tool.",
        "capture_mode": "agent_tool",
    }]
    serve_worker._validate_decrypted_tool_effect(
        "memory", {**memory_sinks[0], "effect_id": "wake-memory-effect"})
    # The durable outbox still contains only the encrypted wrapper; the model's
    # plaintext action is revealed only after sink-side enclave decryption.
    with db.get_pool().connection() as conn:
        stored_payload = conn.execute(
            "SELECT payload::text FROM v2_effect_outbox "
            "WHERE user_id=%s AND effect_type=%s",
            (uid, worker.ENCRYPTED_TOOL_EFFECT_TYPES["memory"]),
        ).fetchone()[0]
    assert "likes tea" not in stored_payload
    assert "effect_envelope" in stored_payload
    assert _job_status(job_id)[0] == "completed"


def test_wake_mixed_valid_invalid_workspace_batch_applies_valid_call(
    monkeypatch,
):
    uid = "u_wake_mixed_workspace_batch"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w-mixed-workspace")
    _patch_tool_effect_encryption(monkeypatch)
    captured = {}

    async def direct_loop(**kwargs):
        results = await kwargs["dispatch_tools"](
            [
                ToolCall(
                    id="valid",
                    name="workspace_write",
                    args={
                        "path": "/workspace/valid.md",
                        "content": "kept",
                        "expected_revision": 0,
                    },
                ),
                ToolCall(
                    id="invalid",
                    name="workspace_write",
                    args={},
                    args_ok=False,
                ),
            ]
        )
        captured["results"] = results
        return worker.v2_tool_loop.LoopOutcome(
            final_text="",
            rounds=1,
            stop_reason="final_text",
            replied_intermediate=False,
        )

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", direct_loop)
    sink_calls = []
    deps = _wake_deps(tail=[], sink_calls=sink_calls)
    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "manual_wake",
            deps,
            _BYOK,
            worker.ENCLAVE_SEMAPHORE,
            str(job["claimed_by"]),
        )
    )

    assert status == "completed"
    assert [result.call_id for result in captured["results"]] == [
        "valid",
        "invalid",
    ]
    assert captured["results"][0].content == "ok: workspace_write applied"
    assert captured["results"][1].content.startswith("error: unparseable args")
    assert [kind for kind, _payload in sink_calls] == ["workspace_batch"]


def test_wake_memory_write_refused_when_process_job_seeds_no_authorization(monkeypatch):
    """Negative control for the above: confirms the assertion strings actually
    distinguish authorized vs refused (guards against a vacuously-true positive
    test) by directly exercising `provenance.write_gate` with
    turn_authorization=False — the same deterministic gate `_run_wake`'s
    dispatcher relies on being True for."""
    from model_api_runtime.v2 import provenance as v2_provenance

    allowed, reason = v2_provenance.write_gate("memory_write", turn_authorization=False)
    assert allowed is False
    assert "refused" in reason

    allowed_wake, _ = v2_provenance.write_gate("memory_write", turn_authorization=True)
    assert allowed_wake is True


# ------------------------------------------------------------------
# Real provider failure -> silent mark_failed (never surfaced, never a bubble).
# ------------------------------------------------------------------

def test_wake_provider_error_silent_mark_failed(monkeypatch):
    uid = "u_wake_toolloop_provider_err"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None):
        raise provider_client.ProviderError("boom", status_code=500)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "manual_wake", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, str(job["claimed_by"])))

    assert status == "failed"
    assert _bubbles(uid) == []
    assert surface_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert "wake_failed" in (row[1] or "")
    assert not any(e["kind"] == "error" for e in _status_events(uid))


# ------------------------------------------------------------------
# provider_config-kind failures (dead/broke BYOK key) still set payment
# cooldown BEFORE the silent mark_failed (D3 Task 7 behavior preserved).
# ------------------------------------------------------------------

def test_wake_provider_config_error_still_sets_payment_cooldown(monkeypatch):
    import time as _time

    uid = "u_wake_toolloop_provider_config"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None):
        raise provider_client.ProviderError("out of credits", status_code=402)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)

    cooldown_calls = []
    orig_upsert = jobs_store.upsert_wake_schedule

    def _spy_upsert(user_id_, **kw):
        cooldown_calls.append((user_id_, kw))
        return orig_upsert(user_id_, **kw)

    monkeypatch.setattr(jobs_store, "upsert_wake_schedule", _spy_upsert)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    before = _time.time()
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, str(job["claimed_by"])))
    after = _time.time()

    assert status == "failed"
    assert len(cooldown_calls) == 1
    called_uid, kwargs = cooldown_calls[0]
    assert called_uid == uid
    cooldown_at = kwargs["payment_cooldown_until"]
    assert before + worker._WAKE_COOLDOWN_SEC - 5 <= cooldown_at <= after + worker._WAKE_COOLDOWN_SEC + 5

    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is not None
    assert schedule["payment_cooldown_until"] is not None


def test_wake_transient_error_does_not_set_payment_cooldown(monkeypatch):
    uid = "u_wake_toolloop_transient"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None):
        raise provider_client.ProviderError("timeout-ish", status_code=503)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)

    cooldown_calls = []
    orig_upsert = jobs_store.upsert_wake_schedule

    def _spy_upsert(user_id_, **kw):
        cooldown_calls.append((user_id_, kw))
        return orig_upsert(user_id_, **kw)

    monkeypatch.setattr(jobs_store, "upsert_wake_schedule", _spy_upsert)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, str(job["claimed_by"])))

    assert status == "failed"
    assert cooldown_calls == []
    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is None or schedule["payment_cooldown_until"] is None


# ------------------------------------------------------------------
# screen_watch lane: its own system prompt + safe screen availability grounding
# flows through `extra_context`; caption text remains an explicit tool read.
# ------------------------------------------------------------------

def test_screen_watch_lane_uses_its_own_prompt_and_screen_context(monkeypatch):
    uid = "u_wake_toolloop_screenwatch"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    job = jobs_store.claim_next_job("w")

    async def _fake_cap_data(store, action_type, **kw):
        assert action_type == "screen_recent"
        return {"frames": [{"frame_id": "f1", "caption": "a stack trace"}]}

    monkeypatch.setattr(worker, "_cap_data", _fake_cap_data)
    _patch_real_write(monkeypatch)

    seen = {}

    async def _fake(config, messages, *, tools=None):
        seen["messages"] = messages
        seen["tools"] = tools
        return _text_round("你在看这个报错？")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "screen_watch", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, str(job["claimed_by"])))

    assert status == "completed"
    system_msg = next(m for m in seen["messages"] if m["role"] == "system")
    assert "watching the screen" in system_msg["content"]
    joined = " ".join(str(m.get("content", "")) for m in seen["messages"])
    assert "a stack trace" not in joined
    assert '"recent_count":1' in joined
    assert "screen_recent" in {spec.name for spec in seen["tools"]}
    assert _bubbles(uid)[0]["body_ct"] == "你在看这个报错？"
