"""Strip a closed set of leaked tool-call markup from visible model text.

Some OpenAI-compatible relays render native tool calls as XML-like text before
parsing them back into structured calls.  A partial parse can leave those
markers inside an otherwise useful reply.  This module deliberately recognizes
only the tool-protocol tag names below; it is not a general HTML/XML sanitizer.
"""
from __future__ import annotations

import re
import unicodedata


ERROR_CLASS = "upstream_unavailable"
REASON = "tool_markup_leak_sanitized"

TOOL_TAG_STEMS = (
    "function_call",
    "tool_result",
    "tool_call",
    "tool_use",
    "parameter",
    "invoke",
)
_TAG_TOKEN_RE = re.compile(
    r"<\s*(?P<closing>/\s*)?"
    r"(?:[A-Za-z][A-Za-z0-9_.-]*:)?"
    r"(?P<stem>" + "|".join(TOOL_TAG_STEMS) + r")s?"
    r"(?=[\s/>])(?P<tail>[^>]*)>",
    flags=re.IGNORECASE,
)
_CODE_FENCE = "```"


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _remove_intervals(text: str, intervals: list[tuple[int, int]]) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end in _merge_intervals(intervals):
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _strip_unfenced_segment(text: str) -> tuple[str, str, bool]:
    tokens = list(_TAG_TOKEN_RE.finditer(text))
    if not tokens:
        return text, text, False

    # Every recognized marker is removed.  When an opening marker has a matching
    # close, the primary result removes the whole protocol block as well.
    # ``marker_only`` is retained as a safety candidate: if whole-block removal
    # would eat the entire useful reply, its payload is preferable to a false
    # fallback bubble. Unmatched markers always lose only the marker because
    # there is no safe boundary for guessing where their payload ends.
    marker_intervals = [(match.start(), match.end()) for match in tokens]
    block_intervals = list(marker_intervals)
    stack: list[tuple[str, int]] = []
    for match in tokens:
        # Pair by the allowlisted stem so namespace prefixes and singular/plural
        # drift cannot stop an otherwise recognizable block from closing.
        name = match.group("stem").lower()
        closing = bool(match.group("closing"))
        self_closing = not closing and match.group("tail").rstrip().endswith("/")
        if self_closing:
            continue
        if not closing:
            stack.append((name, match.start()))
            continue

        opening_index = next(
            (index for index in range(len(stack) - 1, -1, -1) if stack[index][0] == name),
            None,
        )
        if opening_index is None:
            continue
        block_intervals.append((stack[opening_index][1], match.end()))
        del stack[opening_index:]

    return (
        _remove_intervals(text, block_intervals),
        _remove_intervals(text, marker_intervals),
        True,
    )


def is_degenerate_visible_text(text: object) -> bool:
    """Mirror the visible-reply gate: empty/punctuation-only is not a reply."""
    for char in str(text or ""):
        category = unicodedata.category(char)
        if category[0] in {"L", "N"} or category == "So":
            return False
    return True


def strip_tool_markup(text: str) -> tuple[str, bool]:
    """Return ``(clean_text, removed)`` while preserving fenced code verbatim.

    Clean input is returned byte-for-byte.  If a known tool marker is removed,
    only surrounding outer whitespace is normalized; normal HTML, comparisons,
    emoticons such as ``<3``, and unknown tag names are untouched. One optional
    XML namespace prefix is accepted, but matching and pairing use only the
    allowlisted local name. Triple-backtick fences are protected; inline
    single-backtick spans are intentionally outside this narrow leak boundary.
    """
    raw = str(text or "")
    block_output: list[str] = []
    marker_output: list[str] = []
    cursor = 0
    removed = False

    while cursor < len(raw):
        fence_start = raw.find(_CODE_FENCE, cursor)
        if fence_start < 0:
            block_clean, marker_clean, changed = _strip_unfenced_segment(raw[cursor:])
            block_output.append(block_clean)
            marker_output.append(marker_clean)
            removed = removed or changed
            break

        block_clean, marker_clean, changed = _strip_unfenced_segment(
            raw[cursor:fence_start]
        )
        block_output.append(block_clean)
        marker_output.append(marker_clean)
        removed = removed or changed

        fence_end = raw.find(_CODE_FENCE, fence_start + len(_CODE_FENCE))
        if fence_end < 0:
            block_output.append(raw[fence_start:])
            marker_output.append(raw[fence_start:])
            cursor = len(raw)
            break
        fence_end += len(_CODE_FENCE)
        block_output.append(raw[fence_start:fence_end])
        marker_output.append(raw[fence_start:fence_end])
        cursor = fence_end

    if not raw:
        return "", False
    if not removed:
        return raw, False
    block_clean = "".join(block_output).strip()
    marker_clean = "".join(marker_output).strip()
    clean = (
        marker_clean
        if is_degenerate_visible_text(block_clean)
        and not is_degenerate_visible_text(marker_clean)
        else block_clean
    )
    return clean, True
