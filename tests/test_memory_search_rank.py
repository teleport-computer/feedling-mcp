"""memory_search ranking: memgarden.retrieval.rank + io's pinned jieba tokenizer.

Pure unit: no DB, no network. The previous io ranker (``backend/memory_bm25.py``)
is frozen below as a reference so the rolling-upgrade protocol stays provably
bit-identical to what an old backend expects.
"""
from __future__ import annotations

import math
import random
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import memory_search_contract as contract  # noqa: E402
from enclave import memory_search  # noqa: E402
from memgarden import retrieval  # noqa: E402
from memgarden import timestamps  # noqa: E402
from memory import jieba_tokenizer as jt  # noqa: E402


def ids(items):
    return [item["id"] for item in items]


def rank(items, query, **kw):
    return ids(memory_search.rank(items, query, **kw))


# --------------------------------------------------------------------------- #
# tokenizer and version contract
# --------------------------------------------------------------------------- #

def test_prewarm_initializes_dictionary_before_first_query():
    jt._tokenizer.cache_clear()
    assert jt._tokenizer.cache_info().currsize == 0
    jt.prewarm()
    # Check before calling _tokenizer ourselves: lazy initialization here must
    # not accidentally make a no-op prewarm pass this test.
    warmed = jt._tokenizer.cache_info()
    assert warmed.currsize == 1
    assert warmed.misses == 1
    tokenizer = jt._tokenizer()
    assert tokenizer.initialized and tokenizer.total > 0 and tokenizer.FREQ
    jt.tokenize("咖啡磨豆机维修编号")
    assert jt._tokenizer.cache_info().misses == warmed.misses


def test_pinned_precise_tokenizer_keeps_identifiers():
    assert version("jieba") == "0.42.1"
    assert jt.tokenize("NP-4286 / CR2450") == ["np-4286", "cr2450"]
    assert jt.tokenize("咖啡磨豆机维修编号") == list(
        jt._tokenizer().cut("咖啡磨豆机维修编号", cut_all=False, HMM=False))
    assert jt.tokenize(" !!! 🪴 \n") == []
    assert not Path(jt._tokenizer().tmp_dir).exists()  # no shared/stale dictionary cache
    assert isinstance(jt.TOKENIZER, retrieval.Tokenizer)


def test_wire_version_names_the_real_tokenizer_and_memgarden_ranker():
    # The backend never imports jieba, so the contract pins the name as a literal.
    assert contract.TOKENIZER_NAME == jt.NAME == jt.TOKENIZER.name
    real = retrieval.rank("x", [{"id": "a", "summary": "x"}], tokenizer=jt.TOKENIZER,
                          **contract.RANK_OPTIONS)
    assert contract.VERSION == real.version == "memgarden-bm25-v1+tok:jieba-0.42.1"
    assert contract.ACCEPTED == (contract.VERSION, contract.PREVIOUS, contract.LEGACY)
    assert len(set(contract.ACCEPTED)) == 3


# --------------------------------------------------------------------------- #
# PREVIOUS protocol == the old io memory_bm25, bit for bit
# --------------------------------------------------------------------------- #

def _old_search_text(item):
    fields = [item.get(key) for key in ("summary", "content", "_search_content", "bucket", "source")]
    fields.extend(item.get("threads") or [])
    return "\n".join(dict.fromkeys(str(value) for value in fields if value))


def _old_rank(items, query):
    """Verbatim math of the deleted backend/memory_bm25.py::rank (minus limits)."""
    query_terms = jt.tokenize(query)
    if not query_terms:
        return []
    docs = [Counter(jt.tokenize(_old_search_text(item))) for item in items]
    n, total, df = len(docs), sum(sum(d.values()) for d in docs), Counter()
    for d in docs:
        df.update(d.keys())
    if not n or not total:
        return []

    def score(terms):
        length = sum(terms.values())
        normalizer = 1.2 * (1.0 - 0.75 + 0.75 * length / (total / n))
        value = 0.0
        for term in sorted(set(query_terms)):
            f = terms.get(term, 0)
            if f:
                idf = math.log1p((n - df[term] + 0.5) / (df[term] + 0.5))
                value += idf * f * (1.2 + 1.0) / (f + normalizer)
        return value

    def occurred(item):
        valid, parsed = timestamps.sort_key(str(item.get("occurred_at") or ""))
        return parsed.timestamp() if valid else float("-inf")

    scored = [(item, score(d)) for item, d in zip(items, docs)]
    return sorted((p for p in scored if p[1] > 0),
                  key=lambda p: (-p[1], -occurred(p[0]), str(p[0].get("id") or "")))


_WORDS = ["咖啡", "磨豆机", "维修", "我", "喜欢", "什么", "猫咪", "体检", "NP-4286", "CR2450",
          "coffee", "repair", "the", "what", "妈妈", "膝盖", "马拉松", "的", "了", "工作"]


def _garden(seed):
    rng = random.Random(seed)
    items = []
    for i in range(rng.randint(3, 40)):
        items.append({
            "id": f"m{i:02d}",
            "summary": "".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 6))),
            "_search_content": " ".join(rng.choice(_WORDS) for _ in range(rng.randint(0, 8))),
            "bucket": rng.choice(["生活", "工作", ""]),
            "source": rng.choice(["", "capture", "咖啡"]),
            "threads": rng.sample(["猫咪", "coffee", "维修"], rng.randint(0, 2)),
            "occurred_at": rng.choice(["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "", "bad"]),
        })
    query = "".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 5)))
    return items, query


