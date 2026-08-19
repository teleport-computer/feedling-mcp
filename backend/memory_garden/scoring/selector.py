"""Select memory ids from readside index items.

This module is intentionally pure and small. It reuses the existing hosted
memory relevance scorer, but adapts readside index items instead of decrypted
full memory cards. The selector is the missing middle step:

    index(query) -> select ids -> fetch(ids)
"""

from __future__ import annotations

import re
from typing import Any

from .relevance import memory_relevance_details


SENSITIVE_QUERY_MARKERS = {
    "隐私",
    "私密",
    "敏感",
    "亲密",
    "暧昧",
    "性",
    "xp",
    "kink",
    "intimacy",
    "private",
    "sexual",
    "boundary",
}

SALIENCE_WEIGHT = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

WEAK_TOPIC_CHARS = set("我你他她它的是了在和就都不吗呢啊吧这那什么怎么一个有没给要说想看喜欢记得知道")
CONCRETE_SINGLE_ZH_TERMS = {
    "猫",
    "狗",
}
GENERIC_TOPIC_TOKENS = {
    "ai",
    "api",
    "app",
    "bot",
    "bug",
    "chat",
    "code",
    "data",
    "debug",
    "file",
    "fix",
    "image",
    "ios",
    "issue",
    "json",
    "key",
    "memory",
    "message",
    "model",
    "page",
    "photo",
    "prompt",
    "project",
    "reply",
    "screen",
    "server",
    "system",
    "task",
    "test",
    "token",
    "tool",
    "user",
    "work",
}


def _zh_topic_terms(text: str) -> set[str]:
    """Extract concrete Chinese topic terms from an unsegmented query.

    Single Chinese characters are too noisy for readside selection. A query like
    "猫咪不吃饭我很担心" should match "猫咪", not any card that happens to
    contain "想" or "道".
    """

    terms: set[str] = set()
    for char in re.findall(r"[\u4e00-\u9fff]", text):
        if char in CONCRETE_SINGLE_ZH_TERMS:
            terms.add(char)
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        max_width = min(6, len(chunk))
        for width in range(2, max_width + 1):
            for idx in range(0, len(chunk) - width + 1):
                term = chunk[idx: idx + width]
                if all(ch in WEAK_TOPIC_CHARS for ch in term):
                    continue
                terms.add(term)
    return terms


def _text(value: Any, limit: int = 240) -> str:
    clean = " ".join(str(value or "").split())
    return clean[:limit]


def query_allows_sensitive(query: str) -> bool:
    compact = str(query or "").lower()
    return any(marker in compact for marker in SENSITIVE_QUERY_MARKERS)


def index_item_to_relevance_memory(item: dict) -> dict:
    """Adapt a MemoryIndexItem into the legacy relevance scorer shape."""

    bucket_refs = item.get("bucket_refs") if isinstance(item.get("bucket_refs"), list) else []
    v1_terms: list[str] = []
    if item.get("bucket"):
        v1_terms.append(_text(item.get("bucket"), 80))
    if isinstance(item.get("threads"), list):
        v1_terms.extend(_text(thread, 80) for thread in item.get("threads") or [])
    buckets = " ".join([_text(bucket, 80) for bucket in bucket_refs] + v1_terms)
    summary = _text(item.get("summary"), 500)
    return {
        "id": _text(item.get("id"), 120),
        # 内核形状：summary / content / bucket（2026-08-17 边界整理，
        # 此前这里合成的是 io 的老形状 title/description/linked_dimension）。
        "summary": summary,
        "bucket": buckets,
        # 私有搜索语料放进 content 位 —— 正文里的关键词才搜得到。
        # 索引本身仍是「无原话」的表面：这个 dict 只在 enclave 内用于打分，
        # 用完即弃，不会被序列化。
        "content": _text(item.get("_search_content"), 5000),
        "occurred_at": _text(item.get("occurred_at"), 80),
        "created_at": _text(item.get("created_at"), 80),
        "type": "memory_index_item",
    }


def _salience_score(item: dict) -> float:
    salience = str(item.get("salience") or "medium").strip().lower()
    return float(SALIENCE_WEIGHT.get(salience, 2)) / 10.0


def _metadata_score(item: dict) -> float:
    open_bonus = 0.08 if item.get("is_open_thread") is True else 0.0
    score = float(item.get("score") or 0.0)
    score_bonus = min(0.08, max(0.0, score) / 100.0)
    return round(open_bonus + _salience_score(item) + score_bonus, 4)


