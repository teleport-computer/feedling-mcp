"""V2 会话摘要折叠（append-and-merge fold）。

当有界逐字 tail 超出预算时，maintenance lane 的 compaction job 把「最旧」的一批消息
折成条目化 bullet 行，追加到既有 SUMMARY 后面——**永不重写**已有摘要（cache-friendly，
避免上下文塌缩）。本模块是纯折叠逻辑：LLM 调用通过 `llm` 参数注入（生产环境将传入
`provider_client.reliable_chat_completion_async`），本模块不导入 hosted/agent_runtime/
任何 provider 实现，也不做加解密——调用方负责传入已解密的 `old_messages` 和用户自己的
`provider_config`（BYOK，无平台兜底 key）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

# Provider output limits are not a storage or trust boundary: a buggy adapter,
# test double, or non-conforming endpoint can return more than ``max_tokens``.
# Keep every incremental append small enough to inspect and prompt safely.  A
# rejected fold is a *full* no-op so the caller cannot advance the summary
# watermark past messages that were not represented by valid new bullets.
_MAX_NEW_BULLETS = 32
_MAX_NEW_BULLET_CHARS = 1_000
_MAX_NEW_BULLETS_CHARS = 8_000

_SYSTEM_PROMPT = (
    "你正在为一个长对话做增量摘要维护。"
    "你会看到「现有摘要」（已经存在，不需要你重复）和「需要归纳的更早对话」（尚未被摘要覆盖的旧消息）。"
    "请仅针对「需要归纳的更早对话」产出全新的条目化 bullet 行，"
    "这些行会被直接追加到现有摘要后面。"
    "不要重写、复述或重复现有摘要中已有的条目；只输出新增的 bullet 行，"
    "每行一条，以 \"- \" 开头，不要输出其他任何文字。"
)


def _bullet_key(text: str) -> str:
    """Canonical comparison key for duplicate detection.

    Case and inconsequential whitespace are ignored so a model cannot make an
    old item look new merely by changing capitalization or spacing.
    """
    return " ".join(text.split()).casefold()


def _validated_new_bullets(reply: Any, *, current_summary: str) -> str | None:
    """Return a normalized, bounded bullet-only append or ``None``.

    Validation is deliberately all-or-nothing.  Salvaging the valid-looking
    subset of a malformed response would let the compaction caller advance its
    watermark even though the rejected portion may be the only representation
    of some old messages.
    """
    if not isinstance(reply, str):
        return None

    candidate = reply.strip()
    if not candidate or len(candidate) > _MAX_NEW_BULLETS_CHARS:
        return None

    lines = candidate.splitlines()
    if not lines or len(lines) > _MAX_NEW_BULLETS:
        return None

    existing_keys: set[str] = set()
    for existing_line in current_summary.splitlines():
        existing = existing_line.strip()
        if existing.startswith("- "):
            existing = existing[2:].strip()
        if existing:
            existing_keys.add(_bullet_key(existing))

    new_keys: set[str] = set()
    normalized: list[str] = []
    for line in lines:
        # No prose, alternate Markdown markers, blank bullets, or continuation
        # lines: every physical line must be one complete item.
        if not line.startswith("- "):
            return None
        body = line[2:].strip()
        if not body or len(body) > _MAX_NEW_BULLET_CHARS:
            return None
        key = _bullet_key(body)
        if not key or key in existing_keys or key in new_keys:
            return None
        new_keys.add(key)
        normalized.append(f"- {body}")

    rendered = "\n".join(normalized)
    if len(rendered) > _MAX_NEW_BULLETS_CHARS:
        return None
    return rendered


def _render_old_messages(old_messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in old_messages:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def compact(
    *,
    provider_config: Any,
    current_summary: str,
    old_messages: list[dict[str, Any]],
    llm: Callable[..., Awaitable[Any]],
    usage_out: Callable[[dict | None], None] | None = None,
) -> str:
    """把 `old_messages` 折叠成新 bullet 行，append 到 `current_summary` 后返回。

    - 空、非 bullet、越界或重复的 LLM 回复 → no-op，原样返回 `current_summary`。
    - `current_summary` 为空 → 直接返回新 bullet 行。
    - 否则 → `current_summary` 后追加换行 + 新 bullet 行。

    这里绝不部分接纳畸形输出：调用方以“返回值是否变化”决定是否推进 watermark，
    因此任何验证失败都必须保留原摘要，确保未被可靠摘要的消息仍留在待折叠区间。
    """
    user_content = (
        "现有摘要（勿重复）：\n"
        f"{current_summary}\n\n"
        "需要归纳的更早对话：\n"
        f"{_render_old_messages(old_messages)}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        result = await llm(
            provider_config, messages, max_tokens=500, temperature=0.3, timeout=60.0)
    except Exception:
        if usage_out is not None:
            usage_out(None)
        raise
    if usage_out is not None:
        usage_out(result.get("usage") if isinstance(result, dict) else None)
    raw_reply = result.get("reply") if isinstance(result, dict) else None
    new_bullets = _validated_new_bullets(raw_reply, current_summary=current_summary)
    if new_bullets is None:
        return current_summary
    if not current_summary.strip():
        return new_bullets
    separator = "" if current_summary.endswith("\n") else "\n"
    return current_summary + separator + new_bullets
