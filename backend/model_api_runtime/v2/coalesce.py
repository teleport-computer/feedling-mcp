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


def _seq(m: dict[str, Any]) -> int:
    """Return a validated DB sequence identity.

    On the seq-native path a missing/corrupt identity is a correctness error,
    not something to sort as zero and silently skip: doing that could advance
    the durable cursor past a user message that never reached the model.
    """
    raw = m.get("seq")
    if isinstance(raw, bool):
        raise ValueError("message seq must be a positive integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdecimal():
        value = int(raw.strip())
    else:
        raise ValueError("message seq must be a positive integer")
    if value <= 0:
        raise ValueError("message seq must be a positive integer")
    return value


def last_replied_ts(messages: list[dict[str, Any]]) -> float:
    """最近一条模型作者消息的 ts（无则 0.0）。此 ts 之后的用户消息都未回复，须并入下一回合。"""
    latest = 0.0
    for m in messages:
        if str(m.get("role") or "") in _ASSISTANT_ROLES:
            ts = _ts(m)
            if ts > latest:
                latest = ts
    return latest


def last_replied_seq(messages: list[dict[str, Any]]) -> int:
    """Legacy-assistant-derived seq boundary, for migrations/tests only.

    Production turns load their durable boundary from ``cursor.load_seq``;
    this helper exists so any compatibility caller that previously used
    :func:`last_replied_ts` can move to the same total order without inventing
    timestamp tie-breaking.
    """
    latest = 0
    for m in messages:
        if str(m.get("role") or "") in _ASSISTANT_ROLES:
            latest = max(latest, _seq(m))
    return latest


def coalesce_pending(
    messages: list[dict[str, Any]],
    *,
    since_ts: float | None = None,
    since_seq: int | None = None,
    decrypt: Callable[[dict[str, Any]], str] = _plain_content,
) -> tuple[list[dict[str, Any]], float | int]:
    """Coalesce unseen user messages in one deterministic turn.

    New callers pass ``since_seq`` and receive an integer seq cursor; rows are
    ordered and filtered exclusively by the database identity, so equal
    timestamps cannot drop a message.  ``since_ts`` remains as an explicit
    compatibility path until worker integration is complete. Supplying both
    boundaries is rejected rather than choosing one silently.

    Output on the seq path includes each row's ``seq``. Rows with an invalid
    or missing seq fail closed. Both paths dedupe by id and drop empty content.
    """
    if since_seq is not None and since_ts is not None:
        raise ValueError("pass exactly one of since_seq or since_ts")

    seq_native = since_seq is not None
    if seq_native:
        if isinstance(since_seq, bool):
            raise ValueError("since_seq must be a non-negative integer")
        if isinstance(since_seq, int):
            boundary_seq = since_seq
        elif isinstance(since_seq, str) and since_seq.strip().isdecimal():
            boundary_seq = int(since_seq.strip())
        else:
            raise ValueError("since_seq must be a non-negative integer")
        if boundary_seq < 0:
            raise ValueError("since_seq must be a non-negative integer")
        user_rows = [m for m in messages if str(m.get("role") or "") in _USER_ROLES]
        ordered = sorted(user_rows, key=_seq)
    else:
        boundary_ts = float(since_ts or 0.0)
        ordered = sorted(messages, key=_ts)

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    cursor: float | int = 0 if seq_native else 0.0
    for m in ordered:
        if str(m.get("role") or "") not in _USER_ROLES:
            continue
        ts = _ts(m)
        if seq_native:
            seq = _seq(m)
            if seq <= boundary_seq:
                continue
        else:
            if ts <= boundary_ts:
                continue
        mid = str(m.get("id") or "")
        if mid and mid in seen:
            continue
        content = str(decrypt(m) or "").strip()
        if not content:
            continue
        if mid:
            seen.add(mid)
        item = {"id": mid, "ts": ts, "content": content}
        # Image/file routing metadata is part of the current input identity.
        # Dropping it here leaves the prompt tail unable to distinguish the
        # active attachment from historical ones, so the worker omits the
        # current pixels/visual observation and the model answers from history.
        for key in (
            "has_image",
            "image_mime",
            "vision_route_id",
            "has_file",
            "file_name",
            "file_mime",
        ):
            if key in m:
                item[key] = m[key]
        if m.get("include_reasoning") is True:
            item["include_reasoning"] = True
        # Preserve the stable database identity whenever the reader supplied
        # one, including on the compatibility timestamp path.  The timestamp
        # cursor remains a timestamp; callers that are migrating can still
        # inspect/advance the durable seq cursor without a second coalesce.
        if m.get("seq") is not None:
            item["seq"] = _seq(m)
        if seq_native:
            cursor = max(int(cursor), seq)
        else:
            cursor = max(float(cursor), ts)
        out.append(item)
    return out, cursor
