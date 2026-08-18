"""
context_memories selection — pure helpers, no native deps.
============================================================

Lives outside enclave_app.py so it can be unit-tested without the full
nacl / cryptography stack. enclave_app.py imports from here.

Default resident/MCP selection keeps the historical behavior:
  · Up to 3 turning-point cards (title prefix `转折｜`), newest first
  · Up to 2 most-recently-created cards
  · Up to 3 cards relevant to the latest user message
  · Dedupe by id, cap total at 8

Hosted model_api selection is stricter. It treats memories as candidates,
not truth injected by the platform: entity/phrase matches can be selected,
generic English tokens and single Chinese characters can only support other
signals. This avoids false positives like ordinary "project" matching the
specific entity "TOHO Project".
"""

from __future__ import annotations

import re


from .. import timestamps as memory_timestamps


#: 内核认的卡片文本字段 —— **固定的，不适应宿主**。
#: 宿主负责把自己的形状翻译成这一种（io 侧见 memory/card_shape.py::to_garden_card）。
#: 2026-08-17 边界整理：此前这里兼容 io 的两种历史形状，那是把宿主的债背进了内核。
_MEMORY_TEXT_FIELDS = ("summary", "content", "bucket")

#: 内核认的卡片角色。宿主负责把自己的识别方式（标题前缀、来源名、显式字段…）
#: 翻译成这两个值 —— 内核不认任何宿主的命名约定。
ROLE_TURNING_POINT = "turning_point"
ROLE_CORRECTION = "correction"


def char_bigrams(s: str) -> set:
    """Lowercase character bigrams. Empty/single-char input → empty set."""
    if not s:
        return set()
    s = s.lower()
    if len(s) < 2:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def bigram_jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


_EN_STOPWORDS = {
    "the", "and", "you", "your", "for", "with", "that", "this", "what",
    "when", "where", "why", "how", "can", "could", "would", "should",
    "about", "again", "then", "than", "into", "from", "have", "has",
    "had", "will", "just", "really", "very", "also", "there", "here",
}
_EN_GENERIC_TERMS = {
    # Product / engineering words are common in IO conversations. They are
    # useful as weak support, but must not by themselves summon persona cards.
    "ai", "api", "app", "bot", "bug", "card", "chat", "code", "data",
    "debug", "file", "fix", "image", "ios", "issue", "json", "key",
    "memory", "message", "model", "page", "photo", "prompt", "project",
    "reply", "screen", "server", "system", "task", "test", "token",
    "tool", "user", "work",
    # General daily words.
    "day", "today", "tomorrow", "tonight", "week", "month", "thing",
    "stuff", "help", "done", "finish", "finished", "tired",
}
_ZH_STOP_CHARS = set("我你他她它的是了在和就都不吗呢啊吧这那什么怎么一个有没给要说")
_ZH_GENERIC_PHRASES = {
    "今天", "明天", "昨天", "现在", "刚刚", "这个", "那个", "一下",
    "东西", "事情", "项目", "任务", "工作", "问题", "代码", "系统",
    "消息", "聊天", "记忆", "模型", "图片", "截图", "文件", "页面",
    "完成", "测试", "修复", "好累", "很累",
}


def _text_for_memory(memory: dict) -> str:
    """匹配用的 haystack。

    **优先用宿主显式给的 `search_text`** —— 内核不从展示字段猜搜索语料。
    宿主最清楚自己哪些字段承载可搜索的文本（io 的老卡把它散在 title /
    her_quote / linked_dimension 里，光看 summary 会漏掉一大半）。

    没给 search_text 时退回 summary + content：让只有规范字段的简单宿主
    也能直接用，不必先理解这套机制。
    """
    explicit = str(memory.get("search_text") or "").strip()
    if explicit:
        return str(memory.get("search_text"))
    return " ".join(str(memory.get(key) or "") for key in _MEMORY_TEXT_FIELDS)


def _norm_compact(text: str) -> str:
    return re.sub(r"[\s_\-·・/|｜:：,，.。!！?？()（）\[\]【】<>《》\"'“”‘’]+", "", (text or "").lower())


def _en_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9_]{2,}", (text or "").lower())
        if token not in _EN_STOPWORDS
    ]


def _en_rare_tokens(text: str) -> set[str]:
    return {token for token in _en_tokens(text) if token not in _EN_GENERIC_TERMS and len(token) >= 3}


def _en_weak_tokens(text: str) -> set[str]:
    return {token for token in _en_tokens(text) if token in _EN_GENERIC_TERMS}


