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

_SYSTEM_PROMPT = (
    "你正在为一个长对话做增量摘要维护。"
    "你会看到「现有摘要」（已经存在，不需要你重复）和「需要归纳的更早对话」（尚未被摘要覆盖的旧消息）。"
    "请仅针对「需要归纳的更早对话」产出全新的条目化 bullet 行，"
    "这些行会被直接追加到现有摘要后面。"
    "不要重写、复述或重复现有摘要中已有的条目；只输出新增的 bullet 行，"
    "每行一条，以 \"- \" 开头，不要输出其他任何文字。"
)


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
    llm: Callable[..., Awaitable[dict[str, Any]]],
) -> str:
    """把 `old_messages` 折叠成新 bullet 行，append 到 `current_summary` 后返回。

    - 空 LLM 回复 → no-op，原样返回 `current_summary`。
    - `current_summary` 为空 → 直接返回新 bullet 行。
    - 否则 → `current_summary` 后追加换行 + 新 bullet 行。
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
    result = await llm(provider_config, messages, max_tokens=500, temperature=0.3, timeout=60.0)
    new_bullets = str((result or {}).get("reply") or "").strip()
    if not new_bullets:
        return current_summary
    if not current_summary.strip():
        return new_bullets
    return current_summary.rstrip() + "\n" + new_bullets
