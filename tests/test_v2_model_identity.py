"""Runtime V2 的「你是什么模型」自称块（`model_identity.override_block`）。

背景（prod usr_6bb6…，2026-07-25）：V2 的 system prompt 从不告诉 agent 它跑在哪个
模型上，于是用户问「你是什么模型」时 agent 只能猜；一次猜错被用户口头「更正」后，
错误结论进了长期记忆，此后无论 BYOK 路由怎么切都照记忆复读。V1 常驻路径早有
`agent_runtime.spawners._identity_override_block`，V2 没有移植。

这些用例锁住移植后的语义：官方原生 provider 保持沉默（壳子自称即真身），第三方 /
中转一律注入真实 model id。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import model_identity


def test_official_anthropic_and_openai_get_no_block():
    # 官方直连时 agent 自称 Claude / GPT 就是事实，注入反而制造矛盾。
    assert model_identity.override_block("anthropic", "claude-sonnet-4-5", "") == ""
    assert model_identity.override_block("openai", "gpt-4o", "") == ""


def test_official_provider_with_default_base_url_still_official():
    # validate_config 会给官方 provider 也持久化默认 base_url，非空 != 非官方。
    assert model_identity.override_block(
        "anthropic", "claude-sonnet-4-5", "https://api.anthropic.com/v1"
    ) == ""
    assert model_identity.override_block(
        "openai", "gpt-4o", "https://api.openai.com/v1/"
    ) == ""


def test_third_party_block_states_the_real_model_id():
    block = model_identity.override_block(
        "deepseek", "deepseek-chat", "https://api.deepseek.com"
    )
    assert "deepseek-chat" in block
    assert "Claude" in block  # 明确反声明：不要自称 Claude/Anthropic 的产品


def test_official_provider_on_custom_base_url_is_treated_as_relay():
    # 中转冒充官方 provider：仍要如实说出配置的 model id。
    block = model_identity.override_block(
        "anthropic", "claude-sonnet-4-5", "https://relay.example/anthropic"
    )
    assert "claude-sonnet-4-5" in block


def test_reseller_marketing_tags_are_stripped_from_self_reference():
    block = model_identity.override_block(
        "openai_compatible", "[Kiro] claude-opus-4-6 【不补】", "https://relay.example/v1"
    )
    assert "claude-opus-4-6" in block
    assert "Kiro" not in block
    assert "不补" not in block


def test_empty_model_falls_back_to_provider_name():
    assert "gemini" in model_identity.override_block("gemini", "", "")


def test_absent_provider_stays_silent():
    # provider 缺省只出现在 legacy/native 路径，改写它们会误伤原生身份。
    assert model_identity.override_block("", "", "") == ""
    assert model_identity.override_block("  ", "gpt-4o", "https://x") == ""


def test_block_from_provider_config_reads_the_live_route():
    import provider_client

    cfg = provider_client.ProviderConfig(
        provider="deepseek",
        model="deepseek-chat",
        api_key="k",
        base_url="https://api.deepseek.com",
    )
    assert "deepseek-chat" in model_identity.override_block_for_config(cfg)
    assert model_identity.override_block_for_config(None) == ""


# --- 接线：身份块必须真的进到回合的 system 位 ---------------------------------


def _cfg(provider: str, model: str, base_url: str = ""):
    import provider_client

    return provider_client.ProviderConfig(
        provider=provider, model=model, api_key="k", base_url=base_url
    )


def _system_of(**kwargs) -> str:
    from model_api_runtime.v2 import worker

    build_messages = worker._make_build_messages_fn(
        system_prompt="SYS", summary="", tail=[{"role": "user", "content": "hi"}], **kwargs
    )
    messages = build_messages([])
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


def test_turn_system_prompt_carries_the_identity_block_for_third_party_routes():
    system = _system_of(
        provider_config=_cfg("deepseek", "deepseek-chat", "https://api.deepseek.com")
    )
    assert "deepseek-chat" in system
    # 特权 system 位，不能降级成 untrusted 数据块。
    assert system.startswith("SYS\n\n")


def test_turn_system_prompt_stays_silent_for_official_routes():
    system = _system_of(provider_config=_cfg("anthropic", "claude-sonnet-4-5"))
    assert "你的真实身份" not in system


def test_identity_block_precedes_user_editable_skill_blocks():
    # 用户可编辑的 workspace skill 文本不能排在特权身份声明之前。
    system = _system_of(
        provider_config=_cfg("deepseek", "deepseek-chat", "https://api.deepseek.com"),
        trusted_system_blocks=("<feedling-skill>SKILL</feedling-skill>",),
    )
    assert system.index("deepseek-chat") < system.index("SKILL")


def test_callers_without_a_provider_config_are_unchanged():
    assert "你的真实身份" not in _system_of()
