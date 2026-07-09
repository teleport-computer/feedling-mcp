"""V2 tool-call 步骤 status 推送的脱敏 + 合并 + 限频（spec §9 两条红线）。

红线 1（脱敏）：status 只带标签 + 粗计数，绝不带解密原文/记忆/截屏/tool 原始输出。
红线 2（限频/合并）：并行读瞬间冒的多条合并为一条并限频，不刷屏。

纯函数 + 可注入时钟的 RateLimiter；无 DB、无 I/O。executor 用它构造 status 事件的 kind/label/detail，
再由 jobs_store.append_status_event 落盘（该函数的 detail_json 列名与这里的 "detail" 键一一对应）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

# action.type → status kind（§9）。读类塌缩到少数 reading_* kind；写类到其动词；responder 到 writing_reply。
ACTION_STATUS_KIND: dict[str, str] = {
    "identity_get": "reading_memory",
    "memory_index": "reading_memory",
    "memory_fetch": "reading_memory",
    "memory_search": "reading_memory",
    "perception_snapshot": "reading_perception",
    "perception_trend": "reading_perception",
    "perception_history": "reading_perception",
    "screen_recent": "reading_screen",
    "screen_read": "reading_screen",
    "photo_recent": "reading_photo",
    "photo_read": "reading_photo",
    "chat_image_read": "retrieving_chat_image",
    "web_search": "searching_web",
    "web_fetch": "searching_web",
    "memory_write": "capturing_memory",
    "identity_patch": "updating_identity",
    "final_response": "writing_reply",
    "sleep": "sleeping",
}

# 会被合并成一条的 kind（红线 2）：并行读几乎同时冒出，app 不能看到 6 行闪烁。
_MERGEABLE = frozenset({
    "reading_memory", "reading_perception", "reading_screen",
    "reading_photo", "retrieving_chat_image", "searching_web",
})

# 刻意含糊的人类标签（红线 1：标签 + 粗计数，绝无原文）。
_KIND_LABEL: dict[str, str] = {
    "processing": "处理中",
    "reading_memory": "读取上下文",
    "reading_perception": "读取感知",
    "reading_screen": "查看屏幕",
    "reading_photo": "查看照片",
    "retrieving_chat_image": "读取图片",
    "searching_web": "查资料",
    "capturing_memory": "记录记忆",
    "updating_identity": "更新设定",
    "scheduling": "安排提醒",
    "writing_reply": "正在回复",
    "done": "完成",
    "sleeping": "休息",
    "error": "出问题了",
}


def status_kind_for_action(action_type: str) -> str:
    return ACTION_STATUS_KIND.get(action_type, "processing")


def redact_status(kind: str, *, count: int | None = None) -> dict[str, Any]:
    """一条 status 事件的公开 payload——只标签 + 粗计数，绝不带明文/记忆体/截屏/tool 原始输出（红线 1）。"""
    label = _KIND_LABEL.get(kind, "处理中")
    detail: dict[str, Any] = {}
    if count is not None and count > 0:
        detail["count"] = int(count)
        label = f"{label}（{int(count)}）"
    return {"kind": kind, "label": label, "detail": detail}


def merge_parallel_reads(kinds: list[str]) -> list[dict[str, Any]]:
    """把一批并行读 kind 合并成 ≤1 条（红线 2）。非可合并 kind 原样保序穿过；一个
    孤立的（前后不相邻其他可合并 kind 的）可合并 kind 不算「burst」，原样保留自身
    kind——只有 ≥2 个连续可合并 kind 才折叠成统一的 reading_memory 聚合条目。"""
    out: list[dict[str, Any]] = []
    group: list[str] = []
    for k in kinds:
        if k in _MERGEABLE:
            group.append(k)
        else:
            if group:
                out.append(_flush_group(group))
                group = []
            out.append(redact_status(k))
    if group:
        out.append(_flush_group(group))
    return out


def _flush_group(kinds: list[str]) -> dict[str, Any]:
    if len(kinds) == 1:
        return redact_status(kinds[0])
    return _merge_group(kinds)


def _merge_group(kinds: list[str]) -> dict[str, Any]:
    # 合并后用统一「读取上下文」标签；detail.kinds 记录粗粒度子类，绝无原文。
    return {"kind": "reading_memory", "label": "读取上下文", "detail": {"kinds": list(kinds)}}


class RateLimiter:
    """丢弃同一 kind 内快于 min_interval 的 status 冒泡，避免并行/循环 action 刷屏（红线 2）。
    可注入时钟，测试确定性。"""

    def __init__(self, *, min_interval: float = 0.5, now: Callable[[], float] = time.monotonic):
        self._min = float(min_interval)
        self._now = now
        self._last: dict[str, float] = {}

    def allow(self, kind: str) -> bool:
        t = self._now()
        last = self._last.get(kind)
        if last is not None and (t - last) < self._min:
            return False
        self._last[kind] = t
        return True
