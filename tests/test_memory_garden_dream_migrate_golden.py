"""Dream / migrate prompt 的基线快照 —— 守「文本改动必须被看见」。

背景:2026-08-16 的内核提取(``ec660613``)把 capture / dream / migrate 三份 prompt
都搬进了 ``memory_garden/prompts/``。**只有 capture 配了逐字节 golden**
(``test_memory_garden_capture_golden.py``),dream 与 migrate 没有 ——
实测把 ``_DREAM_PROMPT_TEMPLATE`` 里的「更干净」改成「更简洁」,
全套 memory_garden 测试 132 passed,**一条都不红**。

这不是"文本不许改"。像 ``ac291a4f fix(prompts): stop using TA for the person``
那样声明清楚、更新基线、双签,是完全正当的路径。这里守的是
**改了必须被看见**,不能混在重构批次里悄悄发生。

为什么 dream 尤其要守:它是**每晚重写整个记忆花园**的那条路
(参见 dream churn 事故:834 张卡被压成 1 张)。提示词漂一个词,
影响面是全量记忆,而在此之前没有任何检查会红。

比对的入口刻意选**运行时真正调用的那个**:
- dream → ``memory.dream_prompt_v1.build_dream_prompt``(适配层壳,内部装配称呼规则;
  Runtime V2 与 resident consumer 都调它)
- migrate → ``memory_garden.prompts.migrate.build_migrate_prompt``
  (**只有 resident consumer 在用**,V2 侧无调用方;老壳
  ``memory/migrate_prompt_v1.py`` 已在 ``5e50e79e`` 删除)

基线更新方式:改动是有意的 → 重跑本文件顶部的生成参数、覆盖 fixture、
在提交说明里写明为什么。没有自动重写机制,这是故意的。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from memory.dream_prompt_v1 import build_dream_prompt as build_dream_via_shell
from memory_garden.prompts.migrate import build_migrate_prompt

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "memory_garden"
    / "dream_migrate_prompt_baseline.json"
)


def _baseline() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _case_names(kind: str) -> list[str]:
    return sorted(_baseline()[kind])


@pytest.mark.parametrize("case_name", _case_names("dream"))
def test_dream_prompt_is_byte_identical_to_baseline(case_name: str) -> None:
    """dream prompt 逐字节不变 —— V2 与 resident 共用这一份。"""
    case = _baseline()["dream"][case_name]
    actual = build_dream_via_shell(**case["params"])
    assert actual == case["text"], (
        f"dream prompt 的 {case_name} 用例变了。若是有意改动:重新生成 fixture "
        f"并在提交说明里写明原因;若不是,说明有改动无意中动了模板。"
    )


@pytest.mark.parametrize("case_name", _case_names("migrate"))
def test_migrate_prompt_is_byte_identical_to_baseline(case_name: str) -> None:
    """migrate prompt 逐字节不变 —— 目前只有 resident consumer 在用。"""
    case = _baseline()["migrate"][case_name]
    actual = build_migrate_prompt(**case["params"])
    assert actual == case["text"], (
        f"migrate prompt 的 {case_name} 用例变了。若是有意改动:重新生成 fixture "
        f"并在提交说明里写明原因;若不是,说明有改动无意中动了模板。"
    )


def test_fixture_covers_the_shapes_that_break_templates() -> None:
    """基线必须真的覆盖这两类形状 —— 断言**参数本身**,不是 case 名字。

    早先这条只检查 key 名里有没有 ``braces_in_content`` / ``all_empty``,
    于是把 ``all_empty`` 的参数全填上值、把 ``braces_in_content`` 的花括号去掉,
    它照样通过 —— 名字检查冒充了形状检查。判据改成看真实取值。

    为什么钉这两类:全空用例暴露"默认档措辞"被改;含花括号的用例守住
    参数里的 ``{}`` 原样进入产出(防未来有人加二次 format / 改拼装顺序时静默吃掉它)。
    """
    baseline = _baseline()
    for kind in ("dream", "migrate"):
        cases = baseline[kind]

        assert "all_empty" in cases, f"{kind} 基线缺全空用例"
        empty_params = cases["all_empty"]["params"]
        non_empty = {k: v for k, v in empty_params.items() if str(v).strip()}
        assert not non_empty, (
            f"{kind}.all_empty 已经不是全空了:{sorted(non_empty)} —— "
            "这个用例的意义就是压默认档措辞,填了值就守不住了"
        )

        assert "braces_in_content" in cases, f"{kind} 基线缺花括号用例"
        braces_params = cases["braces_in_content"]["params"]
        with_braces = [
            k for k, v in braces_params.items() if "{" in str(v) and "}" in str(v)
        ]
        assert with_braces, (
            f"{kind}.braces_in_content 的参数里已经没有花括号了:{braces_params} —— "
            "名字还在但形状没了"
        )
        text = cases["braces_in_content"]["text"]
        assert "{" in text and "}" in text, (
            f"{kind}.braces_in_content 的产出里没有花括号 —— "
            "参数里的 {} 应当原样出现在 prompt 中"
        )


def test_dream_shell_is_an_adapter_not_a_second_template() -> None:
    """壳只许装配参数后转调内核,不许自己复制一份模板。

    判据是**完全相等**,不做任何 strip:用壳内部同样的两个助手
    (``sanitize_user_name`` / ``_naming_rule``)构造出内核入参,
    两边产出必须一字不差。

    早先这条用「删掉所有含『称呼』的行再比对」,那会把模板主体里静态的
    称呼说明一起删掉 —— 改动那行时测试照样绿,声明不成立。

    顺带钉住壳里那个刻意的不对称(见壳的 docstring):
    ``naming_rule`` 取**未 sanitize** 的原始 user_name,
    模板里的 ``user_name`` 取 **sanitize 后**的值。两者不同源。
    """
    from identity.user_naming import _naming_rule, sanitize_user_name
    from memory_garden.prompts import dream as kernel

    # 刻意用带前后空格的名字:sanitize 与否会产生不同结果,
    # 抄错一边就会被这条抓住。
    raw_user_name = "  老王 "
    params = dict(
        ai_name="io",
        user_name=raw_user_name,
        cards="卡1: 老婆是重庆人",
        recent_conversations="用户:今天开了一天会",
    )

    via_shell = build_dream_via_shell(**params)
    via_kernel = kernel.build_dream_prompt(
        ai_name=params["ai_name"],
        user_name=sanitize_user_name(raw_user_name),
        naming_rule=_naming_rule(raw_user_name),
        cards=params["cards"],
        recent_conversations=params["recent_conversations"],
    )

    assert via_shell == via_kernel, (
        "壳的产出与内核不一致 —— 壳里可能复制了一份模板,"
        "或者 naming_rule / user_name 的 sanitize 取值被改成了同源。"
    )
