"""Hybrid-recall settings and contracts shared by every host that runs recall.

Moved from enclave/recall_hybrid.py (T779 step 2a): the enclave today and the
serve-worker for plaintext accounts must read the same calibration, budget and
vector validity rule, and report fallbacks with the same fixed vocabulary.
"""
from __future__ import annotations

import math
import os

MIN_COSINE_ENV = "FEEDLING_MEMORY_RECALL_MIN_COSINE"
BUDGET_ENV = "FEEDLING_MEMORY_RECALL_HYBRID_BUDGET_MS"
DEFAULT_BUDGET_MS = 2000
NORM_TOLERANCE = 1e-3


class Fallback(Exception):
    """This turn uses the lexical path; ``reason`` is a fixed vocabulary word."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
