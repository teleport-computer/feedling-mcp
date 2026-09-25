"""Strip a closed set of leaked tool-call markup from visible model text.

Some OpenAI-compatible relays render native tool calls as XML-like text before
parsing them back into structured calls.  A partial parse can leave those
markers inside an otherwise useful reply.  This module deliberately recognizes
only the tool-protocol tag names below; it is not a general HTML/XML sanitizer.

T621 adds one more closed shape: a *narrated* tool call.  Instead of emitting a
structured call, some models write the call as prose —
``[Calling generate_image with prompt: "..."]`` — and finish the turn
(``finish=stop``, zero ``tool_calls``).  Nothing runs, and the bracket used to
reach the user verbatim.  The shape is anchored on a bracketed call verb plus a
tool-looking name, so ordinary bracketed prose is untouched.
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

# Narrated tool call: ``[<verb> <tool_name> <payload>]``.  Longer verbs are
# listed before their prefixes so the alternation cannot stop early ("tool
# call" vs "tool").  A name the caller offered this turn is enough on its own.
# Any other name must look like a tool identifier (snake_case
# ``generate_image`` / ``mcp__server__tool``) AND be followed by invocation
# evidence — an argument list ``(``, a ``:``, ``with``, a ``key=`` pair, or the
# closing bracket itself — so ``[Calling all fans]``, ``[call me later]`` and
# ``[Using user_name as the variable name]`` stay out of the closed set.
NARRATED_CALL_VERBS = (
    "calling",
    "call",
    "invoking",
    "invoke",
    "using tool",
    "using",
    "tool call",
    "tool",
    "function call",
    "function",
)
_NARRATED_CALL_HEAD_RE = re.compile(
    r"\[\s*(?:"
    + "|".join(re.escape(verb).replace(r"\ ", r"\s+") for verb in NARRATED_CALL_VERBS)
    + r")(?![A-Za-z0-9_])\s*:?\s*`?(?P<name>[A-Za-z][A-Za-z0-9_.\-]*)`?(?=[\s:(\]])",
    flags=re.IGNORECASE,
)
_NARRATED_CALL_EVIDENCE_RE = re.compile(
    r"\s*(?:\(|:|with(?![A-Za-z0-9_])|[A-Za-z_][A-Za-z0-9_]*\s*=|\])",
    flags=re.IGNORECASE,
)
_QUOTE_OPENER_LEAD = frozenset(" \t\n\r:=(,[{")

# DeepSeek DSML: the model writes its native tool-call block as text —
# ``<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="reply">…`` (T727, T557 L1).
# Anchored on the literal DSML sentinel between bars, so it stays a closed
# family. The generic marker-only fallback must never apply here: the reply
# tool's ``aside`` parameter sits beside its body and would reach the user as
# visible text. Instead a block keeps only the closed ``text`` parameter of a
# ``reply`` invoke (the payload the model meant to send, matching the
# "wrapped reply payload is not lost" rule) and drops everything else; a block
# without one is removed whole and the reply falls to the existing fallback.
_DSML_TOKEN_RE = re.compile(
    r"<\s*(?P<closing>/\s*)?[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<tail>[^>]*)>"
)
_DSML_ATTR_NAME_RE = re.compile(r"""\bname\s*=\s*["']([^"']*)["']""")

# Provider-native end-of-turn sentinels sometimes leak into ``content`` instead
# of being consumed by the relay.  Match only a small, explicit, whole-message
# family: this must never grow into a generic angle-bracket/HTML sanitizer.
# Unbarred tags such as ``<end_of_turn>`` are safe here only because the sole
# consumer uses ``fullmatch`` below, not because generic tags are acceptable.
MODEL_SENTINEL_TOKENS = (
    "</s>",
    "<|endoftext|>",
    "<|im_end|>",
    "<|eot_id|>",
    "<|end_of_text|>",
    "<|end_of_turn|>",
    "<end_of_turn>",
)
_MODEL_SENTINEL_ONLY_RE = re.compile(
    r"(?:"
    + "|".join(re.escape(token) for token in MODEL_SENTINEL_TOKENS)
    + r")+",
    flags=re.IGNORECASE,
)


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


def _is_tool_like_name(name: str, tool_names: frozenset[str]) -> bool:
    lowered = name.lower()
    if lowered in tool_names:
        return True
    return "_" in lowered.strip("_")


def _narrated_call_end(text: str, start: int, *, honor_quotes: bool) -> int | None:
    """Index just past the ``]`` closing the narrated call opened at ``start``.

    Brackets nest (``memory_write(actions=[{...}])``) and a quoted argument may
    carry its own brackets (``prompt: "[夜景]"``, ``prompt: 'a ] b'``); inside a
    quote a backslash escapes the next character, so ``"a \\" ] b"`` does not
    end the string early.  A single quote only opens a string when it sits
    where an argument value starts (after ``:``, ``=``, ``(``, ``,``, ``[``,
    ``{`` or whitespace) so an apostrophe in ``user's cat`` is plain text.
    When the quotes do not balance the caller retries with
    ``honor_quotes=False``.
    """
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif honor_quotes and (
            char == '"'
            or (char == "'" and index > start and text[index - 1] in _QUOTE_OPENER_LEAD)
        ):
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _narrated_calls(
    text: str, tool_names: frozenset[str]
) -> list[tuple[int, int, str]]:
    """``(start, end, name)`` of every narrated call in ``text``.

    One left-to-right pass keeps call state and fence state in source order:
    a fence opened *outside* a call protects everything up to its close (a
    user pasting the shape as an example keeps it verbatim), while a fence
    that appears *inside* a call's payload is part of the call and neither
    protects anything nor toggles the outer fence state (T621 review r2).
    """
    found: list[tuple[int, int, str]] = []
    cursor = 0
    while True:
        match = _NARRATED_CALL_HEAD_RE.search(text, cursor)
        if match is None:
            return found
        fence_start = text.find(_CODE_FENCE, cursor, match.start())
        if fence_start >= 0:
            fence_end = text.find(_CODE_FENCE, fence_start + len(_CODE_FENCE))
            if fence_end < 0:
                return found  # unclosed fence protects the rest of the message
            cursor = fence_end + len(_CODE_FENCE)
            continue
        name = match.group("name")
        if not _is_tool_like_name(name, tool_names):
            cursor = match.end()
            continue
        if (
            name.lower() not in tool_names
            and _NARRATED_CALL_EVIDENCE_RE.match(text, match.end()) is None
        ):
            cursor = match.end()
            continue
        end = _narrated_call_end(text, match.start(), honor_quotes=True)
        if end is None:
            end = _narrated_call_end(text, match.start(), honor_quotes=False)
        if end is None:
            # Cut off mid-call (max_tokens / transport): the head alone is
            # unambiguous and the payload is never user-facing, so the rest of
            # the text goes with it rather than leaking a torn bracket.
            end = len(text)
        found.append((match.start(), end, name))
        cursor = end


def _dsml_reply_payload(text: str, start: int, end: int) -> str:
    """Closed ``text`` parameters that are direct children of a valid ``reply``.

    Parsed with a parent stack. A ``reply`` invoke is valid only at the block's
    top level or directly inside a top-level ``calls``; a ``text`` parameter is captured
    only when its parent is such an invoke, so nothing under ``aside``, a
    non-reply invoke or an unknown container can start a capture. The block
    fails closed (empty payload, whole block removed) on any marker inside a
    captured body or any closing marker that does not match its opener.
    """
    payloads: list[str] = []
    stack: list[tuple[str, bool]] = []  # (element name, is a valid reply invoke)
    capture_from: int | None = None
    for match in _DSML_TOKEN_RE.finditer(text, start, end):
        name = match.group("name").lower()
        closing = bool(match.group("closing"))
        self_closing = not closing and match.group("tail").rstrip().endswith("/")
        attr = _DSML_ATTR_NAME_RE.search(match.group("tail"))
        attr_name = attr.group(1).strip().lower() if attr else ""
        if capture_from is not None:
            if not (closing and name == "parameter"):
                return ""
            payloads.append(text[capture_from:match.start()].strip())
            capture_from = None
            stack.pop()
            continue
        if self_closing:
            continue
        if closing:
            if not stack or stack[-1][0] != name:
                return ""
            stack.pop()
            continue
        parent = stack[-1] if stack else None
        if name == "invoke":
            valid_reply = attr_name == "reply" and (
                not stack or (len(stack) == 1 and stack[0][0] == "calls")
            )
            stack.append((name, valid_reply))
        else:
            stack.append((name, False))
            if (
                name == "parameter"
                and attr_name == "text"
                and parent is not None
                and parent[0] == "invoke"
                and parent[1]
            ):
                capture_from = match.end()
    return "\n\n".join(payload for payload in payloads if payload)


def _dsml_blocks(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, replacement)`` of DSML tool-call blocks, in source order.

    Same fence rule as narrated calls: a fence opened outside a block protects
    its contents, while a fence inside a block's payload is part of the block.
    An unclosed block runs to the end of the text; a stray closing marker is
    removed alone.
    """
    found: list[tuple[int, int, str]] = []
    stack: list[tuple[str, int]] = []
    cursor = 0
    while True:
        match = _DSML_TOKEN_RE.search(text, cursor)
        if not stack:
            fence_start = text.find(
                _CODE_FENCE, cursor, match.start() if match else len(text)
            )
            if fence_start >= 0:
                fence_end = text.find(_CODE_FENCE, fence_start + len(_CODE_FENCE))
                if fence_end < 0:
                    return found
                cursor = fence_end + len(_CODE_FENCE)
                continue
        if match is None:
            break
        name = match.group("name").lower()
        closing = bool(match.group("closing"))
        self_closing = not closing and match.group("tail").rstrip().endswith("/")
        cursor = match.end()
        if self_closing:
            if not stack:
                found.append((match.start(), match.end(), ""))
            continue
        if not closing:
            stack.append((name, match.start()))
            continue
        opening_index = next(
            (i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == name), None
        )
        if opening_index is None:
            if not stack:
                found.append((match.start(), match.end(), ""))
            continue
        outer_start = stack[0][1]
        del stack[opening_index:]
        if not stack:
            found.append(
                (outer_start, match.end(), _dsml_reply_payload(text, outer_start, match.end()))
            )
    if stack:
        found.append((stack[0][1], len(text), _dsml_reply_payload(text, stack[0][1], len(text))))
    return found


def _replace_dsml_blocks(text: str, blocks: list[tuple[int, int, str]]) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in blocks:
        pieces.append(text[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def find_narrated_tool_calls(text: str, *, tool_names=()) -> tuple[str, ...]:
    """Names of narrated tool calls in ``text`` (fenced code excluded), in order.

    Exposed for observability at the call sites; ``strip_tool_markup`` is the
    only remover.  ``tool_names`` widens the closed set to names the caller
    offered this turn (needed for ``task``, the one platform tool without an
    underscore).
    """
    names = frozenset(str(name).lower() for name in tool_names if name)
    return tuple(name for _start, _end, name in _narrated_calls(str(text or ""), names))


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
    visible = str(text or "").strip()
    if _MODEL_SENTINEL_ONLY_RE.fullmatch(visible):
        return True
    for char in visible:
        category = unicodedata.category(char)
        if category[0] in {"L", "N"} or category == "So":
            return False
    return True


def strip_tool_markup(text: str, *, tool_names=()) -> tuple[str, bool]:
    """Return ``(clean_text, removed)`` while preserving fenced code verbatim.

    Clean input is returned byte-for-byte.  If a known tool marker is removed,
    only surrounding outer whitespace is normalized; normal HTML, comparisons,
    emoticons such as ``<3``, and unknown tag names are untouched. One optional
    XML namespace prefix is accepted, but matching and pairing use only the
    allowlisted local name. Triple-backtick fences are protected; inline
    single-backtick spans are intentionally outside this narrow leak boundary.

    Narrated calls (``[Calling generate_image ...]``, see module docstring) are
    removed whole.  ``tool_names`` adds the tool names offered this turn to the
    name anchor; tool-looking names are recognized without it.
    """
    raw = str(text or "")
    names = frozenset(str(name).lower() for name in tool_names if name)
    dsml_blocks = _dsml_blocks(raw)
    raw = _replace_dsml_blocks(raw, dsml_blocks)
    # Narrated calls next, on the raw text: a call is self-delimiting (the
    # bracket *is* the payload) so it is removed whole under both strategies,
    # and its payload may contain a fence that must not become a protected
    # code block (T621 review P1).
    narrated_intervals = [
        (start, end) for start, end, _name in _narrated_calls(raw, names)
    ]
    removed = bool(dsml_blocks or narrated_intervals)
    raw = _remove_intervals(raw, narrated_intervals)
    block_output: list[str] = []
    marker_output: list[str] = []
    cursor = 0

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
