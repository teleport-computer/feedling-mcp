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
# a model emits is handled identically on both runtimes.
_THINK_RE = re.compile(
    r"<\s*(?P<tag>think|thinking|reasoning|thought)\s*>\s*"
    r"(?P<body>.*?)"
    r"\s*<\s*/\s*(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)

_ENV_FLAG = "FEEDLING_V2_SELF_THINKING"

INSTRUCTION = (
    " At the very start of every reply, put one short first-person sentence, in the "
    "user's language, saying what you are thinking or about to do, wrapped in "
    "<think>…</think>; then your actual reply after it. Keep it to one short sentence, "
    "never put raw tool output, secrets, file paths, or internal identifiers inside "
    "<think>, and never mention the <think> convention in the reply itself."
)

_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")


def enabled() -> bool:
    return os.environ.get(_ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


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


def split_thinking(text: str) -> tuple[str, str]:
    """Return ``(thinking, reply)``.

    Peels ``<think>…</think>`` block(s) into ``thinking``; ``reply`` is the
    remaining text. No block → ``("", original)`` unchanged (fail-open). If
    peeling would leave an empty reply (model put everything inside the block),
    the block content becomes the reply and thinking is empty.
    """
    raw = str(text or "")
    blocks: list[str] = []

    def _collect(m: "re.Match") -> str:
        body = (m.group("body") or "").strip()
        if body:
            blocks.append(body)
        return ""

    stripped = _THINK_RE.sub(_collect, raw)
    if not blocks:
        return "", raw  # fail-open: reply byte-identical to the original
    reply = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    joined = " ".join(blocks)
    if not reply:
        # Everything was inside the block — don't emit an empty reply.
        return "", joined.strip()
    return _sanitize(joined), reply
