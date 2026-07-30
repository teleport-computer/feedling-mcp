"""v1 self-authored thinking — <think> block parser (runtime-neutral).

io is prompted to wrap a short thinking line in <think>…</think> at the start of
its reply. split_thinking peels that block into the thinking channel and returns
the clean reply. Fail-open: no block → reply byte-identical, no thinking; a block
that would empty the reply becomes the reply instead (never an empty bubble).

The same <think> marker is what the V1 resident consumer already extracts, so V1
needs no consumer change — only the gated prompt instruction. Pure logic here;
whether real models emit the block is validated by real-model e2e.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from core import self_thinking as st  # noqa: E402


def test_leading_block_split_into_thinking_and_clean_reply():
    thinking, reply = st.split_thinking("<think>我先查下天气</think>今天北京晴，25°")
    assert thinking == "我先查下天气"
    assert reply == "今天北京晴，25°"


def test_block_with_newline_before_reply():
    thinking, reply = st.split_thinking("<think>我先查下天气</think>\n今天北京晴")
    assert thinking == "我先查下天气"
    assert reply == "今天北京晴"


def test_no_block_is_failopen_reply_unchanged():
    original = "今天北京晴，25°"
    thinking, reply = st.split_thinking(original)
    assert thinking == ""
    assert reply == original


def test_block_only_becomes_reply_never_empty():
    # Model put the whole answer inside <think> → don't empty the reply; use it
    # AS the reply, no thinking (fail-open, e2e-driven guard).
    thinking, reply = st.split_thinking("<think>只想了一下</think>")
    assert thinking == ""
    assert reply == "只想了一下"


def test_alternate_tag_thinking_accepted():
    thinking, reply = st.split_thinking("<thinking>想一下</thinking>正文")
    assert thinking == "想一下"
    assert reply == "正文"


def test_thinking_sanitized_no_control_or_bidi():
    thinking, _ = st.split_thinking("<think>想\x00一下‮坏</think>正文")
    assert "\x00" not in thinking
    assert "‮" not in thinking


def test_thinking_length_capped():
    long = "很长" * 500
    thinking, _ = st.split_thinking(f"<think>{long}</think>正文")
    assert len(thinking) <= st.MAX_THINKING_CHARS


def test_case_insensitive_and_spaced_tag():
    thinking, reply = st.split_thinking("<Think >想</ Think>正文")
    assert thinking == "想"
    assert reply == "正文"


def test_reply_with_stray_close_tag_is_failopen():
    # No opening tag → no block → reply unchanged.
    original = "答案 </think> 混进来了"
    thinking, reply = st.split_thinking(original)
    assert thinking == ""
    assert reply == original
