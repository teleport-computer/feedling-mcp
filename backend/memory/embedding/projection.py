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


def body_projection(body: Mapping) -> tuple[str, str]:
    """(projection_hash, text) of one raw decrypted card body.

    The single shape both the serve-worker sweep (which writes vectors) and the
    enclave recall (which checks them) use, so a stored hash is comparable
    across the two. IO's Garden adapter carries retrieval cues only inside the
    lexical search_text, so the structured field is copied explicitly.
    """
    from memory import card_shape  # local: keep importing this module dependency-free

    garden = card_shape.to_garden_card(body)
    garden["retrieval_cues"] = body.get("retrieval_cues")
    return projection_hash(garden), card_projection_text(garden)
