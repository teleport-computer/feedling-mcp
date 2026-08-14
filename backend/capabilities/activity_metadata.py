"""Display-safe metadata derived at the trusted capability boundary."""
from __future__ import annotations

from typing import Any, Mapping

from capabilities import result_budget
from memory_garden.prompts.buckets import COMMON_BUCKETS_V1


_HISTORY_TOOL_NAMES = frozenset({"history_search", "history_fetch"})
_PERCEPTION_TOOL_NAMES = frozenset({
    "perception_snapshot",
    "perception_history",
    "perception_trend",
})
_PERCEPTION_DOMAIN_ERROR_CODES = frozenset({
    "invalid_days",
    "unknown_or_unhistorized_signal",
    "unknown_signals",
})

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

    ``omitted_*`` now also carries rows the *lease* clamped away before the
    enclave ever saw them, so those are charged too. Deliberately conservative
    and bounded: neighbor clamping only kicks in once the turn has fewer than
    ~16 rows left, where over-charging by at most 19 rows simply tips a budget
    that is about to be exhausted anyway. Under-charging is the dangerous
    direction (see the worker's ``_history_rows_charged``).
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


def perception_result_metadata(tool_name: str, result: Mapping[str, Any]) -> dict:
    """Classify a perception result without retaining any perceived value.

    This runs at the trusted capability boundary, before the result is rendered
    into provider text.  The worker may persist only this tiny classification;
    location labels, health values, calendar titles, and error messages never
    cross the observability boundary.
    """
    name = str(tool_name or "")
    if name not in _PERCEPTION_TOOL_NAMES or not isinstance(result, Mapping):
        return {}
    if result.get("ok") is not True:
        error = result.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        message_code = (
            _safe_error_code(error.get("message"))
            if isinstance(error, Mapping)
            else ""
        )
        # Capability error codes are intentionally coarse. Preserve the
        # perception builder's stable domain slug only through an exact
        # allowlist; an arbitrary one-word message must never become telemetry.
        safe_code = (
            message_code
            if message_code in _PERCEPTION_DOMAIN_ERROR_CODES
            else _safe_error_code(code)
        )
        metadata = {"perception_result_kind": "error"}
        if safe_code:
            metadata["perception_error_code"] = safe_code
        return metadata

    data = result.get("data")
    if not isinstance(data, Mapping):
        return {"perception_result_kind": "empty"}
    if name == "perception_snapshot":
        signals = data.get("signals")
        has_value = (
            isinstance(signals, Mapping)
            and any(_signal_doc_has_value(doc) for doc in signals.values())
        )
    elif name == "perception_history":
        rows = data.get("daily")
        has_value = (
            isinstance(rows, list)
            and any(
                isinstance(row, Mapping)
                and _signal_doc_has_value(row.get("doc"))
                for row in rows
            )
        )
    else:
        trend = data.get("trend")
        has_value = isinstance(trend, Mapping) and (
            trend.get("current") is not None
            or bool(trend.get("daily"))
        )
    return {"perception_result_kind": "value" if has_value else "empty"}


def _signal_doc_has_value(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("disabled") is True:
        return False
    for key, item in value.items():
        if key in {"disabled", "reason"} or item is None:
            continue
        if isinstance(item, Mapping):
            if _signal_doc_has_value(item):
                return True
        elif isinstance(item, (list, tuple, set)):
            if any(_collection_item_has_value(child) for child in item):
                return True
        elif item != "":
            # Zero and False are real sensor values, not missing data.
            return True
    return False


def _collection_item_has_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return _signal_doc_has_value(value)
    return value is not None and value != ""


def _safe_error_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > 64:
        return ""
    return text if all(ch.isalnum() or ch in {"_", "-", "."} for ch in text) else ""


def _omitted(data: Mapping[str, Any]) -> int:
    total = 0
    for key in ("omitted_before", "omitted_after"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
    return total
