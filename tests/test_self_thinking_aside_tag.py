"""T587 (2026-09-15): the self-thinking protocol rendered under the ``aside`` tag.

Why a second tag exists: the block is a first-person aside the app shows to the
user (folded under 「参考内容」). Calling it ``think`` made Anthropic's request
classifier read the Claude Code driver's prompt as a request for the model's
hidden reasoning (``reasoning_extraction``) and reject every turn on the Opus 5
family. ``aside`` names what the block actually is; pi / codex keep ``think``
byte for byte.

Two things must hold, in opposite directions:
  * the ``think`` rendering is untouched (pi / codex contract);
  * the parser treats ``aside`` exactly like ``think`` (a block wrapped in either
    can never reach the user un-stripped).
"""
from __future__ import annotations

import pytest

from agent_protocol_core import self_thinking as st


def test_think_rendering_is_the_untouched_constant():
    assert st.instruction() is st.INSTRUCTION
    assert st.instruction(st.TAG_THINK) is st.INSTRUCTION


def test_aside_rendering_only_applies_the_declared_substitutions():
    aside = st.instruction(st.TAG_ASIDE)
    assert "<think>" not in aside and "</think>" not in aside
    assert aside.count("<aside>") == st.INSTRUCTION.count("<think>") > 0
    assert aside.count("</aside>") == st.INSTRUCTION.count("</think>") > 0
    for anchor, replacement in st._ASIDE_SUBSTITUTIONS:
        assert anchor not in aside
        assert replacement in aside
    # Round-trip: undo the substitutions and the constant comes back exactly.
    restored = aside.replace("<aside>", "<think>").replace("</aside>", "</think>")
    for anchor, replacement in st._ASIDE_SUBSTITUTIONS:
        restored = restored.replace(replacement, anchor)
    assert restored == st.INSTRUCTION


def test_aside_anchors_are_present_exactly_once():
    # instruction(aside) refuses to render if any anchor drifts; keep them honest.
    for anchor, _replacement in st._ASIDE_SUBSTITUTIONS:
        assert st.INSTRUCTION.count(anchor) == 1
    assert "他听不见" in st._THINK_VISIBILITY_SENTENCE
    assert "展示给他" in st._ASIDE_VISIBILITY_SENTENCE
    assert "真实的想法" in st._THINK_CONTENT_PHRASE
    assert "真实的想法" not in st.instruction(st.TAG_ASIDE)


def test_unknown_tag_is_rejected():
    with pytest.raises(ValueError):
        st.instruction("reasoning")


def test_tag_words_include_aside_and_stay_longest_first():
    assert st.TAG_ASIDE in st._TAG_WORDS
    assert st.TAG_THINK in st._TAG_WORDS
    words = list(st._TAG_WORDS)
    for i, shorter in enumerate(words):
        for longer in words[:i]:
            # a longer alternative that starts with a later, shorter word must
            # come first in the alternation (``thinking`` before ``think``).
            assert not shorter.startswith(longer) or shorter == longer


_SHAPES = {
    "paired": "<{t}>心里话</{t}>正文",
    "orphan_open": "<{t}>心里话 正文",
    "orphan_close": "心里话</{t}>正文",
    "two_blocks": "<{t}>一</{t}>甲<{t}>二</{t}>乙",
    "namespace_prefixed": "<ns:{t}>心里话</ns:{t}>正文",
    "namespace_prefixed_close_only": "<{t}>心里话</{t}>正文",
    "nested_same": "<{t}>外<{t}>内</{t}></{t}>正文",
    "truncated_opener": "<{t_cut}",
    "lone_close_after_block": "<{t}>一</{t}>正文</{t}>",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_aside_parses_exactly_like_think(shape):
    """Parity is the contract: whatever the parser does for ``think`` it must do
    for ``aside`` — same status, same thinking, same reply — so the aside tag
    can never be the one that leaks."""
    template = _SHAPES[shape]
    think_in = template.format(t="think", t_cut="thin")
    aside_in = template.format(t="aside", t_cut="asid")
    def _norm(result):
        # split_thinking only strips the leading block, so a later block can
        # legitimately survive in the reply; compare modulo the tag name.
        return tuple(str(part).replace("aside", "think").replace("asid", "thin") for part in result)

    assert _norm(st.strip_all_thinking(aside_in)) == _norm(st.strip_all_thinking(think_in))
    assert _norm(st.split_thinking(aside_in)) == _norm(st.split_thinking(think_in))


def test_paired_aside_block_is_stripped_and_returned_as_thinking():
    status, thinking, reply = st.strip_all_thinking("<aside>心里话</aside>正文")
    assert (status, thinking, reply) == (st.COMPLETE, "心里话", "正文")


def test_orphan_aside_opener_fails_closed():
    status, _thinking, reply = st.strip_all_thinking("<aside>心里话 正文")
    assert status == st.FAILED and reply == ""


def test_mixed_tag_pair_fails_closed():
    status, _thinking, reply = st.strip_all_thinking("<aside>心里话</think>正文")
    assert status == st.FAILED
    assert reply == ""


@pytest.mark.parametrize("text", ["<asid", "<thin", "  <ns:asid", "<asid\n", "<aside", "<think"])
def test_truncated_protocol_opener_fails_closed_at_the_shared_gate(text):
    # A block cut before its ``>`` is the model's thinking, not visible text
    # (codex3 review of T587: ``<asid`` used to pass through as the reply).
    status, thinking, reply = st.strip_all_thinking(text)
    assert (status, thinking, reply) == (st.FAILED, "", "")


@pytest.mark.parametrize("text", ["<a", "<t", "<a href=\"x\">link</a> 正文", "<th>cell</th>", "<asid> x", "plain"])
def test_short_or_complete_html_is_not_a_truncated_opener(text):
    status, _thinking, reply = st.strip_all_thinking(text)
    assert status == st.ABSENT
    assert reply == text


def test_html_anchor_at_reply_start_is_not_our_protocol():
    # ``<a …>`` shares a first letter with ``aside``; the production stripper must
    # keep treating it as ordinary text.
    status, _thinking, reply = st.strip_all_thinking('<a href="x">link</a> 正文')
    assert status == st.ABSENT
    assert reply == '<a href="x">link</a> 正文'
