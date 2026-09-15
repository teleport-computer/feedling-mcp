"""Dream / migrate prompt 的基线快照 —— 守「文本改动必须被看见」。

背景:2026-08-16 的内核提取(``ec660613``)把 capture / dream / migrate 三份 prompt
都搬进了 ``memgarden/prompts/``。**只有 capture 配了逐字节 golden**
(``test_memgarden_capture_golden.py``),dream 与 migrate 没有 ——
实测把 ``_DREAM_PROMPT_TEMPLATE`` 里的「更干净」改成「更简洁」,
全套 memgarden 测试 132 passed,**一条都不红**。

这不是"文本不许改"。像 ``ac291a4f fix(prompts): stop using TA for the person``
那样声明清楚、更新基线、双签,是完全正当的路径。这里守的是
**改了必须被看见**,不能混在重构批次里悄悄发生。

为什么 dream 尤其要守:它是**每晚重写整个记忆花园**的那条路
(参见 dream churn 事故:834 张卡被压成 1 张)。提示词漂一个词,
影响面是全量记忆,而在此之前没有任何检查会红。

比对的入口刻意选**运行时真正调用的那个**:
- dream → ``memory.garden_component.open_dream_session(...)`` 开出的组件会话的
  第一问(Runtime V2 与 resident consumer 都经它;卡片以**整张卡**的形式传入,
  由组件带正文渲染、按预算截断、标 TRUNCATED)。旧入口
  ``memory.dream_prompt_v1.build_dream_prompt`` 已随 import * 壳删除(2026-09-15)
- migrate → ``memgarden.prompts.migrate.build_migrate_prompt``
  (**只有 resident consumer 在用**,V2 侧无调用方;老壳
  ``memory/migrate_prompt_v1.py`` 已在 ``5e50e79e`` 删除)

基线变更记录:
- 2026-09-15 dream 基线整体重生成:memgarden 的 Dream 带正文渲染(Step 1 措辞加
  "and its body"、新增 TRUNCATED 规则、卡片区改成 ``- id=… | bucket=…`` +
  summary/content 块);入口换成组件会话,所以 params 里的 ``cards`` 从渲染好的串
  变成卡片列表,且至少 10 张(组件的整理门槛)。英文花园的称呼规则改用内核默认
  那份(不再夹中文「用户」「TA」)。
- 2026-09-15 english_garden 称呼规则一行改回 io 的 ``_naming_rule``:Dream 请求现在
  和 Capture 一样传 io 的规则(memgarden ``MaintenanceRequest.naming_rule``)。上一条
  「改用内核默认」时 ``MaintenanceRequest`` 还没有这个字段;留着的话同一个人白天落卡
  按 io 规则、夜里整理按内核规则,两份提示词对称呼的禁令不一致。中文用例不变
  (两份中文规则逐字相同)。

基线更新方式:改动是有意的 → 重跑本文件顶部的生成参数、覆盖 fixture、
在提交说明里写明为什么。没有自动重写机制,这是故意的。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from memgarden.prompts.migrate import build_migrate_prompt
from memory import garden_component


def build_dream_via_runtime(**params) -> str:
    """The first prompt a Dream run sends, through the entry both runtimes use."""
    session, _disclosure = garden_component.open_dream_session(
        garden_component.build_garden(garden_component.CallableModel(lambda _p: "")),
        **params,
    )
    prompt = session.next_prompt()
    assert prompt, "a golden Dream case must be large enough for the component to run"
    return prompt

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "memgarden"
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
    actual = build_dream_via_runtime(**case["params"])
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
        # locale 是必填参数（没有默认值），不算「填了内容」——
        # 这个用例守的是「其余参数全空时模板的默认措辞」。
        # dream 的 cards 也不算:组件对不到 10 张卡的花园不出提示词,
        # 全空用例只能用最小的卡(只有 id + 一行摘要)压默认措辞。
        non_empty = {k: v for k, v in empty_params.items()
                     if str(v).strip() and k not in {"locale", "cards"}}
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


def test_dream_fixture_covers_bodies_legacy_fields_and_truncation() -> None:
    """dream 基线必须真的压住这次改动的形状:正文进提示词、老字段名被翻译、
    超长卡被截断并标 TRUNCATED —— 断言取值本身,不看 case 名字。"""
    cases = _baseline()["dream"]
    typical = cases["typical"]["text"]
    assert "  content:\n    老婆是重庆人，每次回重庆都要吃火锅。" in typical

    legacy = cases["legacy_fields_and_truncated_card"]
    kinds = {k for card in legacy["params"]["cards"] for k in card}
    assert {"title", "body", "category", "thread", "description", "plaintext"} <= kinds
    assert "- id=mem_1 | bucket=家庭 | threads=家人" in legacy["text"]
    assert "summary: 每周三跑步" in legacy["text"]
    assert "- id=mem_3 | TRUNCATED" in legacy["text"]
    assert any(len(card.get("content", "")) > garden_component.DREAM_CARD_BODY_CHARS
               for card in legacy["params"]["cards"])

    english = cases["english_garden"]
    assert english["params"]["locale"] == "en"
    # 称呼规则和 Capture 同一份(io 的 _naming_rule,英文版点名禁「用户」/「TA」),
    # 不是内核默认那版。见顶部基线变更记录 2026-09-15 第二条。
    from identity.user_naming import _naming_rule
    from memgarden.naming import naming_rule as kernel_naming_rule
    user_name = english["params"]["user_name"]
    assert _naming_rule(user_name, locale="en") in english["text"]
    assert kernel_naming_rule(user_name, locale="en") not in english["text"]
