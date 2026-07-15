"""Task 7 (PR C, spec C6+C9a): worker.process_job's chat branch on the unified
provider-native tool loop (`tool_loop.run_tool_loop`) — replacing the old
two-layer while/replan + json_planner + forced responder.respond pipeline.

Style mirrors tests/test_v2_worker.py: real jobs_store (real DB claim/mark_*/
runtime_state), real core_store (real DB chat/reload), real
model_api_runtime.v2.coalesce/executor/effect_outbox/tool_loop; the two
boundaries stubbed are `cap_registry.run_capability` (capability correctness
has its own test files) and `provider_client.chat_completion_async` (the LLM
wire boundary tool_loop.run_tool_loop calls once per round — scripted here to
drive specific round shapes).
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
from provider_types import ToolExchange
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import effect_id as v2_effect_id
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import planner as v2_planner
from model_api_runtime.v2 import responder as v2_responder
from model_api_runtime.v2 import worker

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"),
    reason="needs PG",
)

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")


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
    """Mirrors test_v2_worker.py's fixture: claim_next_job() is a global claim,
    not filtered by user_id, so a stray row from another test module would
    otherwise get claimed here instead of this file's own row."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _patch_real_write(monkeypatch):
    """`worker._write_encrypted_reply`'s real envelope-build path needs a live
    enclave (`core_envelope._build_shared_envelope_for_store` calls
    `enclave._get_enclave_info()`), unavailable in this test process — the
    same reason every DB-backed V2 test (test_v2_worker.py,
    test_v2_p0_exactly_once.py) stubs it. This variant still performs a REAL
    `store.append_chat(..., strict=True)` DB write (a fixed-shape envelope —
    the server stores ciphertext verbatim regardless of shape, see
    `append_chat`'s docstring) so `_bubbles` below reads back genuine
    chat_messages rows, only the encryption step itself is skipped."""
    def _real_write(store, text):
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test"}
        return store.append_chat("openclaw", "model_api", envelope, strict=True)

    monkeypatch.setattr(worker, "_write_encrypted_reply", _real_write)


def _reply_effect_dispatch(user_id):
    """Test-local production-shaped sink for the `reply` effect_type — mirrors
    `serve_worker._sink_reply`'s real write (`worker._write_encrypted_reply`)
    without pulling in serve_worker's hosted-adjacent wiring."""
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
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid: token,
        apply_pending_effects=_apply_effects,
    )


def _bubbles(uid):
    """Real chat_messages rows written for this user's model-authored replies,
    in seq (durable write) order — `role="openclaw"`/`source="model_api"` is
    exactly what `worker._write_encrypted_reply` always writes."""
    store = core_store.get_store(uid)
    store.reload()
    return [m for m in store.chat_messages if m.get("role") == "openclaw" and m.get("source") == "model_api"]


def _turn_metric_row(job_id):
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT model_calls, failed, status FROM v2_turn_metrics WHERE job_id=%s",
            (job_id,)).fetchone()
    return row


def test_single_round_plain_text_writes_exactly_one_bubble_no_planner_or_responder(monkeypatch):
    """Round 1: no tool_calls, plain text -> that text IS the final reply
    (Global Constraints). Must never touch the old json_planner/responder
    machinery at all."""
    uid = "u_toolloop_happy"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    def _boom_plan(*a, **k):
        raise AssertionError("the unified tool loop must never call v2_planner.plan")

    async def _boom_respond(*a, **k):
        raise AssertionError("the unified tool loop must never call v2_responder.respond")

    monkeypatch.setattr(v2_planner, "plan", _boom_plan)
    monkeypatch.setattr(v2_responder, "respond", _boom_respond)
    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("hello from the model")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == 1
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1
    assert bubbles[0]["body_ct"] == "hello from the model"
    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[0] == 1          # exactly one model call
    assert row[1] is False      # not failed
    assert row[2] == "ok"
    assert _job_status_row(job_id)[0] == "completed"


