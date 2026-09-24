"""T723 A+B: presence wakes look before deciding, and ask why they reach out now.

prod 2026-09-17..24: GLM-5.3 decided in its first provider call 96% of the time
and stayed silent 93% of the time; heartbeat asides were asked "how you mean to
pick up what they said" although no line of theirs was waiting.
"""
from __future__ import annotations

import asyncio

import pytest

import test_v2_wake_tool_loop as T2
import test_v2_wake_worker as W
from agent_protocol_core import self_thinking
from capabilities import tool_schema as cap_tool_schema
from model_api_runtime.v2 import jobs_store, tool_loop, worker
import provider_client

pytestmark = W.pytestmark if hasattr(W, "pytestmark") else []

DECISION = {"reply", cap_tool_schema.STAY_SILENT_TOOL}
USAGE = {"prompt_tokens": 1, "completion_tokens": 1}


def _silent(reason="nothing new"):
    return {"reply": "", "usage": USAGE, "tool_calls": [{
        "id": "s", "name": cap_tool_schema.STAY_SILENT_TOOL, "args": {"reason": reason}}]}


def _reply(text, aside="I want to tell them."):
    return {"reply": "", "usage": USAGE, "tool_calls": [{
        "id": "r", "name": "reply", "args": {"text": text, "aside": aside}}]}


def _text(text):
    return {"reply": text, "tool_calls": [], "usage": USAGE}


def _lookup():
    return {"reply": "", "usage": USAGE, "tool_calls": [{
        "id": "m", "name": "memory_index", "args": {}}]}


def _fake(monkeypatch, responses):
    it = iter(responses)
    calls = []

    async def _chat(config, messages, *, tools=None, **kwargs):
        calls.append({
            "names": {spec.name for spec in (tools or [])},
            "messages": messages,
            "tool_choice": kwargs.get("tool_choice"),
        })
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _chat)
    return calls


def _run(monkeypatch, lane, responses, *, uid_suffix):
    uid = f"u_look_first_{lane}_{uid_suffix}"
    W.conftest.seed_user(uid)
    W._reset(uid)
    trace_id = f"trace-{uid}"
    job_id, _ = jobs_store.enqueue_job(uid, lane, trace_id=trace_id)
    claimed_by = W._claim(job_id)
    calls = _fake(monkeypatch, responses)
    T2._patch_real_write(monkeypatch)
    T2._patch_tool_effect_encryption(monkeypatch)
    sink_calls = []
    traces = []
    deps = T2._wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        sink_calls=sink_calls,
    )
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(event_type)
    status = asyncio.run(worker._run_wake(
        job_id, uid, lane, deps, W._BYOK, asyncio.Semaphore(4), claimed_by,
        trace_id=trace_id,
    ))
    delivered = [str(p.get("text") or "") for kind, p in sink_calls if kind == "reply"]
    delivered += [str(m.get("content") or m.get("text") or "") for m in T2._bubbles(uid)]
    return status, calls, delivered, traces


def _delivered_text(delivered):
    return " ".join(delivered)


@pytest.mark.parametrize("lane", sorted(worker._PRESENCE_WAKE_LANES))
def test_presence_wake_first_call_withholds_the_decision(monkeypatch, lane):
    status, calls, delivered, _ = _run(monkeypatch, lane, [_lookup(), _silent()], uid_suffix="lookup")
    assert status == "completed"
    assert calls[0]["names"] and not calls[0]["names"] & DECISION
    assert DECISION <= calls[1]["names"]
    # A real lookup needs no decide instruction and no draft.
    second = str(calls[1]["messages"])
    assert tool_loop._WAKE_DIRECT_TEXT_CORRECTION not in second
    assert tool_loop._WAKE_CHOICE_INSTRUCTION not in second
    assert delivered == []


def test_screen_watch_first_call_still_offers_the_decision(monkeypatch):
    assert "screen_watch" not in worker._PRESENCE_WAKE_LANES
    status, calls, _, _ = _run(monkeypatch, "screen_watch", [_silent()], uid_suffix="sw")
    assert status == "completed"
    assert DECISION <= calls[0]["names"]
    assert len(calls) == 1


def test_text_on_the_look_round_is_a_draft_never_delivered(monkeypatch):
    draft = "draft written before looking"
    final = "final message after deciding"
    status, calls, delivered, traces = _run(
        monkeypatch, "heartbeat", [_text(draft), _reply(final)], uid_suffix="draft")
    assert status == "completed"
    assert len(calls) == 2
    assert not calls[0]["names"] & DECISION
    assert DECISION <= calls[1]["names"]
    # Not forced: the decision round keeps the ordinary tool surface.
    assert calls[1]["names"] - DECISION
    assert calls[1]["tool_choice"] != "required"
    second = calls[1]["messages"]
    assert any(m.get("role") == "assistant" and m.get("content") == draft
               for m in second if isinstance(m, dict))
    assert tool_loop._WAKE_DIRECT_TEXT_CORRECTION in str(second)
    assert draft not in _delivered_text(delivered)
    assert final in _delivered_text(delivered)
    assert "wake.direct_text_correction" not in traces


