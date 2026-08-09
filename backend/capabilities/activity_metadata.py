"""Display-safe metadata derived at the trusted capability boundary."""
from __future__ import annotations

from typing import Any, Mapping

from capabilities import result_budget
from memory.prompts_v1 import COMMON_BUCKETS_V1


_HISTORY_TOOL_NAMES = frozenset({"history_search", "history_fetch"})

_MEMORY_CATEGORY_KEYS = (
    "work",
    "growth",
    "family",
    "friends",
    "pets",
    "relationship",
    "feelings",
    "preferences",
    "values",
    "health",
    "interests",
    "money",
    "food",
    "travel",
)
_MEMORY_BUCKET_TO_KEY = {
    label.casefold(): key
    for pair, key in zip(COMMON_BUCKETS_V1, _MEMORY_CATEGORY_KEYS, strict=True)
    for label in pair
}


def memory_result_metadata(tool_name: str, result: Mapping[str, Any]) -> dict:
    """Project confirmed search/fetch counts without retaining memory content.

    The count comes from the actual capability items array before provider
    result truncation. Category counts are emitted only when every returned item
    has a canonical bucket; one unknown/custom bucket makes the whole
    classification optional field disappear so the UI falls back to total only.
    """
    if str(tool_name) not in {"memory_index", "memory_search", "memory_fetch"}:
        return {}
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        return {}
    data = result.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
        return {}
    items = data["items"]
    metadata: dict[str, Any] = {"memory_count": len(items)}
    if str(tool_name) in {"memory_index", "memory_search"}:
        has_completeness = False
        for source_key, metadata_key in (
            ("total", "memory_total"),
            ("returned", "memory_returned"),
        ):
            value = data.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metadata[metadata_key] = value
                has_completeness = True
        if has_completeness:
            metadata["memory_query_kind"] = str(tool_name)
    if not items:
        return metadata

    counts: dict[str, int] = {}
    ordered_keys: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            return metadata
        bucket = str(item.get("bucket") or "").strip().casefold()
        category = _MEMORY_BUCKET_TO_KEY.get(bucket)
        if not category:
            return metadata
        if category not in counts:
            ordered_keys.append(category)
            counts[category] = 0
        counts[category] += 1
    metadata["memory_categories"] = [
        {"key": key, "count": counts[key]} for key in ordered_keys
    ]
    return metadata


def history_result_metadata(tool_name: str, result: Mapping[str, Any]) -> dict:
    """Content-free raw-row accounting for the chat turn's history budget.

    The worker's ``_HistoryTurnBudget`` accumulates rows across provider
    rounds (spec §5) but only ever sees the executor's ``ToolResult``; this
    projects the trusted capability payload's counters into result metadata
    without retaining any decrypted text. ``history_search`` charges the
    enclave-confirmed ``scanned_count``; ``history_fetch`` charges the
    anchor plus every neighbor row it decrypted.
    """
    if str(tool_name) not in _HISTORY_TOOL_NAMES:
        return {}
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        return {}
    data = result.get("data")
    if not isinstance(data, Mapping):
        return {}
    if str(tool_name) == "history_search":
        scanned = data.get("scanned_count")
        rows = scanned if isinstance(scanned, int) and not isinstance(scanned, bool) else 0
    else:
        before = data.get("before") if isinstance(data.get("before"), list) else []
        after = data.get("after") if isinstance(data.get("after"), list) else []
        # 加回结构化缩减丢掉的邻居：那些行**已经**在 enclave 里解密过了，
        # 只是没进最终 payload。按缩减后的列表记账会让回合预算少算一截，正好
        # 在窗口放宽到 31 条之后失真最大。
        rows = 1 + len(before) + len(after) + _omitted(data)
    return {
        "history_scanned_rows": max(0, rows),
        # 共享预算策略的类型标记（capabilities/result_budget.py）：executor 与
        # tool_loop 据此认出"这是必须整额保住的 JSON 结果"。走可信 metadata
        # 通道，provider 文本永远进不来。
        result_budget.RESULT_KIND_METADATA_KEY: str(tool_name),
    }


def _omitted(data: Mapping[str, Any]) -> int:
    total = 0
    for key in ("omitted_before", "omitted_after"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
    return total
