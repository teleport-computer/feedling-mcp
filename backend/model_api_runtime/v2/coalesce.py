"""V2 多消息 coalesce（spec §7.1）。

claim 时把该用户自「上次已回复游标」以来所有未回复用户消息并成一轮：single-flight 唯一索引
保证同 user 同 lane 至多一个活跃 job，A/B/C 三条消息只产生一个模型回合（不是三条独立回复）。

纯函数、无 DB、无 LLM。输入是**已解密**的消息 dict（明文由 worker 经 **B 的 `_read_messages`** 在
enclave 内解密取得），故本模块可注入 decrypt 便于测试与复用。
"""
from __future__ import annotations

from typing import Any, Callable

# 视为用户可见（触发回合）的角色。
_USER_ROLES = frozenset({"user", "human"})
# 视为模型作者（已回复）的角色——与既有 chat 约定一致（openclaw/assistant/agent）。
_ASSISTANT_ROLES = frozenset({"openclaw", "assistant", "agent"})


def _plain_content(m: dict[str, Any]) -> str:
    return str(m.get("content") or "")


def _ts(m: dict[str, Any]) -> float:
    try:
        return float(m.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def last_replied_ts(messages: list[dict[str, Any]]) -> float:
    """最近一条模型作者消息的 ts（无则 0.0）。此 ts 之后的用户消息都未回复，须并入下一回合。"""
    latest = 0.0
    for m in messages:
        if str(m.get("role") or "") in _ASSISTANT_ROLES:
            ts = _ts(m)
            if ts > latest:
                latest = ts
    return latest


def coalesce_pending(
    messages: list[dict[str, Any]],
    *,
    since_ts: float,
    decrypt: Callable[[dict[str, Any]], str] = _plain_content,
) -> tuple[list[dict[str, Any]], float]:
    """把 ts > since_ts 的未回复用户消息按时间升序并成一轮。

    返回 (coalesced, cursor)。cursor = 折入的最大用户 ts（0.0 表示无），调用方记录它，
    使后续回合不再重复折入同一批。按 id 去重、丢空内容。
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    cursor = 0.0
    for m in sorted(messages, key=_ts):
        if str(m.get("role") or "") not in _USER_ROLES:
            continue
        ts = _ts(m)
        if ts <= since_ts:
            continue
        mid = str(m.get("id") or "")
        if mid and mid in seen:
            continue
        content = str(decrypt(m) or "").strip()
        if not content:
            continue
        if mid:
            seen.add(mid)
        out.append({"id": mid, "ts": ts, "content": content})
        if ts > cursor:
            cursor = ts
    return out, cursor