def _job_status_row(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


def test_intermediate_reply_then_terminal_text_and_exactly_once_replay(monkeypatch):
    """Two-round script: round 0 the model calls `reply` (intermediate bubble)
    ALONGSIDE a `web_search` read tool call; round 1 the model stops with plain
    terminal text. Both bubbles land via the PR A effect outbox, the
    intermediate one visible BEFORE the terminal one (C6: drained immediately,
    not batched to end-of-turn). Then a re-drive that re-enqueues the SAME
    effect_id (job_id + effect_type + ordinal are what `effect_id.derive`
    hashes — a retry of the same turn reproduces it exactly) must NOT produce a
    duplicate bubble (PR A's ON CONFLICT DO NOTHING + pending-only drain)."""
    uid = "u_toolloop_tworound"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(
        cap_registry, "run_capability",
        lambda action_type, store, **k: _FakeCapResult({"snippet": "search result"}))
    _patch_real_write(monkeypatch)
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc("r1", "reply", text="intermediate"), _tc("s1", "web_search", query="x")),
        _text_round("final answer"),
    ])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == 2
    # Round 1 carries the native assistant call and call-id-matched observation.
    exchanges = [m for m in calls[1]["messages"] if isinstance(m, ToolExchange)]
    assert len(exchanges) == 1
    assert "search result" in " ".join(r.content for r in exchanges[0].results)

    bubbles = _bubbles(uid)
    assert len(bubbles) == 2
    # `_bubbles` reflects chat_messages' `seq` (identity-column) order, i.e. real
    # write order: the intermediate bubble must land before the terminal one.
    assert [b["body_ct"] for b in bubbles] == ["intermediate", "final answer"]

    # Exactly-once replay: reconstruct the FIRST reply effect's deterministic id
    # (ordinal 0 -- the intermediate `reply` tool call was the turn's first
    # enqueue_effect call) and re-drive enqueue+drain exactly as a retried turn
    # would.
    gen = db.get_runtime_generation(uid)
    eid = v2_effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    replay_id = v2_effect_outbox.enqueue_effect(
        job_id=job_id, user_id=uid, effect_type="reply", ordinal=0,
        expected_generation=gen, payload={"text": "intermediate"})
    assert replay_id == eid  # same deterministic id -> ON CONFLICT DO NOTHING, no new row
    result = _apply_effects(uid)
    assert result == {"applied": 0, "discarded": 0}  # already applied -> not in the pending set

    bubbles_after = _bubbles(uid)
    assert len(bubbles_after) == 2  # NO duplicate bubble


# ------------------------------------------------------------------
# PR C final review, BUG #2 (minor, no-filler): if the unified loop returns a
# LoopOutcome with NO reply produced (final_text empty AND replied_intermediate
# is False), the chat lane must mark the turn FAILED, not silently complete it
# with no bubble. Unlike test_v2_worker.py's existing BUG-4 successor test
# (`test_chat_turn_always_replies_even_when_model_only_calls_tools`), which
# exercises the NORMAL budget-forced-final-round path (last round has
# tools=None and the provider correctly returns plain text), this drives the
# genuinely misbehaving shape: the LAST round (tools=None) has the provider
# ignore that and return a non-reply tool_call anyway. `tool_loop.run_tool_loop`
# still dispatches it (tool_calls are honored regardless of what `tools` was
# passed on the wire), but the `for` loop is then exhausted with no terminal
# `on_reply` call ever having fired -> falls through to the
# `LoopOutcome("", rounds, "budget_exhausted", False)` return.
# ------------------------------------------------------------------

def test_chat_turn_with_no_reply_produced_marks_job_failed_not_completed(monkeypatch):
    uid = "u_toolloop_noreply"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(worker, "_TURN_MAX_LLM_CALLS", 1)
    monkeypatch.setattr(
        cap_registry, "run_capability",
        lambda action_type, store, **k: _FakeCapResult({"snippet": "irrelevant"}))
    # The ONE and only (last) round: tools=None is what the provider is asked
    # for, but it misbehaves and returns a non-reply tool_call anyway.
    calls = _script_provider(monkeypatch, [_tool_round(_tc("c1", "web_search", query="x"))])
    write_calls = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_calls.update(n=write_calls["n"] + 1) or {"id": "should-not-happen"})
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert len(calls) == 1
    assert write_calls["n"] == 0          # no filler bubble, no bubble at all
    assert _bubbles(uid) == []
    status_row = _job_status_row(job_id)
    assert status_row[0] == "failed"
    assert "empty_reply" in (status_row[1] or "")
    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[1] is True                 # failed=True in the metric row too