def _en_phrases(text: str) -> set[str]:
    phrases: set[str] = set()
    for match in re.finditer(r"[a-z0-9_]+(?:[\s_\-]+[a-z0-9_]+){1,4}", (text or "").lower()):
        tokens = [
            token
            for token in re.findall(r"[a-z0-9_]{2,}", match.group(0))
            if token not in _EN_STOPWORDS
        ]
        if len(tokens) < 2:
            continue
        # Keep phrases with at least one non-generic token. "toho project"
        # stays, plain "api project" does not become an entity phrase.
        if any(token not in _EN_GENERIC_TERMS for token in tokens):
            phrases.add(" ".join(tokens))
    return phrases


def _mixed_phrases(text: str) -> set[str]:
    phrases: set[str] = set()
    for raw in re.findall(r"(?:[\u4e00-\u9fff]+[a-zA-Z0-9_]+|[a-zA-Z0-9_]+[\u4e00-\u9fff]+)[\u4e00-\u9fffA-Za-z0-9_]*", text or ""):
        compact = _norm_compact(raw)
        if len(compact) >= 4:
            phrases.add(compact)
    return phrases


def _cjk_chunks(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]{2,}", text or "")


def _cjk_ngrams(text: str, *, min_len: int = 2, max_len: int = 4) -> set[str]:
    grams: set[str] = set()
    for chunk in _cjk_chunks(text):
        for size in range(min_len, min(max_len, len(chunk)) + 1):
            for idx in range(0, len(chunk) - size + 1):
                gram = chunk[idx:idx + size]
                stop_count = sum(1 for ch in gram if ch in _ZH_STOP_CHARS)
                # Avoid promoting connective phrases like "有什么" or "天有"
                # into strong evidence. They can still count as weak support.
                if gram and gram not in _ZH_GENERIC_PHRASES and stop_count == 0:
                    grams.add(gram)
    return grams


def _weak_cjk_terms(text: str) -> set[str]:
    raw = text or ""
    weak = {phrase for phrase in _ZH_GENERIC_PHRASES if phrase in raw}
    for ch in re.findall(r"[\u4e00-\u9fff]", raw):
        if ch not in _ZH_STOP_CHARS:
            weak.add(ch)
    return weak


def _weak_support_terms(terms: set[str]) -> set[str]:
    return {
        term for term in terms
        if re.search(r"[a-z0-9_]", term) or len(term) >= 2
    }


def _strong_phrases(text: str) -> set[str]:
    phrases = set()
    phrases.update(_en_phrases(text))
    phrases.update(_mixed_phrases(text))
    phrases.update(_cjk_ngrams(text, min_len=2, max_len=4))
    return phrases


def _memory_entities(memory: dict) -> set[str]:
    hay = _text_for_memory(memory)
    entities = _strong_phrases(hay)

    # Parenthesized aliases are common in imported persona/memory materials:
    # "TOHO Project（东方Project）". Preserve both sides as entity candidates.
    for inside in re.findall(r"[（(]([^（）()]{2,80})[）)]", hay):
        entities.update(_strong_phrases(inside))
        compact = _norm_compact(inside)
        if len(compact) >= 4:
            entities.add(compact)
    return entities


def _meaning_units(text: str) -> set[str]:
    """Small bilingual lexical units for relevance without heavy deps."""

    units = set(_en_rare_tokens(text))
    units.update(_en_weak_tokens(text))
    units.update(_strong_phrases(text))
    units.update(_weak_cjk_terms(text))
    return units


