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


# --- Task 4: usage_out out-param (D4 load-testing needs per-turn token usage) ---


def test_respond_populates_usage_out_when_provider_returns_usage(monkeypatch):
    """`usage_out` is a pure out-param: respond() still returns the stripped text
    unchanged (str, D1 contract preserved), and additionally writes prompt/
    completion token counts into the caller-supplied dict."""
    async def fake_reliable(config, messages, **kwargs):
        return {"reply": "hi", "usage": {"prompt_tokens": 11, "completion_tokens": 7}}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    usage_out: dict = {}
    out = asyncio.run(responder.respond(
        provider_config=cfg,
        summary="",
        tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        usage_out=usage_out,
    ))
    assert out == "hi"
    assert usage_out == {"prompt_tokens": 11, "completion_tokens": 7}


def test_respond_usage_out_is_none_values_when_provider_omits_usage(monkeypatch):
    """Providers differ in whether/how they report usage — no usage key at all
    must not crash respond() and must leave usage_out's values as None."""
    async def fake_reliable(config, messages, **kw):
        return {"reply": "hi"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    usage_out: dict = {}
    out = asyncio.run(responder.respond(
        provider_config=cfg,
        summary="",
        tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        usage_out=usage_out,
    ))
    assert out == "hi"
    assert usage_out == {"prompt_tokens": None, "completion_tokens": None}


def test_respond_usage_out_none_default_is_untouched(monkeypatch):
    """When usage_out isn't passed at all (the default), respond() must not try
    to write anywhere — the D1 call sites that predate this kwarg keep working."""
    async def fake_reliable(config, messages, **kw):
        return {"reply": "hi", "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    out = asyncio.run(responder.respond(
        provider_config=cfg,
        summary="",
        tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
    ))
    assert out == "hi"


def test_respond_accepts_system_prompt_override(monkeypatch):
    """D3 Task 6 (wake lanes): the wake handler needs a different system prompt
    (proactive framing) than the chat default. `system_prompt` is an optional
    kwarg — passing it must make the FIRST system message carry the override
    verbatim instead of `responder._SYSTEM_PROMPT`."""
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
        system_prompt="CUSTOM WAKE PROMPT",
    ))
    messages = seen["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "CUSTOM WAKE PROMPT"
    assert responder._SYSTEM_PROMPT not in messages[0]["content"]


# --- D3 Task 7: ResponderError carries `.kind` (provider_client.classify_provider_error)
# so the wake worker can decide to write a BYOK payment cooldown WITHOUT re-parsing the
# error message string. ---


def test_respond_provider_config_error_sets_kind_provider_config(monkeypatch):
    """A 402-out-of-credits-shaped provider failure must classify as
    "provider_config" (see provider_client.classify_provider_error /
    _PROVIDER_CONFIG_STATUS) and be attached to the raised ResponderError as `.kind`."""
    async def fake_reliable(config, messages, **kw):
        raise provider_client.ProviderError("insufficient credits", status_code=402)

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError) as excinfo:
        asyncio.run(responder.respond(
            provider_config=cfg,
            summary="",
            tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        ))
    assert excinfo.value.kind == "provider_config"


def test_respond_transient_error_sets_kind_transient(monkeypatch):
    """A retryable-shaped failure (e.g. 503) must classify as "transient", NOT
    "provider_config" — the wake worker must not cooldown a key that's just
    having a temporary blip."""
    async def fake_reliable(config, messages, **kw):
        raise provider_client.ProviderError("upstream hiccup", status_code=503)

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError) as excinfo:
        asyncio.run(responder.respond(
            provider_config=cfg,
            summary="",
            tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        ))
    assert excinfo.value.kind == "transient"


def test_respond_empty_reply_kind_is_default_empty_string(monkeypatch):
    """empty_reply/no_user_messages aren't provider errors at all — `.kind` must
    stay at the class-level default ("") rather than some misleading classification."""
    async def fake_reliable(config, messages, **kw):
        return {"reply": "   "}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError) as excinfo:
        asyncio.run(responder.respond(
            provider_config=cfg,
            summary="",
            tail=[{"id": "1", "ts": 1.0, "role": "user", "content": "hi"}],
        ))
    assert excinfo.value.kind == ""


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


# --- Task 1 (D-round): BUG-1 defence-in-depth in `_fold_action_results` ---


def test_fold_action_results_drops_image_blob():
    from model_api_runtime.v2 import responder
    action_results = {
        "chat_image_read": [{"ok": True, "data": {
            "message_id": "m1", "image_mime": "image/jpeg", "image_b64": "A" * 50000}}],
    }
    folded = responder._fold_action_results(action_results)
    assert folded["chat_image_read"]["message_id"] == "m1"
    assert folded["chat_image_read"]["image_mime"] == "image/jpeg"
    assert "image_b64" not in folded["chat_image_read"]


def test_fold_action_results_caps_a_single_oversized_action():
    from model_api_runtime.v2 import responder
    action_results = {
        "memory_fetch": [{"ok": True, "data": {"body": "B" * 50000}}],
        "perception_snapshot": [{"ok": True, "data": {"mood": "calm"}}],
    }
    folded = responder._fold_action_results(action_results)
    assert folded["memory_fetch"]["_truncated"] is True
    assert len(folded["memory_fetch"]["preview"]) <= responder._PER_ACTION_CHAR_CAP
    # The small action must survive intact — the point of the cap is that one
    # fat capability cannot evict the others from the 8000-char context budget.
    assert folded["perception_snapshot"] == {"mood": "calm"}
