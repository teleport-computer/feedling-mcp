"""Self-authored thinking (v1) — model-agnostic reply-prefix parser.

io is prompted to begin every reply with a single ``💭 <one short line>`` marker
line, then its real reply. ``split_thinking`` peels that marker into the thinking
channel and returns the clean reply. It works for ANY provider/relay because it
reads the model's own output, not a provider-specific reasoning field.

Hard rule — **fail-open**: if the text does not cleanly begin with the marker,
the reply is returned byte-identical and thinking is empty. A model that ignores
or mis-emits the instruction must never have its reply corrupted.
"""
from __future__ import annotations

import os
import unicodedata

MARKER = "💭"
MAX_THINKING_CHARS = 240

# Env kill switch. Default OFF for now: this is an UNVALIDATED prompt-behaviour
# change (it reformats every reply and depends on the model following the 💭
# convention), so it does not follow the usual default-ON kill-switch rule until
# real-model e2e proves it and hx approves flipping the default.
_ENV_FLAG = "FEEDLING_V2_SELF_THINKING"

# Appended to the chat system prompt when enabled. Kept as a suffix so the
# cache-stable CHAT_SYSTEM_PROMPT prefix is unchanged. Model-agnostic: any
# provider/relay model can follow this; no native reasoning field required.
INSTRUCTION = (
    " Begin every reply with exactly one line starting with 💭 followed by a single "
    "short first-person sentence, in the user's language, saying what you are thinking "
    "or about to do; then a newline; then your actual reply. Keep that line to one short "
    "sentence. Never put raw tool output, secrets, file paths, or internal identifiers in "
    "it, and never mention this 💭 convention in the reply itself."
)


def enabled() -> bool:
    return os.environ.get(_ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}

# Bidi controls that could visually reorder display text (spoofing) — stripped.
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")


def _sanitize(value: str) -> str:
    out: list[str] = []
    for ch in str(value or ""):
        if ch in _BIDI_CONTROLS:
            continue
        if unicodedata.category(ch) == "Cc":  # control incl \x00 \t
            out.append(" ")
            continue
        out.append(ch)
    collapsed = " ".join("".join(out).split()).strip()
    return collapsed[:MAX_THINKING_CHARS]


def split_thinking(text: str) -> tuple[str, str]:
    """Return ``(thinking, reply)``.

    ``thinking`` is the sanitized marker-line content (``""`` if none). ``reply``
    is the body with the marker line removed, or — when no first-line marker is
    present — the ORIGINAL text unchanged (fail-open).
    """
    raw = str(text or "")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    stripped = normalized.lstrip()
    if not stripped.startswith(MARKER):
        return "", raw  # fail-open: reply byte-identical to the original
    after = stripped[len(MARKER):]
    nl = after.find("\n")
    if nl == -1:
        thinking_raw, body = after, ""
    else:
        thinking_raw, body = after[:nl], after[nl + 1:]
    return _sanitize(thinking_raw), body