def _memory_relevance(query: str, memory: dict) -> dict:
    hay = _text_for_memory(memory)
    query = query or ""
    bigram_score = bigram_jaccard(char_bigrams(query), char_bigrams(hay))

    q_phrases = _strong_phrases(query)
    h_phrases = _strong_phrases(hay)
    q_rare = _en_rare_tokens(query)
    h_rare = _en_rare_tokens(hay)
    q_weak = _en_weak_tokens(query) | _weak_cjk_terms(query)
    h_weak = _en_weak_tokens(hay) | _weak_cjk_terms(hay)

    q_compact = _norm_compact(query)
    entities = _memory_entities(memory)
    entity_matches = {
        ent for ent in entities
        if len(ent) >= 4 and (ent in q_compact or ent.replace(" ", "") in q_compact)
    }
    phrase_matches = set(q_phrases & h_phrases)
    rare_matches = set(q_rare & h_rare)
    weak_matches = set(q_weak & h_weak)

    # If a query strong phrase is contained in a memory entity (or vice versa),
    # treat it as phrase-level evidence. This covers "东方 Project" vs
    # "东方Project" without allowing single "project" to pass.
    for phrase in q_phrases:
        compact = _norm_compact(phrase)
        if len(compact) < 3:
            continue
        for entity in entities:
            entity_compact = _norm_compact(entity)
            if compact and entity_compact and (compact in entity_compact or entity_compact in compact):
                phrase_matches.add(phrase)

    score = 0.0
    confidence = "none"
    reason = "no_overlap"
    if entity_matches:
        score = 0.86 + min(0.08, 0.02 * len(entity_matches))
        confidence = "strong"
        reason = "entity_phrase_match"
    elif phrase_matches:
        score = 0.68 + min(0.12, 0.03 * len(phrase_matches))
        confidence = "strong"
        reason = "phrase_match"
    elif len(rare_matches) >= 2:
        score = 0.52 + min(0.12, 0.03 * len(rare_matches))
        confidence = "medium"
        reason = "multiple_rare_terms"
    elif len(rare_matches) == 1 and weak_matches:
        score = 0.36
        confidence = "medium"
        reason = "rare_term_with_support"
    elif len(rare_matches) == 1:
        score = 0.28
        confidence = "weak"
        reason = "single_rare_term"
    elif len(_weak_support_terms(weak_matches)) >= 3 or (
        len(_weak_support_terms(weak_matches)) >= 2 and bigram_score >= 0.12
    ):
        support_count = len(_weak_support_terms(weak_matches))
        score = min(0.42, 0.24 + 0.04 * support_count + min(0.06, bigram_score))
        confidence = "medium"
        reason = "multiple_weak_terms"
    elif weak_matches:
        score = min(0.18, 0.06 * len(weak_matches) + min(0.04, bigram_score))
        confidence = "weak"
        reason = "weak_generic_overlap"
    elif bigram_score >= 0.18:
        score = min(0.16, bigram_score)
        confidence = "weak"
        reason = "weak_bigram_overlap"

    matched_units = sorted((entity_matches | phrase_matches | rare_matches | weak_matches), key=lambda x: (-len(x), x))[:12]
    return {
        "score": round(float(score), 4),
        "confidence": confidence,
        "matched_units": matched_units,
        "matched_phrases": sorted((entity_matches | phrase_matches), key=lambda x: (-len(x), x))[:8],
        "reason": reason,
        "bigram_score": round(float(bigram_score), 4),
    }


def memory_relevance_details(query: str, memory: dict) -> dict:
    """Public explainable relevance wrapper used by hosted API internals."""

    return dict(_memory_relevance(query, memory))


def _is_correction_memory(memory: dict) -> bool:
    """是不是纠正卡 —— 只认宿主打好的 `roles` 标记。

    2026-08-17 边界整理：此前靠 source 名与标题里的「纠正/correction」字样判断，
    那是把宿主的命名约定写进了内核，而且对没有 title 的新形状卡完全失效。
    识别的脆弱部分已收到 io 侧（memory/card_shape.py::roles_of）。
    """
    return ROLE_CORRECTION in (memory.get("roles") or [])


def _legacy_is_correction_memory(memory: dict) -> bool:
    source = str(memory.get("source") or "").lower()
    if source in {"model_api_correction", "user_correction", "settings_correction"}:
        return True
    title = str(memory.get("title") or "").lower()
    return any(marker in title for marker in ("correction", "纠正", "设定更新", "边界更新"))


def _selection_for_trace(memory: dict, relevance: dict, *, selected: bool, bucket: str = "") -> dict:
    return {
        "id": str(memory.get("id") or ""),
        "title": str(memory.get("title") or "")[:160],
        "type": str(memory.get("type") or "")[:40],
        "score": float(relevance.get("score") or 0.0),
        "confidence": str(relevance.get("confidence") or "none"),
        "matched_units": list(relevance.get("matched_units") or [])[:8],
        "matched_phrases": list(relevance.get("matched_phrases") or [])[:6],
        "reason": str(relevance.get("reason") or "")[:120],
        "bucket": bucket,
        "selected": bool(selected),
    }


