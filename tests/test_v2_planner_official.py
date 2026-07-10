"""V2 official 结构化 JSON planner（spec §7.2/7.3）。

守 §7.3 硬不变量：official planner 用**该用户 JIT 解密出的 BYOK key**，绝不用平台 key。
注入 chat_completion_async 探针断言传入的 config.api_key == 用户 key，且平台 key 哨兵从不出现。
解析失败/空 plan → 回退确定性规则（仍零额外 LLM，parity 压 responder）。

official_plan 是原生 async（hosted-runtime-v2 并发修复：worker 直接 await 它，不再经
asyncio.to_thread 桥线程池 —— 见 provider_client.reliable_chat_completion_async 的模块
docstring）；内部 await reliable_chat_completion_async，后者 await chat_completion_async
（不是同步版 chat_completion），故探针挂在 chat_completion_async 上，调用点用 asyncio.run
收口。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402
from capabilities import registry as cap_registry  # noqa: E402
from model_api_runtime.v2 import planner as v2_planner  # noqa: E402

# Vocab reconcile: these control verbs must never survive validate_plan/rule fallback
# for the foreground chat path — capture/dream/sleep belong to subproject D.
# NB: schedule_wake / cancel_wake were retired here originally, but the scheduled-lane
# round promoted them to real WRITE capabilities (registry.WRITE_ACTIONS); they are now
# a legitimate part of the planner vocabulary and MUST survive validate_plan, so they
# are no longer in this retired set.
_REMOVED_CONTROL_VERBS = frozenset({
    "capture_memory", "schedule_followup", "sleep",
})

_USER_KEY = "sk-user-byok-real"
_PLATFORM_SENTINEL = "sk-PLATFORM-NEVER-USE"


def _user_cfg():
    return provider_client.ProviderConfig(
        provider="openai", model="gpt-5", api_key=_USER_KEY, base_url="")


def _kwargs():
    return dict(
        provider_config=_user_cfg(),
        coalesced_messages=[{"content": "plan my week"}],
        digest={"recent": []}, memory_index={"items": [{"id": "m1"}]},
        perception_summary={"now": "morning"},
        runtime_state={"identity": {}},
        lane="chat", reason="user asked",
    )


def test_official_planner_uses_user_byok_key_never_platform(monkeypatch):
    seen = {}

    async def _probe(config, messages, **kw):
        seen["api_key"] = config.api_key
        assert config.api_key != _PLATFORM_SENTINEL, "platform key must NEVER be reached (§7.3)"
        return {"reply": '{"plan":[{"type":"memory_fetch","payload":{"ids":["m1"]}},{"type":"final_response"}],"reason":"ok"}'}

    monkeypatch.setattr(provider_client, "chat_completion_async", _probe)  # reliable_ wraps chat_completion_async
    steps = asyncio.run(v2_planner.official_plan(**_kwargs()))
    assert seen["api_key"] == _USER_KEY
    assert steps[-1]["type"] == "final_response"
    assert steps[0] == {"type": "memory_fetch", "payload": {"ids": ["m1"]}}


def test_official_planner_parses_markdown_fenced_json(monkeypatch):
    async def _probe(config, messages, **kw):
        return {"reply": "```json\n{\"plan\":[{\"type\":\"final_response\"}]}\n```"}
    monkeypatch.setattr(provider_client, "chat_completion_async", _probe)
    steps = asyncio.run(v2_planner.official_plan(**_kwargs()))
    assert steps == [{"type": "final_response", "payload": {}}]


def test_official_planner_falls_back_to_rule_on_garbage(monkeypatch):
    async def _probe(config, messages, **kw):
        return {"reply": "I am not JSON at all, sorry."}
    monkeypatch.setattr(provider_client, "chat_completion_async", _probe)
    steps = asyncio.run(v2_planner.official_plan(**_kwargs()))
    # rule fallback for chat lane with memory_index items → memory_fetch + final_response
    assert steps[-1]["type"] == "final_response"


def test_official_planner_falls_back_to_rule_on_provider_error(monkeypatch):
    async def _boom(config, messages, **kw):
        raise provider_client.ProviderError("down", status_code=503)
    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    steps = asyncio.run(v2_planner.official_plan(**_kwargs()))
    assert steps[-1]["type"] == "final_response"   # never strands the turn


def test_official_planner_drops_removed_control_verbs_and_falls_back(monkeypatch):
    """A stray/unknown-model reply that still tries to speak the retired vocabulary
    (capture_memory/schedule_followup/sleep/...) must never reach the executor as a
    'promised' action — validate_plan drops them, the plan goes empty, and
    official_plan falls back to the deterministic rule planner (chat lane here),
    which itself only emits registry-clean actions."""

    async def _probe(config, messages, **kw):
        return {"reply": json.dumps({
            "plan": [
                {"type": "schedule_followup", "payload": {}},
                {"type": "capture_memory", "payload": {}},
                {"type": "sleep", "payload": {}},
                {"type": "not_a_real_action", "payload": {}},
            ],
            "reason": "junk",
        })}

    monkeypatch.setattr(provider_client, "chat_completion_async", _probe)
    steps = asyncio.run(v2_planner.official_plan(**_kwargs()))

    allowed = set(cap_registry.CAPABILITIES) | {"final_response"}
    assert steps, "expected the rule-plan fallback to produce a non-empty plan"
    for step in steps:
        assert step["type"] not in _REMOVED_CONTROL_VERBS
        assert step["type"] in allowed
    assert steps[-1]["type"] == "final_response"   # chat-lane fallback always replies
