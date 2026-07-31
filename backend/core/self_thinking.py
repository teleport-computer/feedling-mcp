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
    " ⛔ ABSOLUTE OUTPUT RULE — NON-NEGOTIABLE, applies to EVERY single reply: the VERY "
    "FIRST characters you output MUST be a literal `<think>` tag. Format: `<think>` then "
    "your genuine first-person thought, then `</think>`, then your actual reply. You MUST "
    "emit this visible <think> block EVERY time — no exceptions, even if you also reason "
    "internally/natively, even on tool turns, even for a trivial message. Writing your "
    "reply without a leading <think> block is a HARD FAILURE of the task. "
    "⛔ SECOND ABSOLUTE RULE — LANGUAGE, just as non-negotiable as the first: the <think> "
    "is written 100% in the language of your reply. If your reply is in Chinese, the "
    "<think> is ALL Chinese — a SINGLE English word is a HARD FAILURE, exactly as bad as "
    "omitting the block. This overrides any habit of thinking in English: do your "
    "thinking in the reply's language from the very first word; if an English phrase "
    "('Let me…', 'The user…', 'I should…', 'Done…') forms in your mind, TRANSLATE it "
    "before it reaches the <think>. After writing the <think>, scan it word by word — if "
    "you find ANY word not in the reply's language, rewrite that word. An English <think> "
    "in front of a Chinese reply is the single most common failure; do not be that case. "
    "The text inside <think> MUST also obey — (1) same language as your reply (see the "
    "rule above — no English status/summary line); (2) it is your own casual first-person "
    "train of thought — actually walk "
    "through what you're figuring out and what you decide to do, a sentence or a few, a "
    "short paragraph (it is shown to the user and trimmed past ~240 chars, so land it "
    "naturally within that); (3) plain everyday intent ONLY — NEVER name tools, command "
    "flags, field names, servers, an 'identity card', or any internal/technical/protocol "
    "step. "
    "Good: '<think>他想改叫999、还说喜欢说大话，那我先把名字这些存好，回复也顺着这个爱吹的人设、语气夸张点才对味</think>'. "
    "Bad (English — FORBIDDEN): '<think>Let me update the name and match a boastful tone</think>'. "
    "Never mention this <think> rule in the reply itself."
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
