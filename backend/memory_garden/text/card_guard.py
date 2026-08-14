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

from agent_protocol_core import protocol_leak
from ..prompts.buckets import _text_is_chinese


def guard_enabled() -> bool:
    """Kill switch(默认 ON)。误杀时可关掉止血,不用回滚重部署。

    ⚠️ 纯检测函数(``field_pollution_reason`` 等)刻意**不读 env** —— 只有这一个边界
    函数读,由调用层把结果传进各写入路径。默认 ON:内测无灰度,开关只当回滚闸。
    """
    return os.environ.get("FEEDLING_MEMORY_CARD_GUARD", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )

# harmony 特殊 token(强证据):``<|channel|>`` / ``<|message|>`` / ``<|start|>`` 等。
_HARMONY_SPECIAL_RE = re.compile(r"<\|(?:channel|message|start|end|constrain|return)\|>")
# 通道前缀 + tool-route(强证据):analysis/commentary/final 通道词紧邻 ``to=functions.<name>``。
# 这正是本次事故串的形态,也是「模型把内部通道文本漏成字段值」的高置信指纹。
_CHANNEL_ROUTE_RE = re.compile(r"\b(?:analysis|commentary|final)\b[^\n]{0,40}?\bto=functions\.\w+")
# 裸 tool-route(弱证据):单独一个 ``to=functions.x`` —— 正常 prose 也可能字面提到它。
_BARE_ROUTE_RE = re.compile(r"\bto=functions\.\w+")
# provider 报错回显(弱证据):``output error code: NNN``。
_PROVIDER_ERROR_RE = re.compile(r"output error code:\s*\d{3}", re.IGNORECASE)

# 已知机器 taxonomy 残片(精确匹配)。形状无法区分合法自定义桶,故只精确列举。
_BUCKET_DENYLIST = frozenset({
    "long_term_preference_or_event_v1",
})


def _strong_signal(t: str) -> bool:
    """强证据:harmony 特殊 token,或通道前缀紧邻 tool-route。正常内容几乎不可能出现。"""
    return bool(_HARMONY_SPECIAL_RE.search(t) or _CHANNEL_ROUTE_RE.search(t))


def _weak_signal_count(t: str) -> int:
    """弱证据个数。**单独一个弱证据不足以判硬字段脏** —— 用户贴 JSON / 讨论 400 /
    字面提到 ``to=functions.x`` 都会命中单个弱证据(codex code_review:``ORPHAN_JSON_TAIL``
    自身就被 protocol_leak 标注为「无法和用户贴 JSON 区分」的弱信号)。"""
    n = 0
    if _BARE_ROUTE_RE.search(t):
        n += 1
    if protocol_leak.looks_like_protocol_head(t):
        n += 1
    if protocol_leak.is_orphan_json_tail(t):
        n += 1
    if _PROVIDER_ERROR_RE.search(t):
        n += 1
    return n


def hard_field_pollution_reason(text) -> str | None:
    """**硬字段(summary/content)**判据 —— 误杀代价高(整卡丢弃 / 400),故从严:
    只在**强证据**、或**≥2 个弱证据共现**时判脏。本次事故串含通道前缀+route(强),仍被抓。

    这样 ``The API fixture is {"cards": []}``、``preserve to=functions.x literally``、
    ``尾部多了 "enabled": true}]}`` 这类开发者正常记忆(各只命中一个弱证据)不会被误杀。
    """
    t = str(text or "")
    if not t.strip():
        return None
    if _strong_signal(t):
        return "harmony_marker"
    if _weak_signal_count(t) >= 2:
        return "multi_weak_leak"
    return None


def field_pollution_reason(text) -> str | None:
    """**软字段(bucket/threads)**判据 —— 都是短标签,误杀=就地清洗/丢弃、代价低,故从宽:
    强证据或**任一弱证据**即判脏。正常短标签(``工作`` / ``user_onboarding``)不会命中弱证据。
    """
    t = str(text or "")
    if not t.strip():
        return None
    if _strong_signal(t):
        return "harmony_marker"
    if _BARE_ROUTE_RE.search(t):
        return "bare_tool_route"
    if protocol_leak.looks_like_protocol_head(t):
        return "protocol_head"
    if protocol_leak.is_orphan_json_tail(t):
        return "torn_json_tail"
    # 注:报错回显不在软字段单独判脏 —— 到这里真正的 to=functions / 撕裂尾巴已被上面返回,
    # 剩下的 "output error code" + 松散 "to=" 子串(redirect_to=/邮箱)几乎全是假阳性
    # (codex code_review)。报错回显只在 _weak_signal_count 里作为「需第二个弱证据」的一票。
    return None


def bucket_pollution_reason(text) -> str | None:
    """桶专用:软字段级泄漏 + 精确 taxonomy denylist。**不含形状规则**(见 docstring)。"""
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
