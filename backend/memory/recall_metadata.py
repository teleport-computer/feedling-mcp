"""Bounded memory recall projections; no persistence, IO or model calls."""
from __future__ import annotations

from datetime import datetime, timezone

from memory import card_shape


def cues(value: object) -> list[str]:
    """Optional hints are data, not evidence; never stringify nested objects."""
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())[:120]
        if text and text not in result:
            result.append(text)
        if len(result) == 5:
            break
    return result


def links(value: object) -> list[str]:
    values = [value] if isinstance(value, str) else value
    return list(dict.fromkeys(v for v in (values if isinstance(values, list) else [])
                             if isinstance(v, str) and v and len(v) <= 160))[:20]


def fields(inner: dict, envelope: dict) -> dict:
    result = {}
    hints = cues(inner.get("retrieval_cues"))
    if hints:
        result["retrieval_cues"] = hints
    for key in ("anchor_memory_ids", "supersedes"):
        values = links(inner.get(key)) or links(envelope.get(key))
        if values:
            result[key] = values
    return result


def one_hop(sources: list[dict], candidates: list[dict], *, cap: int = 6) -> list[dict]:
    """Callers supply only same-user readable cards. No recursive expansion."""
    excluded = {c.get("id") for c in sources}
    found = {}
    for source in sources:
        anchors = links(source.get("anchor_memory_ids"))
        supersedes = links(source.get("supersedes"))
        threads = links(source.get("threads"))
        for card in candidates:
            mid = card.get("id")
            if not isinstance(mid, str) or mid in excluded:
                continue
            reason = ("anchor" if mid in anchors else "supersedes" if mid in supersedes
                      else "thread" if set(threads).intersection(links(card.get("threads")))
                      else "")
            if card_shape.is_retired(card) and not (
                reason in {"anchor", "supersedes"} and card.get("status") == "superseded"
            ):
                continue
            summary = " ".join(card_shape.summary_of(card).split())
            if reason and summary:
                rank = (0 if reason != "thread" else 1, str(source.get("id") or ""), reason)
                if mid in found and found[mid][0] <= rank:
                    continue
                found[mid] = (rank, {
                    "id": mid, "summary": summary[:120],
                    "source_id": source.get("id"), "relation": reason,
                    "status": str(card.get("status") or "active"),
                })
    ordered = sorted(found.values(), key=lambda item: (item[0][0], str(item[1]["id"])))
    return [item for _, item in ordered[:cap]]


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
