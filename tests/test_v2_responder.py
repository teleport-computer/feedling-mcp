"""最小 responder：把 summary + tail 交给用户 BYOK provider，返回 model-authored 文本。

respond() 是原生 async（hosted-runtime-v2 并发修复 —— 见 provider_client.
reliable_chat_completion_async 的模块docstring）：内部 await 该 async 包裹，故这里的
探针是 async 函数、monkeypatch 目标是 `reliable_chat_completion_async`，调用点用
`asyncio.run` 收口。

respond() 现在消费 `summary`（早前对话摘要字符串）+ `tail`（双角色逐条消息列表），
组装委托给纯函数 `context.build_turn_messages`（Task 1）——不再是 coalesced_messages
（仅 user）+ runtime_state。"""
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
        summary="- prior stuff",
        tail=[
            {"id": "1", "ts": 1.0, "role": "user", "content": "hi"},
            {"id": "2", "ts": 2.0, "role": "openclaw", "content": "hey"},
            {"id": "3", "ts": 3.0, "role": "user", "content": "now"},
        ],
    ))
    assert out == "hello from model"           # 去空白
    assert seen["config"] is cfg               # 用的是传入的 BYOK config

    messages = seen["messages"]
    # 系统提示打头
    assert messages[0]["role"] == "system"
    assert responder._SYSTEM_PROMPT in messages[0]["content"]
    # 摘要作为 system block 出现
    assert any("prior stuff" in m["content"] for m in messages if m["role"] == "system")
    # tail 的 user/assistant/user 三条按序出现（跳过 system 消息）
    turn_roles = [m["role"] for m in messages if m["role"] != "system"]
    assert turn_roles == ["user", "assistant", "user"]


def test_respond_raises_on_empty_reply(monkeypatch):
    async def fake_reliable(config, messages, **kw):
        return {"reply": "   "}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError):
        asyncio.run(responder.respond(
            provider_config=cfg,
            summary="",
            tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        ))


def test_respond_raises_on_empty_tail():
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError):
        asyncio.run(responder.respond(provider_config=cfg, summary="", tail=[]))


def test_respond_raises_on_all_blank_tail():
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError):
        asyncio.run(responder.respond(
            provider_config=cfg,
            summary="",
            tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "   "}],
        ))


def test_respond_raises_on_provider_error(monkeypatch):
    async def fake_reliable(config, messages, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError, match="provider_call_failed: RuntimeError"):
        asyncio.run(responder.respond(
            provider_config=cfg,
            summary="",
            tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        ))


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
        summary="",
        tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
    ))
    assert len(calls) == 1
    assert calls[0] is sentinel_cfg


# --- Plan C §7.5: responder folds executor action_results into the prompt ---
# `action_results` is an ADDITIVE, defaulted (`None`) kwarg — these only add coverage
# for the new behaviour; the tests above (no action_results passed at all) must keep
# passing unchanged.


def test_respond_folds_action_results_into_provider_messages(monkeypatch):
    seen = {}

    async def fake_reliable(config, messages, **kwargs):
        seen["messages"] = messages
        return {"reply": "  你昨天提过这件事。  "}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    reply = asyncio.run(responder.respond(
        provider_config=cfg,
        summary="",
        tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "still on for tmr?"}],
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
        summary="",
        tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        action_results={
            "memory_fetch": [{"ok": False, "data": {"cards": ["SHOULD-NOT-APPEAR"]}}],
            "perception_fetch": [{"ok": True, "data": None}],
        },
    ))
    blob = "".join(str(m.get("content") or "") for m in seen["messages"])
    assert "SHOULD-NOT-APPEAR" not in blob


def test_respond_action_results_none_is_the_same_as_omitted(monkeypatch):
    """Explicitly passing action_results=None must behave exactly like the call form
    that never mentions the kwarg at all."""
    async def fake_reliable(config, messages, **kw):
        return {"reply": "ok"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    out = asyncio.run(responder.respond(
        provider_config=cfg,
        summary="",
        tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
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
        summary="",
        tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        action_results={"memory_fetch": [{"ok": True, "data": {"cards": ["x"]}}]},
    ))
    assert len(calls) == 1
    assert calls[0] is sentinel_cfg
