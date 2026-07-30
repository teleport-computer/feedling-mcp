"""Self-authored thinking — runtime-neutral shared kernel.

io is prompted to open every reply with a short first-person thought wrapped in
``<think>…</think>``, then the actual reply. The ``<think>`` marker is used
because BOTH runtimes already know it: the V1 resident consumer extracts it
natively, and V2 calls :func:`split_thinking` here to peel it into the thinking
envelope. Lives in ``core`` so agent_runtime (V1) and model_api_runtime (V2) can
both import it top-down. Pure stdlib.

``split_thinking`` is a small state machine, NOT a regex scrub (Codex review): it
only treats a *leading* ``<think>`` as protocol, and returns an explicit status so
the caller can fail closed. Hard invariants:

  * a raw ``<think`` / ``</think`` fragment of the leading protocol NEVER reaches
    the user-visible reply (same risk class as the protocol-JSON tail leak);
  * private thinking content is NEVER promoted to the reply when the block cannot
    be cleanly resolved;
  * with the feature off (or ABSENT) the reply is byte-identical to today.
"""
from __future__ import annotations

import os
import re
import unicodedata

# Parse outcomes.
ABSENT = "absent"       # no leading <think> → reply is the original text
COMPLETE = "complete"   # clean <tag>…</tag> + non-empty reply
FAILED = "failed"       # unresolvable (truncated/mismatched/nested/no reply)

MAX_THINKING_CHARS = 240

# Shown in the thinking channel when the block is malformed, so the user sees that
# io tried to think rather than nothing/garbage. (zh for now; localization TBD.)
THINKING_FAILED_MARKER = "（思考没写完）"

_TAG_WORDS = ("thinking", "reasoning", "thought", "think")  # longest-first
_TAG_ALT = "|".join(_TAG_WORDS)
# Invisible leading chars (BOM, zero-width) that must not hide a truncated opener.
_INVISIBLE = "﻿​‌‍⁠⁦⁧⁨⁩"
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")
# Any of our protocol tags (open or close) — used only to detect nesting INSIDE a
# resolved thinking block, never to scrub the reply.
_ANY_TAG = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT})\b", re.IGNORECASE)

_ENV_FLAG = "FEEDLING_V2_SELF_THINKING"

INSTRUCTION = (
    " Keep ALL of your thinking/reasoning to ONE short first-person sentence, and "
    "put it in <think>…</think> at the very start of your reply, then the actual "
    "reply. This one rule applies to EVERYTHING you think — do not produce any other, "
    "longer, or separate reasoning anywhere. The sentence MUST be: (1) exactly ONE "
    "short sentence; (2) in the SAME language as the user's latest message from start "
    "to finish — if they wrote Chinese it must be all Chinese, even on tool turns; "
    "never switch to English partway and never add an English status/summary line "
    "(no 'Done...', no 'The ... has been updated'); (3) first-person and casual, like "
    "a quick thought crossing your mind; (4) plain everyday intent ONLY — never "
    "mention tool names, command flags, field names, servers, an 'identity card', or "
    "any internal, technical, or protocol step. "
    "Good: '我来帮你把名字和相处天数改一下'. Bad: 'Let me call identity-write with "
    "--self-introduction'. Never mention this <think> convention in the reply itself."
)


def enabled() -> bool:
    return os.environ.get(_ENV_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


def _sanitize(value: str) -> str:
    out: list[str] = []
    for ch in str(value or ""):
        if ch in _BIDI_CONTROLS or ch in _INVISIBLE:
            continue
        if unicodedata.category(ch) == "Cc":  # control incl \x00 \t \n
            out.append(" ")
            continue
        out.append(ch)
    return " ".join("".join(out).split()).strip()[:MAX_THINKING_CHARS]


def _lstrip_invisible(s: str) -> str:
    i = 0
    while i < len(s) and (s[i].isspace() or s[i] in _INVISIBLE):
        i += 1
    return s[i:]


def split_thinking(text: str) -> tuple[str, str, str]:
    """Return ``(status, thinking, reply)`` — see module docstring for the contract."""
    raw = str(text or "")
    head = _lstrip_invisible(raw)
    if not head.startswith("<"):
        return ABSENT, "", raw  # no leading protocol candidate → reply untouched

    # Parse the leading tag token: '<' ws '/'? ws letters ws '>'?
    m = re.match(r"<\s*(/?)\s*([A-Za-z]*)\s*(>?)", head)
    slash, word, gt = m.group(1), (m.group(2) or "").lower(), m.group(3)

    is_full = word in _TAG_WORDS
    is_prefix = bool(word) and any(w.startswith(word) for w in _TAG_WORDS)

    # Not one of our tags at all (e.g. <div>, <3) → leave as ordinary reply.
    if not is_prefix:
        return ABSENT, "", raw

    # A leading close tag, or a truncated / partial-word opener → cannot be a clean
    # opener. Fail closed; never leak the fragment.
    if slash or not is_full or not gt:
        return FAILED, "", ""

    # Full '<tag>' opener with '>'. Find its matching close.
    rest = head[m.end():]
    close = re.search(rf"<\s*/\s*{word}\s*>", rest, re.IGNORECASE)
    if not close:
        return FAILED, "", ""  # truncated or mismatched close
    inner = rest[: close.start()]
    reply = rest[close.end():].strip()

    # Nesting or an extra protocol tag inside the thinking block → ambiguous.
    if _ANY_TAG.search(inner):
        return FAILED, "", ""
    if not reply:
        return FAILED, "", ""  # clean block but no public reply
    return COMPLETE, _sanitize(inner), reply
