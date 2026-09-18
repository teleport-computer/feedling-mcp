"""Content-free projection of private provider wire traces.

Never copy a trace row: its ``wire`` contains prompts, credentials and replies.
Only producer-owned enums and numeric measurements cross into the ledger.
"""
from __future__ import annotations

import math


TIMEOUT_KINDS = frozenset({"none", "wire_deadline", "connect", "read", "write", "pool", "unknown"})
ERROR_CLASSES = frozenset({"transient", "provider_config", "unknown"})


def _integer(value, *, minimum=0, maximum=None):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        return None
    return value if maximum is None or value <= maximum else None


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def project(trace):
    """Missing trace is unmeasured (None), never zero attempted requests."""
    if not isinstance(trace, dict) or not isinstance(trace.get("attempts"), list):
        return None
    wires = []
    outer = set()
    for row in trace["attempts"]:
        if not isinstance(row, dict):
            continue
        attempt = _integer(row.get("outer_attempt"), minimum=1)
        if attempt is not None:
            outer.add(attempt)
        if row.get("kind") != "http_attempt":
            continue
        error = row.get("error_class")
        error = error if isinstance(error, str) else "unknown" if error is not None else None
        timeout = row.get("timeout_kind")
        timeout = timeout if isinstance(timeout, str) else "unknown"
        wires.append({
            "outer_attempt": attempt,
            "inner_attempt": _integer(row.get("inner_attempt"), minimum=1),
            "status_code": _integer(row.get("status"), minimum=100, maximum=599),
            "dur_ms": _duration(row.get("duration_ms")),
            "provider": row["provider"][:64] if isinstance(row.get("provider"), str) else "",
            "model": row["model"][:128] if isinstance(row.get("model"), str) else "",
            "error_class": error if error in ERROR_CLASSES else "unknown" if error else None,
            "timeout_kind": timeout if timeout in TIMEOUT_KINDS else "unknown",
        })
    return {"wire_attempt_count": len(wires), "outer_attempt_count": len(outer),
            "wire_attempts": wires}
