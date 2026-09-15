"""Request-local full-corpus search; decrypted text/postings stay in enclave.

Ranking is ``memgarden.retrieval.rank`` with io's jieba tokenizer: the same
ruler as automatic recall (``enclave/routes/chat.py``). No hit returns no
items; nothing pads the result with recent cards.
"""

from memgarden import retrieval

import memory_search_contract as search_contract
from enclave import readside
from memory import jieba_tokenizer


def search_text(item: dict) -> str:
    # Preserve existing searchable fields, without counting an identical
    # summary/content projection twice. Private content never leaves readside.
    fields = [item.get(key) for key in (
        "summary", "content", "_search_content", "bucket", "source",
    )]
    fields.extend(item.get("threads") or [])
    return "\n".join(dict.fromkeys(str(value) for value in fields if value))


def rank(items: list[dict], query: str, *, protocol: str = search_contract.VERSION) -> list[dict]:
    """Ordered matching items. ``protocol`` PREVIOUS reproduces the old enclave BM25."""
    options = (search_contract.PREVIOUS_RANK_OPTIONS if protocol == search_contract.PREVIOUS
               else search_contract.RANK_OPTIONS)
    try:
        result = retrieval.rank(
            query, items, tokenizer=jieba_tokenizer.TOKENIZER, text_of=search_text,
            max_cards=search_contract.MAX_CARDS, max_text_bytes=search_contract.MAX_TEXT_BYTES,
            **options)
    except retrieval.SearchLimitExceeded as exc:
        raise search_contract.SearchLimitExceeded() from exc
    # Hits carry ids only. ``Hit.matched`` holds query terms (user text) and is
    # deliberately dropped here: it never reaches a response, log or trace.
    by_id: dict[str, dict] = {}
    for item in items:
        by_id.setdefault(str(item.get("id") or ""), item)
    return [by_id[hit_id] for hit_id in dict.fromkeys(result.ids) if hit_id in by_id]


def search(moments: list[dict], user_id: str, content_sk, payload: dict) -> dict:
    search_contract.check_request({**payload, "moments": moments})
    protocol = payload.get("search_protocol") or search_contract.VERSION
    items, unavailable = [], []
    chunk_size = readside.memory_readside_hard_max()
    for offset in range(0, len(moments), chunk_size):
        chunk, failed = readside.decrypt_readside_items(
            moments[offset:offset + chunk_size], user_id, content_sk,
            item_builder=readside.build_memory_search_item)
        items.extend(chunk)
        unavailable.extend(failed)
    # Statistics include every authorized readable card, independent of the
    # output bucket/thread filter and decrypt chunk boundary.
    ranked = rank(items, str(payload.get("query") or "")[:500], protocol=protocol)
    ordered = readside.memory_index_filter_items(ranked, {**payload, "query": ""})
    limit = readside.memory_readside_effective_limit(payload.get("limit"))
    public = []
    for item in ordered[:limit]:
        clean = readside.memory_public_item(item)
        clean.pop("_search_content", None)
        public.append(clean)
    return {"user_id": user_id, "items": public, "unavailable_ids": unavailable,
            "ranking": (search_contract.PREVIOUS if protocol == search_contract.PREVIOUS
                        else search_contract.VERSION)}
