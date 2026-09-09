"""Request-local full-corpus search; decrypted text/postings stay in enclave."""

import memory_bm25
import memory_search_contract as search_contract
from enclave import readside


def search(moments: list[dict], user_id: str, content_sk, payload: dict) -> dict:
    search_contract.check_request({**payload, "moments": moments})
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
    ranked = memory_bm25.rank(items, str(payload.get("query") or "")[:500])
    ordered = readside.memory_index_filter_items(
        [item for item, _score in ranked], {**payload, "query": ""})
    limit = readside.memory_readside_effective_limit(payload.get("limit"))
    public = []
    for item in ordered[:limit]:
        clean = readside.memory_public_item(item)
        clean.pop("_search_content", None)
        public.append(clean)
    return {"user_id": user_id, "items": public,
            "unavailable_ids": unavailable, "ranking": search_contract.VERSION}
