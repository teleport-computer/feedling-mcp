"""Pure-function memory-readside adapters (no Flask/FastAPI/httpx)."""

from __future__ import annotations

import json
import os

from enclave import envelope


MEMORY_READSIDE_MODEL_API_DEFAULT_LIMIT = 500
MEMORY_READSIDE_MODEL_API_MIN_LIMIT = 1


def memory_readside_model_api_limit() -> int:
    """自动注入的候选池大小。

    正整数配置原样传给 backend；backend 的 memory/list 契约负责显式拒绝
    超出其支持范围的值。这里不能再静默钳位，否则运维旋钮只可下调不可上调。
    """
    raw = str(os.environ.get("MEMORY_READSIDE_MODEL_API_LIMIT", "")).strip()
    try:
        value = int(raw) if raw else MEMORY_READSIDE_MODEL_API_DEFAULT_LIMIT
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "MEMORY_READSIDE_MODEL_API_LIMIT must be an integer"
        ) from exc
    if value < MEMORY_READSIDE_MODEL_API_MIN_LIMIT:
        raise ValueError(
            "MEMORY_READSIDE_MODEL_API_LIMIT must be positive"
        )
    return value


def memory_readside_hard_max() -> int:
    raw = os.environ.get("FEEDLING_MEMORY_READSIDE_HARD_MAX", "1000")
    try:
        value = int(str(raw or "1000").strip())
    except (TypeError, ValueError):
        value = 1000
    return max(1, value)


def memory_readside_effective_limit(raw_limit=None) -> int:
    """Mirror backend readside limit semantics inside the enclave.

    The explicit payload limit controls index/fetch candidate windows:
    - unset: full window, capped by HARD_MAX (same as the backend)
    - positive integer: that many candidates, capped by HARD_MAX
    - 0: "full window", still capped by FEEDLING_MEMORY_READSIDE_HARD_MAX

    This is separate from MEMORY_READSIDE_MODEL_API_LIMIT, which controls the
    automatic chat-recall candidate page. Keep both knobs distinct.
    """
    if raw_limit is None or str(raw_limit).strip() == "":
        raw_limit = "0"
    try:
        requested = int(str(raw_limit).strip())
    except (TypeError, ValueError):
        requested = 0
    if requested < 0:
        requested = 0
    hard_max = memory_readside_hard_max()
    if requested == 0:
        return hard_max
    return max(1, min(requested, hard_max))


def memory_readside_text(value, max_chars: int = 2000) -> str:
    return str(value or "").strip()[:max_chars]


def memory_readside_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip()[:160] for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:160]]
    return []


def memory_readside_summary(inner: dict) -> str:
    for key in ("summary", "description", "title"):
        text = memory_readside_text(inner.get(key), 500)
        if text:
            return text
    return ""


def memory_default_bucket(value) -> str:
    mem_type = str(value or "").strip().lower()
    if mem_type in {"moment", "quote"}:
        return "我们的关系"
    if mem_type in {"fact", "event"}:
        return "未分类"
    return "未分类"


def memory_inner_to_v1(inner: dict, envelope: dict | None = None) -> dict:
    """Adapt decrypted legacy memory body into the v1 inner shape."""
    envelope = envelope or {}
    if not isinstance(inner, dict):
        return {"summary": "", "content": "", "bucket": "未分类", "threads": []}
    if all(key in inner for key in ("summary", "content", "bucket", "threads")):
        return {
            "summary": memory_readside_text(inner.get("summary"), 500),
            "content": memory_readside_text(inner.get("content"), 5000),
            "bucket": memory_readside_text(inner.get("bucket"), 80) or "未分类",
            "threads": memory_readside_list(inner.get("threads"))[:8],
            # 通话溯源:agent 拿到它就能调 voice_transcript_read
            # 回看原文。这个 dict 是显式重建的,漏加 = 字段被
            # 静默剥掉,写进去也等于没写。
            **({"voice_call_id": inner["voice_call_id"]} if "voice_call_id" in inner else {}),
        }

    summary = memory_readside_summary(inner)
    description = memory_readside_text(inner.get("description") or inner.get("title") or summary, 2000)
    quote = memory_readside_text(inner.get("her_quote") or inner.get("verbatim") or inner.get("context"), 1000)
    follow_up = memory_readside_text(inner.get("follow_up"), 1000)
    content = "\n".join([
        f"记忆: {description or summary}",
        # User-visible fallback: no "用户"/system labels ("TA" is the app
        # surface's name for the AI). Subject-free reads naturally.
        f"上下文: {quote or '对话中明确提到。'}",
        f"使用提示: {follow_up or '自然使用这条记忆，不要机械复述。'}",
    ])
    threads = memory_readside_list(inner.get("threads"))
    if not threads:
        threads = memory_readside_list(inner.get("linked_dimension"))
    if not threads:
        threads = memory_readside_list(inner.get("anchor_memory_ids"))
    adapted = {
        "summary": summary,
        "content": content,
        "bucket": memory_readside_text(inner.get("bucket"), 80)
        or memory_default_bucket(inner.get("type") or envelope.get("type")),
        "threads": threads[:8],
    }
    return adapted


def memory_readside_status(envelope: dict, inner: dict) -> str:
    return str(envelope.get("status") or inner.get("status") or "active").strip().lower() or "active"


def memory_public_item(item: dict) -> dict:
    """Return a memory item without retired classification metadata."""
    clean = dict(item)
    for key in ("is_sensitive", "sensitivity_class", "sensitive_scope"):
        clean.pop(key, None)
    return clean


