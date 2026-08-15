"""Runtime V2 的「你是什么模型」自称块（`model_identity.override_block`）。

背景（prod usr_6bb6…，2026-07-25）：V2 的 system prompt 从不告诉 agent 它跑在哪个
模型上，于是用户问「你是什么模型」时 agent 只能猜；一次猜错被用户口头「更正」后，
错误结论进了长期记忆，此后无论 BYOK 路由怎么切都照记忆复读。V1 常驻路径早有
`agent_runtime.spawners._identity_override_block`，V2 没有移植。

这些用例锁住移植后的语义：**每条路由都注入**真实 model id，但文案分两套 —— 第三方/
中转要额外压住"我是 Claude/GPT"的错误自称，官方直连只需钉死精确型号（它确实是那家
的产品，反声明会自相矛盾）。

官方也注入是 2026-07-25 test 实测后的决定：V1 沉默是对的（CLI 壳子知道自己的 model，
实测精确答出 claude-sonnet-4-5），但 V2 是裸 API 调用没有壳子，不注入时 anthropic
自称 "Claude 3.5 Sonnet"（版本错）、openai 自称 "GPT-5"（配置是 gpt-4o-mini）。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import model_identity


def test_official_provider_pins_the_exact_model_id():
    """官方直连也注入：钉死精确型号，但不带第三方那句反声明（它确实是该厂产品）。"""
    block = model_identity.override_block("anthropic", "claude-sonnet-4-5", "")
    assert "claude-sonnet-4-5" in block
    assert "官方直连" in block and "训练印象里的其它模型名" not in block

    gpt = model_identity.override_block("openai", "gpt-4o", "")
    assert "gpt-4o" in gpt
    assert "官方直连" in gpt and "训练印象里的其它模型名" not in gpt


def test_official_provider_with_default_base_url_uses_official_wording():
    # validate_config 会给官方 provider 也持久化默认 base_url，非空 != 非官方。
    block = model_identity.override_block(
        "anthropic", "claude-sonnet-4-5", "https://api.anthropic.com/v1"
    )
    assert "claude-sonnet-4-5" in block and "官方直连" in block
    gpt = model_identity.override_block("openai", "gpt-4o", "https://api.openai.com/v1/")
    assert "gpt-4o" in gpt and "官方直连" in gpt


def test_third_party_block_states_the_real_model_id():
    block = model_identity.override_block(
        "deepseek", "deepseek-chat", "https://api.deepseek.com"
    )
    assert "deepseek-chat" in block
    # 第三方保留反声明，但措辞不能与「id 本身就叫 claude-*」的中转自相矛盾。
    assert "训练印象里的其它模型名" in block
    assert "官方直连" not in block


def test_official_provider_on_custom_base_url_is_treated_as_relay():
    # 中转冒充官方 provider：说出配置的 model id，且必须走第三方文案（带反声明）。
    block = model_identity.override_block(
        "anthropic", "claude-sonnet-4-5", "https://relay.example/anthropic"
    )
    assert "claude-sonnet-4-5" in block
    assert "训练印象里的其它模型名" in block


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
        system_prompt="SYSPROMPT-SENTINEL",
        summary="",
        tail=[{"role": "user", "content": "hi"}],
        **kwargs,
    )
    messages = build_messages([])
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


def test_turn_system_prompt_carries_the_identity_block_for_third_party_routes():
    provider_config = _cfg(
        "deepseek", "deepseek-chat", "https://api.deepseek.com"
    )
    identity_block = model_identity.override_block_for_config(provider_config)
    persona_block = "PERSONA-SENTINEL"
    system = _system_of(
        provider_config=provider_config,
        trusted_system_blocks=(persona_block,),
    )
    assert "deepseek-chat" in system
    # 平台身份事实必须拥有最高优先级，其次才是用户可编辑 persona 和通用提示。
    assert system.startswith(identity_block)
    assert system.index(identity_block) < system.index(persona_block)
    assert system.index(identity_block) < system.index("SYSPROMPT-SENTINEL")


def test_turn_system_prompt_pins_the_model_id_for_official_routes_too():
    system = _system_of(provider_config=_cfg("anthropic", "claude-sonnet-4-5"))
    assert "claude-sonnet-4-5" in system
    assert "官方直连" in system


def test_identity_block_precedes_user_editable_skill_blocks():
    # 用户可编辑的 workspace skill 文本不能排在特权身份声明之前。
    system = _system_of(
        provider_config=_cfg("deepseek", "deepseek-chat", "https://api.deepseek.com"),
        trusted_system_blocks=("<feedling-skill>SKILL</feedling-skill>",),
    )
    assert system.index("deepseek-chat") < system.index("SKILL")


def test_callers_without_a_provider_config_are_unchanged():
    assert "你的真实身份" not in _system_of()