def _annotate(memory: dict, relevance: dict, *, bucket: str) -> dict:
    out = dict(memory)
    out["selection"] = {
        "score": float(relevance.get("score") or 0.0),
        "confidence": str(relevance.get("confidence") or "none"),
        "matched_units": list(relevance.get("matched_units") or [])[:8],
        "matched_phrases": list(relevance.get("matched_phrases") or [])[:6],
        "reason": str(relevance.get("reason") or "")[:120],
        "bucket": bucket,
    }
    return out


def _query_trace(query: str) -> dict:
    return {
        "query_units": sorted(_meaning_units(query), key=lambda x: (-len(x), x))[:40],
        "query_strong_phrases": sorted(_strong_phrases(query), key=lambda x: (-len(x), x))[:30],
        "query_rare_terms": sorted(_en_rare_tokens(query))[:30],
        "query_weak_terms": sorted((_en_weak_tokens(query) | _weak_cjk_terms(query)), key=lambda x: (-len(x), x))[:30],
    }


def _is_global_correction(memory: dict) -> bool:
    """这条纠正是不是「全局边界」类（改称呼/改人设/立规矩），要享受加权。

    2026-08-17 边界整理：改读规范字段与宿主给的 search_text ——
    此前读 title/description/context，那是 io 的字段名，翻译后就没了。

    ⚠️ 关键词表本身仍是中文为主，属于**宿主的语言约定**。提示词英文化那批
    会把它一起挪走（届时这个判断也该由宿主在 roles 里表达，内核只认标签）。
    """
    text = " ".join(
        str(memory.get(k) or "") for k in ("summary", "content", "search_text")
    ).lower()
    return any(
        marker in text
        for marker in (
            "不要", "别再", "不准", "不许", "do not", "don't",
            "never", "称呼", "名字", "name", "persona", "设定",
            "人设", "口吻", "语气", "boundary", "边界",
        )
    )


