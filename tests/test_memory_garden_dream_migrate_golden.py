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
    """基线必须覆盖会撞 str.format 的花括号与全空输入 —— 否则守卫有洞。

    这两类是模板类 bug 的高发形状:花括号会被 ``str.format`` 当占位符,
    全空输入会暴露"默认档措辞"被改。少了任一类,golden 就只守住了顺风路径。
    """
    baseline = _baseline()
    for kind in ("dream", "migrate"):
        names = set(baseline[kind])
        assert "braces_in_content" in names, f"{kind} 基线缺花括号用例"
        assert "all_empty" in names, f"{kind} 基线缺全空用例"


def test_dream_shell_and_kernel_are_not_two_implementations() -> None:
    """壳只许装配参数,不许自己复制一份模板。

    ``memory/dream_prompt_v1.py`` 是适配层(补 ``naming_rule`` 后转调内核),
    不是纯 re-export —— 所以不能用 ``is`` 判定。判据改成:
    壳的产出必须**包含**内核在同样输入下的模板主体。
    若哪天有人在壳里复制一份模板改改,这条会红。
    """
    from memory_garden.prompts import dream as kernel

    params = _baseline()["dream"]["typical"]["params"]
    via_shell = build_dream_via_shell(**params)
    naming_rule = "叫他老王。"
    via_kernel = kernel.build_dream_prompt(naming_rule=naming_rule, **params)

    # 壳与内核只应差在称呼那一段:去掉各自的称呼行之后必须逐字节相同。
    def _strip_naming(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if "称呼" not in line
        )

    assert _strip_naming(via_shell) == _strip_naming(via_kernel), (
        "壳与内核的模板主体不一致 —— 壳里可能复制了一份模板。"
    )
