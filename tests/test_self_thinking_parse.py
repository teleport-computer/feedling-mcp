"""v1 self-authored thinking — reply-prefix parser (model-agnostic).

Mechanism (spec: self-thinking v1, all models): io is prompted to begin every
reply with a single ``💭 <one short line>`` marker line, then its real reply.
This parser splits that marker off into the thinking channel and returns the
clean reply. It must be **fail-open**: anything that does not cleanly match the
first-line marker leaves the reply byte-identical and yields no thinking, so a
model that ignores or mis-emits the instruction never has its reply corrupted.

Pure logic — unit tested here. Whether real models actually emit the marker is a
prompt-behaviour question validated separately by real-model e2e.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from model_api_runtime.v2 import self_thinking as st  # noqa: E402


def test_marker_first_line_split_into_thinking_and_clean_reply():
    thinking, reply = st.split_thinking("💭 我先查下天气\n今天北京晴，25°")
    assert thinking == "我先查下天气"
    assert reply == "今天北京晴，25°"


def test_no_marker_is_failopen_reply_unchanged():
    original = "今天北京晴，25°"
    thinking, reply = st.split_thinking(original)
    assert thinking == ""
    assert reply == original


def test_marker_not_on_first_line_is_not_stripped():
    original = "好的\n💭 这不该被当成思考"
    thinking, reply = st.split_thinking(original)
    assert thinking == ""
    assert reply == original


def test_leading_whitespace_before_marker_still_parsed():
    thinking, reply = st.split_thinking("  \n💭 想一下\n正文")
    assert thinking == "想一下"
    assert reply == "正文"


def test_empty_marker_yields_no_thinking_but_strips_bare_marker_line():
    # A bare "💭" with no text must not leak into the reply and must not count
    # as thinking.
    thinking, reply = st.split_thinking("💭\n正文在这")
    assert thinking == ""
    assert reply == "正文在这"


def test_marker_only_no_reply_body():
    thinking, reply = st.split_thinking("💭 只想了一下")
    assert thinking == "只想了一下"
    assert reply == ""


def test_thinking_sanitized_no_control_or_bidi():
    thinking, _ = st.split_thinking("💭 想\x00一下‮坏\n正文")
    assert "\x00" not in thinking
    assert "‮" not in thinking


def test_thinking_length_capped():
    long = "很长" * 500
    thinking, _ = st.split_thinking(f"💭 {long}\n正文")
    assert len(thinking) <= st.MAX_THINKING_CHARS


def test_crlf_newline_after_marker_handled():
    thinking, reply = st.split_thinking("💭 想\r\n正文")
    assert thinking == "想"
    assert reply == "正文"
