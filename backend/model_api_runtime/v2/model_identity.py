"""Runtime V2 的模型自称块：让 agent 如实回答「你是什么模型」。

V2 的 system prompt 此前不含任何真实模型信息，agent 只能猜自己跑在哪个模型上。
prod 实测（usr_6bb6…，2026-07-25）：猜错一次后被用户口头「更正」，错误结论写进了
长期记忆，此后 BYOK 路由切到别的 provider 也照记忆复读旧答案。这个块把当回合真实
生效的 provider/model 作为特权指令送进 system 位，切换路由即刻生效。

与 V1 常驻路径 ``agent_runtime.spawners._identity_override_block`` 是同一套语义
（官方原生沉默 / 第三方如实自称 / 清洗中转营销标签），**故意不共用实现**：V1 的文案
要额外压住命令行外壳（Claude Code / Codex）的自称，V2 是纯 API 路径没有外壳。改动
任一侧的判定语义时请同步另一侧。
"""
from __future__ import annotations

import re
from typing import Any

import provider_client

# 中转/经销商会把真实 model id 包在营销标签里，例如 ``[Kiro] claude-opus-4-6 【不补】``。
# 只在自称时剥掉：路由与计费仍用原始 model 串。
_IDENTITY_TAG_RE = re.compile(r"(?:\[[^\]]*\]|【[^】]*】)")


def _sanitize_model_name(raw: str) -> str:
    s = str(raw or "")
    return re.sub(r"\s+", " ", _IDENTITY_TAG_RE.sub(" ", s)).strip()


def _is_official(provider: str, base_url: str) -> bool:
    """True 当该路由是官方原生 endpoint——用「钉死精确型号」那套文案。

    官方 provider（anthropic/openai）的 base_url 为空或等于该 provider 的**默认**
    endpoint 才算官方——``validate_config`` 会给官方 provider 也持久化默认 base_url，
    单纯「非空」不能作判据。自定义/非默认 endpoint 把官方 provider 翻成非官方（中转
    冒充防御，走带反声明的第三方文案）。非 anthropic/openai 的 provider 一律非官方。
    """
    p = provider_client.normalize_provider(provider)
    if p not in {"anthropic", "openai"}:
        return False
    bu = str(base_url or "").strip().rstrip("/")
    if not bu:
        return True
    return bu == provider_client.default_base_url(p).strip().rstrip("/")


_AUTHORITY = (
    "这条以当前生效的运行时配置为准：它比你的训练印象、对话历史、摘要或记忆里任何关于"
    "你模型身份的说法都更新、更权威，与它们冲突时一律以本段为准，不要复述旧答案。"
    "（这不影响你作为陪伴角色的人设——问「你是谁」仍按人设回答，仅「什么模型 / 什么 AI」时说真实模型。）"
)


def override_block(provider: str, model: str, base_url: str) -> str:
    """特权自称块；provider 缺省时返回 ""。

    文案分两套。第三方/中转要额外压住「我是 Claude/GPT」的错误自称；官方直连只钉死
    精确型号——它确实是该厂产品，带反声明会自相矛盾。两套都注入：V2 是裸 API 调用，
    没有 CLI 壳子提供准确身份，模型对自己的具体版本号往往是错的（2026-07-25 test
    实测：anthropic 自称 "Claude 3.5 Sonnet"、openai 自称 "GPT-5"）。

    自称内容源为配置的 model id（空则回退 provider 名，再回退通用串）。刻意与 persona
    人设解耦：只压「什么模型 / 什么 AI」类元问题，不动「你是谁」的角色扮演。
    """
    normalized = provider_client.normalize_provider(provider)
    if not normalized:
        # provider 缺省只出现在 legacy/native/default 路径，改写会误伤原生身份。
        return ""
    name = _sanitize_model_name(model) or str(provider or "").strip() or "a third-party model"
    if _is_official(provider, base_url):
        return (
            "## 你的真实身份\n"
            f"你的底层大模型是 `{name}`（{normalized} 官方直连）。当用户问你是什么模型、"
            f"哪个版本、用的什么 AI 时，如实回答 `{name}` 这个精确型号，不要说成别的版本号"
            "或只给系列名。" + _AUTHORITY
        )
    # 反声明刻意不写死「不要自称 Claude」：中转卖的常就是 claude-*/gpt-*，清洗后的
    # id 本身带这些字样，写死会和上一句「如实回答你是 claude-opus-4-6」自相矛盾
    # （V1 文案的原始毛病）。改成压住「用训练印象替换配置值」这个真正的错误动作。
    return (
        "## 你的真实身份\n"
        f"你的底层大模型是 `{name}`，经第三方 endpoint 接入（不是模型厂商的官方服务）。"
        f"当用户问你是什么模型、由谁提供、用的什么 AI 时，如实回答 `{name}`，"
        "不要用训练印象里的其它模型名或厂商替换它。" + _AUTHORITY
    )


def override_block_for_config(provider_config: Any) -> str:
    """从当回合真实生效的 ``ProviderConfig`` 取身份块；无 config 时静默返回 ""。"""
    if provider_config is None:
        return ""
    return override_block(
        str(getattr(provider_config, "provider", "") or ""),
        str(getattr(provider_config, "model", "") or ""),
        str(getattr(provider_config, "base_url", "") or ""),
    )
