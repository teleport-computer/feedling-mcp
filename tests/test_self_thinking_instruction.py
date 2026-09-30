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


def test_aside_field_instruction_parity(monkeypatch):
    from model_api_runtime.v2 import context, tool_loop, worker
    import tools.chat_resident_consumer as resident

    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    monkeypatch.setattr(resident, "_supports_mandatory_self_thinking_v1", lambda: True)
    field = st.instruction_for_field().strip()
    assert field in context.chat_system_prompt()
    for lane in ("scheduled", "screen_watch"):
        assert field in worker._wake_system_prompt_for_lane(lane, "base")
    # Presence wakes share the rendering with one intent phrase swapped (T723).
    presence_field = st.instruction_for_field(presence=True).strip()
    assert presence_field == field.replace(
        st._ASIDE_CONTENT_PHRASE, st._PRESENCE_ASIDE_CONTENT_PHRASE)
    for lane in sorted(worker._PRESENCE_WAKE_LANES):
        prompt = worker._wake_system_prompt_for_lane(lane, "base")
        assert presence_field in prompt and field not in prompt
    assert st._ASIDE_CONTENT_PHRASE in field
    assert st._ASIDE_VISIBILITY_SENTENCE in field
    assert "<think>" not in field and "<aside>" not in field
    # T687: resident V1 deliberately does NOT share the JSON-field rendering —
    # CLI models hand-write their reply, and prose inside a JSON string broke
    # on unescaped quotes. It renders the tag form for its driver's tag.
    tag = resident._self_thinking_tag()
    assert resident._foreground_self_thinking_instruction() == st.instruction(tag).strip()
    assert f"<{tag}>" in resident._wake_think_permission_line()
    assert "aside 字段" not in resident._foreground_self_thinking_instruction()
    assert "语言跟着他走" in field
    assert "不出现工具名、参数、字段名" in field
    assert tool_loop._REPLY_TOOL_SPEC.parameters["properties"]["aside"]["description"] == st.ASIDE_FIELD_DESCRIPTION
    assert tool_loop._REPLY_TOOL_SPEC.parameters["required"] == ["text"]


# T734: aside-field copy in the account's reply language. The Chinese hashes
# are the pre-T734 renderings (Chinese accounts must not change by a byte); the
# English chat rendering is the copy measured in T730 (aside_en arm).
_FIELD_SHA = {
    ("reply", False, "zh"): "d2bb2e85e47295c15b14ee65d9bb1e141513397f5bcfa46891364c1bc9204387",
    ("reply", True, "zh"): "401f00b17cb5cc69e1c6e091293df7ef6e85e35a6e5103002d11a9407a75a907",
    ("json", False, "zh"): "9f046a90662a1fea9308923e3404900811e5279942b391639725e0fa6adf5389",
    ("reply", False, "en"): "ea7e130f0f1e9f87fc2e16a505d56155bdd38a99fb328c5c66a811ca3d852399",
    ("reply", True, "en"): "b1cd6c70002c38d9a0ea45079a3810425dd5f5b32001a9d00d01a98f4dde2f4c",
    ("json", False, "en"): "460e9f233573f1d399671fb5f12d200e23019f4a317bd5ea49f0449e6b587d89",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_field_copy_is_pinned_per_reply_language():
    for (protocol, presence, language), digest in _FIELD_SHA.items():
        rendered = st.instruction_for_field(
            protocol=protocol, presence=presence, language=language,
        )
        assert _sha(rendered) == digest, (protocol, presence, language)


def test_field_copy_selection_mirrors_reply_language_policy_branch():
    for protocol, presence in (("reply", False), ("reply", True), ("json", False)):
        zh = st.instruction_for_field(protocol=protocol, presence=presence)
        en = st.instruction_for_field(protocol=protocol, presence=presence, language="en")
        assert en != zh
        assert en.startswith(" For your final reply")
        # Same paragraph structure as the Chinese rendering it mirrors.
        assert en.count("\n\n") == zh.count("\n\n")
        for language in (None, "", "zh", "zh-Hans", "EN", "ja"):
            assert st.instruction_for_field(
                protocol=protocol, presence=presence, language=language,
            ) == zh
    # Outside the quoted bad example and the slip list, the English copy has no Han.
    en = st.instruction_for_field(language="en")
    body = "\n".join(
        line for line in en.split("\n")
        if "让我" not in line
    )
    assert re.search(r"[㐀-鿿]", body) is None


def test_screen_watch_suffix_follows_reply_language():
    assert st.screen_watch_instruction("en") == st.SCREEN_WATCH_INSTRUCTION_EN
    assert _sha(st.SCREEN_WATCH_INSTRUCTION_EN) == (
        "6eb1c52a911b70b983f55a749c743a2ed17893e7f39cd24b87874ef254fcdfc2"
    )
    for language in (None, "", "zh-Hans", "EN"):
        assert st.screen_watch_instruction(language) is st.SCREEN_WATCH_INSTRUCTION


def test_host_prompts_render_the_account_language(monkeypatch):
    from model_api_runtime.v2 import context, worker

    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    for language in ("en", "zh"):
        field = st.instruction_for_field(language=language).strip()
        other = st.instruction_for_field(language="zh" if language == "en" else "en").strip()
        chat = context.chat_system_prompt(language=language)
        assert field in chat and other not in chat
        for lane in ("scheduled", "screen_watch"):
            prompt = worker._wake_system_prompt_for_lane(lane, "base", language=language)
            assert field in prompt and other not in prompt
        presence = st.instruction_for_field(presence=True, language=language).strip()
        for lane in sorted(worker._PRESENCE_WAKE_LANES):
            assert presence in worker._wake_system_prompt_for_lane(
                lane, "base", language=language,
            )
    screen = worker._wake_system_prompt_for_lane("screen_watch", "base", language="en")
    assert st.SCREEN_WATCH_INSTRUCTION_EN.strip() in screen
    assert st.SCREEN_WATCH_INSTRUCTION.strip() not in screen
