"""Deterministic, content-free writing-system classification.

This module deliberately has no Runtime, database, provider, or locale
dependencies.  It measures Unicode writing systems, not linguistic intent: a
legitimate request such as "translate this to English" can therefore be
classified as a mismatch.  Foreground correction gives the model one explicit
escape to repeat the original in that case instead of changing this shared
classifier's historical meaning.
"""
from __future__ import annotations

import unicodedata
from typing import Literal


MIN_LETTER_COUNT = 10
DOMINANT_SHARE = 0.60

CORRECTION_INSTRUCTION = (
    "你刚才这条回复,语言和这个人正在说的语言对不上。除非这个人要求过你用别的语言,"
    "否则用这个人的语言把同一条回复重说一遍:内容、语气、分寸都不变,只换语言。"
    "要是这个人确实要求过现在这种语言,就原样重复原回复。"
)

WritingSystem = Literal[
    "han",
    "latin",
    "kana",
    "hangul",
    "cyrillic",
    "other",
    "mixed",
    "indeterminate",
]

WRITING_SYSTEMS = frozenset({
    "han",
    "latin",
    "kana",
    "hangul",
    "cyrillic",
    "other",
    "mixed",
    "indeterminate",
})

_COUNTED_SYSTEMS = (
    "han",
    "latin",
    "kana",
    "hangul",
    "cyrillic",
    "other",
)


def _is_han(codepoint: int) -> bool:
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
        or 0x30000 <= codepoint <= 0x3134F
    )


def _letter_system(character: str) -> str:
    codepoint = ord(character)
    if _is_han(codepoint):
        return "han"
    name = unicodedata.name(character, "")
    if name.startswith(("HIRAGANA ", "KATAKANA ")):
        return "kana"
    if name.startswith("HANGUL "):
        return "hangul"
    if name.startswith("CYRILLIC "):
        return "cyrillic"
    if name.startswith("LATIN "):
        return "latin"
    return "other"


def classify_writing_system(text: str) -> WritingSystem:
    """Return the dominant Unicode writing system in ``text``.

    Only characters whose Unicode general category starts with ``L`` enter the
    denominator.  Punctuation, digits, emoji, whitespace, and combining marks
    do not vote.  Fewer than ten letters is indeterminate; otherwise a system
    must exceed (not merely equal) 60 percent, or the result is mixed.
    """

    counts = {system: 0 for system in _COUNTED_SYSTEMS}
    total = 0
    for character in str(text or ""):
        if not unicodedata.category(character).startswith("L"):
            continue
        counts[_letter_system(character)] += 1
        total += 1
    if total < MIN_LETTER_COUNT:
        return "indeterminate"
    dominant = max(_COUNTED_SYSTEMS, key=counts.__getitem__)
    if counts[dominant] / total > DOMINANT_SHARE:
        return dominant  # type: ignore[return-value]
    return "mixed"
