"""v1 self-authored thinking — leading <think> parser (runtime-neutral).

Contract (hardened after Codex review — the parser is a mini state machine, not a
regex scrub):

  split_thinking(text) -> (status, thinking, reply)

  ABSENT   : no leading <think> protocol candidate → reply is the ORIGINAL text
             byte-identical, thinking "" (UI shows no thinking block).
  COMPLETE : exactly one clean leading <tag>…</tag>, matched, non-nested, followed
             by a NON-empty reply → thinking = sanitized block, reply = the rest.
  FAILED   : anything the parser cannot resolve to a clean (thinking, reply) split
             — truncation anywhere in the opener/closer, mismatched/nested tags, a
             clean block with no public reply, a leading close tag. thinking "" and
             reply "" (the caller shows a "thinking failed" marker + a generic
             failure bubble). A raw <think fragment or private thinking content
             must NEVER surface as the reply.

The invariant is narrowed (Codex): once the text is a *leading* protocol
candidate, no protocol fragment of it reaches the reply; a <think> that legitimately
appears LATER in a normal reply (e.g. discussing HTML) is left untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from core import self_thinking as st  # noqa: E402

ABSENT, COMPLETE, FAILED = st.ABSENT, st.COMPLETE, st.FAILED


# --- ABSENT: no leading protocol → reply untouched ---------------------------

def test_no_tag_absent_reply_byte_identical():
    original = "今天北京晴，25°"
    assert st.split_thinking(original) == (ABSENT, "", original)


def test_leading_non_think_tag_is_absent_and_kept():
    # user content that starts with some other tag must be left alone
    for s in ("<div>hi</div>", "<3 你", "<foo>bar"):
        status, thinking, reply = st.split_thinking(s)
        assert status == ABSENT
        assert reply == s


def test_think_tag_later_in_reply_is_kept_not_failed():
    # a legit <think> deep in a normal reply (discussing the tag) is NOT ours
    s = "HTML 里的 <think> 标签这样写：<think>x</think>"
    assert st.split_thinking(s) == (ABSENT, "", s)


# --- COMPLETE: clean block + reply ------------------------------------------

def test_complete_block():
    assert st.split_thinking("<think>我先查下天气</think>今天北京晴") == (
        COMPLETE, "我先查下天气", "今天北京晴")


def test_complete_alternate_tag_and_spacing_and_case():
    assert st.split_thinking("<Thinking >想一下</ Thinking >正文")[0] == COMPLETE
    st_, t, r = st.split_thinking("<reasoning>算一下</reasoning>结果是3")
    assert (st_, t, r) == (COMPLETE, "算一下", "结果是3")


def test_complete_reply_may_contain_later_think_text():
    # once the leading block is clean, whatever follows is reply — even a <think>
    status, thinking, reply = st.split_thinking(
        "<think>先解释</think>HTML 用 <think> 表示思考")
    assert status == COMPLETE
    assert thinking == "先解释"
    assert reply == "HTML 用 <think> 表示思考"


def test_complete_thinking_is_sanitized():
    status, thinking, _ = st.split_thinking("<think>想\x00一下‮坏​</think>正文")
    assert status == COMPLETE
    assert "\x00" not in thinking and "‮" not in thinking and "​" not in thinking


def test_complete_thinking_length_capped():
    long = "很长" * 500
    _, thinking, _ = st.split_thinking(f"<think>{long}</think>正文")
    assert len(thinking) <= st.MAX_THINKING_CHARS


# --- FAILED: never leak a tag, never promote private thinking to reply -------

def _prefixes(word):
    return [word[:i] for i in range(1, len(word) + 1)]


def test_truncated_opener_prefix_matrix_all_failed_no_leak():
    # every non-empty prefix of every tag word, opener truncated before '>'
    for word in ("think", "thinking", "reasoning", "thought"):
        for p in _prefixes(word):
            for frag in (f"<{p}", f"< {p}", f"  <{p}", f"<{p} "):
                status, thinking, reply = st.split_thinking(frag)
                assert status in (ABSENT, FAILED)
                # the crucial invariant: no raw opener fragment reaches the reply
                assert "<" not in reply, f"leak on {frag!r} -> {reply!r}"
                assert thinking == "" and (reply == "" or status == ABSENT)


def test_truncated_closer_never_leaks():
    for s in ("<think>secret</thin", "<think>secret</think",
              "<think>secret</ think", "<thinking>x</thinkin"):
        status, thinking, reply = st.split_thinking(s)
        assert status == FAILED
        assert reply == "" and thinking == ""


def test_mismatched_close_tag_is_failed():
    status, thinking, reply = st.split_thinking("<think>secret</thinking>PUBLIC")
    assert status == FAILED
    assert reply == "" and "secret" not in reply


def test_nested_same_tag_is_failed():
    s = "<think>a<think>b</think>c</think>PUBLIC"
    status, thinking, reply = st.split_thinking(s)
    assert status == FAILED
    assert reply == "" and "a" not in reply and "b" not in reply


def test_unclosed_block_is_failed_not_promoted_to_reply():
    # the big privacy bug: unclosed <think> must NOT surface its content as reply
    status, thinking, reply = st.split_thinking("<think>secret plan, still going")
    assert status == FAILED
    assert reply == "" and "secret" not in reply


def test_clean_block_but_no_reply_is_failed():
    status, thinking, reply = st.split_thinking("<think>只想了一下</think>")
    assert status == FAILED
    assert reply == ""


def test_leading_close_tag_is_failed():
    status, _, reply = st.split_thinking("</think>somehow")
    assert status == FAILED
    assert reply == ""


def test_zero_width_and_bom_prefix_cannot_bypass():
    for pre in ("﻿", "​", "⁠", "​﻿ "):
        # truncated opener hidden behind invisibles must still be caught
        status, _, reply = st.split_thinking(f"{pre}<thin")
        assert status == FAILED
        assert "<" not in reply


def test_property_no_raw_think_tag_ever_in_reply():
    cases = [
        "<think>a</think>b", "<think>a", "<thin", "<think", "<thinking>x",
        "<think></think>", "<think>x</thinking>y", "<think>a<think>b</think>c</think>d",
        "</think>", "<think>secret</thin", "正常回复", "<div>x</div>",
        "<think>plan</think>literal <thinkable> and <thinking-cap>",
        "<think>ok</think>tail < /think> more",
    ]
    for s in cases:
        status, thinking, reply = st.split_thinking(s)
        # a raw opener/closer of OUR protocol must never appear at the start of the
        # reply as a leaked fragment; FAILED replies are always empty.
        if status == FAILED:
            assert reply == "" and thinking == ""
        # thinking is never surfaced as reply verbatim on non-complete parses
        if status != COMPLETE:
            assert not reply.lstrip().startswith("<think")
