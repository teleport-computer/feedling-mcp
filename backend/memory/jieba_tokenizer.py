"""io's tokenizer for memgarden's lexical ranker (``memgarden.retrieval``).

memgarden owns the ranking math, stopwords and gate; the kernel stays
dependency-free, so the language resource is plugged in by the host. This is
that plug: jieba precise mode for CJK, full ASCII identifiers kept whole
(``np-4286``, ``v2.3.1``). Its ``name`` enters the ranking version string, so
changing the tokenizer is changing the ruler.

Request-local only: no user text, postings or scores are cached. Only jieba's
immutable, bundled dictionary is shared across requests.
"""

from __future__ import annotations

import re
import tempfile
from functools import lru_cache

import jieba

#: Pinned in backend/requirements.txt. A different dictionary is a different
#: ruler; ``memory_search_contract`` pins the same name and a test keeps the two equal.
NAME = f"jieba-{jieba.__version__}"

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


class JiebaTokenizer:
    """``memgarden.retrieval.Tokenizer`` protocol implementation."""

    name = NAME

    def tokenize(self, text: str) -> list[str]:
        return tokenize(text)


TOKENIZER = JiebaTokenizer()
