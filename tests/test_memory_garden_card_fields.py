"""字段映射：摘要不许漏正文、纯老卡逐字节不变。

这两条是本批的硬约束（都是踩出来的，不是设计洁癖）：

  1. `summary` 会进 selector 的 skipped/selected trace，而 `context_trace=1`
     时整个 trace 会返回客户端。所以 content **绝不能**作为摘要兜底 ——
     否则被敏感闸拒掉的卡，正文反而从 trace 漏出去。

  2. bigram 在原始字符串上算（无空白归一），实测尾部多 3 个空格就让 jaccard
     从 0.066667 掉到 0.058824。所以拼接规则必须精确到空格。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from memory_garden import card_fields  # noqa: E402

#: 改造前 `_text_for_memory` 的实现，逐字复刻。纯老卡必须与它输出完全一致。
_LEGACY_FIELDS = ("title", "description", "her_quote", "context", "linked_dimension")


def _legacy_join(card: dict) -> str:
    return " ".join(str(card.get(key) or "") for key in _LEGACY_FIELDS)


# --------------------------------------------------------------------------- #
# 硬约束 1：摘要不许漏正文
# --------------------------------------------------------------------------- #


def test_content_is_never_used_as_summary():
    """只有正文的卡 —— 摘要必须留空，宁可显示空也不让正文漏进 trace。"""
    card = {"content": "报告显示需要复查甲状腺，医生建议三个月后再查"}
    assert card_fields.summary_of(card) == ""


def test_summary_prefers_the_canonical_field():
    """新一代是主：summary 在，就用 summary。"""
    card = {"summary": "新的摘要", "description": "老的描述", "title": "老的标题"}
    assert card_fields.summary_of(card) == "新的摘要"


def test_summary_falls_back_through_legacy_fields():
    assert card_fields.summary_of({"description": "描述", "title": "标题"}) == "描述"
    assert card_fields.summary_of({"title": "标题"}) == "标题"
    assert card_fields.summary_of({"context": "上下文"}) == "上下文"
    assert card_fields.summary_of({}) == ""


def test_content_only_card_still_has_matchable_text():
    """摘要为空 ≠ 这张卡没用 —— 它要靠私有正文进候选池。"""
    card = {"content": "Docker 构建缓存占满了磁盘"}
    assert card_fields.summary_of(card) == ""
    assert card_fields.private_text(card) == "Docker 构建缓存占满了磁盘"
    assert card_fields.has_matchable_text(card) is True


# --------------------------------------------------------------------------- #
# 硬约束 2：纯老卡逐字节不变
# --------------------------------------------------------------------------- #


_LEGACY_CASES = [
    {"title": "磁盘", "description": "曾经到过 99%", "her_quote": "", "context": "", "linked_dimension": ""},
    {"title": "", "description": "只有描述", "her_quote": "", "context": "", "linked_dimension": ""},
    {"title": "首", "description": "", "her_quote": "", "context": "", "linked_dimension": "尾"},
    {"title": "", "description": "", "her_quote": "只有原话", "context": "", "linked_dimension": ""},
    {},
    {"title": "全", "description": "都", "her_quote": "有", "context": "值", "linked_dimension": "的"},
]


def test_pure_legacy_card_matches_the_old_join_byte_for_byte():
    for card in _LEGACY_CASES:
        assert card_fields.text_for_match(card) == _legacy_join(card), (
            f"纯老卡的匹配文本变了 —— 分数会漂：{card}"
        )


def test_pure_legacy_keeps_interior_and_trailing_blanks():
    """空字段留下的空位也要保留 —— 这正是「不能过滤后重新 join」的原因。"""
    card = {"title": "首", "linked_dimension": "尾"}
    assert card_fields.text_for_match(card) == "首    尾"  # 5 字段 → 4 个分隔符


# --------------------------------------------------------------------------- #
# 新卡与混合卡
# --------------------------------------------------------------------------- #


def test_pure_canonical_card_does_not_inherit_legacy_padding():
    """纯新卡不该背上老 join 留下的四个前导空格。"""
    card = {"summary": "磁盘满了", "content": "DerivedData 堆积"}
    assert card_fields.text_for_match(card) == "磁盘满了 DerivedData 堆积"


def test_canonical_text_actually_participates_in_matching():
    """本批要修的核心：新一代字段必须出现在匹配文本里。"""
    card = {"summary": "磁盘故障复盘", "content": "原因是 Docker 构建缓存占满"}
    hay = card_fields.text_for_match(card)
    assert "Docker" in hay, "正文没进匹配文本 —— 正文里的关键词照样搜不到"


def test_hybrid_card_keeps_legacy_prefix_then_appends_canonical():
    card = {"title": "首", "linked_dimension": "尾", "summary": "新摘要"}
    assert card_fields.text_for_match(card) == "首    尾 新摘要"


def test_empty_canonical_values_do_not_add_separators():
    card = {"title": "首", "description": "描述", "summary": "  ", "content": ""}
    assert card_fields.text_for_match(card) == _legacy_join(card)


# --------------------------------------------------------------------------- #
# 映射是可替换的（下一步的注入点）
# --------------------------------------------------------------------------- #


def test_field_map_is_immutable():
    import pytest

    with pytest.raises(Exception):
        card_fields.DEFAULT_FIELD_MAP.summary_fields = ()  # type: ignore[misc]


def test_a_different_field_map_reads_a_different_library_shape():
    """接别的记忆库＝换一个实例，挑卡/打分/索引一行都不用改。"""
    notion = card_fields.FieldMap(
        summary_fields=("Name",),
        legacy_match_fields=(),
        canonical_match_fields=("Name", "Notes"),
        private_text_fields=("Notes",),
    )
    record = {"Name": "磁盘满了", "Notes": "DerivedData 堆积"}
    assert card_fields.summary_of(record, notion) == "磁盘满了"
    assert card_fields.private_text(record, notion) == "DerivedData 堆积"
    assert card_fields.text_for_match(record, notion) == "磁盘满了 DerivedData 堆积"