def test_previous_protocol_reproduces_old_enclave_bm25_exactly():
    for seed in range(60):
        items, query = _garden(seed)
        old = _old_rank(items, query)
        new = retrieval.rank(query, items, tokenizer=jt.TOKENIZER,
                             text_of=memory_search.search_text, **contract.PREVIOUS_RANK_OPTIONS)
        assert [(item["id"], score) for item, score in old] == [(h.id, h.score) for h in new.hits], seed
        assert ids(memory_search.rank(items, query, protocol=contract.PREVIOUS)) == \
            [item["id"] for item, _ in old]


def test_new_protocol_only_removes_results_never_reorders_survivors():
    # The gate and query stopwords filter; scoring of the remaining terms is BM25.
    # Stopword-free queries keep the old order for everything that passes the gate.
    checked = filtered = 0
    for seed in range(60):
        items, query = _garden(seed)
        if set(jt.tokenize(query)) & retrieval.DEFAULT_STOPWORDS:
            continue
        old = [item["id"] for item, _ in _old_rank(items, query)]
        new = rank(items, query)
        assert set(new) <= set(old), seed
        assert new == [mid for mid in old if mid in set(new)], seed
        checked += 1
        filtered += len(new) < len(old)
    assert checked >= 20 and filtered >= 3  # not vacuous: the gate really removed results


# --------------------------------------------------------------------------- #
# current protocol behaviour
# --------------------------------------------------------------------------- #

def test_rank_matches_noncontiguous_terms_not_literal_substring():
    rows = [{"id": "late", "summary": "coffee grinder needs repair", "score": .01},
            {"id": "early", "summary": "coffee shop", "score": 99},
            {"id": "absent", "summary": "other"}]
    ranked = memory_search.rank(rows, "repair coffee")
    assert ids(ranked) == ["late", "early"]
    assert ranked[0]["score"] == .01
    assert rows[0]["score"] == .01


def test_identifiers_are_not_substrings():
    rows = [{"id": "a", "summary": "NP-4286 CR2450"},
            {"id": "b", "summary": "NP-42860 CR24501"}]
    assert rank(rows, "np-4286") == ["a"]
    assert rank(rows, "cr2450") == ["a"]


def test_ties_do_not_depend_on_candidate_order_and_empty_is_empty():
    rows = [{"id": "b", "summary": "needle"}, {"id": "a", "summary": "needle"}]
    assert rank(rows, "needle") == ["a", "b"]
    assert rank(rows, "needle") == rank(rows[::-1], "needle")
    assert rank(rows, "💡!!") == []
    assert rank([], "needle") == []


def test_no_hit_query_returns_nothing_instead_of_stopword_matches():
    garden = [
        {"id": "drama", "summary": "《我的解放日志》看完了"},
        {"id": "cat", "summary": "猫咪体检指标偏高"},
        {"id": "car", "summary": "我喜欢骑车上班"},
        *[{"id": f"f{i}", "summary": f"第{i}次整理工作笔记"} for i in range(12)],
    ]
    # Old ranker: every card sharing 我/的/什么 came back.
    assert [item["id"] for item, _ in _old_rank(garden, "我姐姐叫什么名字")]
    assert rank(garden, "我姐姐叫什么名字") == []
    # A stopword alone never matches; a real term still does.
    assert rank(garden, "我的") == []
    assert rank(garden, "猫咪体检") == ["cat"]


def test_rare_identifier_with_chat_words_passes_the_gate():
    garden = [{"id": "ticket", "summary": "JIRA-4821 支付回调超时"},
              *[{"id": f"f{i}", "summary": f"今天第{i}次开会讨论排期和需求"} for i in range(30)]]
    assert rank(garden, "JIRA-4821 那个工单后来怎么样了") == ["ticket"]
    # Shared everyday words alone are weak evidence against 30 cards that all have them.
    assert rank(garden, "那个工单后来怎么样了") == []


def test_deleted_or_updated_corpus_has_no_stale_stats_or_text_cache():
    row = {"id": "a", "summary": "old value"}
    assert rank([row], "old")
    row["summary"] = "new value"
    assert rank([row], "old") == []
    assert rank([], "new") == []
    assert rank([row], "new")


def test_projection_deduplicates_same_field_and_searches_private_content_and_source():
    item = {"id": "a", "summary": "same", "_search_content": "same"}
    assert memory_search.search_text(item) == "same"
    item["_search_content"] = "unpublished repair code"
    assert rank([item], "repair") == ["a"]
    assert rank([{"id": "s", "summary": "x", "source": "voicecall"}], "voicecall") == ["s"]


def test_equal_scores_tie_by_occurred_at_then_id_with_bad_times_last():
    rows = [{"id": mid, "summary": "needle", "occurred_at": when} for mid, when in
            [("a", "2026-01-01T00:00:00Z"), ("b", "2026-02-01T00:00:00Z"),
             ("c", "bad"), ("d", "2026-02-01T08:00:00+08:00")]]
    assert rank(rows, "needle") == ["b", "d", "a", "c"]


def test_duplicate_ids_are_returned_once():
    rows = [{"id": "a", "summary": "needle"}, {"id": "a", "summary": "needle needle"}]
    assert rank(rows, "needle") == ["a"]


def test_resource_limits_are_explicit_and_use_the_io_exception(monkeypatch):
    monkeypatch.setattr(contract, "MAX_TEXT_BYTES", 3)
    with pytest.raises(contract.SearchLimitExceeded):
        memory_search.rank([{"id": "a", "summary": "needle"}], "needle")
    with pytest.raises(contract.SearchLimitExceeded):
        memory_search.rank([{"id": "a", "summary": "needle"}], "!!!")
    monkeypatch.setattr(contract, "MAX_TEXT_BYTES", 1 << 20)
    monkeypatch.setattr(contract, "MAX_CARDS", 1)
    with pytest.raises(contract.SearchLimitExceeded):
        memory_search.rank([{"id": "a", "summary": "x"}, {"id": "b", "summary": "x"}], "x")
