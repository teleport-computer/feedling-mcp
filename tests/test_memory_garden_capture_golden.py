"""落卡 prompt 的基线快照 —— 守「行为逐字节不变」。

fixture 里的五组文本是在 **origin/test 基线的 checkout 上**跑出来的，
比对的是 io 侧兼容壳 ``memory.capture_prompt_v1.build_capture_prompt``
（它的签名在基线与本分支上完全一致，所以能端到端对比）。

覆盖的边界：典型输入 / 全空 / 中英混合 / 名字带前后空格（走 sanitize）/
正文里含花括号（会撞 ``str.format``）。

这些一旦红，说明模板结构、默认档措辞、或称呼装配被改动了 —— 那是行为变更，
不能混在重构批次里悄悄发生。
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from memory.capture_prompt_v1 import build_capture_prompt as build_via_shell
from memory_garden.policies import CURATED_ARCHIVE, HISTORY_IMPORT
from memory_garden.prompts.capture import build_capture_prompt as build_via_kernel

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "memory_garden"
    / "capture_prompt_baseline.json"
)

# 与生成 fixture 时完全相同的入参（见提交说明里的生成脚本）。
CASES: dict[str, dict] = {
    "typical": dict(
        ai_name="io",
        user_name="老王",
        buckets="家庭、工作、健康",
        threads="老婆、跑步",
        identity="认识 214 天，伴侣关系",
        window="用户：今天开了一天会，心率一直很高\n我：辛苦了，早点休息",
        cards="卡1: 老婆是重庆人",
    ),
    "all_empty": dict(
        ai_name="", user_name="", buckets="", threads="",
        identity="", window="", cards="",
    ),
    "mixed_language": dict(
        ai_name="Iris",
        user_name="Alex",
        buckets="work / family",
        threads="standup、跑步",
        identity="met 30 days ago",
        window="user: shipped the migration today\nme: nice, 辛苦了",
        cards="c1: prefers oat milk",
    ),
    "name_needs_sanitize": dict(
        ai_name="  io  ",
        user_name="  老王  ",
        buckets="家庭",
        threads="老婆",
        identity="（暂无）",
        window="用户：随便聊聊\n我：好啊",
        cards="",
    ),
    "braces_in_content": dict(
        ai_name="io",
        user_name="老王",
        buckets="工作",
        threads="项目",
        identity="（暂无）",
        window='用户：这个 JSON {"a": 1} 报错了\n我：我看看',
        cards='卡1: 他在调 {"cards": []} 的解析',
    ),
}


def _baseline() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_covers_every_case():
    assert set(_baseline()) == set(CASES), "fixture 与用例集合不同步"


#: 语言规则统一（2026-08-14）之后，产出与基线的差异**只允许出现在语言那一段**。
#: 基线快照仍是改造前的原文，不更新 —— 它的价值就在于持续证明「只改了那一段」。
_LANG_BLOCK_MARKERS = ("   · 语言：", "   · 称呼：")


def _strip_language_block(text: str) -> str:
    start = text.index(_LANG_BLOCK_MARKERS[0])
    end = text.index(_LANG_BLOCK_MARKERS[1])
    return text[:start] + text[end:]


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_everything_except_the_language_block_is_byte_identical_to_baseline(case_name):
    """除语言段外逐字节不变 —— 守住「这次只动了语言那一段」。"""
    expected = _baseline()[case_name]["text"]
    actual = build_via_shell(**CASES[case_name])
    assert _strip_language_block(actual) == _strip_language_block(expected)


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_language_block_now_comes_from_the_shared_rule(case_name):
    """语言段本身换成了共用规则（capture 与 genesis 同源）。"""
    from memory_garden.policies import language_rule

    actual = build_via_shell(**CASES[case_name])
    assert language_rule("conversation_capture", indent="     ", first_prefix="   · ") in actual
    # 旧的内联文本不许再出现
    assert "用 TA 跟你对话的语言记" not in actual


@pytest.mark.parametrize("policy_name", [None, "", "conversation_capture"])
def test_kernel_default_and_fallbacks_match_baseline_typical(policy_name):
    """内核层：默认档与「没传」的回落产出同一份文本（除语言段外与基线一致）。

    注意未知名不在这里 —— 它现在会抛 UnknownPolicyError，见
    test_memory_garden_policies.py::test_unknown_policy_name_raises。
    """
    from identity.user_naming import _naming_rule, sanitize_user_name

    args = dict(CASES["typical"])
    raw_name = args.pop("user_name")
    text = build_via_kernel(
        user_name=sanitize_user_name(raw_name),
        naming_rule=_naming_rule(raw_name),
        policy=policy_name,
        **args,
    )
    assert _strip_language_block(text) == _strip_language_block(
        _baseline()["typical"]["text"]
    )
    # 三种传法（没传 / 空串 / 显式默认档）必须产出完全同一份
    assert text == build_via_shell(**CASES["typical"])


def _kernel_args() -> dict:
    from identity.user_naming import _naming_rule, sanitize_user_name

    args = dict(CASES["typical"])
    raw_name = args.pop("user_name")
    return dict(
        user_name=sanitize_user_name(raw_name),
        naming_rule=_naming_rule(raw_name),
        **args,
    )


@pytest.mark.parametrize("policy", [HISTORY_IMPORT, CURATED_ARCHIVE])
def test_other_policies_are_rejected_until_template_is_policy_aware(policy):
    """其余两档必须**明确拒绝**，不能只换 rubric 就放行。

    这个模板的其余部分还写死着「并入（优先）」、输出 schema 里没有 occurred_at、
    也没有 tags→threads 的指令。只替换 rubric 会产出自相矛盾的 prompt：
    一边说「宁多勿漏」，一边说「并入优先」且无处放日期。
    （codex code_review 2026-08-14 指出。）

    完整策略化和 genesis 接线一起做（批 7）。这条测试届时会失败 ——
    **那是提醒信号，不是回归**：改的时候要连同各档位的 golden 一起补上。
    """
    with pytest.raises(NotImplementedError) as excinfo:
        build_via_kernel(**_kernel_args(), policy=policy)
    assert policy.name in str(excinfo.value)


@pytest.mark.parametrize("policy", ["curated_archive", "history_import"])
def test_rejection_is_the_same_whether_passed_by_name_or_object(policy):
    """按名字传和按对象传走同一条路径。"""
    with pytest.raises(NotImplementedError):
        build_via_kernel(**_kernel_args(), policy=policy)
