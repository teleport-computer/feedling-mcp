"""Authorized read of the caller's own card vectors for enclave recall (T523).

Served on the backend to the enclave, which forwards the end user's own auth
headers exactly like its /v1/memory/list read; the identity is the
authenticated store, never a caller-supplied user id. The enclave asks for the
ids of this turn's plaintext candidates; the answer is the intersection of
those ids with the caller's currently eligible cards. Stored rows are not proof
of eligibility: every request re-derives the eligible set from the user's
current cards (active, not retired, shared, plaintext, owned) and returns only
rows whose stored projection hash still equals that card's current projection.
Rows the sweep has not pruned yet (deleted, retired, local_only, newly
encrypted, edited) are therefore never returned.
"""
from __future__ import annotations

import base64
import struct

import db
from memory import service
from memory.embedding import sweep

MAX_IDS = 2000
MAX_ID_CHARS = 200
MAX_MODEL_ID_CHARS = 300


def _ids(raw) -> list[str] | None:
    if not isinstance(raw, list) or len(raw) > MAX_IDS:
        return None
    out = []
    for value in raw:
        if not isinstance(value, str) or not value or len(value) > MAX_ID_CHARS:
            return None
        out.append(value)
    return list(dict.fromkeys(out))


def authorized_vectors(store, payload) -> tuple[dict, int]:
    payload = payload if isinstance(payload, dict) else {}
    model_id = payload.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > MAX_MODEL_ID_CHARS:
        return {"error": "invalid_model_id"}, 400
    ids = _ids(payload.get("ids"))
    if ids is None:
        return {"error": "invalid_ids", "max": MAX_IDS}, 400
    user_id = store.user_id
    if not ids:
        return {"model_id": model_id, "dim": 0, "count": 0, "vectors": []}, 200
    eligible, _ = sweep._eligible(service._load_moments(store), user_id)
    stored = db.memory_vectors_load(user_id, model_id)
    rows = []
    dim = None
    for moment_id in sorted(set(ids)):
        current = eligible.get(moment_id)
        hit = stored.get(moment_id)
        if current is None or hit is None or hit[0] != current[0]:
            continue
        digest, vector = hit
        if dim is None:
            dim = len(vector)
        if len(vector) != dim:
            continue  # one model id has one dimension; a stray row is not served
        rows.append({"id": moment_id, "projection_hash": digest,
                     "vector_b64": base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode()})
    return {"model_id": model_id, "dim": dim or 0, "count": len(rows), "vectors": rows}, 200