def build_memory_index_item(envelope: dict, inner: dict) -> dict:
    adapted = memory_inner_to_v1(inner, envelope)
    return {
        "id": envelope.get("id", ""),
        "summary": adapted.get("summary", ""),
        "bucket": adapted.get("bucket", ""),
        "threads": list(adapted.get("threads") or [])[:8],
        "importance": float(envelope.get("importance") or 0.5),
        "pulse": float(envelope.get("pulse") or 0.3),
        "status": memory_readside_status(envelope, inner),
        "occurred_at": memory_readside_text(envelope.get("occurred_at"), 80),
        "created_at": memory_readside_text(envelope.get("created_at"), 80),
        "updated_at": memory_readside_text(envelope.get("updated_at"), 80),
        "last_referenced_at": memory_readside_text(envelope.get("last_referenced_at"), 80),
        "score": float(envelope.get("score") or 0),
    }


def build_memory_search_item(envelope: dict, inner: dict) -> dict:
    """Build an index-shaped item with enclave-private search text.

    ``content`` must never appear in the memory-index response, but exact search
    still needs to match it while plaintext exists inside the enclave. The route
    strips ``_search_content`` before serialization.
    """
    adapted = memory_inner_to_v1(inner, envelope)
    item = build_memory_index_item(envelope, inner)
    item["_search_content"] = adapted.get("content", "")
    return item


def build_memory_fetch_item(envelope: dict, inner: dict) -> dict:
    adapted = memory_inner_to_v1(inner, envelope)
    return {
        "id": envelope.get("id", ""),
        "summary": adapted.get("summary", ""),
        "content": adapted.get("content", ""),
        "bucket": adapted.get("bucket", ""),
        "threads": list(adapted.get("threads") or [])[:8],
        "importance": float(envelope.get("importance") or 0.5),
        "pulse": float(envelope.get("pulse") or 0.3),
        "status": memory_readside_status(envelope, inner),
        "source": memory_readside_text(envelope.get("source"), 160),
        "occurred_at": memory_readside_text(envelope.get("occurred_at"), 80),
        "created_at": memory_readside_text(envelope.get("created_at"), 80),
        "updated_at": memory_readside_text(envelope.get("updated_at"), 80),
        "last_referenced_at": memory_readside_text(envelope.get("last_referenced_at"), 80),
        # 通话溯源。这个返回体是显式白名单,不加就到不了 agent —— 卡上写了也白写。
        # 只放 fetch 不放 index:index 是选择器用的轻量投影,多一个不参与语义的 id
        # 只是噪音;agent 在 fetch 到全文时看到它就够了。
        "voice_call_id": memory_readside_text(adapted.get("voice_call_id"), 96),
    }


def memory_index_filter_items(items: list[dict], payload: dict) -> list[dict]:
    bucket = memory_readside_text(payload.get("bucket"), 120)
    thread = memory_readside_text(payload.get("thread"), 120)
    query = memory_readside_text(payload.get("query"), 500).casefold().strip()
    filtered = []
    for item in items:
        if bucket and item.get("bucket") != bucket:
            continue
        if thread and thread not in (item.get("threads") or []):
            continue
        if query:
            haystack = "\n".join(
                [
                    str(item.get("content") or ""),
                    str(item.get("summary") or ""),
                    str(item.get("_search_content") or ""),
                    str(item.get("bucket") or ""),
                    str(item.get("source") or ""),
                    *[str(value) for value in (item.get("threads") or [])],
                ]
            ).casefold()
            if query not in haystack:
                continue
        filtered.append(item)
    return filtered


def decrypt_readside_items(
    moments: list,
    authorized_user_id: str,
    content_sk,
    *,
    item_builder,
) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    unavailable_ids: list[str] = []
    for moment in moments:
        if not isinstance(moment, dict):
            continue
        memory_id = str(moment.get("id") or "")
        if moment.get("visibility") == "local_only" or (
            not moment.get("K_enclave")
            and moment.get("body") is None
            and moment.get("body_b64") is None
        ):
            if memory_id:
                unavailable_ids.append(memory_id)
            continue
        try:
            plaintext = envelope.read_envelope(moment, authorized_user_id, content_sk)
            inner = json.loads(plaintext.decode("utf-8"))
            if not isinstance(inner, dict):
                raise ValueError("memory plaintext is not an object")
        except (envelope.DecryptFailure, json.JSONDecodeError, ValueError):
            if memory_id:
                unavailable_ids.append(memory_id)
            continue
        items.append(item_builder(moment, inner))
    return items, unavailable_ids


def moments_to_cards(moments: list, authorized_user_id: str, content_sk) -> list[dict]:
    """把 /v1/memory/list 的 envelope 列表解密成 context_memories 明文卡。
    失败（local_only、解密错）静默丢弃——context_memories 是 best-effort。
    纯同步计算：调用方负责放进 to_thread（backend 拉取已上移到路由层）。"""
    out: list[dict] = []
    for m in moments or []:
        if m.get("visibility") == "local_only":
            continue  # enclave doesn't have K_enclave for these
        try:
            plaintext = envelope.read_envelope(m, authorized_user_id, content_sk)
            inner = json.loads(plaintext.decode("utf-8"))
        except (envelope.DecryptFailure, json.JSONDecodeError):
            continue
        out.append({
            "id": m.get("id"),
            "title": inner.get("title"),
            "description": inner.get("description"),
            # v1 memories keep their real text in summary/content with
            # title/description empty; surface them so consumers (e.g. the
            # Garden「talk in chat」quote expansion) can render actual text.
            "summary": inner.get("summary"),
            "content": inner.get("content"),
            "type": inner.get("type"),
            "source": m.get("source"),
            "occurred_at": m.get("occurred_at"),
            "created_at": m.get("created_at"),
            "her_quote": inner.get("her_quote"),
            "context": inner.get("context"),
            "linked_dimension": inner.get("linked_dimension"),
        })
    return out
