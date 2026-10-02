"""Serve-worker-only, opt-in reconciliation of plaintext card vectors.

No write hooks, job queue, decryption, model download or retrieval policy.
Inference runs outside the memory transaction; a fresh projection check inside
that transaction prevents late results from restoring deleted/changed cards.
"""
from __future__ import annotations

import json
import logging
import os
import threading

import db
from memory import card_shape, service
from memory.embedding import e5_onnx, projection, query_service

log = logging.getLogger("feedling.memory.embedding")
# Bound work per scheduler visit so one garden cannot monopolize a tick.
# This is a work-count cap, not a measured time bound.
MAX_CARDS_PER_TICK = 32
_embedder = None
_embedder_lock = threading.Lock()
_loading = False
_load_error: str | None = None
_unavailable_logged = False


def enabled() -> bool:
    return os.environ.get("FEEDLING_MEMORY_EMBEDDING_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def get_embedder():
    global _embedder, _loading, _load_error
    # Only the process that claimed the model may build it (Runtime V2 slot
    # processes never do; T779 step 2b).
    query_service.assert_embedder_owner()
    with _embedder_lock:
        if _embedder is None:
            _loading = True
            try:
                _embedder = e5_onnx.E5SmallOnnxEmbedder()
                _load_error = None
            except Exception as exc:
                _load_error = type(exc).__name__[:40]
                raise
            finally:
                _loading = False
        return _embedder


def model_state() -> dict:
    """Content-free state of this process's model, for the heartbeat (T779 step 2c).

    Never builds the model and never waits on the load lock: it only reads what
    get_embedder() has already left behind, so it is safe while a load is in
    progress and in mode off (where the sweep alone may have loaded it)."""
    embedder = _embedder
    if embedder is None:
        if _loading:
            return {"state": "loading", "owner_pid": os.getpid(), "model_id": None,
                    "load_seconds": None, "reason": None}
        return {"state": "failed" if _load_error else "not_loaded", "owner_pid": None,
                "model_id": None, "load_seconds": None, "reason": _load_error}
    available = bool(getattr(embedder, "available", False))
    return {"state": "ready" if available else "unavailable", "owner_pid": os.getpid(),
            "model_id": str(getattr(embedder, "model_id", "") or "")[:200],
            "load_seconds": round(float(getattr(embedder, "load_seconds", 0.0) or 0.0), 2),
            "reason": None if available else str(getattr(embedder, "unavailable_reason", ""))[:40]}


def _eligible(moments: list, user_id: str) -> tuple[dict, int]:
    result = {}
    encrypted = 0
    for card in service._active_memory_moments(moments):
        if card_shape.is_retired(card) or card.get("visibility") != "shared":
            continue
        if card.get("body_ct") or card.get("K_enclave") or card.get("body") is None:
            encrypted += 1
            continue
        if card.get("owner_user_id") != user_id or not card.get("id"):
            continue
        body = card["body"]
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                continue
        if not isinstance(body, dict) or card_shape.is_retired(body):
            continue
        digest, text = projection.body_projection(body)
        if text:
            result[str(card["id"])] = (digest, text)
    return result, encrypted


def tick(store) -> int:
    global _unavailable_logged
    if not enabled():
        return 0
    embedder = get_embedder()
    if not embedder.available:
        if not _unavailable_logged:
            log.warning("memory_embedding unavailable_reason=%s", embedder.unavailable_reason)
            _unavailable_logged = True
        return 0
    user_id = store.user_id
    try:
        wanted, encrypted = _eligible(service._load_moments(store), user_id)
        existing = db.memory_vectors_load(user_id, embedder.model_id)
        missing = [(mid, digest, text) for mid, (digest, text) in wanted.items()
                   if mid not in existing or existing[mid][0] != digest]
        stale = sum(mid in existing for mid, _, _ in missing)
        batch = missing[:MAX_CARDS_PER_TICK]
        vectors = query_service.encode_passages(embedder, [text for _, _, text in batch]) if batch else []
        if len(vectors) != len(batch):
            raise ValueError("embedding_batch_size_mismatch")
        rows = [(mid, digest, vector) for (mid, digest, _), vector in zip(batch, vectors)]
        if any(len(vector) != embedder.dim for _, _, vector in rows):
            raise ValueError("embedding_dimension_mismatch")
        with service.mutation_lock(store):
            fresh, _ = _eligible(service._load_moments(store), user_id)
            rows = [(mid, digest, vector) for mid, digest, vector in rows
                    if mid in fresh and fresh[mid][0] == digest]
            db.memory_vectors_upsert(user_id, embedder.model_id, rows)
            pruned = db.memory_vectors_prune(user_id, embedder.model_id, list(fresh))
        log.info("memory_embedding encoded=%d stale=%d skipped_encrypted=%d pruned=%d",
                 len(rows), stale, encrypted, pruned)
        return len(rows)
    except Exception:
        # Scheduler logs traceback strings for unhandled errors; keep model/card
        # payloads out of that path. The next scan retries unchanged missing rows.
        log.warning("memory_embedding encoded=0 unavailable_reason=sweep_failed")
        return 0