def test_empty_look_round_decides_with_the_choice_instruction(monkeypatch):
    status, calls, delivered, _ = _run(
        monkeypatch, "heartbeat", [_text(""), _silent()], uid_suffix="empty")
    assert status == "completed"
    assert len(calls) == 2
    second = calls[1]["messages"]
    assert tool_loop._WAKE_CHOICE_INSTRUCTION in str(second)
    assert not any(m.get("role") == "assistant" and not m.get("tool_calls")
                   and m.get("content") == "" for m in second if isinstance(m, dict))
    assert delivered == []


def test_early_stay_silent_is_not_a_protocol_violation(monkeypatch):
    status, calls, delivered, traces = _run(
        monkeypatch, "heartbeat", [_silent("early"), _silent("after looking")], uid_suffix="early_silent")
    assert status == "completed"
    assert len(calls) == 2
    # Ordinary surface, not the forced two-tool choice a rejected call triggers.
    assert calls[1]["names"] - DECISION
    assert calls[1]["tool_choice"] != "required"
    assert delivered == []
    assert "wake.direct_text_correction" not in traces


def test_early_reply_is_carried_as_draft_and_only_the_decision_is_delivered(monkeypatch):
    early = "early reply text"
    final = "decided reply text"
    status, calls, delivered, _ = _run(
        monkeypatch, "heartbeat", [_reply(early), _reply(final)], uid_suffix="early_reply")
    assert status == "completed"
    assert len(calls) == 2
    assert any(m.get("role") == "assistant" and m.get("content") == early
               for m in calls[1]["messages"] if isinstance(m, dict))
    assert early not in _delivered_text(delivered)
    assert final in _delivered_text(delivered)


def test_look_first_needs_three_calls_of_budget(monkeypatch):
    monkeypatch.setattr(worker, "_TURN_MAX_LLM_CALLS", 2)
    status, calls, _, _ = _run(monkeypatch, "heartbeat", [_silent()], uid_suffix="budget")
    assert status == "completed"
    assert DECISION <= calls[0]["names"]


def test_look_first_requires_a_regular_wake():
    with pytest.raises(ValueError, match="wake_look_first"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=W._BYOK, build_messages=lambda t: [], dispatch_tools=None,
            on_reply=None, fold_new_messages=None, add_usage=lambda u: None,
            max_calls=5,
            wake_look_first=True, regular_wake_choice_required=False,
        ))


# ---- B: presence aside wording ---------------------------------------------

@pytest.mark.parametrize("lane", sorted(worker._PRESENCE_WAKE_LANES))
def test_presence_wake_aside_asks_why_now(monkeypatch, lane):
    monkeypatch.setattr(self_thinking, "enabled", lambda: True)
    prompt = worker._wake_system_prompt_for_lane(lane, worker._WAKE_SYSTEM_PROMPT)
    assert self_thinking._PRESENCE_ASIDE_CONTENT_PHRASE in prompt
    assert self_thinking._ASIDE_CONTENT_PHRASE not in prompt
    assert worker._PRESENCE_ASIDE_INTENT in prompt
    assert worker._CHAT_ASIDE_INTENT not in prompt


@pytest.mark.parametrize("lane", ["screen_watch", "scheduled"])
def test_other_wake_lanes_keep_the_chat_aside(monkeypatch, lane):
    monkeypatch.setattr(self_thinking, "enabled", lambda: True)
    prompt = worker._wake_system_prompt_for_lane(lane, worker._WAKE_SYSTEM_PROMPT)
    assert self_thinking._ASIDE_CONTENT_PHRASE in prompt
    assert self_thinking._PRESENCE_ASIDE_CONTENT_PHRASE not in prompt


def test_presence_field_differs_only_in_the_intent_phrase():
    chat = self_thinking.instruction_for_field()
    presence = self_thinking.instruction_for_field(presence=True)
    assert presence == chat.replace(
        self_thinking._ASIDE_CONTENT_PHRASE, self_thinking._PRESENCE_ASIDE_CONTENT_PHRASE)
    assert presence != chat
    with pytest.raises(ValueError):
        self_thinking.instruction_for_field(protocol="json", presence=True)


def test_presence_english_instruction_differs_only_in_the_intent():
    assert worker._PRESENCE_WAKE_SELF_THINKING_INSTRUCTION == (
        worker._OPTIONAL_WAKE_SELF_THINKING_INSTRUCTION.replace(
            worker._CHAT_ASIDE_INTENT, worker._PRESENCE_ASIDE_INTENT))
    assert worker._PRESENCE_WAKE_SELF_THINKING_INSTRUCTION != (
        worker._OPTIONAL_WAKE_SELF_THINKING_INSTRUCTION)
