"""io 自己的「模型输出残片长什么样」—— 喂给内核的识别器。

内核（``memory_garden.text.card_guard``）只管**判据强弱怎么权衡**：强证据直接判脏、
硬字段要两个弱证据、软字段一个就够。**具体指纹是宿主的**，因为它们照着 io 的协议、
provider 和工具调用格式调，换个宿主既拦不住该拦的、又会误伤正常文本。

## 这些指纹是怎么来的

2026-07-28 的一次事故：一张卡的 bucket 被填成模型原始输出残片 ——
``analysis to=functions.memory_write`` + ``output error code: 400`` +
一截撕裂的协议 JSON 尾巴，而 summary/content 干净。整卡 JSON 合法，所以 payload
级的拒绝拦不住；占位符检测也拦不住（这些残片有实义字符）。

## 强/弱的划分不是随手定的（codex plan_review 2026-07-28 拍板）

    强   harmony 特殊 token、通道词紧邻工具路由 —— 正常内容几乎不可能出现
    弱   裸工具路由、报错回显、协议头、撕裂尾巴 —— 用户可能在正当讨论它们
         （"preserve to=functions.x literally"、"尾部多了 }]}"、"我在查一个 400"）

**报错回显绝不单独判脏**，只作组合信号里的一票。
"""
from __future__ import annotations

import re

from agent_protocol_core import protocol_leak
from memory_garden.text.leak_signals import GENERIC_SIGNALS, LeakSignals, combine

# harmony 特殊 token（强）：``<|channel|>`` / ``<|message|>`` / ``<|start|>`` 等。
_HARMONY_SPECIAL_RE = re.compile(r"<\|(?:channel|message|start|end|constrain|return)\|>")
# 通道前缀 + 工具路由（强）：analysis/commentary/final 紧邻 ``to=functions.<name>``。
# 这正是 07-28 事故串的形态，也是「模型把内部通道文本漏成字段值」的高置信指纹。
_CHANNEL_ROUTE_RE = re.compile(r"\b(?:analysis|commentary|final)\b[^\n]{0,40}?\bto=functions\.\w+")
# 裸工具路由（弱）：单独一个 ``to=functions.x`` —— 正常 prose 也可能字面提到。
_BARE_ROUTE_RE = re.compile(r"\bto=functions\.\w+")
# provider 报错回显（弱）：``output error code: NNN``。
_PROVIDER_ERROR_RE = re.compile(r"output error code:\s*\d{3}", re.IGNORECASE)

#: 已知机器味桶名，精确匹配。形状规则区分不了合法的自定义桶
#: （``long_term_preference_or_event_v1`` 与合法的 ``project_feedling_v1``
#: 结构完全相同），所以只精确列举观测到的残片。
_BUCKET_DENYLIST = frozenset({
    "long_term_preference_or_event_v1",
})


def _harmony_marker(t: str) -> str | None:
    return "harmony_marker" if _HARMONY_SPECIAL_RE.search(t) else None


def _channel_route(t: str) -> str | None:
    return "harmony_marker" if _CHANNEL_ROUTE_RE.search(t) else None


def _bare_route(t: str) -> str | None:
    return "bare_tool_route" if _BARE_ROUTE_RE.search(t) else None


def _protocol_head(t: str) -> str | None:
    """io 自家协议的头：``messages``/``actions`` 键名，或 identity/memory/proactive 动作。"""
    return "protocol_head" if protocol_leak.looks_like_protocol_head(t) else None


def _provider_error(t: str) -> str | None:
    return "provider_error_echo" if _PROVIDER_ERROR_RE.search(t) else None


#: io 那套 = 通用集 + 自家指纹。传给内核的三个判据函数。
IO_LEAK_SIGNALS: LeakSignals = combine(
    GENERIC_SIGNALS,
    LeakSignals(
        strong=(_harmony_marker, _channel_route),
        weak=(_bare_route, _protocol_head, _provider_error),
        bucket_denylist=_BUCKET_DENYLIST,
    ),
)
