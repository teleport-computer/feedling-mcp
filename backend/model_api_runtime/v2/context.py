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
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        messages.append({"role": _norm_role(m.get("role")), "content": m["content"]})

    if action_context.strip():
        messages.append({"role": "system", "content": action_context})

    return messages


def needs_compaction(tail: list[dict], *, budget: int) -> bool:
    count = sum(1 for m in tail if str(m.get("content") or "").strip())
    return count > budget
