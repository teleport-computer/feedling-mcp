"""最小 responder：把合并消息交给用户 BYOK provider，返回 model-authored 文本。

respond() 是原生 async（hosted-runtime-v2 并发修复 —— 见 provider_client.
reliable_chat_completion_async 的模块docstring）：内部 await 该 async 包裹，故这里的
探针是 async 函数、monkeypatch 目标是 `reliable_chat_completion_async`，调用点用
`asyncio.run` 收口。"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client
from model_api_runtime.v2 import responder


def test_respond_returns_provider_reply(monkeypatch):
    seen = {}

    async def fake_reliable(config, messages, **kwargs):
        seen["config"] = config
        seen["messages"] = messages
        return {"reply": "  hello from model  ", "usage": {}}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    out = asyncio.run(responder.respond(
        provider_config=cfg,
        coalesced_messages=[{"role": "user", "content": "hi"}],
        runtime_state={},
    ))
    assert out == "hello from model"           # 去空白
    assert seen["config"] is cfg               # 用的是传入的 BYOK config
    # 合并的用户消息被带进 provider 请求
    assert {"role": "user", "content": "hi"} in seen["messages"]


def test_respond_raises_on_empty_reply(monkeypatch):
    async def fake_reliable(config, messages, **kw):
        return {"reply": "   "}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError):
        asyncio.run(responder.respond(
            provider_config=cfg,
            coalesced_messages=[{"role": "user", "content": "hi"}],
            runtime_state={},
        ))


def test_respond_raises_on_no_user_messages():
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError):
        asyncio.run(responder.respond(provider_config=cfg, coalesced_messages=[], runtime_state={}))


def test_respond_uses_only_the_injected_byok_config_no_platform_key_path(monkeypatch):
    """Hard invariant: BYOK-only. The responder must call reliable_chat_completion_async
    with EXACTLY the provider_config that was passed in — no substitution, no
    fallback to any platform/system key, no second call path."""
    calls = []

    async def fake_reliable(config, messages, **kwargs):
        calls.append(config)
        return {"reply": "ok"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    # A sentinel object (not even a real ProviderConfig) — proves respond() never
    # constructs or resolves its own config; it only forwards what it was given.
    sentinel_cfg = object()
    asyncio.run(responder.respond(
        provider_config=sentinel_cfg,
        coalesced_messages=[{"role": "user", "content": "hi"}],
        runtime_state={},
    ))
    assert len(calls) == 1
    assert calls[0] is sentinel_cfg


# --- Plan C §7.5: responder folds executor action_results into the prompt ---
# `action_results` is an ADDITIVE, defaulted (`None`) kwarg — the tests above (Plan B,
# no action_results passed at all) must keep passing unchanged; these only add coverage
# for the new behaviour.


def test_respond_folds_action_results_into_provider_messages(monkeypatch):
    seen = {}

    async def fake_reliable(config, messages, **kwargs):
        seen["messages"] = messages
        return {"reply": "  你昨天提过这件事。  "}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    reply = asyncio.run(responder.respond(
        provider_config=cfg,
        coalesced_messages=[{"role": "user", "content": "still on for tmr?"}],
        runtime_state={"identity": {"agent_name": "小克"}},
        action_results={
            "memory_fetch": [{"ok": True, "data": {"cards": ["REMEMBERED-FACT"]}}],
        },
    ))
    blob = "".join(str(m.get("content") or "") for m in seen["messages"])
    assert "REMEMBERED-FACT" in blob
    assert reply == "你昨天提过这件事。"


def test_respond_ignores_action_results_when_missing_or_not_ok(monkeypatch):
    """Only ok=True runs with non-empty data get folded; failed/empty runs are dropped
    silently rather than leaking a failure trace into the model-visible prompt."""
    seen = {}

    async def fake_reliable(config, messages, **kwargs):
        seen["messages"] = messages
        return {"reply": "ok"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    asyncio.run(responder.respond(
        provider_config=cfg,
        coalesced_messages=[{"role": "user", "content": "hi"}],
        runtime_state={},
        action_results={
            "memory_fetch": [{"ok": False, "data": {"cards": ["SHOULD-NOT-APPEAR"]}}],
            "perception_fetch": [{"ok": True, "data": None}],
        },
    ))
    blob = "".join(str(m.get("content") or "") for m in seen["messages"])
    assert "SHOULD-NOT-APPEAR" not in blob


def test_respond_action_results_none_is_the_same_as_omitted(monkeypatch):
    """Explicitly passing action_results=None must behave exactly like the Plan B
    call form that never mentions the kwarg at all."""
    async def fake_reliable(config, messages, **kw):
        return {"reply": "ok"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    out = asyncio.run(responder.respond(
        provider_config=cfg,
        coalesced_messages=[{"role": "user", "content": "hi"}],
        runtime_state={},
        action_results=None,
    ))
    assert out == "ok"


def test_respond_action_results_never_reaches_platform_key_either(monkeypatch):
    """BYOK-only (§7.3) must hold even when action_results is populated: still exactly
    the injected provider_config, no substitution."""
    calls = []

    async def fake_reliable(config, messages, **kwargs):
        calls.append(config)
        return {"reply": "ok"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    sentinel_cfg = object()
    asyncio.run(responder.respond(
        provider_config=sentinel_cfg,
        coalesced_messages=[{"role": "user", "content": "hi"}],
        runtime_state={},
        action_results={"memory_fetch": [{"ok": True, "data": {"cards": ["x"]}}]},
    ))
    assert len(calls) == 1
    assert calls[0] is sentinel_cfg
