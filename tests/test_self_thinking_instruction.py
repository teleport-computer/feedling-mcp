"""自我思考文案的守卫 —— 文案改了就要有人知道。

2026-09-02 跟着模块从 memgarden 仓库搬过来：这套文案是 **io 的产品设定**
（人格口气、屏幕监看说辞、FEEDLING_ 开关），不该跟一个公开的记忆库一起发。
搬家只换位置，断言和期望文案一个字没动。
"""
from __future__ import annotations

import hashlib
import pathlib
import re


# 模块现在在 io 的 backend/ 下，包名不变（`from agent_protocol_core import …`
# 照常工作），所以 25 个引用点一处都没改。
CORE_SRC = pathlib.Path(__file__).resolve().parent.parent / "backend"

from agent_protocol_core import self_thinking as st  # noqa: E402


EXPECTED_ZH = (
    "每轮最终回复以 <think> 开头，里面是你此刻真实的心里话，</think> 之后才是正文。中间调工具的轮次不写。\n"
    "心里话是你自己跟自己说的，用你平时的口气：写你在意什么、想怎么回、为什么。不是对他的分析，不是步骤汇报。\n"
    "心里话和正文都用他正在说的语言，整段如此，一个词也不换。\n"
    "坏例子（他在说中文）：<think>Let me update the name…</think>\n"
    "不提工具名、参数、内部字段，也不提这条规则本身。"
)

EXPECTED_EN = (
    "Start every final reply with <think> — your genuine inner voice right now — then </think>, then what you actually say. Tool-call turns get no <think>.\n"
    "The inner voice is you talking to yourself in your usual tone — what you notice, what you want to do, why. Not an assessment of them, not a progress report.\n"
    "Both the inner voice and the reply stay in the language they're speaking, the whole way through.\n"
    "Bad (they're speaking English): <think>让我更新名字…</think>\n"
    "Never mention tool names, parameters, internal fields, or this rule itself."
)


def test_import_resolves_to_repository_core_source():
    expected = CORE_SRC / "agent_protocol_core" / "self_thinking.py"
    assert pathlib.Path(st.__file__).resolve() == expected.resolve()


def test_reviewed_renderings_are_exact_atomic_blocks():
    assert st.INSTRUCTION_ZH == EXPECTED_ZH
    assert st.INSTRUCTION_EN == EXPECTED_EN


def test_each_rendering_stays_one_policy_block():
    for rendering in (st.INSTRUCTION_ZH, st.INSTRUCTION_EN):
        assert rendering.split("\n\n") == [rendering]


def test_chinese_renderings_follow_host_house_style():
    # Mirrors origin/test tests/test_v2_context.py:806
    # test_t101_platform_chinese_has_no_house_style_punctuation_regressions.
    for rendering in (st.INSTRUCTION_ZH, st._ABSENT_CORRECTION_ZH):
        assert "——" not in rendering
        assert re.search(r"[㐀-鿿],|,[㐀-鿿]", rendering) is None


def test_selection_mirrors_reply_language_policy_branch():
    assert st.instruction_for_language("en") is st.INSTRUCTION_EN
    for language in (None, "", "zh", "zh-Hans", "EN"):
        assert st.instruction_for_language(language) is st.INSTRUCTION_ZH


def test_legacy_instruction_remains_the_deployed_string_contract():
    assert isinstance(st.INSTRUCTION, str)
    assert hashlib.sha256(st.INSTRUCTION.encode()).hexdigest() == (
        "dfa9f806b4fdcc189cc63d2fc1810a5326f0a3f5b9042f889e48f499ca9bc2ff"
    )


def test_absent_correction_localizes_wrapper_and_contract_together():
    zh = st.absent_correction_instruction_for_language("zh-Hans")
    en = st.absent_correction_instruction_for_language("en")

    assert zh.startswith("上一轮最终回复缺少规定的 <think>…</think> 结构。")
    assert en.startswith("The previous final reply did not include the required")
    assert zh.endswith(st.INSTRUCTION_ZH)
    assert en.endswith(st.INSTRUCTION_EN)
    assert zh.count("\n\n") == 1
    assert en.count("\n\n") == 1


def test_literal_bad_examples_parse_and_nesting_still_fails_closed():
    assert st.split_thinking("<think>Let me update the name…</think>正文") == (
        st.COMPLETE,
        "Let me update the name…",
        "正文",
    )
    assert st.split_thinking("<think>让我更新名字…</think>reply") == (
        st.COMPLETE,
        "让我更新名字…",
        "reply",
    )
    assert st.split_thinking(
        "<think>outer <think>inner</think></think>reply"
    ) == (st.FAILED, "", "")
