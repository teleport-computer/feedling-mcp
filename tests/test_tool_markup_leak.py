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
        "function_calls",
        "invoke",
        "parameter",
        "tool_use",
        "tool_call",
        "tool_result",
    ],
)
def test_every_allowlisted_paired_tag_is_removed(tag):
    assert tool_markup_leak.strip_tool_markup(
        f"<{tag}>internal</{tag}>正文"
    ) == ("正文", True)


@pytest.mark.parametrize(
    "tag",
    ["function_call", "function_calls", "tool_call", "tool_calls"],
)
def test_singular_and_plural_wrapper_names_are_recognized(tag):
    assert tool_markup_leak.strip_tool_markup(
        f"<{tag}>internal</{tag}>正文"
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