def select_context_memories_with_trace(
    moments: list[dict],
    latest_user_text: str,
    cap: int = 8,
    mode: str = "default",
) -> tuple[list[dict], dict]:
    """Pick memory cards and return a privacy-light selection trace."""
    if not moments:
        return [], {**_query_trace(latest_user_text), "selected": [], "rejected_sample": []}

    # 2026-08-17 边界整理：生命周期过滤（归档/被取代）已归宿主，在翻译前完成。
    # 内核不再认识 io 的 is_archived / archived_at / archive_reason 三个字段 ——
    # 那是宿主的命名约定，换个宿主就不成立。

    strict = str(mode or "").strip().lower() in {"model_api", "strict"}
    chosen_ids: set = set()
    out: list[dict] = []
    selected_trace: list[dict] = []
    rejected: list[tuple[float, str, dict, dict]] = []

    def choose(memory: dict, relevance: dict, *, bucket: str) -> None:
        mid = memory.get("id")
        if mid in chosen_ids or len(out) >= cap:
            return
        chosen_ids.add(mid)
        out.append(_annotate(memory, relevance, bucket=bucket))
        selected_trace.append(_selection_for_trace(memory, relevance, selected=True, bucket=bucket))

    if strict:
        scored = [(m, _memory_relevance(latest_user_text, m)) for m in moments]

        corrections = []
        for m, rel in scored:
            if not _is_correction_memory(m):
                continue
            global_boundary = _is_global_correction(m)
            if global_boundary:
                rel = {
                    **rel,
                    "score": max(float(rel.get("score") or 0.0), 0.72),
                    "confidence": "strong",
                    "reason": "global_correction",
                }
            if global_boundary or rel.get("confidence") in {"strong", "medium"}:
                corrections.append((
                    float(rel.get("score") or 0.0),
                    memory_timestamps.sort_key(m.get("created_at")),
                    m,
                    rel,
                ))
        corrections.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for _, __, m, rel in corrections[:2]:
            choose(m, rel, bucket="correction")

        query_relevant = []
        for m, rel in scored:
            if m.get("id") in chosen_ids:
                continue
            conf = str(rel.get("confidence") or "none")
            reason = str(rel.get("reason") or "")
            score = float(rel.get("score") or 0.0)
            if conf == "strong" and score >= 0.55:
                query_relevant.append((
                    score,
                    memory_timestamps.sort_key(m.get("occurred_at")),
                    m,
                    rel,
                ))
            elif conf == "medium" and score >= 0.35 and reason != "weak_generic_overlap":
                query_relevant.append((
                    score,
                    memory_timestamps.sort_key(m.get("occurred_at")),
                    m,
                    rel,
                ))
            elif score > 0:
                rejected.append((score, reason, m, rel))
        query_relevant.sort(key=lambda x: (x[0], x[1]), reverse=True)
        for _, __, m, rel in query_relevant[: min(3, max(0, cap - len(out)))]:
            choose(m, rel, bucket="query")

        chosen_set = {item["id"] for item in selected_trace}
        for m, rel in scored:
            if m.get("id") in chosen_set or float(rel.get("score") or 0.0) <= 0:
                continue
            if not any(existing[2].get("id") == m.get("id") for existing in rejected):
                rejected.append((float(rel.get("score") or 0.0), str(rel.get("reason") or ""), m, rel))
        rejected.sort(key=lambda x: x[0], reverse=True)
        # Soft-recall index (P3 / D3 "LLM 软召回"): a compact list of MORE cards
        # than the lexical filter selected, by title + date only. The hosted
        # model can naturally "recall" one of these if it is relevant to the
        # current message even though it didn't lexically match — the model
        # decides, instead of the keyword filter hard-dropping it. Built from the
        # already-decrypted `moments`, so it costs no extra provider/enclave call.
        # Turning points first, then most recent. Existing `selected` cards are
        # excluded (they're already passed in full).
        index_ids = {item.get("id") for item in selected_trace}
        index_pool = sorted(
            [m for m in moments if m.get("id") not in index_ids],
            key=lambda m: (
                ROLE_TURNING_POINT in (m.get("roles") or []),
                memory_timestamps.sort_key(m.get("occurred_at")),
                memory_timestamps.sort_key(m.get("created_at")),
            ),
            reverse=True,
        )[:20]
        index_sample = [
            {
                "id": m.get("id"),
                "type": str(m.get("type") or ""),
                "title": str(m.get("title") or "")[:120],
                "occurred_at": str(m.get("occurred_at") or "")[:10],
            }
            for m in index_pool
        ]
        trace = {
            **_query_trace(latest_user_text),
            "mode": "model_api",
            "selected": selected_trace,
            "index_sample": index_sample,
            "rejected_sample": [
                _selection_for_trace(m, rel, selected=False, bucket="rejected")
                for _, __, m, rel in rejected[:8]
            ],
        }
        return out[:cap], trace

    # Bucket 1 — turning points by occurred_at desc, max 3
    turning = sorted(
        [m for m in moments if ROLE_TURNING_POINT in (m.get("roles") or [])],
        key=lambda m: memory_timestamps.sort_key(m.get("occurred_at")),
        reverse=True,
    )[:3]
    for m in turning:
        choose(m, _memory_relevance(latest_user_text, m), bucket="turning")

    # Bucket 2 — most-recently-created (skip already-chosen), max 2
    recent_pool = [m for m in moments if m.get("id") not in chosen_ids]
    recent = sorted(
        recent_pool,
        key=lambda m: memory_timestamps.sort_key(m.get("created_at")),
        reverse=True,
    )[:2]
    for m in recent:
        choose(m, _memory_relevance(latest_user_text, m), bucket="recent")

    # Bucket 3 — relevance to latest user message, max 3
    if latest_user_text:
        scored = []
        for m in moments:
            if m.get("id") in chosen_ids:
                continue
            rel = _memory_relevance(latest_user_text, m)
            score = float(rel.get("score") or 0.0)
            if score > 0:
                scored.append((score, m, rel))
        scored.sort(key=lambda x: -x[0])
        for _, m, rel in scored[:3]:
            choose(m, rel, bucket="query")

    trace = {
        **_query_trace(latest_user_text),
        "mode": "default",
        "selected": selected_trace,
        "rejected_sample": [],
    }
    return out[:cap], trace


def select_context_memories(
    moments: list[dict],
    latest_user_text: str,
    cap: int = 8,
    mode: str = "default",
) -> list[dict]:
    """Pick up to `cap` memory cards.

    Default mode keeps the resident/MCP behavior: turning points + recent +
    relevance. ``model_api`` / ``strict`` mode avoids context pollution for
    hosted chat by returning only explicit corrections plus query-relevant cards.

    `moments` are pre-decrypted dicts with at least these keys:
      id, title, description, occurred_at, created_at
    """
    selected, _trace = select_context_memories_with_trace(
        moments,
        latest_user_text,
        cap=cap,
        mode=mode,
    )
    # Preserve the historical public shape unless the caller explicitly asks
    # for trace via select_context_memories_with_trace.
    return [
        {k: v for k, v in memory.items() if k != "selection"}
        for memory in selected
    ]
