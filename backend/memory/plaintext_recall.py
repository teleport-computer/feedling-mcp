"""Automatic recall for plaintext accounts next to the data (T779 step 2b).

Runs in the Runtime V2 slot process. Selection is the shared
``memory.recall_select`` (the enclave calls the same code); this module only
decides whether a turn may be served here and reads the same inputs the enclave
would read, through callables the assembly layer injects:

- ``effective_mode(user_id)``    accounts.registry.effective_content_encryption
- ``history_page(user_id, through_seq)``  the backend history page the enclave gets
- ``list_moments(user_id, limit)``        the memory/list page the enclave gets
- ``stored_vectors(user_id, model_id, ids)``  memory.embedding.serve.authorized_vectors
- ``encoder``  the parent process's query encoder client (``encode(texts, deadline)``)

Nothing here decrypts. A turn whose inputs contain any sealed piece is not
served here (the caller keeps the enclave path); a plaintext account's sealed
memory cards are left out of the candidate pool (approved T779 policy).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

from core import envelope as core_envelope
from core import history_view, plaintext_row
from memory import recall_select
from memory.embedding import recall_policy

MODE_ENV = "FEEDLING_V2_PLAINTEXT_RECALL"
MODES = ("off", "shadow", "on")

# The enclave's request options for Runtime V2 (serve_worker._read_context_memories).
V2_QUERY_ARGS = {"context_mode": "", "context_recent": True, "want_trace": True}


LOCAL_TIMEOUT_ENV = "FEEDLING_V2_PLAINTEXT_RECALL_TIMEOUT_MS"
SHADOW_TIMEOUT_ENV = "FEEDLING_V2_RECALL_SHADOW_TIMEOUT_MS"
DEFAULT_LOCAL_TIMEOUT_MS = 3000
DEFAULT_SHADOW_TIMEOUT_MS = 3000

# At most one card-vector read in flight per process. A read that outlives its
# turn's budget keeps this until the database call really returns, so slow
# reads cannot pile up; the next turn meanwhile stays lexical (vectors_busy).
_VECTOR_READ_PERMIT = threading.BoundedSemaphore(1)


def _timeout_seconds(env: str, default_ms: int) -> float:
    try:
        value = int(str(os.environ.get(env, default_ms)).strip())
    except ValueError:
        value = default_ms
    return max(1, min(value, 60_000)) / 1000.0


def local_timeout_seconds() -> float:
    return _timeout_seconds(LOCAL_TIMEOUT_ENV, DEFAULT_LOCAL_TIMEOUT_MS)


def shadow_timeout_seconds() -> float:
    return _timeout_seconds(SHADOW_TIMEOUT_ENV, DEFAULT_SHADOW_TIMEOUT_MS)


def run_bounded(fn, timeout: float, permit: threading.BoundedSemaphore):
    """Run ``fn()`` on its own thread and wait at most ``timeout`` seconds.

    Returns ("ok", value) | ("error", exc) | ("timeout", None) | ("busy", None).
    The thread owns ``permit`` until ``fn`` really returns, so a caller that
    stops waiting never frees capacity that is still in use.
    """
    if not permit.acquire(blocking=False):
        return "busy", None
    box: dict = {}
    done = threading.Event()

    def work():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — handed back to the waiter
            box["error"] = exc
        finally:
            done.set()
            permit.release()

    threading.Thread(target=work, name="plaintext-recall-bounded", daemon=True).start()
    if not done.wait(max(0.0, timeout)):
        return "timeout", None
    if "error" in box:
        return "error", box["error"]
    return "ok", box.get("value")


def mode() -> str:
    raw = str(os.environ.get(MODE_ENV, "off") or "off").strip().lower()
    return raw if raw in MODES else "off"


class PlaintextReadFailure(Exception):
    """A plaintext row the enclave reader would also reject; ``reason`` matches it."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NotServedHere(Exception):
    """This turn keeps the enclave path; ``reason`` is a fixed vocabulary word."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _row_reader(user_id: str):
    def read(env):
        if not isinstance(env, dict):
            raise PlaintextReadFailure("envelope must be an object")
        if plaintext_row.is_sealed_row(env):
            # Unreachable after page_is_plaintext; never decrypt here.
            raise NotServedHere("sealed_history")
        return plaintext_row.read_plaintext_row(env, user_id, PlaintextReadFailure)
    return read


def page_is_plaintext(rows) -> bool:
    """True when every piece history_view would read on this page is plaintext.

    Mirrors what history_view reads: local_only rows are not read; an omitted
    body is not read but its image/file caption is; otherwise the row itself and,
    for image/file rows, its caption. Captions are projected with the shared
    ``caption_envelope_from_row`` and judged by the same sealed test.
    """
    for row in rows or []:
        if not isinstance(row, dict):
            return False
        if row.get("visibility") == "local_only":
            continue
        if row.get("content_type", "text") in ("image", "file"):
            caption = core_envelope.caption_envelope_from_row(row)
            if caption is not None and plaintext_row.is_sealed_row(caption):
                return False
        if row.get("body_omitted"):
            continue
        if plaintext_row.is_sealed_row(row):
            return False
    return True


def plaintext_cards(moments, user_id: str, inner_out: dict | None = None) -> tuple[list[dict], int]:
    """(cards, sealed_count): enclave readside.moments_to_cards for plaintext rows.

    Same order, same local_only skip, same read-failure drop; sealed rows are
    counted and left out instead of decrypted.
    """
    read = _row_reader(user_id)
    out: list[dict] = []
    sealed = 0
    for m in moments or []:
        if m.get("visibility") == "local_only":
            continue
        if isinstance(m, dict) and plaintext_row.is_sealed_row(m):
            sealed += 1
            continue
        try:
            inner = json.loads(read(m).decode("utf-8"))
        except (PlaintextReadFailure, json.JSONDecodeError):
            continue
        if inner_out is not None and m.get("id"):
            inner_out[str(m.get("id"))] = inner
        out.append(recall_select.card_from_inner(inner, m))
    return out, sealed


@dataclass
class _Dim:
    dim: int


@dataclass
class _Precomputed:
    """recall_select encoder backed by vectors the parent already returned."""

    by_text: dict
    timing: dict = field(default_factory=dict)

    def encode_queries(self, embedder, texts, deadline, timing=None):
        if timing is not None:
            timing.update(self.timing)
        return [self.by_text[text] for text in texts]


@dataclass
class LocalResult:
    payload: dict            # context_memories / context_memory_trace / context_memory_log
    input_fingerprint: dict
    summary: dict
    sealed_cards: int
    elapsed_ms: float


def _hybrid_state(cards_moments, user_id, current, combined, deps, deadline, started):
    state = {"deadline": deadline, "started": started, "embedder": None, "model_id": None,
             "stored": None, "fallback_reason": None, "vectors_ms": None,
             "vectors_requested": 0, "vectors_rejected": 0}
    encoder = None
    if not recall_select.unified_recall_enabled():
        state["fallback_reason"] = "legacy_ranker"
    elif recall_policy.min_cosine() is None:
        state["fallback_reason"] = "min_cosine_unset"
    elif deps.encoder is None:
        state["fallback_reason"] = "embedder_unavailable"
    elif current:
        texts = [current] + ([combined] if combined != current else [])
        try:
            reply = deps.encoder.encode(texts, deadline)
            state["embedder"] = _Dim(reply.dim)
            state["model_id"] = reply.model_id
            encoder = _Precomputed(dict(zip(texts, reply.vectors)), dict(reply.timing))
            fetch_started = time.monotonic()
            if fetch_started >= deadline:
                raise recall_policy.Fallback("deadline_exceeded")
            ids = recall_policy.plaintext_candidate_ids(cards_moments, user_id)
            state["vectors_requested"] = len(ids)
            if ids:
                status, value = run_bounded(
                    lambda: deps.stored_vectors(user_id, reply.model_id, ids),
                    deadline - time.monotonic(), _VECTOR_READ_PERMIT)
                if status == "busy":
                    raise recall_policy.Fallback("vectors_busy")
                if status == "timeout":
                    raise recall_policy.Fallback("deadline_exceeded")
                if status == "error":
                    if isinstance(value, recall_policy.Fallback):
                        raise value
                    # A database error on the vector read keeps this turn on the
                    # same candidate pool, lexically; it never drops the path.
                    raise recall_policy.Fallback("vectors_unavailable")
                payload = value
            else:
                payload = {"model_id": reply.model_id, "vectors": []}
            stored, rejected = recall_policy.decode_vectors(payload, reply.model_id, reply.dim)
            wanted = set(ids)
            foreign = [mid for mid in stored if mid not in wanted]
            for mid in foreign:
                del stored[mid]
            state["stored"], state["vectors_rejected"] = stored, rejected + len(foreign)
            state["vectors_ms"] = round((time.monotonic() - fetch_started) * 1000.0, 1)
            if time.monotonic() > deadline:
                raise recall_policy.Fallback("deadline_exceeded")
        except recall_policy.Fallback as exc:
            state["fallback_reason"] = exc.reason
            encoder = None
    return state, encoder


@dataclass
class Deps:
    effective_mode: object
    history_page: object
    list_moments: object
    stored_vectors: object
    encoder: object = None


def select(user_id: str, through_seq: int, deps: Deps) -> LocalResult:
    """Select this turn's context cards here, or raise NotServedHere."""
    started = time.monotonic()
    if deps.effective_mode(user_id) != "off":
        raise NotServedHere("account_encrypted")
    rows = deps.history_page(user_id, int(through_seq))
    if not page_is_plaintext(rows):
        raise NotServedHere("sealed_history")
    decrypted, _errors = history_view.history_items(rows, _row_reader(user_id), PlaintextReadFailure)
    moments = deps.list_moments(user_id, recall_select.memory_readside_model_api_limit())
    query_args = {**V2_QUERY_ARGS, "authorized_user_id": user_id, "content_sk": None}
    hybrid = None
    encoder = None
    inner = None
    if recall_policy.hybrid_enabled():
        inner = {}
    cards, sealed = plaintext_cards(moments, user_id, inner_out=inner)
    if inner is not None:
        current, combined = recall_select.query_texts(decrypted)
        hybrid, encoder = _hybrid_state(
            moments, user_id, current, combined, deps,
            started + recall_policy.budget_seconds(), started)
        query_args["hybrid"] = hybrid
    evidence: dict = {}
    picked, trace, log = recall_select.select_context_memories(
        cards, decrypted, query_args, inner=inner, encoder=encoder, evidence=evidence)
    return LocalResult(
        payload={"context_memories": picked, "context_memory_trace": trace,
                 "context_memory_log": log},
        input_fingerprint=recall_select.input_fingerprint(
            decrypted, cards, query_args, evidence=evidence),
        summary=recall_select.decision_summary(picked, log),
        sealed_cards=sealed,
        elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
    )


