"""IO-owned projection of already normalized, authorized Garden cards.

Do not call the BM25 search-text helper: changing that external implementation
must not silently reuse an old embedding. Bump this version for semantic edits.
"""
import hashlib
from collections.abc import Mapping

PROJECTION_VERSION = "io-memory-v1"


def card_projection_text(card: Mapping) -> str:
    fields = [card.get(key) for key in ("summary", "content", "bucket")]
    for key in ("threads", "retrieval_cues"):
        value = card.get(key)
        if isinstance(value, (list, tuple)):
            fields.extend(value)
    # No search_text override; that field belongs to lexical retrieval. Preserve
    # order, trim boundaries and remove exact duplicates across all fields.
    return "\n".join(dict.fromkeys(
        value.strip() for value in fields if isinstance(value, str) and value.strip()
    ))


def projection_hash(card: Mapping) -> str:
    return hashlib.sha256(card_projection_text(card).encode("utf-8")).hexdigest()[:16]
