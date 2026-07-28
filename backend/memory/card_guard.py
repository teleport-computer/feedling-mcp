"""记忆卡「模型原始输出泄漏」污染检测 —— 纯字段级判据。

背景(2026-07-28):一张记忆卡的 bucket 被填成模型原始输出残片——一段 harmony 通道元
标记(``analysis to=functions.memory_write``)+ provider 报错回显(``output error code: 400``)
+ 撕裂协议 JSON 尾巴(``…relationship"}]}``),而 title/content 干净。整卡 JSON 合法,所以
``core/protocol_leak`` 的 payload 级拒绝(整条解析失败才拒)拦不住;``card_text`` 的占位符
检测也拦不住(这些残片有实义字符)。

本模块只回答一件事:**这段字段值是不是协议/harmony/报错泄漏**。它是纯函数、零 I/O,
复用 ``core/protocol_leak`` 的通用证据原语(撕裂尾巴 / 协议头),不重复实现;记忆侧的
「硬字段拒卡 / 软字段清洗」路由仍在 ``card_text``,本模块不碰。

设计取舍(codex plan_review 2026-07-28 拍板):
- **报错回显(``output error code: NNN``)只作组合信号**,绝不单独判脏 —— 用户可能在正当
  讨论一个 400。
- **桶的机器 taxonomy 不靠形状规则**:``long_term_preference_or_event_v1`` 与合法的
  ``project_feedling_v1`` 结构完全相同(都是全小写 snake_case + ``_v1``),没有干净形状能
  区分。只用**精确 denylist**列举观测到的残片 + 与强证据共现,其余机器味的桶靠 prompt
  层「优先复用已有/常用桶」引导(另议),不在这里确定性误杀。
"""
from __future__ import annotations

import os
import re

from core import protocol_leak
from memory.prompts_v1 import _text_is_chinese


def guard_enabled() -> bool:
    """Kill switch(默认 ON)。误杀时可关掉止血,不用回滚重部署。

    ⚠️ 纯检测函数(``field_pollution_reason`` 等)刻意**不读 env** —— 只有这一个边界
    函数读,由调用层把结果传进各写入路径。默认 ON:内测无灰度,开关只当回滚闸。
    """
    return os.environ.get("FEEDLING_MEMORY_CARD_GUARD", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )

# harmony tool-route:``to=functions.<name>``(通道前缀 analysis/commentary/final 可有可无)。
_HARMONY_TOOL_ROUTE_RE = re.compile(r"\bto=functions\.\w+")
# harmony 特殊 token:``<|channel|>`` / ``<|message|>`` / ``<|start|>`` 等。
_HARMONY_SPECIAL_RE = re.compile(r"<\|(?:channel|message|start|end|constrain|return)\|>")
# provider 报错回显 —— 仅作组合信号(见模块 docstring),不单独判脏。
_PROVIDER_ERROR_RE = re.compile(r"output error code:\s*\d{3}", re.IGNORECASE)

# 已知机器 taxonomy 残片(精确匹配)。形状无法区分合法自定义桶,故只精确列举。
_BUCKET_DENYLIST = frozenset({
    "long_term_preference_or_event_v1",
})


def field_pollution_reason(text) -> str | None:
    """字段值是协议/harmony/报错泄漏时返回一个短码,否则 ``None``。

    强证据(单独即判脏):harmony tool-route、harmony 特殊 token、协议头、撕裂 JSON 尾巴。
    组合证据:provider 报错回显 + 协议前缀/撕裂尾巴共现。
    """
    t = str(text or "")
    if not t.strip():
        return None
    if _HARMONY_TOOL_ROUTE_RE.search(t):
        return "harmony_tool_route"
    if _HARMONY_SPECIAL_RE.search(t):
        return "harmony_special_token"
    if protocol_leak.looks_like_protocol_head(t):
        return "protocol_head"
    if protocol_leak.is_orphan_json_tail(t):
        return "torn_json_tail"
    if _PROVIDER_ERROR_RE.search(t) and ("to=" in t or protocol_leak.is_orphan_json_tail(t)):
        return "provider_error_echo"
    return None


def bucket_pollution_reason(text) -> str | None:
    """桶专用:字段级泄漏 + 精确 taxonomy denylist。**不含形状规则**(见 docstring)。"""
    t = str(text or "").strip()
    if not t:
        return None
    if t in _BUCKET_DENYLIST:
        return "known_taxonomy_residue"
    return field_pollution_reason(t)


def default_bucket_for_text(text) -> str:
    """本地化默认桶。``normalize_bucket_language`` 只翻译 COMMON pair map 里的桶,
    ``未分类`` 不在其中(codex plan_review 抓到:英文卡拿到 ``未分类`` 不会被转成
    ``Uncategorized``),所以默认值必须在这里按卡片语言直接给对。空文本 → 中文默认。"""
    return "Uncategorized" if (str(text or "").strip() and not _text_is_chinese(text)) else "未分类"
