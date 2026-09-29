"""Automatic recall: pick up to eight context cards for one conversation moment.

Moved unchanged out of ``enclave/routes/chat.py`` (T779 step 1) so the same
selection can later run next to the data for plaintext accounts. Nothing here
decrypts, fetches or encodes: callers hand over cards already read for the
authorized user and, for hybrid recall, an ``encoder`` that owns query encoding
(the enclave passes ``enclave.recall_hybrid``). This module must not import
``enclave`` (CONTRIBUTING dependency direction).
"""
from __future__ import annotations

import copy
import os
import time

import memory_search_contract as search_contract
from memgarden import observability as mg_observability
from memgarden import retrieval as mg_retrieval
# Deprecated in memgarden; imported only for the automatic-recall kill switch
# below. Delete together with that switch.
from memgarden.scoring import relevance as memory_relevance
from memory import card_shape
from memory import jieba_tokenizer
from memory import recall_metadata
from memory.embedding import projection as embedding_projection
from memory.embedding import recall_policy

CONTEXT_MEMORY_CAP = 8

#: Kill switch for automatic recall, default ON. ON: memgarden
#: ``retrieval.select_context`` with io's jieba tokenizer, the ranker
#: memory_search uses (recall has a looser gate, see
#: ``memory_search_contract.RECALL_RANK_OPTIONS``). OFF ("0"/"false"/"no"/"off"): the previous
#: ``scoring.relevance`` selector, unchanged. Turn it off when zero-injection
#: turns jump or users report the companion suddenly "forgot" things; active
#: memory_search keeps the new ranker either way. Read per request.
RECALL_RANKER_ENV = "FEEDLING_MEMORY_RECALL_UNIFIED_RANKER"


