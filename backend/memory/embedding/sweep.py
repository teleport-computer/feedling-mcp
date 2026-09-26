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
from memory.embedding import e5_onnx, projection

log = logging.getLogger("feedling.memory.embedding")
# Bound work per scheduler visit so one garden cannot monopolize a tick.
# This is a work-count cap, not a measured time bound.
MAX_CARDS_PER_TICK = 32
_embedder = None
_embedder_lock = threading.Lock()
_unavailable_logged = False


def enabled() -> bool:
    return os.environ.get("FEEDLING_MEMORY_EMBEDDING_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def get_embedder():
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            _embedder = e5_onnx.E5SmallOnnxEmbedder()
        return _embedder


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
        vectors = embedder.encode_passages([text for _, _, text in batch]) if batch else []
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
