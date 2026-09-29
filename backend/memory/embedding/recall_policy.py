"""Hybrid-recall settings and contracts shared by every host that runs recall.

Moved from enclave/recall_hybrid.py (T779 step 2a): the enclave today and the
serve-worker for plaintext accounts must read the same calibration, budget and
vector validity rule, and report fallbacks with the same fixed vocabulary.
"""
from __future__ import annotations

import base64
import math
import os
import struct

HYBRID_ENV = "FEEDLING_MEMORY_RECALL_HYBRID"
MIN_COSINE_ENV = "FEEDLING_MEMORY_RECALL_MIN_COSINE"
BUDGET_ENV = "FEEDLING_MEMORY_RECALL_HYBRID_BUDGET_MS"
DEFAULT_BUDGET_MS = 2000
NORM_TOLERANCE = 1e-3
MAX_VECTOR_IDS = 2000  # the backend's cap; the candidate pool is far smaller


class Fallback(Exception):
    """This turn uses the lexical path; ``reason`` is a fixed vocabulary word."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def hybrid_enabled() -> bool:
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


def valid_unit(vector, dim: int) -> bool:
    if len(vector) != dim or any(not math.isfinite(v) for v in vector):
        return False
    norm = math.sqrt(sum(v * v for v in vector))
    return abs(norm - 1.0) <= NORM_TOLERANCE


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
            if not valid_unit(vector, dim):
                raise ValueError
        except Exception:
            rejected += 1
            continue
        out[mid] = (digest, vector)
    return out, rejected
