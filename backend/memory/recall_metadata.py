"""Bounded memory recall projections; no persistence, IO or model calls.

Cue normalization is memgarden's ``prompts.recall_fields.retrieval_cues`` and
the one-hop related read is ``memgarden.related.one_hop`` (fed through
``card_shape.to_related_card``); what stays here is io's own projection of
envelope link fields and the "new in the last 7 days" product policy.
"""
from __future__ import annotations

from datetime import datetime, timezone

from memgarden.prompts.recall_fields import retrieval_cues
from memgarden import related

from memory import card_shape


def fields(inner: dict, envelope: dict) -> dict:
    result = {}
    hints = retrieval_cues(inner.get("retrieval_cues"))
    if hints:
        result["retrieval_cues"] = hints
    for key in ("anchor_memory_ids", "supersedes"):
        values = related.links(inner.get(key)) or related.links(envelope.get(key))
        if values:
            result[key] = values
    return result


def recent_cards(cards: list[dict], *, now: datetime | None = None, cap: int = 3) -> list[dict]:
    """Newly created in the last seven days, not merely fetched/updated today."""
    now = now or datetime.now(timezone.utc)
    recent = []
    for card in cards:
        if card_shape.is_retired(card) or not card_shape.summary_of(card):
            continue
        try:
            created = datetime.fromisoformat(str(card.get("created_at") or "").replace("Z", "+00:00"))
            if created.tzinfo is None:
                continue
            age = (now - created).total_seconds()
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= age <= 7 * 86400:
            recent.append((created, str(card.get("id") or ""), card))
    recent.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [card for _, _, card in recent[:cap]]
