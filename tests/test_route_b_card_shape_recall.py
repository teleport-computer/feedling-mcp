"""Route B 召回：新一代形状的卡必须能被想起来，且正文绝不外泄。

2026-08-16 线上事故（usr_5a8a31b255ecd942）：
用户问「我有一只狗吗」「磁盘之前满了什么原因」，模型答「记忆里没有」——
但卡就在库里，花园界面看得到。

根因：写入端早已迁到新一代形状（summary/content），挑卡端还停在老一代
（title/description/...）。随着数据迁移，读不到的比例一路上升 ——
不是某个提交一次弄坏的，是渐变失效。

本文件用**真实卡形状**做验收，不用简化桩。
"""
from __future__ import annotations

import json
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from enclave import readside  # noqa: E402

#: 用户真实卡的形状：genesis 导入写的就是这样，title/description 是空的。
DOG = {
    "id": "m_dog",
    "title": None,
    "description": None,
    "summary": "hx 有一只狗",
    "content": "hx 养了一只狗，平时会带它散步。",
    "occurred_at": "",
    "created_at": "2026-08-10T10:11:55",
}
DISK = {
    "id": "m_disk",
    "title": None,
    "description": None,
    "summary": "磁盘故障复盘",
    # 关键词只在正文里 —— 这是「只让摘要参与匹配」修不掉的那一半
    "content": "原因是 iOS DerivedData 堆积，单个 800M 起步。",
    "occurred_at": "",
    "created_at": "2026-08-10T10:12:00",
}
LEGACY = {
    "id": "m_legacy",
    "title": "跑步",
    "description": "hx 最近开始规律跑步",
    "summary": None,
    "content": None,
    "occurred_at": "2026-08-14T03:55:24Z",
    "created_at": "2026-08-14T03:55:24Z",
}


# --------------------------------------------------------------------------- #
# 召回
# --------------------------------------------------------------------------- #


def test_canonical_shape_card_can_be_recalled():
    """本次事故的直接验收：问狗，要能想起那张狗的卡。"""
    selected, _ = readside.select_context_memories_via_readside(
        [DOG, LEGACY], "我有一只狗吗", cap=8
    )
    assert "m_dog" in [c["id"] for c in selected], "新一代形状的卡仍然进不了上下文"


def test_keyword_only_in_content_is_still_found():
    """摘要里没有、正文里有 —— 也要能召回（Codex 指出的「只修一半」）。"""
    selected, _ = readside.select_context_memories_via_readside(
        [DISK, LEGACY], "DerivedData 为什么把磁盘占满", cap=8
    )
    assert "m_disk" in [c["id"] for c in selected], "正文里的关键词仍然搜不到"


def test_legacy_card_still_recalled():
    """老卡不能因为这次改动而变差。"""
    selected, _ = readside.select_context_memories_via_readside(
        [DOG, LEGACY], "我最近跑步吗", cap=8
    )
    assert "m_legacy" in [c["id"] for c in selected]


def test_irrelevant_cards_are_still_rejected():
    """闸没有被改松 —— 不相关就是不该选。"""
    selected, _ = readside.select_context_memories_via_readside(
        [DOG, DISK], "帮我写一段 SQL", cap=8
    )
    assert [c["id"] for c in selected] == []


# --------------------------------------------------------------------------- #
# 正文不许外泄
# --------------------------------------------------------------------------- #

_SECRET = "康帕内拉是只布偶猫"


def _blob(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def test_private_search_content_never_appears_in_the_trace():
    """整个 trace 里不许出现 `_search_content` 这个键。"""
    _, trace = readside.select_context_memories_via_readside([DOG, DISK], "狗", cap=8)
    assert "_search_content" in _blob(readside.context_moment_to_index_item(DOG)), (
        "前提失效：索引项本来就该带私有正文，否则这条测试测了个寂寞"
    )
    assert "_search_content" not in _blob(trace)


def test_rejected_card_content_does_not_leak_through_trace():
    """被拒掉的卡，正文绝不能出现在返回给客户端的 trace 里。

    这是本批最容易写错的地方：一旦让 content 给 summary 兜底，
    被敏感闸或不相关判据拒掉的卡，正文就会从 trace 漏出去。
    """
    secret_card = {
        "id": "m_secret",
        "title": None,
        "description": None,
        "summary": None,          # 没有摘要 —— 正是会诱使人用正文兜底的形状
        "content": _SECRET,
        "created_at": "2026-08-10T10:00:00",
    }
    _, trace = readside.select_context_memories_via_readside(
        [secret_card, LEGACY], "帮我订一张机票", cap=8
    )
    assert _SECRET not in _blob(trace), "被拒卡的正文从 trace 漏出去了"


def test_summary_stays_empty_for_content_only_cards():
    """宁可摘要显示为空，也不把正文塞进这个会外泄的字段。"""
    item = readside.context_moment_to_index_item(
        {"id": "m_x", "summary": None, "content": _SECRET}
    )
    assert item["summary"] == ""
    assert item["_search_content"] == _SECRET


def test_sensitive_canonical_card_is_still_gated():
    """上一批修的敏感闸，对新放进来的这批卡同样有效。"""
    sensitive = {**DOG, "id": "m_sensitive", "is_sensitive": True}
    selected, _ = readside.select_context_memories_via_readside(
        [sensitive], "我有一只狗吗", cap=8
    )
    assert [c["id"] for c in selected] == [], "新形状的敏感卡绕过了闸门"
