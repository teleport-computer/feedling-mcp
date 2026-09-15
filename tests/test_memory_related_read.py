"""Related read (memory_fetch ``related_items``) on memgarden.related.one_hop.

Pure unit. io's previous implementation (``memory/recall_metadata.py::one_hop``,
T513) is frozen below as the reference: io-shaped cards (legacy archive markers,
title/description/context summaries) translated by ``card_shape.to_related_card``
must give byte-identical results.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from memgarden import related  # noqa: E402
from memgarden.prompts.recall_fields import retrieval_cues  # noqa: E402
from memory import card_shape, recall_metadata  # noqa: E402


def _reference_one_hop(sources, candidates, *, cap=6):
    """Verbatim copy of the deleted io recall_metadata.one_hop."""
    excluded = {c.get("id") for c in sources}
    found = {}
    for source in sources:
        anchors = recall_metadata.links(source.get("anchor_memory_ids"))
        supersedes = recall_metadata.links(source.get("supersedes"))
        threads = recall_metadata.links(source.get("threads"))
        for card in candidates:
            mid = card.get("id")
            if not isinstance(mid, str) or mid in excluded:
                continue
            reason = ("anchor" if mid in anchors else "supersedes" if mid in supersedes
                      else "thread" if set(threads).intersection(recall_metadata.links(card.get("threads")))
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


def _production(sources, candidates, **kw):
    return related.one_hop([card_shape.to_related_card(c) for c in sources],
                           [card_shape.to_related_card(c) for c in candidates], **kw)


_LIFECYCLE = [{}, {"status": "active"}, {"status": "superseded"}, {"status": "Superseded"},
              {"status": "archived"}, {"status": "deleted"}, {"status": "pending"},
              {"archived_at": "2026-01-01"}, {"archive_reason": "merged"}, {"is_archived": True},
              {"is_archived": False}, {"archived": True}, {"superseded_by": "n1"},
              {"status": "superseded", "archive_reason": "merged"},
              {"status": "superseded", "archived_at": "2026-01-01"},
              {"status": "active", "archived_at": "2026-01-01"}]
_SUMMARY = [{"summary": "摘要 {id}"}, {"title": "标题 {id}"}, {"description": " 描述\n{id} "},
            {"context": "t {id}"}, {"description": "  ", "title": "兜底 {id}"}, {"summary": "   "}, {}]


def _garden(seed):
    rng = random.Random(seed)
    ids = [f"m{i:02d}" for i in range(rng.randint(2, 24))]
    threads = ["a", "b", "c"]
    cards = []
    for mid in ids:
        card = {"id": mid, "threads": rng.sample(threads, rng.randint(0, 2))}
        card.update(rng.choice(_LIFECYCLE))
        card.update({k: v.replace("{id}", mid) for k, v in rng.choice(_SUMMARY).items()})
        cards.append(card)
    sources = []
    for mid in rng.sample(ids, rng.randint(1, min(3, len(ids)))):
        source = {"id": mid, "threads": rng.sample(threads, rng.randint(0, 2))}
        if rng.random() < .6:
            source["anchor_memory_ids"] = rng.sample(ids, rng.randint(1, min(3, len(ids))))
        if rng.random() < .6:
            source["supersedes"] = rng.sample(ids, rng.randint(1, min(3, len(ids))))
        sources.append(source)
    return sources, cards, rng.choice([6, 7, 0, 1, 30])


def test_io_shaped_cards_match_the_previous_io_related_read_exactly():
    differs_without_translation = 0
    for seed in range(300):
        sources, cards, cap = _garden(seed)
        expected = _reference_one_hop(sources, cards, cap=cap)
        assert _production(sources, cards, cap=cap) == expected, seed
        differs_without_translation += related.one_hop(sources, cards, cap=cap) != expected
    # The translation is load-bearing, not decorative.
    assert differs_without_translation >= 50


def test_legacy_archive_marker_never_resurrects_and_superseded_history_keeps_its_status():
    source = {"id": "s", "anchor_memory_ids": ["old", "gone"], "threads": ["t"]}
    cards = [
        {"id": "old", "title": "旧版本", "status": "superseded", "archive_reason": "merged"},
        {"id": "gone", "summary": "归档卡", "status": "active", "archived_at": "2026-01-01"},
        {"id": "sib", "description": "同线索", "threads": ["t"], "is_archived": True},
        {"id": "live", "context": "同线索活卡", "threads": ["t"]},
    ]
    assert _production([source], cards) == [
        {"id": "old", "summary": "旧版本", "source_id": "s", "relation": "anchor", "status": "superseded"},
        {"id": "live", "summary": "同线索活卡", "source_id": "s", "relation": "thread", "status": "active"},
    ]


def test_translation_copies_and_never_mutates():
    raw = {"id": "a", "title": "T", "status": "active", "archived_at": "x", "threads": ["t"]}
    out = card_shape.to_related_card(raw)
    assert out == {**raw, "summary": "T", "status": "archived"}
    assert raw == {"id": "a", "title": "T", "status": "active", "archived_at": "x", "threads": ["t"]}


def test_match_text_cues_use_memgarden_normalization():
    # Previously sliced [:5] before filtering and kept duplicates.
    cues = [{"k": 1}, "取件", " 取件 ", 7, "别名", "什么时候取", "2026-09-01", "第六条"]
    card = {"summary": "包裹", "retrieval_cues": cues}
    assert card_shape.text_for_match(card) == "包裹 " + " ".join(retrieval_cues(cues))
    assert retrieval_cues(cues) == ["取件", "别名", "什么时候取", "2026-09-01", "第六条"]
