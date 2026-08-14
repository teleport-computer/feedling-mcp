"""三个策略档位：共用结构，尺子必须保持不同。

这些测试守的是本批最容易被后人「顺手统一掉」的东西 —— 三把尺子看起来很像，
但统一成任何一把都是事故：

    统一成「少而厚」   → 用户手动整理的 100 条只落 2 张卡
    统一成「宁多勿漏」 → 日常聊天每句废话都变成卡
"""
from __future__ import annotations

import pytest

from memory_garden.policies import (
    CURATED_ARCHIVE,
    DEFAULT_POLICY,
    POLICIES,
    get_policy,
)


def test_three_policies_exist():
    assert set(POLICIES) == {
        "conversation_capture",
        "history_import",
        "curated_archive",
    }


def test_conversation_capture_is_few_and_thick():
    p = get_policy("conversation_capture")
    assert p.max_cards == 2, "日常聊天是少而厚，不能放开张数"
    assert p.prefer_merge is True
    assert "宁少勿多" in p.selection_rubric


def test_curated_archive_keeps_everything():
    p = get_policy("curated_archive")
    assert p.max_cards is None, "用户整理的档案宁多勿漏，不能有张数上限"
    assert p.keep_dates is True, "档案里的日期要原样保留"
    assert p.seed_threads_from_tags is True
    assert "宁多勿漏" in p.selection_rubric


def test_history_import_filters_one_off_events():
    p = get_policy("history_import")
    assert "一次性事件" in p.selection_rubric
    assert "闲聊" in p.selection_rubric


def test_rubrics_are_all_different():
    """三把尺子的文字必须真的不同 —— 被抹平就是本批要防的那个事故。"""
    rubrics = {name: p.selection_rubric for name, p in POLICIES.items()}
    assert len(set(rubrics.values())) == 3, f"尺子被抹平了: {list(rubrics)}"


def test_conversation_and_archive_are_opposites():
    """日常聊天与用户档案在「多与少」这件事上必须是相反的。"""
    chat = get_policy("conversation_capture")
    archive = get_policy("curated_archive")
    assert chat.max_cards is not None and archive.max_cards is None
    assert "宁少勿多" in chat.selection_rubric
    assert "宁多勿漏" in archive.selection_rubric


@pytest.mark.parametrize("bad", [None, "", "   ", "nonexistent_policy"])
def test_unknown_policy_falls_back_to_default(bad):
    """回落而不是抛错：接线过程中漏传 policy 应退回现行为，不炸生产路径。"""
    assert get_policy(bad) is DEFAULT_POLICY
    assert DEFAULT_POLICY.name == "conversation_capture"


def test_policies_are_immutable():
    """档位是冻结的 dataclass —— 防止运行时被某条路径就地改掉。"""
    with pytest.raises(Exception):
        CURATED_ARCHIVE.max_cards = 2  # type: ignore[misc]
