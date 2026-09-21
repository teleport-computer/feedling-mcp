"""Wire/resource contract shared by search callers and the enclave.

No tokenizer or user-content processing belongs in the backend caller.

Ranking is ``memgarden.retrieval`` with io's jieba tokenizer
(``backend/memory/jieba_tokenizer.py``), the same ruler automatic recall uses.
``VERSION`` is derived from memgarden, so a kernel change to the formula,
stopwords or gate changes the wire string instead of silently reusing it.
"""

import json

from memgarden import retrieval

#: Must equal ``memory.jieba_tokenizer.NAME`` (pinned jieba); a test keeps them equal.
#: Kept as a literal so the backend caller never imports jieba.
TOKENIZER_NAME = "jieba-0.42.1"

#: Keyword options passed to ``retrieval.rank`` / ``select_context`` on top of
#: memgarden's defaults. Empty on purpose: the defaults (query stopwords,
#: coverage >= 0.25 or strong evidence >= 1.25 x max single-term IDF) were
#: calibrated on memgarden ``evals/retrieval`` with this tokenizer; numbers are
#: in the 2026-09-15 io changelog entry. A non-empty value adds ``+cfg:<hash>``.
RANK_OPTIONS: dict = {}


class _NameOnly:
    name = TOKENIZER_NAME

    def tokenize(self, text: str) -> list[str]:
        return []


VERSION = retrieval.rank("", [], tokenizer=_NameOnly(), **RANK_OPTIONS).version

#: Automatic recall (``enclave/routes/chat.py``) ranks the same way but with a
#: looser strong-evidence gate. Its query is the latest two non-empty user
#: messages, newest first. Conversational words may not occur in any card, and
#: memgarden counts unseen query words against coverage ("nobody wrote this"
#: is a no-hit signal for a search). In a chat window that signal is chit-chat,
#: not absence: with the search gate, "担心家里猫咪最近不吃饭" inside an ordinary evening chat recalled
#: neither cat card in gardens of 2-200 cards, where the previous selector did.
#: 0.5 x max IDF lets one strongly matching anchor through again. Calibrated
#: 2026-09-15 on memgarden evals/retrieval (io projection + jieba) plus
#: padded-window probes; numbers in the io changelog entry. Search keeps the
#: default gate (no hit returns empty). The version string carries ``+cfg:``.
RECALL_RANK_OPTIONS: dict = {**RANK_OPTIONS, "strong_evidence": 0.5}
RECALL_VERSION = retrieval.rank("", [], tokenizer=_NameOnly(), **RECALL_RANK_OPTIONS).version
#: memgarden's ranking before ``memgarden-bm25-v2`` (2026-09-15): the coverage
#: gate counted IDF over the real candidate pool, so gardens under ~20 cards
#: gated out correct answers. Kept as a literal (the current kernel cannot
#: derive it) and still served during a rolling restart, for a backend that
#: predates v2. ``coverage_pool_floor=1`` is memgarden's documented switch back
#: to the old gate; scores, order and the strong-evidence gate are unchanged.
PREVIOUS_MEMGARDEN = "memgarden-bm25-v1+tok:jieba-0.42.1"
PREVIOUS_MEMGARDEN_RANK_OPTIONS: dict = {**RANK_OPTIONS, "coverage_pool_floor": 1}
#: The enclave BM25 before memgarden owned the ranking (io ``memory_bm25``). Still
#: served, bit-identical, for a backend that asks for it during a rolling restart.
PREVIOUS = "bm25-jieba-0.42.1-v1"
#: Exact old-ranker semantics: no stopwords, no gate, every positive score returns.
PREVIOUS_RANK_OPTIONS = {"stopwords": frozenset(), "min_coverage": 0.0}
LEGACY = "substring-legacy"
#: Search protocols an enclave serves, each with its ``retrieval.rank`` options.
#: On a memgarden that predates v2, VERSION *is* PREVIOUS_MEMGARDEN and has no
#: ``coverage_pool_floor`` knob; the older entry then collapses into VERSION.
SERVED: dict[str, dict] = {
    VERSION: RANK_OPTIONS,
    **({PREVIOUS_MEMGARDEN: PREVIOUS_MEMGARDEN_RANK_OPTIONS}
       if PREVIOUS_MEMGARDEN != VERSION else {}),
    PREVIOUS: PREVIOUS_RANK_OPTIONS,
}
#: What a backend asks next, in order, when an enclave answers
#: ``memory_search_protocol_unsupported``: an enclave one release behind serves
#: PREVIOUS_MEMGARDEN, one that predates memgarden only PREVIOUS.
FALLBACKS = tuple(p for p in (PREVIOUS_MEMGARDEN, PREVIOUS) if p != VERSION)
#: Ranking labels a backend accepts from an enclave response.
ACCEPTED = tuple(dict.fromkeys((VERSION, PREVIOUS_MEMGARDEN, PREVIOUS, LEGACY)))
MAX_CARDS = 4096
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024


class SearchLimitExceeded(RuntimeError):
    def __init__(self):
        super().__init__("memory_search_resource_limit")


def check_request(payload: dict) -> None:
    """Bound the complete corpus, never silently slice it into a smaller one."""
    if len(payload.get("moments", [])) > MAX_CARDS:
        raise SearchLimitExceeded()
    size = 0
    # Match httpx JSON encoding, without allocating another whole request copy.
    for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False).iterencode(payload):
        size += len(chunk.encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise SearchLimitExceeded()