def compare(local: LocalResult, enclave_diagnostics: dict | None) -> dict:
    """Content-free shadow verdict: comparable / normalized / incomparable / unmeasured."""
    diag = enclave_diagnostics or {}
    if diag.get("error"):
        return {"verdict": "unmeasured", "reason": str(diag["error"])[:80]}
    remote_fp = diag.get("input_fingerprint")
    if not remote_fp or not diag.get("summary"):
        return {"verdict": "unmeasured", "reason": "enclave_diagnostics_missing"}
    if remote_fp == local.input_fingerprint:
        return {"verdict": "comparable", "same": diag["summary"] == local.summary,
                "diff": sorted(k for k in local.summary if diag["summary"].get(k) != local.summary[k])}
    normalized = diag.get("normalized") or {}
    if normalized.get("input_fingerprint") and normalized["input_fingerprint"] == local.input_fingerprint:
        return {"verdict": "normalized", "same": normalized.get("summary") == local.summary,
                "diff": sorted(k for k in local.summary
                               if (normalized.get("summary") or {}).get(k) != local.summary[k]),
                "full_same": diag["summary"] == local.summary}
    return {"verdict": "incomparable",
            "reason": ",".join(sorted(k for k in local.input_fingerprint
                                      if remote_fp.get(k) != local.input_fingerprint[k]))[:120]}
