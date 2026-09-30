"""Keyword ranking for memory search, shared by the enclave and the backend.

Ranking is ``memgarden.retrieval.rank`` with io's jieba tokenizer: the same
ruler as automatic recall (``memory.recall_select``). Plaintext accounts are
ranked here in the backend (T779 step 4); sealed-content accounts are still
ranked inside the enclave by ``enclave.memory_search``, which imports this same
code so the two cannot drift. No enclave import belongs in this module.
"""

from memgarden import retrieval

import memory_search_contract as search_contract
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
    """Ordered matching items. An older ``protocol`` reproduces that ranking."""
    options = (search_contract.SERVED.get(protocol, search_contract.RANK_OPTIONS)
               if isinstance(protocol, str) else search_contract.RANK_OPTIONS)
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
