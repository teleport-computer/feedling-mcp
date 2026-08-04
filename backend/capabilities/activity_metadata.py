"""Display-safe metadata derived at the trusted capability boundary."""
from __future__ import annotations

from typing import Any, Mapping

from memory.prompts_v1 import COMMON_BUCKETS_V1


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
