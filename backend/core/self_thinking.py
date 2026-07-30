"""Self-authored thinking (v1) — runtime-neutral shared kernel.

io is prompted to wrap a short first-person thinking line in ``<think>…</think>``
at the start of its reply. This marker is used because BOTH runtimes already know
it:

  * V1 (resident consumer) already extracts ``<think>`` blocks into its thinking
    channel (``_split_tagged_thinking``) — so V1 needs no change to the fragile
    multi-path reply cleaner, only the gated prompt instruction in the spawner.
  * V2 (hosted worker) has no such cleaner, so it calls :func:`split_thinking`
    here to peel the block into the thinking envelope and reply with the rest.

Lives in ``core`` so agent_runtime (V1) and model_api_runtime (V2) can both import
it top-down without a dependency-direction violation. Pure stdlib.

Hard rule — **fail-open**: text without a ``<think>`` block returns the reply
byte-identical and no thinking; a block that would leave an empty reply is
treated as the reply instead (never emit an empty bubble).
"""
from __future__ import annotations

import os
import re
import unicodedata

MAX_THINKING_CHARS = 240

# Same tag set the V1 consumer's _split_tagged_thinking accepts, so whichever tag
# a model emits is handled identically on both runtimes. Longest alternatives
# first so "thinking" is not shadowed by the "think" prefix.
_TAGS = r"thinking|reasoning|thought|think"
# A leading opener, possibly still truncated (no '>' yet): only the START of the
# reply is treated as ours (the instruction says "at the very start"), so a
# ``<think>`` deep inside legitimate reply text is left untouched.
_OPEN_ANY = re.compile(rf"^\s*<\s*(?:{_TAGS})\b", re.IGNORECASE)
_OPEN_FULL = re.compile(rf"^\s*<\s*({_TAGS})\s*>", re.IGNORECASE)
# Any think tag fragment — used to SCRUB the reply so a stray/torn tag can never
# render to the user (same class of defense as the protocol-JSON tail-leak fix).
_TAG_FRAG = re.compile(rf"</?\s*(?:{_TAGS})\s*>?", re.IGNORECASE)

# Kill switch, DEFAULT ON (hx: this feature ships enabled — tested on test, on in
# main). It is a rollback闸, not a feature gate: set the env var to 0/false/off to
# disable and restore byte-identical prior behaviour on both runtimes.
_ENV_FLAG = "FEEDLING_V2_SELF_THINKING"
_OFF_VALUES = frozenset({"0", "false", "no", "off"})

INSTRUCTION = (
    " Begin every reply with your private thought wrapped in <think>…</think>, then "
    "the actual reply. The <think> content MUST follow ALL of these: (1) exactly ONE "
    "short sentence; (2) in the SAME language as the user's latest message for the "
    "WHOLE thought — if they wrote Chinese it must be Chinese from start to finish, "
    "even on tool turns; never switch to English partway and never append an English "
    "status or summary line (no 'Done...', no 'The ... has been updated'); (3) "
    "first-person and casual, like a quick thought crossing your mind; (4) plain "
    "everyday intent ONLY — never mention tool names, command flags, field names, "
    "servers, an 'identity card', or any internal, technical, or protocol step. "
    "Good: '我来帮你把名字和相处天数改一下'. Bad: 'Let me call identity-write with "
    "--self-introduction'. Never mention this <think> convention in the reply itself."
)

_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")


def enabled() -> bool:
    return os.environ.get(_ENV_FLAG, "1").strip().lower() not in _OFF_VALUES


def _sanitize(value: str) -> str:
    out: list[str] = []
    for ch in str(value or ""):
        if ch in _BIDI_CONTROLS:
            continue
        if unicodedata.category(ch) == "Cc":
            out.append(" ")
            continue
        out.append(ch)
    return " ".join("".join(out).split()).strip()[:MAX_THINKING_CHARS]


def _scrub(text: str) -> str:
    """Remove any think-tag fragment so a torn/stray tag can never reach the user."""
    return _TAG_FRAG.sub("", text)


def split_thinking(text: str) -> tuple[str, str]:
    """Return ``(thinking, reply)``.

    Peels a LEADING ``<think>…</think>`` block into ``thinking`` and returns the
    remaining ``reply``. Only a leading opener is treated as ours (the instruction
    says "at the very start"), so a ``<think>`` deep inside a legitimate reply is
    left alone.

    Robust against malformed / truncated output (same risk class as the
    protocol-JSON tail leak): a raw ``<think>``/``</think>`` fragment must NEVER
    render to the user.

    - no leading opener            → ``("", original)`` byte-identical (fail-open)
    - opener truncated before '>'  → ``("", "")`` (nothing usable; no tag leaks)
    - opener with no closing tag   → all content is the reply, tag stripped
    - complete block               → body is thinking, remainder is reply
    - empty reply after peeling    → the block content becomes the reply (never an
                                     empty bubble), tags scrubbed either way
    """
    raw = str(text or "")
    if not _OPEN_ANY.match(raw):
        return "", raw  # fail-open: no leading think opener → reply untouched
    m = _OPEN_FULL.match(raw)
    if not m:
        # The opener itself is truncated ("<think" with no '>') — drop it entirely
        # so the partial tag never leaks; the turn's degenerate/empty handling
        # produces a proper fallback.
        return "", ""
    tag = m.group(1)
    after = raw[m.end():]
    close = re.search(rf"<\s*/\s*{re.escape(tag)}\s*>", after, re.IGNORECASE)
    if close:
        thinking_raw, reply = after[: close.start()], after[close.end():]
    else:
        # Opener present, closing tag missing (truncated) — treat all content as
        # thinking; there is no separate reply body.
        thinking_raw, reply = after, ""
    reply = re.sub(r"\n{3,}", "\n\n", _scrub(reply)).strip()
    if not reply:
        # No reply body → surface the think content as the reply (never empty,
        # never a leaked tag).
        return "", _scrub(thinking_raw).strip()
    return _sanitize(thinking_raw), reply
