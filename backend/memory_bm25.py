"""Request-local lexical ranking. No user text, postings or scores are cached.

Only jieba's immutable, bundled dictionary is shared across requests. Full
ASCII identifiers are kept as tokens; Chinese uses jieba's precise mode.
"""

from __future__ import annotations

import math
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

import jieba
import memory_search_contract as search_contract
from memgarden import timestamps


VERSION = search_contract.VERSION
K1 = 1.2
B = 0.75
_ASCII_TOKEN = re.compile(r"([a-z0-9]+(?:[-_./][a-z0-9]+)*)")


@lru_cache(maxsize=1)
def _tokenizer() -> jieba.Tokenizer:
    # A private tokenizer avoids application-level add_word/set_dictionary
    # calls on jieba's global singleton changing the search contract.
    tokenizer = jieba.Tokenizer()
    # Never trust a shared /tmp/jieba.cache: another process could replace it
    # and silently change our pinned vocabulary. Only the bundled dictionary
    # enters this private startup cache, which is removed after initialization.
    with tempfile.TemporaryDirectory(prefix="feedling-jieba-") as cache_dir:
        tokenizer.tmp_dir = cache_dir
        tokenizer.initialize()
    return tokenizer


def prewarm() -> None:
    """Initialize the bundled dictionary before the enclave accepts requests."""
    _tokenizer()


def tokenize(text: str) -> list[str]:
    result = []
    for index, part in enumerate(_ASCII_TOKEN.split(str(text).casefold())):
        if not part:
            continue
        if index % 2:
            result.append(part)
        else:
            result.extend(
                token for token in _tokenizer().cut(part, cut_all=False, HMM=False)
                if any(char.isalnum() for char in token)
            )
    return result


@dataclass(frozen=True)
class CorpusStats:
    documents: int
    total_length: int
    document_frequency: Counter


def corpus_stats(documents: Iterable[Counter]) -> CorpusStats:
    count, length, frequencies = 0, 0, Counter()
    for terms in documents:
        count += 1
        length += sum(terms.values())
        frequencies.update(terms.keys())
    return CorpusStats(count, length, frequencies)


def score(terms: Counter, query: Iterable[str], stats: CorpusStats) -> float:
    """BM25 with nonnegative Robertson IDF; duplicate query tokens count once."""
    if not stats.documents or not stats.total_length:
        return 0.0
    length = sum(terms.values())
    average = stats.total_length / stats.documents
    normalizer = K1 * (1.0 - B + B * length / average)
    value = 0.0
    for term in sorted(set(query)):
        frequency = terms.get(term, 0)
        if not frequency:
            continue
        df = stats.document_frequency[term]
        idf = math.log1p((stats.documents - df + 0.5) / (df + 0.5))
        value += idf * frequency * (K1 + 1.0) / (frequency + normalizer)
    return value


def search_text(item: dict) -> str:
    # Preserve existing searchable fields, without counting an identical
    # summary/content projection twice. Private content never leaves readside.
    fields = [item.get(key) for key in (
        "summary", "content", "_search_content", "bucket", "source",
    )]
    fields.extend(item.get("threads") or [])
    return "\n".join(dict.fromkeys(str(value) for value in fields if value))


def rank(items: list[dict], query: str) -> list[tuple[dict, float]]:
    query_terms = tokenize(query)
    if len(items) > search_contract.MAX_CARDS:
        raise search_contract.SearchLimitExceeded()
    documents = []
    total_bytes = 0
    for item in items:
        text = search_text(item)
        total_bytes += len(text.encode("utf-8"))
        if total_bytes > search_contract.MAX_TEXT_BYTES:
            raise search_contract.SearchLimitExceeded()
        if query_terms:
            documents.append(Counter(tokenize(text)))
    if not query_terms:
        return []
    stats = corpus_stats(documents)
    scored = [(item, score(terms, query_terms, stats))
              for item, terms in zip(items, documents)]
    # The public item's existing score is importance/recency, NOT BM25.
    # Never overwrite it. Equal scores use occurred_at descending, then ID.
    return sorted((pair for pair in scored if pair[1] > 0),
                  key=lambda pair: (-pair[1], -_occurred_ts(pair[0]),
                                    str(pair[0].get("id") or "")))


def _occurred_ts(item: dict) -> float:
    valid, parsed = timestamps.sort_key(str(item.get("occurred_at") or ""))
    return parsed.timestamp() if valid else float("-inf")