def unified_recall_enabled() -> bool:
    raw = str(os.environ.get(RECALL_RANKER_ENV, "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def readside_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip()[:160] for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:160]]
    return []


def readside_status(envelope: dict, inner: dict) -> str:
    return str(envelope.get("status") or inner.get("status") or "active").strip().lower() or "active"


def card_from_inner(inner: dict, m: dict) -> dict:
    """One context card from a stored row ``m`` and its already-read body ``inner``."""
    return {
        **recall_metadata.fields(inner, m),
        "id": m.get("id"),
        "bucket": inner.get("bucket"),
        "threads": readside_list(inner.get("threads"))[:8],
        "roles": inner.get("roles") if isinstance(inner.get("roles"), list) else [],
        "status": readside_status(m, inner),
        "archived_at": m.get("archived_at"),
        "is_archived": m.get("is_archived"),
        "archived": m.get("archived"),
        "archive_reason": m.get("archive_reason"),
        "superseded_by": m.get("superseded_by"),
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
    }


def unified_selection(garden_cards: list[dict], query: str,
                      **vector_options) -> tuple[list[dict], dict]:
    """``select_context`` shaped like the legacy selector's result for io consumers.

    Consumers (V1 ``_stash_auto_memories``, V2 ``memory_context.render``) read
    ``selected[].{id, bucket, reason, score, matched_phrases}``. ``matched_phrases``
    are the query tokens the card matched (longest first, the most specific one is
    the one shown). They only travel in the response trace to the caller, which
    already holds the conversation; the persisted ``injection_record`` drops them.
    ``vector_options`` is empty on the lexical path, so that call is unchanged.
    """
    picked, trace = mg_retrieval.select_context(
        query, garden_cards, tokenizer=jieba_tokenizer.TOKENIZER, cap=CONTEXT_MEMORY_CAP,
        **search_contract.RECALL_RANK_OPTIONS, **vector_options)
    matched = {}
    for card in picked:
        units = list((card.get("selection") or {}).get("matched_units") or [])
        matched[str(card.get("id") or "")] = sorted(units, key=lambda u: (-len(u), u))[:6]
    trace = dict(trace)
    trace["selected"] = [{**item, "matched_phrases": matched.get(str(item.get("id") or ""), [])}
                         for item in trace.get("selected") or []]
    return picked, trace


def latest_first_selection(garden_cards, current_query, combined_query, selector):
    """Reserve current-message hits, then fill from the two-message context.

    Both kernel selectors gate *every* bucket before applying quotas: BM25 uses
    the retrieval gate; legacy requires min_relevance and medium/strong
    confidence. Their picked lists are the eligibility signal; do not invent a
    host threshold or admit rejected recent/turning cards to fill spare seats.
    """
    current, current_trace = selector(garden_cards, current_query)
    passes = [("current", current, current_trace, current_query)]
    if combined_query != current_query:
        fallback, fallback_trace = selector(garden_cards, combined_query)
        passes.append(("context", fallback, fallback_trace, combined_query))

    picked, selected, seen = [], [], set()
    # V1 and V2 renderers sort by score. Offset current hits above all fallback
    # scores so a long previous topic cannot evict them at the prompt budget.
    # Keep the kernel score separately: host priority is not lexical evidence.
    priority = (max([0.0, *(float(s.get("score") or 0)
                           for s in passes[-1][2].get("selected", []))]) + 1.0
                if len(passes) > 1 else 0.0)
    for source, candidates, trace, _ in passes:
        reasons = {str(s["id"]): s for s in trace.get("selected", [])}
        for card in candidates:
            mid = str(card.get("id") or "")
            if not mid or mid in seen or len(picked) >= CONTEXT_MEMORY_CAP:
                continue
            seen.add(mid)
            picked.append(card)
            reason = reasons[mid]
            score = float(reason.get("score") or 0)
            selected.append({**reason, "query_source": source, "kernel_score": score,
                             "score": score + (priority if source == "current" else 0)})
    kernel_version = current_trace.get("version") or "legacy-relevance"
    trace = {
        "mode": "latest_first", "version": f"{kernel_version}+host:latest-first-v1",
        "kernel_version": kernel_version, "cap": CONTEXT_MEMORY_CAP,
        "index_count": sum(bool(c.get("id")) for c in garden_cards),
        "selected": selected,
        "passes": [{"source": source, "query_fingerprint": mg_observability.query_fingerprint(query),
                    "trace": pass_trace} for source, _, pass_trace, query in passes],
        # This is a sample from the final query, not all rejected candidates.
        # A current hit rejected by that query is still an injected card.
        "rejected_sample": [s for s in passes[-1][2].get("rejected_sample", [])
                            if str(s.get("id") or "") not in seen],
    }
    return picked, trace


def try_hybrid_selection(hybrid, selectable, garden_cards, inner, current_query, combined_query,
                         *, encoder):
    """Hybrid (dense + BM25) pick for one turn, or (None, None, record) to fall back.

    All-or-nothing: every query vector is encoded before any selection runs, and
    any failure returns no picks so the caller re-runs the unchanged lexical
    path on the untouched cards. The record is content-free. ``encoder`` supplies
    ``encode_queries`` (read at call time); it raises ``recall_policy.Fallback``.
    """
    record = {"status": "fallback", "fallback_reason": hybrid.get("fallback_reason"),
              "encode_ms": None, "encode_queue_ms": None, "encode_compute_ms": None,
              "vectors_ms": hybrid.get("vectors_ms"),
              "vectors_requested": int(hybrid.get("vectors_requested") or 0),
              "vectors_received": len(hybrid.get("stored") or {}),
              "vectors_rejected": int(hybrid.get("vectors_rejected") or 0),
              "with_vector": 0, "hash_mismatch": 0}
    if record["fallback_reason"]:
        return None, None, record
    if not current_query:
        record["fallback_reason"] = "empty_query"
        return None, None, record
    stored = hybrid.get("stored") or {}
    card_vectors = {}
    for card in selectable:
        mid = str(card.get("id") or "")
        hit = stored.get(mid)
        body = inner.get(mid)
        if not mid or hit is None or not isinstance(body, dict):
            continue
        # The backend already served only current-projection rows; re-derive
        # from the caller's own read body so a stale or foreign vector can
        # never stand in for this card.
        if embedding_projection.body_projection(body)[0] != hit[0]:
            record["hash_mismatch"] += 1
            continue
        card_vectors[mid] = hit[1]
    record["with_vector"] = len(card_vectors)
    texts = [current_query] + ([combined_query] if combined_query != current_query else [])
    started = time.monotonic()
    timing: dict = {}
    try:
        vectors = encoder.encode_queries(hybrid["embedder"], texts, hybrid["deadline"], timing)
    except recall_policy.Fallback as exc:
        record["fallback_reason"] = exc.reason
        return None, None, record
    finally:
        # encode_ms is wall time including the wait for the encoder thread;
        # the split is only known when the job finished in time.
        record["encode_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        record.update(timing)
    by_query = dict(zip(texts, vectors))
    model_id = hybrid["model_id"]
    options = {"card_vectors": card_vectors, "min_cosine": recall_policy.min_cosine(),
               "vector_model": model_id,
               "card_vector_models": {mid: model_id for mid in card_vectors}}

    def selector(cards, query):
        return unified_selection(cards, query, query_vector=by_query[query], **options)

    try:
        picked, trace = latest_first_selection(
            copy.deepcopy(garden_cards), current_query, combined_query, selector)
    except Exception:
        # memgarden raises (never degrades) on vector-contract errors; this turn
        # falls back as a whole.
        record["fallback_reason"] = "vector_contract_error"
        return None, None, record
    record["status"] = "active"
    record["lanes"] = [
        {"source": p.get("source"),
         **{k: (p.get("trace", {}).get("hybrid") or {}).get(k)
            for k in ("with_vector", "vector_eligible", "lexical_eligible", "fused", "vector_only")}}
        for p in trace.get("passes") or []]
    return picked, trace, record


def select_context_memories(cards, decrypted, query_args, *, inner=None, encoder=None):
    """Pick context_memories from already-read ``cards`` for the history window ``decrypted``.

    Latest non-empty user message first, then the two most recent user messages
    (assistant turns excluded). ``query_args`` carries the pre-parsed request
    options; ``inner`` ({id: read body}) and ``encoder`` are only used when
    ``query_args["hybrid"]`` is set. Returns (context_memories,
    context_memory_trace, context_memory_log).
    """
    recent_text = [m["content"] for m in decrypted
                   if m.get("role") in {"user", "human"}
                   and isinstance(m.get("content"), str) and m["content"].strip()][-2:]
    current_query = recent_text[-1] if recent_text else ""
    combined_query = "\n".join(reversed(recent_text))

    want_trace = query_args["want_trace"]

    context_memories: list[dict] = []
    context_memory_trace: dict | None = None

    hybrid = query_args.get("hybrid")
    # 生命周期过滤归宿主 —— **必须在翻译之前**。
    # 翻译产物里没有 io 的 archive 字段，放到翻译之后就漏了，已归档的卡
    # 会重新进上下文（codex 2026-08-17 指出）。
    selectable = [c for c in cards if not card_shape.is_retired(c)]
    # 翻成内核认的形状：内核只读 summary/content/bucket + 宿主显式给的
    # search_text，不认 title/her_quote/linked_dimension。
    garden_cards = [card_shape.to_garden_card(c) for c in selectable]

    # 挑卡用翻译后的卡（内核只认那一种形状），但**注入给模型的是原卡** ——
    # 原卡带着 title/her_quote 等 io 侧要渲染和留痕的字段，翻译产物只是
    # 给内核打分用的中间态，不该外流。
    by_original = {str(c.get("id") or ""): c for c in cards if c.get("id")}

    def _back_to_original(picked: list[dict]) -> list[dict]:
        out = []
        for item in picked:
            src = by_original.get(str(item.get("id") or ""))
            out.append(dict(src) if src else item)
        return out

    # 无论调用方要不要实时 trace，都算一条**内容无关**的记录带出去 ——
    # enclave 没有数据库发不了 debug_trace，由调用方（consumer / hosted turn）落库。
    started = time.monotonic()
    selection_trace: dict | None = None

    # Resident 与 Hosted Runtime V2 固定走同一套分桶策略，确保用户切换
    # runtime 时召回不漂移。context_mode/context_strict 仍作为兼容参数接收，
    # 但不再选择不同 policy。
    selector = (unified_selection if unified_recall_enabled()
                else memory_relevance.select_relevant_context_memories_with_trace)
    hybrid_record: dict | None = None
    picked = None
    if hybrid is not None:
        picked, selection_trace, hybrid_record = try_hybrid_selection(
            hybrid, selectable, garden_cards, inner, current_query, combined_query,
            encoder=encoder)
    if picked is None:
        picked, selection_trace = latest_first_selection(
            garden_cards, current_query, combined_query, selector)
    # The persisted label identifies both the kernel and the host merge rule.
    mode = f"relevant:unified:{selection_trace['version']}"
    if hybrid_record is not None:
        mode += ":hybrid" if hybrid_record["status"] == "active" else ":hybrid-fallback"
    context_memories = _back_to_original(picked)
    if query_args.get("context_recent"):
        fresh = recall_metadata.recent_cards(selectable)
        fresh_ids = {c["id"] for c in fresh}
        context_memories = fresh + [c for c in context_memories if c.get("id") not in fresh_ids]
        context_memories = context_memories[:CONTEXT_MEMORY_CAP]
        selection_trace = dict(selection_trace or {})
        selected = selection_trace.get("selected") or []
        # Renderers order by score: fresh cards must stay ahead of relevance
        # picks. BM25 scores are unbounded (the legacy scorer's were <= 1), so
        # the fixed 2.0 becomes "above every relevance score in this trace".
        fresh_score = max([2.0, *(float(item.get("score") or 0) + 1.0 for item in selected
                                  if isinstance(item, dict))])
        # Distinguish recency from relevance; it is not a claim of a query hit.
        selection_trace["selected"] = [
            {"id": c["id"], "bucket": "fresh_recent", "score": fresh_score,
             "reason": "created_within_7_days"} for c in fresh
        ] + [s for s in selected if s.get("id") not in fresh_ids
             and s.get("id") in {c.get("id") for c in context_memories}]
        mode += ":recent7d"
    context_memory_trace = selection_trace if want_trace else None

    context_memory_log = mg_observability.injection_record(
        mode=mode,
        query=combined_query,
        candidate_pool=len(cards),
        selection_trace=selection_trace,
        injected_ids=[str(c.get("id") or "") for c in context_memories],
        cap=CONTEXT_MEMORY_CAP,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )
    if hybrid_record is not None:
        context_memory_log["hybrid"] = hybrid_record
    return context_memories, context_memory_trace, context_memory_log
