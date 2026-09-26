"""Enclave-side hybrid automatic recall (T523): dense query vectors fused with BM25.

Default OFF (``FEEDLING_MEMORY_RECALL_HYBRID``). While off, nothing here runs on a
request: no model load, no vector read, no new trace field; recall is the
unchanged lexical ``select_context`` path.

When on, one turn gets one absolute budget (``FEEDLING_MEMORY_RECALL_HYBRID_BUDGET_MS``,
default 2000) covering the vector read (for this turn's plaintext candidate
ids only, after the memory list) and query encoding. Any failure inside
that budget (model not loaded yet, encoder busy, deadline, read failure,
malformed vectors, a memgarden vector-contract error) makes the WHOLE turn
fall back to the lexical path from the untouched cards; a fallback never keeps
half a hybrid result. The reason is recorded, never the text or the vectors.

The model is loaded by a background thread started at app startup, never on the
request path. Query encoding runs on one dedicated thread with at most
``_MAX_IN_FLIGHT`` jobs (running + queued); beyond that a turn falls back at
once instead of queueing without bound. A timed-out job keeps its permit until
it actually finishes, so abandoned work cannot pile up.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import math
import os
import struct
import threading
import time

from enclave import backend_client

HYBRID_ENV = "FEEDLING_MEMORY_RECALL_HYBRID"
MIN_COSINE_ENV = "FEEDLING_MEMORY_RECALL_MIN_COSINE"
BUDGET_ENV = "FEEDLING_MEMORY_RECALL_HYBRID_BUDGET_MS"
DEFAULT_BUDGET_MS = 2000
VECTORS_PATH = "/v1/memory/vectors"
MAX_VECTOR_IDS = 2000  # the backend's cap; the candidate pool is far smaller
_MAX_IN_FLIGHT = 2
_NORM_TOLERANCE = 1e-3


class Fallback(Exception):
    """This turn uses the lexical path; ``reason`` is a fixed vocabulary word."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def enabled() -> bool:
    raw = str(os.environ.get(HYBRID_ENV, "0") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def min_cosine() -> float | None:
    """memgarden requires the host to calibrate this; no value means no hybrid."""
    try:
        value = float(str(os.environ.get(MIN_COSINE_ENV, "")).strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and -1.0 <= value <= 1.0 else None


def budget_seconds() -> float:
    try:
        value = int(str(os.environ.get(BUDGET_ENV, DEFAULT_BUDGET_MS)).strip())
    except ValueError:
        value = DEFAULT_BUDGET_MS
    return max(1, min(value, 10_000)) / 1000.0


# --------------------------------------------------------------------------- #
# model: background load only
# --------------------------------------------------------------------------- #

_holder_lock = threading.Lock()
_embedder = None
_embedder_state = "idle"  # idle | loading | ready | unavailable
_embedder_reason: str | None = "not_started"


def _load_embedder() -> None:
    global _embedder, _embedder_state, _embedder_reason
    try:
        from memory.embedding import e5_onnx, protocol

        embedder = e5_onnx.E5SmallOnnxEmbedder()
        if not embedder.available:
            with _holder_lock:
                _embedder_state, _embedder_reason = "unavailable", str(embedder.unavailable_reason)
            return
        protocol.warmup(embedder)
    except Exception:
        with _holder_lock:
            _embedder_state, _embedder_reason = "unavailable", "load_failed"
        return
    with _holder_lock:
        _embedder, _embedder_state, _embedder_reason = embedder, "ready", None


def start_warmup() -> bool:
    """Start the one background load (app startup). No-op while the flag is off."""
    global _embedder_state, _embedder_reason
    if not enabled():
        return False
    with _holder_lock:
        if _embedder_state != "idle":
            return False
        _embedder_state, _embedder_reason = "loading", "loading"
    threading.Thread(target=_load_embedder, name="recall-embedder-load", daemon=True).start()
    return True


def embedder_status():
    """(embedder or None, reason or None). Never loads on the calling thread."""
    with _holder_lock:
        if _embedder_state == "ready":
            return _embedder, None
        reason = "embedder_loading" if _embedder_state == "loading" else "embedder_unavailable"
        return None, reason


_STATE_LABELS = {"idle": "not_loaded", "loading": "loading", "ready": "loaded",
                 "unavailable": "failed"}
MODEL_ID_PREFIX_CHARS = 48


def status_snapshot() -> dict:
    """Deploy-state evidence for /healthz: flag values and model load state.

    No user data and no paths; ``failure_reason`` comes from the embedder's
    fixed vocabulary; the model id is truncated to its name/precision/digest
    prefix.
    """
    with _holder_lock:
        state, reason, embedder = _embedder_state, _embedder_reason, _embedder
    return {
        "enabled": enabled(),
        "min_cosine": min_cosine(),
        "embedder_state": _STATE_LABELS.get(state, "unknown"),
        "failure_reason": reason if state == "unavailable" else None,
        "model_id": (str(embedder.model_id)[:MODEL_ID_PREFIX_CHARS] if embedder is not None else None),
    }


# --------------------------------------------------------------------------- #
# bounded query encoding
# --------------------------------------------------------------------------- #

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="recall-embed")
_in_flight = threading.BoundedSemaphore(_MAX_IN_FLIGHT)


def _encode_all(embedder, texts):
    started = time.monotonic()
    vectors = [embedder.encode_query(text) for text in texts]
    return vectors, started, time.monotonic()


def encode_queries(embedder, texts: list[str], deadline: float,
                   timing: dict | None = None) -> list[list[float]]:
    """Encode every text or raise Fallback. ``timing`` (optional) receives
    ``encode_queue_ms`` (waiting for the dedicated thread) and
    ``encode_compute_ms`` (the model calls) when the job finished in time."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Fallback("deadline_exceeded")
    if not _in_flight.acquire(blocking=False):
        raise Fallback("embedder_busy")
    submitted = time.monotonic()  # before submit: the worker may start at once
    try:
        future = _executor.submit(_encode_all, embedder, list(texts))
    except BaseException:
        _in_flight.release()
        raise
    future.add_done_callback(lambda _f: _in_flight.release())
    try:
        vectors, started, finished = future.result(timeout=remaining)
    except concurrent.futures.TimeoutError:
        future.cancel()  # only helps if still queued; a running job keeps its permit
        raise Fallback("deadline_exceeded") from None
    except Exception:
        raise Fallback("encode_failed") from None
    if timing is not None:
        timing["encode_queue_ms"] = round(max(0.0, started - submitted) * 1000.0, 1)
        timing["encode_compute_ms"] = round((finished - started) * 1000.0, 1)
    if len(vectors) != len(texts) or any(not _valid_unit(v, embedder.dim) for v in vectors):
        raise Fallback("encode_failed")
    return vectors


# --------------------------------------------------------------------------- #
# stored card vectors (read through the backend as the authenticated user)
# --------------------------------------------------------------------------- #

def _valid_unit(vector, dim: int) -> bool:
    if len(vector) != dim or any(not math.isfinite(v) for v in vector):
        return False
    norm = math.sqrt(sum(v * v for v in vector))
    return abs(norm - 1.0) <= _NORM_TOLERANCE


def plaintext_candidate_ids(moments, authorized_user_id: str) -> list[str]:
    """Ids of this turn's plaintext-tier, non-local_only candidates owned by the
    authorized user, in list order.

    Mirrors the storage-tier test the sweep and the readside use: an envelope
    with ``body_ct`` or ``K_enclave`` is enclave-encrypted and never has a
    vector, so it is never asked for; the owner binding is the one
    ``read_envelope`` enforces, so a card the reader cannot open is not named.
    """
    out = []
    for moment in moments or []:
        if not isinstance(moment, dict) or moment.get("visibility") == "local_only":
            continue
        if moment.get("owner_user_id") != authorized_user_id:
            continue
        if moment.get("body_ct") or moment.get("K_enclave"):
            continue
        if moment.get("body") is None and moment.get("body_b64") is None:
            continue
        mid = moment.get("id")
        if isinstance(mid, str) and mid:
            out.append(mid)
    return list(dict.fromkeys(out))[:MAX_VECTOR_IDS]


async def fetch_vectors(forward_headers: dict, state: dict, ids: list[str]) -> None:
    """Read this turn's candidate vectors into ``state``; never raises.

    ``vectors_ms`` is the request's own round trip, measured here.
    """
    started = time.monotonic()
    state["vectors_requested"] = len(ids)
    try:
        remaining = state["deadline"] - started
        if remaining <= 0:
            raise Fallback("deadline_exceeded")
        try:
            payload = await asyncio.wait_for(backend_client.backend_post(
                VECTORS_PATH, forward_headers,
                {"model_id": state["model_id"], "ids": ids}), timeout=remaining)
        except asyncio.TimeoutError:
            raise Fallback("deadline_exceeded") from None
        except Exception:
            raise Fallback("vectors_unavailable") from None
        stored, rejected = decode_vectors(payload, state["model_id"], state["embedder"].dim)
        wanted = set(ids)
        foreign = [mid for mid in stored if mid not in wanted]
        for mid in foreign:
            del stored[mid]
        state["stored"], state["vectors_rejected"] = stored, rejected + len(foreign)
    except Fallback as exc:
        state["fallback_reason"] = exc.reason
    except Exception:
        state["fallback_reason"] = "vectors_unavailable"
    finally:
        state["vectors_ms"] = round((time.monotonic() - started) * 1000.0, 1)


def decode_vectors(payload, model_id: str, dim: int) -> tuple[dict, int]:
    """{moment_id: (projection_hash, vector)} and the count of rejected rows."""
    if not isinstance(payload, dict) or payload.get("model_id") != model_id:
        raise Fallback("vectors_model_mismatch")
    rows = payload.get("vectors")
    if not isinstance(rows, list):
        raise Fallback("vectors_malformed")
    out, rejected = {}, 0
    for row in rows:
        try:
            mid = str(row["id"])
            digest = str(row["projection_hash"])
            blob = base64.b64decode(row["vector_b64"], validate=True)
            if not mid or mid in out or len(blob) != dim * 4:
                raise ValueError
            vector = list(struct.unpack(f"<{dim}f", blob))
            if not _valid_unit(vector, dim):
                raise ValueError
        except Exception:
            rejected += 1
            continue
        out[mid] = (digest, vector)
    return out, rejected


# --------------------------------------------------------------------------- #
# per-request state carried from the route into the selection thread
# --------------------------------------------------------------------------- #

def begin(unified_ranker: bool) -> dict:
    """Start a turn's hybrid state. Only called when ``enabled()``."""
    started = time.monotonic()
    state = {"deadline": started + budget_seconds(), "started": started,
             "embedder": None, "model_id": None, "stored": None,
             "fallback_reason": None, "vectors_ms": None, "vectors_requested": 0,
             "vectors_rejected": 0}
    if not unified_ranker:
        state["fallback_reason"] = "legacy_ranker"
        return state
    if min_cosine() is None:
        state["fallback_reason"] = "min_cosine_unset"
        return state
    embedder, reason = embedder_status()
    if embedder is None:
        state["fallback_reason"] = reason
        return state
    state["embedder"] = embedder
    state["model_id"] = embedder.model_id
    return state
