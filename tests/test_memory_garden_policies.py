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
    UnknownPolicyError,
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


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_missing_policy_falls_back_to_default(empty):
    """没传 = 旧调用方，退回现行为是安全的。"""
    assert get_policy(empty) is DEFAULT_POLICY
    assert DEFAULT_POLICY.name == "conversation_capture"


@pytest.mark.parametrize(
    "typo", ["nonexistent_policy", "curated_archiv", "CONVERSATION_CAPTURE", "history import"]
)
def test_unknown_policy_name_raises(typo):
    """显式传错必须炸出来 —— 静默回落的后果不对称。

    ``curated_archive`` 拼错一个字母就会悄悄切成「宁少勿多」，把用户手工整理的
    上百条事实压成一两张卡，而且没有任何信号。
    """
    with pytest.raises(UnknownPolicyError) as excinfo:
        get_policy(typo)
    # 报错要指出可用值，否则调用方还得翻源码
    assert "conversation_capture" in str(excinfo.value)


def test_unknown_policy_error_is_a_valueerror():
    """沿用 ValueError 语义，调用方原有的 except ValueError 仍能兜住。"""
    assert issubclass(UnknownPolicyError, ValueError)


def test_policies_are_immutable():
    """档位是冻结的 dataclass —— 防止运行时被某条路径就地改掉。"""
    with pytest.raises(Exception):
        CURATED_ARCHIVE.max_cards = 2  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 共用的语言规则（已写好、尚未接线）
# --------------------------------------------------------------------------- #


def test_language_rule_is_shared_text_with_only_the_basis_swapped():
    """措辞/举例/标点全共用，只有「依据」不同 —— 那是必要差异，不是不一致。"""
    from memory_garden.policies import language_rule

    chat = language_rule("conversation_capture")
    imported = language_rule("history_import")

    assert chat != imported
    assert "TA 跟你对话" in chat
    assert "素材原文" in imported
    # 除了依据那几个字，其余逐字相同
    assert chat.replace("TA 跟你对话", "素材原文") == imported


def test_language_rule_keeps_both_sides_original_points():
    """合并后必须同时保留两边各自独有的要点，不能丢。"""
    from memory_garden.policies import language_rule

    text = language_rule("conversation_capture")
    assert "旅行" in text, "capture 原有的第二个例子丢了"
    assert "别归成英文桶/线索" in text, "genesis 原有的要点丢了"
    assert "专有名词" in text


def test_language_rule_falls_back_for_unknown_policy():
    from memory_garden.policies import language_rule

    assert "TA 跟你对话" in language_rule("nonexistent")


def test_language_rule_is_not_wired_yet():
    """守住「写好但未接线」这个状态。

    接线会同时改动 capture 与 genesis 两处的 prompt 文本，必须先过真模型 e2e。
    这条测试在接线时会失败 —— **那是提醒，不是回归**：改的时候要连同各档位的
    golden fixture 一起重生成。
    """
    import pathlib

    capture_src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "backend" / "memory_garden" / "prompts" / "capture.py"
    ).read_text(encoding="utf-8")
    assert "{language_rule}" not in capture_src, (
        "capture 模板已接入共用语言规则 —— 请确认已跑过真模型 e2e，"
        "并重新生成各档位的 golden fixture，然后删掉这条测试。"
    )


# --------------------------------------------------------------------------- #
# 与 genesis 的关系：curated 已是唯一来源，history 仍是副本
# --------------------------------------------------------------------------- #


def test_genesis_keep_all_comes_from_policies_not_its_own_copy():
    """curated_archive 那两段的重复**已真正消除** —— genesis 引用本模块。

    这条一旦红，说明有人在 genesis 那边又写回了一份字面量。
    """
    from genesis import prompts as gp
    from memory_garden.policies import KEEP_ALL_MAP_SUFFIX, KEEP_ALL_WRITE_SUFFIX

    assert gp.FACT_MAP_KEEP_ALL_SUFFIX == "\n\n" + KEEP_ALL_MAP_SUFFIX
    assert gp.FACT_WRITE_KEEP_ALL_SUFFIX == "\n\n" + KEEP_ALL_WRITE_SUFFIX


def test_history_import_rubric_still_matches_genesis_verbatim():
    """history_import 仍是副本（原文在 genesis 里不连续，抽出来会改行为）。

    既然是副本，就必须逐字相同 —— 这条钉住两边不许漂移。
    真正消除这份重复需要改动 FACT_MAP_PROMPT 的文本顺序，得先过真模型 e2e。
    """
    from genesis import prompts as gp
    from memory_garden.policies import HISTORY_IMPORT

    for line in HISTORY_IMPORT.selection_rubric.splitlines():
        assert line in gp.FACT_MAP_PROMPT, f"与 genesis 原文漂移了: {line!r}"


def test_keep_all_text_keeps_genesis_original_punctuation():
    """逐字保留意味着连半角标点都不许「顺手改成全角」—— 那也是改 prompt。"""
    from memory_garden.policies import KEEP_ALL_MAP_SUFFIX

    assert "不是聊天记录:" in KEEP_ALL_MAP_SUFFIX, "半角冒号被改掉了"
    assert "宁多勿漏。" in KEEP_ALL_MAP_SUFFIX
