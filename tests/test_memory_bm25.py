from __future__ import annotations

import math
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import memory_bm25 as bm25  # noqa: E402


def test_prewarm_initializes_dictionary_before_first_query():
    bm25._tokenizer.cache_clear()
    assert bm25._tokenizer.cache_info().currsize == 0
    bm25.prewarm()
    # Check before calling _tokenizer ourselves: lazy initialization here must
    # not accidentally make a no-op prewarm pass this test.
    warmed = bm25._tokenizer.cache_info()
    assert warmed.currsize == 1
    assert warmed.misses == 1
    tokenizer = bm25._tokenizer()
    assert tokenizer.initialized and tokenizer.total > 0 and tokenizer.FREQ
    bm25.tokenize("咖啡磨豆机维修编号")
    assert bm25._tokenizer.cache_info().misses == warmed.misses


def test_pinned_precise_tokenizer_keeps_identifiers():
    assert version("jieba") == "0.42.1"
    assert bm25.tokenize("NP-4286 / CR2450") == ["np-4286", "cr2450"]
    assert bm25.tokenize("咖啡磨豆机维修编号") == list(
        bm25._tokenizer().cut("咖啡磨豆机维修编号", cut_all=False, HMM=False))
    assert bm25.tokenize(" !!! 🪴 \n") == []
    assert not Path(bm25._tokenizer().tmp_dir).exists()  # no shared/stale dictionary cache


def test_nonnegative_idf_and_query_frequency():
    docs = [Counter({"code": 1}), Counter({"code": 1})]
    stats = bm25.corpus_stats(docs)
    assert bm25.score(docs[0], ["code"], stats) == pytest.approx(math.log(1.2))
    assert bm25.score(docs[0], ["code", "code"], stats) == bm25.score(docs[0], ["code"], stats)
    assert bm25.score(docs[0], ["missing"], stats) == 0


def test_global_stats_two_pass_pages_are_equivalent():
    pages = [[Counter({"coffee": 8, "noise": 99})],
             [Counter({"coffee": 1, "repair": 1}), Counter({"noise": 1})]]
    flat = [doc for page in pages for doc in page]
    first_pass = bm25.corpus_stats(doc for page in pages for doc in page)
    one_pass = bm25.corpus_stats(flat)
    assert first_pass == one_pass
    scores = [bm25.score(doc, ["coffee", "repair"], first_pass)
              for page in pages for doc in page]
    assert scores == [bm25.score(doc, ["coffee", "repair"], one_pass) for doc in flat]
    assert scores[1] > scores[0]


def test_rank_matches_noncontiguous_terms_not_literal_substring():
    rows = [{"id": "late", "summary": "coffee grinder needs repair", "score": .01},
            {"id": "early", "summary": "coffee shop", "score": 99},
            {"id": "absent", "summary": "other"}]
    ranked = bm25.rank(rows, "repair coffee")
    assert [row["id"] for row, _ in ranked] == ["late", "early"]
    assert ranked[0][0]["score"] == .01
    assert rows[0]["score"] == .01


def test_identifiers_are_not_substrings():
    rows = [{"id": "a", "summary": "NP-4286 CR2450"},
            {"id": "b", "summary": "NP-42860 CR24501"}]
    assert [row["id"] for row, _ in bm25.rank(rows, "np-4286")] == ["a"]
    assert [row["id"] for row, _ in bm25.rank(rows, "cr2450")] == ["a"]


def test_ties_do_not_depend_on_candidate_order_and_empty_is_empty():
    rows = [{"id": "b", "summary": "needle"}, {"id": "a", "summary": "needle"}]
    assert [row["id"] for row, _ in bm25.rank(rows, "needle")] == ["a", "b"]
    assert bm25.rank(rows, "needle") == bm25.rank(rows[::-1], "needle")
    assert bm25.rank(rows, "💡!!") == []
    assert bm25.rank([], "needle") == []


def test_deleted_or_updated_corpus_has_no_stale_stats_or_text_cache():
    row = {"id": "a", "summary": "old value"}
    assert bm25.rank([row], "old")
    row["summary"] = "new value"
    assert bm25.rank([row], "old") == []
    assert bm25.rank([], "new") == []
    assert bm25.rank([row], "new")


def test_projection_deduplicates_same_field_and_searches_private_content():
    item = {"id": "a", "summary": "same", "_search_content": "same"}
    assert bm25.search_text(item) == "same"
    item["_search_content"] = "unpublished repair code"
    assert bm25.rank([item], "repair")


def test_equal_scores_tie_by_occurred_at_then_id_with_bad_times_last():
    rows = [{"id": mid, "summary": "needle", "occurred_at": when} for mid, when in
            [("a", "2026-01-01T00:00:00Z"), ("b", "2026-02-01T00:00:00Z"),
             ("c", "bad"), ("d", "2026-02-01T08:00:00+08:00")]]
    assert [item["id"] for item, _ in bm25.rank(rows, "needle")] == ["b", "d", "a", "c"]


def test_resource_text_limit_is_explicit(monkeypatch):
    monkeypatch.setattr(bm25.search_contract, "MAX_TEXT_BYTES", 3)
    with pytest.raises(bm25.search_contract.SearchLimitExceeded):
        bm25.rank([{"summary": "needle"}], "needle")
    with pytest.raises(bm25.search_contract.SearchLimitExceeded):
        bm25.rank([{"summary": "needle"}], "!!!")