def _topic_match(query: str, item: dict) -> bool:
    """Return whether the index item overlaps with the query on a topic term.

    The legacy scorer can treat phrases like "喜欢" as strong because full
    memories often carry richer surrounding text. For index-only selection we
    need one extra guard: do not fetch a card only because it shares a generic
    preference verb. Buckets and summaries must overlap on a more concrete term.
    """

    query_text = str(query or "").lower()
    haystack = " ".join([
        str(item.get("summary") or "").lower(),
        # 私有搜索语料：正文只在 enclave 内参与匹配，序列化前由出口剥掉。
        # 这道关口和下面的词法打分**必须同时**认它 —— 只接一处的话，
        # 卡会分别卡在 no_index_topic_match 或 no_query_overlap 上（codex 2026-08-16）。
        str(item.get("_search_content") or "").lower(),
        " ".join(str(bucket or "").lower() for bucket in (item.get("bucket_refs") or [])),
        str(item.get("bucket") or "").lower(),
        " ".join(str(thread or "").lower() for thread in (item.get("threads") or [])),
    ])
    for token in re.findall(r"[a-z0-9_]{3,}", query_text):
        if token in GENERIC_TOPIC_TOKENS:
            continue
        if token in haystack:
            return True
    for term in _zh_topic_terms(query_text):
        if term in haystack:
            return True
    return False


def select_memory_index_items(
    query: str,
    index_items: list[dict],
    *,
    cap: int = 8,
    include_sensitive: bool | None = None,
    min_score: float = 0.28,
) -> dict:
    """Select relevant index item ids and return an explainable trace.

    The selector is conservative by default: sensitive items are skipped unless
    the query explicitly asks about sensitive/private/intimate topics or the
    caller opts in with include_sensitive=True.
    """

    allow_sensitive = query_allows_sensitive(query) if include_sensitive is None else bool(include_sensitive)
    scored: list[tuple[float, str, dict, dict]] = []
    skipped: list[dict] = []

    for item in index_items:
        if not isinstance(item, dict):
            continue
        memory_id = str(item.get("id") or "")
        if not memory_id:
            continue
        if item.get("is_sensitive") is True and not allow_sensitive:
            skipped.append({
                "id": memory_id,
                "reason": "sensitive_not_allowed_for_query",
                "summary": _text(item.get("summary"), 160),
            })
            continue

        rel_memory = index_item_to_relevance_memory(item)
        relevance = memory_relevance_details(query, rel_memory)
        lexical_score = float(relevance.get("score") or 0.0)
        has_topic_match = _topic_match(query, item)
        topic_bonus = 0.42 if has_topic_match else 0.0
        combined_score = round(lexical_score + topic_bonus + _metadata_score(item), 4)
        confidence = str(relevance.get("confidence") or "none")
        reason = str(relevance.get("reason") or "")

        if lexical_score <= 0:
            skipped.append({
                "id": memory_id,
                "reason": "no_query_overlap",
                "summary": _text(item.get("summary"), 160),
            })
            continue
        if not has_topic_match:
            skipped.append({
                "id": memory_id,
                "reason": f"no_index_topic_match:{reason}",
                "score": combined_score,
                "confidence": confidence,
                "matched_units": list(relevance.get("matched_units") or [])[:8],
                "summary": _text(item.get("summary"), 160),
            })
            continue
        if combined_score < min_score or confidence == "weak":
            if confidence != "weak" or combined_score < max(min_score, 0.52):
                skipped.append({
                    "id": memory_id,
                    "reason": f"below_threshold:{reason}",
                    "score": combined_score,
                    "confidence": confidence,
                    "matched_units": list(relevance.get("matched_units") or [])[:8],
                    "summary": _text(item.get("summary"), 160),
                })
                continue
            relevance = {
                **relevance,
                "confidence": "medium",
                "reason": f"topic_supported_{reason}",
            }

        scored.append((combined_score, memory_id, item, relevance))

    scored.sort(
        key=lambda row: (
            row[0],
            1 if row[2].get("is_open_thread") is True else 0,
            SALIENCE_WEIGHT.get(str(row[2].get("salience") or "medium").lower(), 2),
            row[1],
        ),
        reverse=True,
    )
    selected = []
    selected_ids = []
    for score, memory_id, item, relevance in scored[: max(0, int(cap or 0))]:
        selected_ids.append(memory_id)
        selected.append({
            "id": memory_id,
            "score": score,
            "confidence": str(relevance.get("confidence") or "none"),
            "reason": str(relevance.get("reason") or ""),
            "matched_units": list(relevance.get("matched_units") or [])[:8],
            "matched_phrases": list(relevance.get("matched_phrases") or [])[:6],
            "summary": _text(item.get("summary"), 160),
            "is_sensitive": bool(item.get("is_sensitive")),
        })

    return {
        "selected_ids": selected_ids,
        "trace": {
            "query": str(query or ""),
            "mode": "memory_index_selector_v1",
            "allow_sensitive": allow_sensitive,
            "selected": selected,
            "skipped_sample": skipped[:12],
        },
    }
