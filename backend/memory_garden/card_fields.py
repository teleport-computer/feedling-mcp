"""卡片字段映射 —— 「正文 / 标题 / 线索在哪个字段」的**唯一真相源**。

## 为什么需要这个模块

存量卡片有两种形状，来源不同：

    legacy 形状   title / description / her_quote / context / linked_dimension
                  ← V1 二次蒸馏(memory_capture)、做梦(memory_dream)

    v1 形状       summary / content
                  ← 历史导入(genesis_import)、V2 二次蒸馏(model_api_capture)、
                    resident 吸收(resident_absorb)

在此之前，「读哪些字段」这件事在**两处各写了一份**，而且两份都只认 legacy 形状：

    memory_garden/scoring/relevance.py  _MEMORY_TEXT_FIELDS
    enclave/readside.py                 context_moment_to_index_item

后果（2026-08-16 实测 usr_5a8a31b255ecd942）：64 张卡里 45 张是 v1 形状，
它们算出来的摘要是空串，被 `if item.get("summary")` 整个过滤掉 ——
**卡在库里、花园界面看得到，但永远进不了 agent 的候选池**。用户问「我有一只狗吗」
「磁盘之前满了什么原因」，模型答「记忆里没有」。

## 这里的契约

调用方**只调函数，不认字段名**。要支持新的形状/新的记忆库，只改这个模块。

下一步（已与 hx 定）：把下面写死的字段元组换成**可声明的映射** —— 每个记忆库
带一份自己的字段说明（配置而非代码），contract 不变，读取路径一行都不用动。
这个模块就是那一步的落点。
"""
from __future__ import annotations

from typing import Any

#: 参与相关性匹配的正文字段。**顺序无关**（会被拼成一段 haystack）。
#: legacy 与 v1 两种形状并列 —— 一张卡只会填其中一套，另一套是空。
TEXT_FIELDS: tuple[str, ...] = (
    # legacy 形状
    "title",
    "description",
    "her_quote",
    "context",
    "linked_dimension",
    # v1 形状
    "summary",
    "content",
)

#: 取「一句话摘要」时的**优先级顺序**（第一个非空者胜出）。
#: description/title 在前是为了保持 legacy 卡的既有表现逐字不变；
#: summary/content 补在后面，让 v1 形状的卡不再算出空串。
SUMMARY_FIELDS: tuple[str, ...] = (
    "description",
    "title",
    "summary",
    "content",
    "context",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def text_for_match(card: dict) -> str:
    """拼出参与相关性匹配的全部文本。字段缺失一律当空串，不报错。"""
    if not isinstance(card, dict):
        return ""
    return " ".join(str(card.get(key) or "") for key in TEXT_FIELDS)


def summary_of(card: dict) -> str:
    """取一句话摘要：按 SUMMARY_FIELDS 顺序，第一个非空者胜出。

    返回空串**只应该**发生在这张卡确实没有任何文本时 —— 调用方据此过滤是安全的。
    """
    if not isinstance(card, dict):
        return ""
    for key in SUMMARY_FIELDS:
        text = _clean(card.get(key))
        if text:
            return text
    return ""


def has_text(card: dict) -> bool:
    """这张卡有没有任何可用文本 —— 用来替代「摘要为空就丢掉」那类判断。"""
    return bool(summary_of(card))
