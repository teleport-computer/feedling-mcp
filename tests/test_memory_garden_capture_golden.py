"""落卡 prompt 的 golden fixture —— 守「行为逐字节不变」。

fixture 是在把模板里内联的那段尺子抽成 ``{selection_rubric}`` 占位符**之前**
生成的。参数化之后默认档位（conversation_capture）的 rubric 与原内联文本逐字相同，
所以默认调用的产出必须与 fixture 字节一致。

这条一旦红，说明有人改了默认档的措辞或模板结构 —— 那是行为变更，
不能混在重构批次里悄悄发生。
"""
from __future__ import annotations

import pathlib

import pytest

from memory_garden.policies import CURATED_ARCHIVE, HISTORY_IMPORT
from memory_garden.prompts.capture import build_capture_prompt

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "memory_garden"
    / "capture_prompt_default.txt"
)

_ARGS = dict(
    ai_name="io",
    user_name="老王",
    naming_rule="叫他老王。",
    buckets="家庭、工作、健康",
    threads="老婆、跑步",
    identity="（暂无）",
    window="用户：今天开了一天会，心率一直很高\n我：辛苦了，早点休息",
    cards="卡1: 老婆是重庆人",
)


def test_default_capture_prompt_is_byte_identical_to_golden():
    expected = _FIXTURE.read_text(encoding="utf-8")
    assert build_capture_prompt(**_ARGS) == expected


@pytest.mark.parametrize("policy_name", ["conversation_capture", None, "", "unknown"])
def test_default_and_fallbacks_all_produce_the_golden_text(policy_name):
    """未知/空档位回落到日常聊天档，产出仍与 golden 一致。"""
    expected = _FIXTURE.read_text(encoding="utf-8")
    assert build_capture_prompt(**_ARGS, policy=policy_name) == expected


@pytest.mark.parametrize("policy", [HISTORY_IMPORT, CURATED_ARCHIVE])
def test_other_policies_change_the_rubric(policy):
    """换档位必须真的换掉尺子那一段，其余结构不动。"""
    default_text = build_capture_prompt(**_ARGS)
    other = build_capture_prompt(**_ARGS, policy=policy)
    assert other != default_text
    assert policy.selection_rubric in other
    # 模板的其余部分照旧
    assert "【每一件决定记的事，怎么处理】" in other
    assert "【这段对话】" in other


def test_policy_accepts_name_string_too():
    by_name = build_capture_prompt(**_ARGS, policy="curated_archive")
    by_object = build_capture_prompt(**_ARGS, policy=CURATED_ARCHIVE)
    assert by_name == by_object
