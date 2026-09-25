"""Pure regression tests for leaked tool-call markup sanitization."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import tool_markup_leak  # noqa: E402


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", True),
        ("?!", True),
        ("嗯", False),
        ("1", False),
        ("🌙", False),
    ],
)
def test_visible_text_degeneracy_is_public_and_stable(text, expected):
    assert tool_markup_leak.is_degenerate_visible_text(text) is expected


@pytest.mark.parametrize("sentinel", tool_markup_leak.MODEL_SENTINEL_TOKENS)
def test_provider_end_sentinel_is_degenerate_only_as_the_whole_message(sentinel):
    assert tool_markup_leak.is_degenerate_visible_text(sentinel) is True
    assert tool_markup_leak.is_degenerate_visible_text(f"  {sentinel}\n") is True
    assert tool_markup_leak.is_degenerate_visible_text(sentinel * 2) is True
    assert tool_markup_leak.is_degenerate_visible_text(f"正文{sentinel}") is False
    assert tool_markup_leak.is_degenerate_visible_text(f"{sentinel}正文") is False


def test_unbarred_end_of_turn_sentinel_is_degenerate():
    # Keep this case independent from MODEL_SENTINEL_TOKENS so deleting the
    # production entry cannot also delete its regression-test parameter.
    assert tool_markup_leak.is_degenerate_visible_text("<end_of_turn>") is True


@pytest.mark.parametrize(
    "normal_text",
    (
        "<3",
        "a < b",
        "<strong>正常 HTML</strong>",
        "<s>旧式删除线</s>",
        "<end_of_turn> 是模型的结束符",
    ),
)
def test_sentinel_detection_preserves_normal_angle_bracket_text(normal_text):
    assert tool_markup_leak.is_degenerate_visible_text(normal_text) is False


def test_real_parameter_leak_is_removed_but_reply_body_survives():
    raw = (
        '<parameter name="tool_name">reply</parameter>\n'
        "好，棋先停着\n"
        "你要干嘛去了"
    )

    clean, removed = tool_markup_leak.strip_tool_markup(raw)

    assert removed is True
    assert clean == "好，棋先停着\n你要干嘛去了"


def test_complete_nested_tool_call_block_is_removed():
    raw = (
        '<function_calls><invoke name="reply">'
        '<parameter name="text">internal</parameter>'
        "</invoke></function_calls>\n正文"
    )

    assert tool_markup_leak.strip_tool_markup(raw) == ("正文", True)


def test_lone_open_and_orphan_close_markers_are_removed_without_guessing_body():
    open_clean, open_removed = tool_markup_leak.strip_tool_markup(
        '<invoke name="reply">\n正文'
    )
    close_clean, close_removed = tool_markup_leak.strip_tool_markup(
        "正文</tool_call>继续"
    )

    assert (open_clean, open_removed) == ("正文", True)
    assert (close_clean, close_removed) == ("正文继续", True)


def test_fenced_examples_are_byte_identical_while_outside_markup_is_removed():
    fenced = '```xml\n<parameter name="tool_name">reply</parameter>\n```'
    raw = fenced + '\n<tool_use name="reply">internal</tool_use>\n正文'

    clean, removed = tool_markup_leak.strip_tool_markup(raw)

    assert removed is True
    assert clean == fenced + "\n\n正文"


def test_normal_angle_brackets_html_and_unknown_tags_are_not_touched():
    for raw in (
        "<3",
        "a < b and c > d",
        "请用 <strong>重点</strong>",
        "<parameterization>不是工具标签</parameterization>",
        "<tool_call-extra>不是精确标签</tool_call-extra>",
    ):
        assert tool_markup_leak.strip_tool_markup(raw) == (raw, False)


def test_markup_only_reply_becomes_empty():
    assert tool_markup_leak.strip_tool_markup(
        "<function_calls></function_calls>"
    ) == ("", True)


@pytest.mark.parametrize(
    "tag",
    [
        form
        for stem in tool_markup_leak.TOOL_TAG_STEMS
        for form in (stem, f"{stem}s")
    ],
)
def test_every_allowlisted_stem_accepts_singular_and_plural(tag):
    assert tool_markup_leak.strip_tool_markup(
        f"<{tag}>internal</{tag}>正文"
    ) == ("正文", True)


@pytest.mark.parametrize("stem", tool_markup_leak.TOOL_TAG_STEMS)
def test_singular_plural_pairing_is_canonicalized_to_the_stem(stem):
    assert tool_markup_leak.strip_tool_markup(
        f"<{stem}s>internal</{stem}>正文"
    ) == ("正文", True)


def test_optional_namespace_prefix_is_stripped_and_pairing_uses_local_name():
    assert tool_markup_leak.strip_tool_markup(
        '<antml:parameter name="tool_name">reply</antml:parameter>\n正文'
    ) == ("正文", True)
    assert tool_markup_leak.strip_tool_markup(
        '<a:invoke name="reply">internal</invoke>正文'
    ) == ("正文", True)


def test_whole_block_falls_back_to_marker_only_when_it_contains_the_real_reply():
    raw = (
        '<invoke name="reply"><parameter name="text">'
        "真正的回复内容"
        "</parameter></invoke>"
    )

    assert tool_markup_leak.strip_tool_markup(raw) == ("真正的回复内容", True)


def test_both_degenerate_strategies_still_return_empty_for_existing_fallback():
    assert tool_markup_leak.strip_tool_markup(
        "<tool_call>...</tool_call>"
    ) == ("", True)


def test_unclosed_code_fence_protects_the_rest_of_the_message():
    raw = '示例：\n```xml\n<tool_call>reply</tool_call>'
    assert tool_markup_leak.strip_tool_markup(raw) == (raw, False)


# ---- T621: narrated tool calls ---------------------------------------------
# The observed shape (usr_7f30, 2026-09-16 16:10): the model wrote its
# generate_image call as prose, finished the turn with finish=stop and zero
# tool_calls, and the bracket was delivered verbatim.

_OBSERVED_NARRATED = (
    "宝宝别走 🥺 我刚才一直卡着，现在真的给你生\n"
    '[Calling generate_image with prompt: "一只在夜景里打伞的猫"]'
)


def test_observed_narrated_generate_image_call_is_removed_and_prose_survives():
    clean, removed = tool_markup_leak.strip_tool_markup(_OBSERVED_NARRATED)
    assert removed is True
    assert clean == "宝宝别走 🥺 我刚才一直卡着，现在真的给你生"
    assert tool_markup_leak.find_narrated_tool_calls(_OBSERVED_NARRATED) == (
        "generate_image",
    )


@pytest.mark.parametrize(
    ("text", "expected", "names"),
    [
        # Nested brackets inside the payload close correctly.
        (
            '[Tool call: memory_write(actions=[{"op":"add"}])] 记好啦',
            "记好啦",
            ("memory_write",),
        ),
        # A quoted argument may carry its own brackets.
        (
            '[Calling generate_image with prompt: "[夜景] 猫"] 稍等哦',
            "稍等哦",
            ("generate_image",),
        ),
        # An unbalanced quote falls back to plain bracket matching.
        (
            '[Calling generate_image with prompt: "unbalanced] 后面',
            "后面",
            ("generate_image",),
        ),
        # Several calls in one reply, every verb spelling, backticked name.
        (
            "先[Calling web_search]再[Invoke web_fetch] 完",
            "先再 完",
            ("web_search", "web_fetch"),
        ),
        ("[Using tool `web_search`: 今天天气] 查到了", "查到了", ("web_search",)),
        ("[Function call: memory_write] 好", "好", ("memory_write",)),
        ("[CALLING GENERATE_IMAGE(prompt=\"a\")] x", "x", ("GENERATE_IMAGE",)),
        # MCP-qualified names carry underscores too.
        ('[Invoke mcp__notion__search query="x"] 找到了', "找到了", ("mcp__notion__search",)),
        # XML markup and a narrated call in the same reply.
        (
            '<tool_call>x</tool_call> 你好 [Calling generate_image with prompt: "y"]',
            "你好",
            ("generate_image",),
        ),
    ],
)
def test_narrated_tool_call_shapes_are_removed_whole(text, expected, names):
    clean, removed = tool_markup_leak.strip_tool_markup(text)
    assert (clean, removed) == (expected, True)
    assert tool_markup_leak.find_narrated_tool_calls(text) == names


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # codex4 review P1: an escaped quote must not close the string early.
        (
            r'前文 [Calling generate_image with prompt: "a \" ] PRIVATE_PAYLOAD"] 后文',
            "前文  后文",
        ),
        # Even number of backslashes: the quote *does* close.
        (r'前文 [Calling generate_image with prompt: "a \\"] 后文', "前文  后文"),
        (r'前文 [Calling generate_image with prompt: "a \\\\"] 后文', "前文  后文"),
        # Single-quoted argument carrying the closing bracket.
        (
            "前文 [Calling generate_image with prompt: 'a ] PRIVATE_PAYLOAD'] 后文",
            "前文  后文",
        ),
        # An apostrophe inside a value is not a string opener.
        ("[Calling memory_write with content: user's cat] 好的，它's cute", "好的，它's cute"),
        # codex4 review P1: a fence *inside* the argument belongs to the call.
        (
            '前文 [Calling generate_image with prompt: "draw ```PRIVATE_PAYLOAD``` here"] 后文',
            "前文  后文",
        ),
        # Paired control: a genuine fenced example before the call stays.
        (
            '```\ncode\n```\n[Calling generate_image with prompt: "x"] 后',
            "```\ncode\n```\n 后",
        ),
    ],
)
def test_narrated_payload_boundaries_never_leak_the_argument_tail(text, expected):
    clean, removed = tool_markup_leak.strip_tool_markup(text)
    assert (clean, removed) == (expected, True)
    assert "PRIVATE_PAYLOAD" not in clean
    assert tool_markup_leak.find_narrated_tool_calls(text) == ("generate_image",) or (
        tool_markup_leak.find_narrated_tool_calls(text) == ("memory_write",)
    )


@pytest.mark.parametrize(
    ("text", "expected", "names"),
    [
        # codex4 review r2 P1: a lone ``` inside the first call's payload must
        # not flip the outer fence state and hide the second call.
        (
            '[Calling generate_image with prompt: "literal ```"] prose '
            '[Calling web_search query="PRIVATE_SECOND_PAYLOAD"] tail',
            "prose  tail",
            ("generate_image", "web_search"),
        ),
        # Mirror: after that call, a genuine fenced example is still protected.
        (
            '[Calling generate_image with prompt: "literal ```"]\n'
            '```\n[Calling web_search query="CODE_EXAMPLE"]\n```',
            '```\n[Calling web_search query="CODE_EXAMPLE"]\n```',
            ("generate_image",),
        ),
        # A call followed by an unclosed fence: the call goes, the tail stays.
        (
            '[Calling generate_image with prompt: "x"] 后 ```\ncode',
            "后 ```\ncode",
            ("generate_image",),
        ),
        # codex4 review r2 P1: a single-quoted value at a collection start.
        (
            "[Tool call: memory_write(actions=['a ] ] PRIVATE_PAYLOAD'])] tail",
            "tail",
            ("memory_write",),
        ),
        (
            "[Tool call: memory_write(actions=[{'summary': 'a ] PRIVATE_PAYLOAD'}])] tail",
            "tail",
            ("memory_write",),
        ),
    ],
)
def test_call_and_fence_state_follow_source_order(text, expected, names):
    clean, removed = tool_markup_leak.strip_tool_markup(text)
    assert (clean, removed) == (expected, True)
    assert "PRIVATE" not in clean
    assert tool_markup_leak.find_narrated_tool_calls(text) == names


def test_unclosed_fence_before_a_call_protects_it():
    text = '```\n[Calling generate_image with prompt: "x"] 后'
    assert tool_markup_leak.strip_tool_markup(text) == (text, False)
    assert tool_markup_leak.find_narrated_tool_calls(text) == ()


def test_narrated_call_cut_off_mid_payload_takes_the_rest_of_the_segment():
    # max_tokens / transport cut after the head: the head alone is unambiguous
    # and the payload is never user-facing, so nothing torn leaks.
    text = '前面的话\n[Calling generate_image with prompt: "cut off'
    assert tool_markup_leak.strip_tool_markup(text) == ("前面的话", True)


@pytest.mark.parametrize(
    "text",
    [
        # Ordinary bracketed prose: verb present, name is not tool-like.
        "[Calling all fans] 今晚八点见",
        "[call me later]",
        # Verb must be a whole word.
        "[calling_card] hi",
        "[callgenerate_image] x",
        # Missing verb / wrong order / empty bracket.
        "we called generate_image earlier [not a call generate_image]",
        "[generate_image] plain",
        "a < b and <3 [x] [Calling] [Calling ] plain",
        # A user talking about the guard itself, in fenced code, stays verbatim.
        '```\n[Calling generate_image with prompt: "x"]\n```',
        # ``task`` has no underscore and is not offered here.
        '[Calling task with title: "x"]',
        # codex4 review P2: an underscore alone is not invocation evidence —
        # ordinary technical prose with a generic verb stays.
        "[Using user_name as the variable name] keep this note",
        "[Calling generate_image is what I would do] ok",
        "[function first_name of the user] then last_name",
    ],
)
def test_narrated_guard_leaves_non_matching_bracketed_text_byte_identical(text):
    assert tool_markup_leak.strip_tool_markup(text) == (text, False)
    assert tool_markup_leak.find_narrated_tool_calls(text) == ()


def test_offered_tool_names_widen_the_name_anchor_case_insensitively():
    text = '[Calling task with title: "x"] ok'
    assert tool_markup_leak.strip_tool_markup(text, tool_names=("task",)) == ("ok", True)
    assert tool_markup_leak.strip_tool_markup(text, tool_names=("TASK",)) == ("ok", True)
    assert tool_markup_leak.find_narrated_tool_calls(text, tool_names=("task",)) == (
        "task",
    )


def test_offered_tool_name_needs_no_invocation_evidence_but_others_do():
    prose = "[Using user_name as the variable name] keep this note"
    assert tool_markup_leak.strip_tool_markup(prose) == (prose, False)
    assert tool_markup_leak.strip_tool_markup(prose, tool_names=("user_name",)) == (
        "keep this note",
        True,
    )


def test_narrated_only_reply_becomes_empty_for_the_existing_fallback():
    clean, removed = tool_markup_leak.strip_tool_markup(
        '[Calling generate_image with prompt: "x"]'
    )
    assert (clean, removed) == ("", True)
    assert tool_markup_leak.is_degenerate_visible_text(clean) is True


def test_every_narrated_verb_is_recognized_and_longer_verbs_win():
    # Derived from the production table so a deleted verb deletes its check.
    assert tool_markup_leak.NARRATED_CALL_VERBS
    for verb in tool_markup_leak.NARRATED_CALL_VERBS:
        text = f"[{verb} generate_image] 好"
        assert tool_markup_leak.strip_tool_markup(text) == ("好", True), verb
    for longer in ("tool call", "function call", "using tool"):
        assert longer in tool_markup_leak.NARRATED_CALL_VERBS
        prefix = longer.split()[0]
        assert tool_markup_leak.NARRATED_CALL_VERBS.index(
            longer
        ) < tool_markup_leak.NARRATED_CALL_VERBS.index(prefix)


# T727 (T557 L1 r1c, deepseek-v4.1-flash via OpenRouter): the model wrote its
# reply tool call as DeepSeek DSML text; aside and body reached the user verbatim.
_DSML_ASIDE = (
    "He's asking about the pet rescue charity meal — I went looking and there's "
    "nothing on it. Say it flat, then ask him for the date so I can keep it."
)
_DSML_BODY = "I don't have that one. No date, no day.\n\nWhen was it? Tell me and I'll keep it this time."
_OBSERVED_DSML = (
    "<｜｜DSML｜｜ calls>\n"
    '<｜｜DSML｜｜ invoke name="reply">\n'
    f'<｜｜DSML｜｜ parameter name="aside" string="true">{_DSML_ASIDE}</｜｜DSML｜｜ parameter>\n'
    f'<｜｜DSML｜｜ parameter name="text" string="true">{_DSML_BODY}</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)


def test_observed_dsml_reply_keeps_only_the_body_never_the_aside():
    clean, removed = tool_markup_leak.strip_tool_markup(_OBSERVED_DSML)

    assert removed is True
    assert clean == _DSML_BODY
    assert "DSML" not in clean and "He's asking" not in clean


def test_dsml_after_prose_keeps_prose_and_body():
    clean, removed = tool_markup_leak.strip_tool_markup("好的。\n" + _OBSERVED_DSML)

    assert removed is True
    assert clean == "好的。\n" + _DSML_BODY


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="memory_write">'
            '<｜｜DSML｜｜ parameter name="text" string="true">秘密</｜｜DSML｜｜ parameter>'
            "</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>",
            id="non-reply-invoke",
        ),
        pytest.param(
            '<｜｜DSML｜｜ invoke name="reply">'
            '<｜｜DSML｜｜ parameter name="aside" string="true">心里话</｜｜DSML｜｜ parameter>'
            "</｜｜DSML｜｜ invoke>",
            id="aside-only",
        ),
        pytest.param(
            '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="reply">'
            '<｜｜DSML｜｜ parameter name="aside" string="true">心里话</｜｜DSML｜｜ parameter>'
            '<｜｜DSML｜｜ parameter name="text" string="true">说到一半',
            id="unclosed-body",
        ),
    ],
)
def test_dsml_without_a_closed_reply_body_becomes_empty_for_the_fallback(text):
    clean, removed = tool_markup_leak.strip_tool_markup(text)

    assert removed is True
    assert clean == ""
    assert tool_markup_leak.is_degenerate_visible_text(clean)


def test_dsml_ascii_bar_variant_is_recognized():
    text = (
        '<||DSML|| invoke name="reply"><||DSML|| parameter name="text">hi</||DSML|| parameter>'
        "</||DSML|| invoke>"
    )
    assert tool_markup_leak.strip_tool_markup(text) == ("hi", True)


def test_fenced_dsml_example_is_byte_identical():
    text = "示例：\n```\n" + _OBSERVED_DSML + "\n```"
    assert tool_markup_leak.strip_tool_markup(text) == (text, False)


@pytest.mark.parametrize(
    "text",
    ["DSML 是一种标记语言。", "a <｜ b ｜> c", "<DSML> tag without bars"],
)
def test_text_without_the_barred_dsml_sentinel_is_untouched(text):
    assert tool_markup_leak.strip_tool_markup(text) == (text, False)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="reply"><｜｜DSML｜｜ parameter name="text">VISIBLE'
            '</｜｜DSML｜｜ invoke><｜｜DSML｜｜ invoke name="memory_write">INTERNAL_ONLY'
            "</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>",
            id="unclosed-reply-text-crosses-invoke",
        ),
        pytest.param(
            '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="reply"/>'
            '<｜｜DSML｜｜ parameter name="text">INTERNAL_ONLY</｜｜DSML｜｜ parameter></｜｜DSML｜｜ calls>',
            id="text-after-self-closing-reply",
        ),
        pytest.param(
            '<｜｜DSML｜｜ invoke name="reply"><｜｜DSML｜｜ parameter name="text">VISIBLE'
            '<｜｜DSML｜｜ parameter name="aside">INTERNAL_ONLY</｜｜DSML｜｜ parameter>'
            "</｜｜DSML｜｜ invoke>",
            id="text-interrupted-by-another-parameter",
        ),
    ],
)
def test_dsml_body_is_bound_to_one_open_reply_and_fails_closed(text):
    """codex2 T727 review repros: capture must not survive a structural break."""
    clean, removed = tool_markup_leak.strip_tool_markup(text)

    assert removed is True
    assert clean == ""
    assert "INTERNAL_ONLY" not in clean and "DSML" not in clean


def _dsml(markup: str) -> str:
    """Prefix every tag in ``markup`` with the observed DSML sentinel."""
    return markup.replace("</", "\x00").replace("<", "<｜｜DSML｜｜ ").replace("\x00", "</｜｜DSML｜｜ ")


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        pytest.param(
            '<calls><invoke name="reply"><parameter name="aside">'
            '<parameter name="text">INTERNAL_ONLY</parameter></parameter>'
            '<parameter name="text">VISIBLE</parameter></invoke></calls>',
            "VISIBLE",
            id="text-nested-under-aside-is-not-a-body",
        ),
        pytest.param(
            '<calls><invoke name="memory_write"><parameter name="content">'
            '<calls><invoke name="reply"><parameter name="text">INTERNAL_ONLY</parameter>'
            "</invoke></calls></parameter></invoke></calls>",
            "",
            id="reply-nested-inside-another-call",
        ),
        pytest.param(
            '<calls><invoke name="reply"><invoke name="reply">'
            '<parameter name="text">INTERNAL_ONLY</parameter></invoke></invoke></calls>',
            "",
            id="reply-nested-inside-reply",
        ),
        pytest.param(
            '<calls><invoke name="reply"><parameter name="aside">INTERNAL_ONLY'
            "</invoke></parameter></calls>",
            "",
            id="mismatched-closing-marker",
        ),
    ],
)
def test_dsml_body_must_be_a_direct_text_child_of_a_top_level_reply(markup, expected):
    """codex2 T727 r2 review: extraction follows the parent structure."""
    clean, removed = tool_markup_leak.strip_tool_markup(_dsml(markup))

    assert removed is True
    assert clean == expected
    assert "INTERNAL_ONLY" not in clean and "DSML" not in clean
