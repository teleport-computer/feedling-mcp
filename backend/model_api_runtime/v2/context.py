"""Pure prompt-assembly helpers for the V2 hosted chat turn.

No I/O, no DB, no LLM calls — just deterministic message-list construction
from a system prompt, an optional conversation summary, a verbatim message
tail, and optional trailing action context. Stdlib only.
"""
from __future__ import annotations

from typing import Any

# Mirrors `_ASSISTANT_ROLES` in `backend/model_api_runtime/v2/coalesce.py`.
# Replicated (not imported) to keep this module dependency-free.
_ASSISTANT_ROLES = frozenset({"openclaw", "assistant", "agent"})

_SUMMARY_HEADER = "对话摘要（早前内容）：\n"


def _norm_role(role: Any) -> str:
    return "assistant" if str(role or "") in _ASSISTANT_ROLES else "user"


def text_of(content: Any) -> str:
    """Extract the human-readable text from a tail row's ``content``.

    ``content`` is either a plain string, or an OpenAI-style content-block list
    (``[{"type":"text","text":...}, {"type":"image_url", ...}]``) once the worker
    has injected images. Mirrors ``provider_client._content_text`` but is
    replicated here to keep this module stdlib-only (dependency direction).
    """
    if isinstance(content, list):
        parts = [
            str(p.get("text") or "").strip()
            for p in content
            if isinstance(p, dict) and str(p.get("text") or "").strip()
        ]
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _has_payload(content: Any) -> bool:
    """True when the row carries anything worth sending: text, or any block at all
    (an image-only turn has no text but IS the user's entire message)."""
    if isinstance(content, list):
        return bool(content)
    return bool(str(content or "").strip())


def build_turn_messages(
    *,
    system_prompt: str,
    summary: str,
    tail: list[dict],
    action_context: str = "",
) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    if summary.strip():
        messages.append({"role": "system", "content": _SUMMARY_HEADER + summary})

    for m in tail:
        content = m.get("content")
        if not _has_payload(content):
            continue
        messages.append({"role": _norm_role(m.get("role")), "content": content})

    if action_context.strip():
        messages.append({"role": "system", "content": action_context})

    return messages


def needs_compaction(tail: list[dict], *, budget: int) -> bool:
    count = sum(1 for m in tail if _has_payload(m.get("content")))
    return count > budget
