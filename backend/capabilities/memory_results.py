"""Bounded V2 memory views. HTTP/CLI read-side payloads stay unchanged.

Only this model-facing facade projects the index and drops whole low-ranked
cards. The executor and batch allocator must preserve the resulting JSON.
"""
from __future__ import annotations

import json
import re

from capabilities import result_budget
from memory import recall_metadata


# An ASCII dot is a boundary only before whitespace/end/CJK, and not after
# another dot or a digit: keep decimal values, versions and ellipses intact.
# Ambiguous numeric sentence endings conservatively retain more summary text.
_SENTENCE_BOUNDARY = re.compile(
    r"(?<=[。！？])|(?<=[!?])(?=\s|$|[\u3400-\u9fff])"
    r"|(?<=\.)(?<![\d.]\.)(?=\s|$|[\u3400-\u9fff])|\n"
)


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _count(value, fallback: int = 0) -> int:
    return value if type(value) is int and value >= 0 else fallback


def _fit(payload: dict, items: list[dict], tool_name: str) -> dict:
    policy = result_budget.for_tool(tool_name)
    cap = policy.result_cap if policy is not None else 2000
    # Reserve the largest counter representation before adding items. Linear
    # work even for a 1000-card response; no repeated serialization of the pool.
    matched = payload["matched"]
    payload = {**payload, "items": [], "returned": matched,
               "omitted": matched, "truncated": False}
    size = _size(payload)
    selected = []
    for item in items:
        addition = _size(item) + (2 if selected else 0)
        if size + addition > cap:
            break
        selected.append(item)
        size += addition
    payload.update(
        items=selected,
        returned=len(selected),
        omitted=matched - len(selected),
        truncated=bool(payload.get("source_truncated") or len(selected) < matched),
    )
    return payload


def index_payload(body: dict, *, tool_name: str) -> dict:
    source = body.get("items") if isinstance(body.get("items"), list) else []
    items = []
    for item in source:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        summary = _SENTENCE_BOUNDARY.split(str(item.get("summary") or ""), maxsplit=1)[0]
        items.append({
            "id": item["id"],
            "date": str(item.get("occurred_at") or item.get("created_at") or "")[:10],
            "bucket": str(item.get("bucket") or ""),
            "summary": " ".join(summary.split()),
            **({"threads": recall_metadata.links(item.get("threads"))[:3]}
               if recall_metadata.links(item.get("threads")) else {}),
            **({"retrieval_cues": recall_metadata.cues(item.get("retrieval_cues"))}
               if recall_metadata.cues(item.get("retrieval_cues")) else {}),
        })
    return _fit({
        "total": _count(body.get("user_card_count"), len(source)),
        "matched": len(source),
        "source_truncated": bool(body.get("truncated")),
    }, items, tool_name)


def fetch_payload(body: dict) -> dict:
    source = body.get("items") if isinstance(body.get("items"), list) else []
    # Keep the complete read-side card (including its up-to-5000-char content).
    # No generic cap_data: that silently clips each body at 2000 chars.
    items = [item for item in source if isinstance(item, dict)]
    truncation = body.get("truncation") or {}
    payload = _fit({
        "matched": len(source),
        "missing_count": len(body.get("missing_ids") or []),
        "unavailable_count": len(body.get("unavailable_ids") or []),
        "source_omitted": _count(truncation.get("omitted_count")),
        "source_truncated": bool(truncation.get("truncated")),
    }, items, "memory_fetch")
    if "related_items" in body:
        # Never evict a fetched full card to make room for an optional neighbor.
        policy = result_budget.for_tool("memory_fetch")
        cap = policy.result_cap if policy else 2000
        related = body.get("related_items") or []
        addition = {"related_items": [], "related_status": body.get("related_status", "unavailable"),
                    "related_omitted": len(related)}
        if _size({**payload, **addition}) <= cap:
            payload.update(addition)
            returned_ids = {item.get("id") for item in payload["items"]}
            for neighbor in related:
                if not isinstance(neighbor, dict) or neighbor.get("source_id") not in returned_ids:
                    continue
                candidate = {**payload, "related_items": [*payload["related_items"], neighbor],
                             "related_omitted": payload["related_omitted"] - 1}
                if _size(candidate) > cap:
                    break
                payload = candidate
    return payload
