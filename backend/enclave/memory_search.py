"""Request-local full-corpus search for sealed-content accounts.

Decrypted text/postings stay in the enclave. Ranking is ``memory.search_rank``
(memgarden ``retrieval.rank`` + jieba), the same code the backend runs for
plaintext accounts (T779 step 4). No hit returns no items; nothing pads the
result with recent cards.
"""

import memory_search_contract as search_contract
from enclave import readside
from memory import search_rank


def search(moments: list[dict], user_id: str, content_sk, payload: dict) -> dict:
    search_contract.check_request({**payload, "moments": moments})
    protocol = payload.get("search_protocol") or search_contract.VERSION
    if not isinstance(protocol, str) or protocol not in search_contract.SERVED:
        protocol = search_contract.VERSION
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
    ranked = search_rank.rank(items, str(payload.get("query") or "")[:500], protocol=protocol)
    ordered = readside.memory_index_filter_items(ranked, {**payload, "query": ""})
    limit = readside.memory_readside_effective_limit(payload.get("limit"))
    public = []
    for item in ordered[:limit]:
        clean = readside.memory_public_item(item)
        clean.pop("_search_content", None)
        public.append(clean)
    return {"user_id": user_id, "items": public, "unavailable_ids": unavailable,
            "ranking": protocol}
