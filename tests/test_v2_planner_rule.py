"""V2 确定性规则 planner（spec §7.3）：弱模型走零 LLM 规则。

守 §7.3 硬不变量：弱模型路径**从不**调用任何 provider（planner 零 LLM）。
注入一个会炸的 chat_completion 探针，断言 is_official=False 时它一次都不被触达。
Pure-unit。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402
from capabilities import registry as cap_registry  # noqa: E402
from model_api_runtime.v2 import planner as v2_planner  # noqa: E402


def _explode(*a, **k):  # platform-key probe: any LLM call is a violation.
    raise AssertionError("weak-model planner must make ZERO LLM calls (§7.3)")


def test_validate_plan_whitelists_and_caps_and_orders():
    raw = {"plan": [
        {"type": "memory_fetch", "payload": {"ids": ["m1"]}},
        {"type": "not_a_real_action"},                       # dropped
        {"type": "final_response"},                          # forced last, once
        {"type": "perception_snapshot"},
        {"type": "screen_recent"},
        {"type": "photo_recent"},
        {"type": "memory_index"},                            # would exceed 5
    ]}
    steps = v2_planner.validate_plan(raw)
    assert len(steps) <= v2_planner.MAX_PLAN_ACTIONS
    assert steps[-1]["type"] == "final_response"
    assert [s["type"] for s in steps].count("final_response") == 1
    assert "not_a_real_action" not in [s["type"] for s in steps]


def test_validate_plan_flat_shape_folds_into_payload():
    raw = {"plan": [{"type": "memory_fetch", "ids": ["m1", "m2"]}]}
    steps = v2_planner.validate_plan(raw)
    assert steps[0] == {"type": "memory_fetch", "payload": {"ids": ["m1", "m2"]}}


def test_rule_plan_chat_lane_reads_then_answers_zero_llm(monkeypatch):
    monkeypatch.setattr(provider_client, "chat_completion", _explode)
    monkeypatch.setattr(provider_client, "reliable_chat_completion", _explode)
    steps = v2_planner.rule_plan(
        coalesced_messages=[{"content": "hello"}],
        memory_index={"items": [{"id": "mem_1"}, {"id": "mem_2"}]},
        lane="chat",
    )
    assert steps[-1]["type"] == "final_response"
    assert steps[0]["type"] == "memory_fetch"
    assert steps[0]["payload"]["ids"] == ["mem_1", "mem_2"]


def test_rule_plan_wake_with_no_input_sleeps():
    # Documents that this deterministic wake fallback is intentional, not accidentally
    # broken by the Task 4 vocab reconcile: `sleep` is a control action interpreted by
    # the wake lane (subproject D), never emitted/consumed by the foreground chat path,
    # and the executor gracefully skips it either way.
    steps = v2_planner.rule_plan(coalesced_messages=[], memory_index={}, lane="manual_wake")
    assert steps == [{"type": "sleep", "payload": {"reason": "no_visible_input"}}]


def test_rule_plan_chat_lane_only_emits_registry_clean_actions():
    """CHAT-lane rule_plan must only ever emit action types the executor can actually
    run (registry.CAPABILITIES) plus the model-authored final_response — never sleep
    or any of the removed schedule_*/capture_memory control strings."""
    allowed = set(cap_registry.CAPABILITIES) | {"final_response"}
    steps = v2_planner.rule_plan(
        coalesced_messages=[{"content": "hello"}],
        memory_index={"items": [{"id": "mem_1"}]},
        lane="chat",
    )
    assert steps, "expected a non-empty plan for chat lane"
    for step in steps:
        assert step["type"] in allowed, f"unexpected action type: {step['type']}"


def test_plan_weak_model_uses_rule_path_zero_llm(monkeypatch):
    # plan() is natively async (hosted-runtime-v2 concurrency fix — worker awaits it
    # directly, no asyncio.to_thread bridge); is_official=False must still resolve
    # to the zero-LLM rule_plan path without ever touching a provider call.
    monkeypatch.setattr(provider_client, "chat_completion", _explode)
    monkeypatch.setattr(provider_client, "reliable_chat_completion", _explode)
    weak_cfg = provider_client.ProviderConfig(
        provider="openrouter", model="x", api_key="sk-user-byok-real", base_url="")
    steps = asyncio.run(v2_planner.plan(
        store=None, provider_config=weak_cfg,
        is_official=False, coalesced_messages=[{"content": "hi"}],
        digest={}, memory_index={"items": []}, perception_summary={},
        runtime_state={}, lane="chat", reason="",
    ))
    assert steps[-1]["type"] == "final_response"
