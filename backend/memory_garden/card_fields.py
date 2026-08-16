"""卡片字段映射 —— 「摘要 / 可搜索正文在哪个字段」的**唯一真相源**。

## 背景：两代 schema 并存

    新一代（当前写入端产出的）  summary / content / bucket / threads
    老一代（存量卡里还有）      title / description / her_quote /
                                context / linked_dimension

**新一代是主**：二次蒸馏、做梦、导入现在写的全是它。老一代只为存量卡兜底，
不会再新增 —— 这个分支只减不增，别往上加功能。

## 这里修的是什么

挑卡端（`scoring/relevance.py` 与 `enclave/readside.py`）此前**各写一份字段列表，
而且两份都只认老一代**。写入端早已迁到新一代，读取端留在原地，于是随着数据迁移，
「想不起来」的比例一路上升 —— 不是某个提交一次弄坏的，是渐变失效。

2026-08-16 实测 usr_5a8a31b255ecd942：64 张卡里 45 张是新一代形状，
它们算出来的摘要是空串，被入口过滤整个丢弃。卡在库里、花园界面看得到，
但永远进不了 agent 的候选池。

## 两条硬约束（都踩过）

1. **摘要会外泄，正文不能**。selector 的 skipped/selected trace 会带上 `summary`，
   而 `context_trace=1` 时整个 trace 会返回客户端。所以 `content` **绝不能**
   作为摘要兜底 —— 否则被敏感闸拒掉的卡，正文反而从 trace 漏出去。
   正文只走私有的 `_search_content`，出口一律剥掉。

2. **纯老卡的匹配文本必须逐字节不变**。bigram 是在原始字符串上算的（没有空白归一），
   实测尾部多 3 个空格就会让 jaccard 从 0.066667 变成 0.058824。所以拼接规则必须
   精确到空格，不能"过滤空字段后重新 join"。

## 与下一步的关系

`FieldMap` 是**可替换的值对象**，不是写死的常量：接别的记忆库时传一个不同的实例
即可，挑卡/打分/索引一行都不用改。下一步（已与 hx 拍板）是让每个记忆库带一份
自己的声明式映射 —— 那一步只是多几个实例，注入点就是这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldMap:
    """一个记忆库的字段映射。不可变 —— 换库＝换实例，不是改字段。"""

    #: 取「一句话摘要」的优先级顺序，第一个非空者胜出。
    #: ⚠️ 只能放**可公开**的字段：这个值会进 selector trace，并可能返回客户端。
    summary_fields: tuple[str, ...]

    #: 老一代的匹配字段。拼接时**原样 join**（含空字段留下的空位），
    #: 以保证纯老卡的 haystack 逐字节不变。顺序即历史顺序，不要动。
    legacy_match_fields: tuple[str, ...]

    #: 新一代的匹配字段。按顺序取值、逐项 strip、丢弃空项后再拼。
    canonical_match_fields: tuple[str, ...]

    #: 参与匹配但**不可外泄**的正文字段（进 `_search_content`，不进 summary）。
    private_text_fields: tuple[str, ...] = field(default=())


#: io 自己的映射。接别的库时不要改这里，另建实例。
DEFAULT_FIELD_MAP = FieldMap(
    # content 刻意不在此列 —— 见模块头「硬约束 1」。
    summary_fields=("summary", "description", "title", "context"),
    legacy_match_fields=("title", "description", "her_quote", "context", "linked_dimension"),
    canonical_match_fields=("summary", "content"),
    private_text_fields=("content",),
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def summary_of(card: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> str:
    """取可公开的一句话摘要。取不到就返回空串 —— **不用正文兜底**。

    返回空串是合法结果：只有正文、没有摘要的卡就该显示为空，
    它靠 `private_text(card)` 进候选池，而不是靠把正文伪装成摘要。
    """
    if not isinstance(card, dict):
        return ""
    for key in field_map.summary_fields:
        text = _clean(card.get(key))
        if text:
            return text
    return ""


def private_text(card: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> str:
    """只在内部参与匹配、绝不可序列化的正文。"""
    if not isinstance(card, dict):
        return ""
    parts = [_clean(card.get(key)) for key in field_map.private_text_fields]
    return " ".join(part for part in parts if part)


def text_for_match(card: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> str:
    """拼出参与相关性匹配的全部文本。

    规则精确到空格（见模块头「硬约束 2」）：

        纯老卡（新字段全空）  → 老字段原样 join，**一个字符都不改**
        纯新卡（老字段全空）  → 只拼新字段，不继承老 join 留下的空位
        混合卡                → 老字段原样 join + 单个空格 + 新字段

    混合卡的分数会变 —— 老信息完整保留，只是多了新信息，这是预期的。
    """
    if not isinstance(card, dict):
        return ""

    # 逐字复刻旧实现：不过滤空字段，空字段照样占一个位置。
    legacy_text = " ".join(
        str(card.get(key) or "") for key in field_map.legacy_match_fields
    )
    canonical_parts = [
        text
        for text in (_clean(card.get(key)) for key in field_map.canonical_match_fields)
        if text
    ]

    if not canonical_parts:
        return legacy_text
    if not legacy_text.strip():
        return " ".join(canonical_parts)
    return legacy_text + " " + " ".join(canonical_parts)


def has_matchable_text(card: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> bool:
    """这张卡有没有任何可参与匹配的文本。

    ⚠️ 判据是 `text_for_match`，不是「摘要非空」—— 只有 her_quote 或
    linked_dimension 的卡同样能参与匹配，不该被入口过滤掉。
    """
    return bool(text_for_match(card, field_map).strip())
