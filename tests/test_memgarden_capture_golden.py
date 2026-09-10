"""落卡 prompt 的基线快照 —— 守「行为不再意外漂移」。

⚠️ **2026-08-20 换了一代基线。** 之前这份 fixture 是提取前的中文原文，测试用
「除语言段外逐字节相同」的方式证明重构没改行为。这次把整套指令换成英文
（桶名只发一套、语言由宿主指定），那种对照方式失效了 —— 差异是全局的，不是
局部的，继续做局部剥离只会把真回归也剥掉。

所以基线重新生成自本分支。**重新生成 golden 是能掩盖真回归的动作**，因此下面
补了一组「守意图」的断言：它们直接检查这次改动想要的性质（指令是英文、桶名只
发一套、语言被显式指定、称呼规则跟着花园语言走），而不是只比对一个不透明的
字符串。fixture 负责挡后续的意外漂移，断言负责挡「基线本身就是错的」。

比对的是 io 侧兼容壳 ``memory.capture_prompt_v1.build_capture_prompt``。

覆盖的边界：典型输入 / 全空 / 中英混合但花园是中文 / 真英文花园 /
名字带前后空格（走 sanitize）/ 正文里含花括号（会撞 ``str.format``）。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from memory.capture_prompt_v1 import build_capture_prompt as build_via_shell
from memgarden.policies import CONVERSATION_CAPTURE, CURATED_ARCHIVE, HISTORY_IMPORT
from memgarden.prompts.capture import build_capture_prompt as build_via_kernel

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "memgarden"
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
        locale="zh-Hans",
    ),
    "all_empty": dict(
        ai_name="", user_name="", buckets="", threads="",
        identity="", window="", cards="",
        locale="zh-Hans",
    ),
    "mixed_language": dict(
        ai_name="Iris",
        user_name="Alex",
        buckets="work / family",
        threads="standup、跑步",
        identity="met 30 days ago",
        window="user: shipped the migration today\nme: nice, 辛苦了",
        cards="c1: prefers oat milk",
        locale="zh-Hans",  # 混合语料但花园是中文桶 —— 桶不许因此裂开
    ),
    "english_garden": dict(
        ai_name="Iris",
        user_name="Alex",
        buckets="Work / Health / Pets",
        threads="standup, running",
        identity="met 30 days ago",
        window="user: bombed the interview today, feeling rough\nme: want to talk about it?",
        cards="c1: prefers oat milk",
        locale="en",
    ),
    "name_needs_sanitize": dict(
        ai_name="  io  ",
        user_name="  老王  ",
        buckets="家庭",
        threads="老婆",
        identity="（暂无）",
        window="用户：随便聊聊\n我：好啊",
        cards="",
        locale="zh-Hans",
    ),
    "braces_in_content": dict(
        ai_name="io",
        user_name="老王",
        buckets="工作",
        threads="项目",
        identity="（暂无）",
        window='用户：这个 JSON {"a": 1} 报错了\n我：我看看',
        cards='卡1: 他在调 {"cards": []} 的解析',
        locale="zh-Hans",
    ),
}


def _baseline() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_covers_every_case():
    assert set(_baseline()) == set(CASES), "fixture 与用例集合不同步"


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_prompt_is_byte_identical_to_baseline(case_name):
    """逐字节不变 —— 挡后续的意外漂移。

    这条红了不一定是 bug，但一定是**行为变更**：要么它是你有意改的（那就连同
    下面那组「守意图」断言一起更新，并重新生成 fixture），要么它是别处改动
    漏出来的副作用（那就是回归）。两者都不该悄悄发生。
    """
    assert build_via_shell(**CASES[case_name]) == _baseline()[case_name]["text"]


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_language_block_comes_from_the_shared_rule(case_name):
    """语言段来自共用规则，且**目标语言是被指定的、不是让模型猜的**。"""
    from memgarden.policies import language_rule

    locale = CASES[case_name]["locale"]
    actual = build_via_shell(**CASES[case_name])
    assert language_rule(
        "conversation_capture", locale=locale, indent="     ", first_prefix="   · "
    ) in actual
    # 「跟着对话的语言走」那种让模型自己判的措辞不许再出现在 capture 里 ——
    # 宿主已经算出来了，再让模型猜一次就是多一处漂移点。
    assert "the language of your conversation" not in actual
    assert "用 TA 跟你对话的语言记" not in actual


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_only_one_bucket_language_is_offered(case_name):
    """桶名只发一套 —— 这次改动的核心。

    旧做法把中英两套桶一起塞进去让模型挑，实测约 1/3 的中文记忆被贴上英文桶，
    才需要 ``normalize_bucket_language`` 常态纠错。给模型一个它不该做的选择题，
    它就会做错。
    """
    from memgarden.prompts.buckets import BUCKET_SETS

    locale = CASES[case_name]["locale"]
    actual = build_via_shell(**CASES[case_name])
    other = "en" if locale == "zh-Hans" else "zh-Hans"
    assert BUCKET_SETS[locale] in actual, "该发的那套桶不见了"
    assert BUCKET_SETS[other] not in actual, "另一套桶又被塞进来了 —— 模型会挑错"


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_naming_rule_follows_the_garden_language(case_name):
    """称呼规则跟着花园语言走。

    它整段会原样插进提示词。英文花园里插一段中文，等于给模型发混合信号 ——
    实测最容易让它顺着写出中文卡。
    """
    actual = build_via_shell(**CASES[case_name])
    marker = "How to refer to them: "
    rule = actual[actual.index(marker) + len(marker):].split("\n", 1)[0]
    if CASES[case_name]["locale"] == "en":
        assert rule.startswith("Refer to ") or rule.startswith("If the material")
    else:
        assert "「" in rule or "不要用" in rule


def test_instructions_are_english_and_only_literals_stay_chinese():
    """指令是英文；剩下的中文必须都是**不该翻译的字面串**。

    桶名、禁用词举例（「用户」「TA」）、线索举例 —— 这些是模型要照抄或要避开的
    具体字符串，翻译了就是教错。除此之外的中文说明都算漏网。
    """
    import re

    text = build_via_shell(**CASES["all_empty"])  # 全空 = 只剩模板本身
    chinese_runs = re.findall(r"[一-鿿]{2,}", text)
    allowed = set(
        "工作 目标与成长 家庭 朋友 宠物 我们的关系 情绪与安抚 偏好与边界 "
        "个性与价值观 健康 爱好 金钱 饮食 地点与旅行 用户 对方 争执 吵架 "
        "妈妈 房子 界面 留存 满意度 用户界面 用户留存 健康 简体中文 这个人 "
        "不要用 指代本人的 猜测性别的他 也不要用第二人称 来指代本人 "
        "如果材料里明确出现了本人希望被称呼的名字 就用那个名字 "
        "没有名字时优先省略主语 例如 常在深夜写代码 累了会突然沉默 "
        "需要主语时 按身份卡 你们的关系 旧卡和对话里的线索判断性别 "
        "用 或 线索不足以判断 才用中性的".split()
    )
    # 中文花园的**称谓规则整段是中文的**，这是对的：它讲的是「卡里别写哪些中文词」，
    # 翻成英文就教不清了（英文里根本没有「用户」这个词要防）。规则由
    # memgarden.naming.referent_rule(locale) 按语言取，所以这里整段豁免。
    from identity.user_naming import _naming_rule
    from memgarden.naming import referent_rule

    referent_zh = set(re.findall(r"[一-鿿]{2,}", referent_rule("zh-Hans")))
    naming_zh = set(re.findall(r"[一-鿿]{2,}", _naming_rule("老王", locale="zh-Hans")))
    leaked = [r for r in chinese_runs
              if r not in allowed and r not in referent_zh and r not in naming_zh]
    assert not leaked, f"这些中文说明没翻译干净：{sorted(set(leaked))}"


@pytest.mark.parametrize("policy_name", [None, "", "conversation_capture"])
def test_kernel_default_and_fallbacks_match_baseline_typical(policy_name):
    """内核层：默认档与「没传」的回落产出同一份文本（除语言段外与基线一致）。

    注意未知名不在这里 —— 它现在会抛 UnknownPolicyError，见
    test_memgarden_policies.py::test_unknown_policy_name_raises。
    """
    from identity.user_naming import _naming_rule, sanitize_user_name

    args = dict(CASES["typical"])
    raw_name = args.pop("user_name")
    locale = args["locale"]
    text = build_via_kernel(
        user_name=sanitize_user_name(raw_name),
        naming_rule=_naming_rule(raw_name, locale=locale),
        policy=policy_name,
        **args,
    )
    assert text == _baseline()["typical"]["text"]
    # 三种传法（没传 / 空串 / 显式默认档）必须产出完全同一份
    assert text == build_via_shell(**CASES["typical"])


def _kernel_args() -> dict:
    from identity.user_naming import _naming_rule, sanitize_user_name

    args = dict(CASES["typical"])
    raw_name = args.pop("user_name")
    return dict(
        user_name=sanitize_user_name(raw_name),
        naming_rule=_naming_rule(raw_name, locale=args["locale"]),
        **args,
    )


@pytest.mark.parametrize("policy", [HISTORY_IMPORT, CURATED_ARCHIVE])
def test_other_policies_now_render_instead_of_being_rejected(policy):
    """其余两档现在真的能用了（memgarden 0.18.0）。

    这条以前断言它们抛 ``NotImplementedError`` —— 当时模板写死着「并入优先」、
    输出 schema 里没有 occurred_at、也没有 tags→threads，只换 rubric 会产出
    自相矛盾的 prompt。那条测试自己写着「届时会失败，那是提醒信号不是回归」。

    信号到了：模板已经按 ``CapturePolicy`` 的标志位渲染开场白、动作偏好、
    日期字段、tags 播种和张数上限。所以这里改成验**渲染结果符合该档的语义**。
    """
    prompt = build_via_kernel(**_kernel_args(), policy=policy)
    assert prompt.strip()
    # keep_dates 的档必须有地方放日期，否则模型只能把它塞进正文
    assert ("occurred_at" in prompt) is policy.keep_dates
    # 不设上限的档要**明说**，否则模型会自己保守
    assert ("no cap" in prompt) is (policy.max_cards is None)
    # 宁多勿漏的档，add 要排在前面并标 preferred
    assert ("add (preferred)" in prompt) is (not policy.prefer_merge)


def test_the_default_ruler_is_unchanged_by_policy_support():
    """🔴 io 走的是默认档，它的产出必须**逐字未变**。

    这是这一组测试真正要守的东西：库支持了新档位，不能把线上正在跑的那条路
    带偏。默认档和显式传 conversation_capture 也必须完全一致。
    """
    default = build_via_kernel(**_kernel_args())
    explicit = build_via_kernel(**_kernel_args(), policy=CONVERSATION_CAPTURE)
    assert default == explicit


@pytest.mark.parametrize("policy", ["curated_archive", "history_import"])
def test_passing_by_name_and_by_object_agree(policy):
    """按名字传和按对象传走同一条路径。"""
    from memgarden.policies import get_policy

    assert (build_via_kernel(**_kernel_args(), policy=policy)
            == build_via_kernel(**_kernel_args(), policy=get_policy(policy)))
