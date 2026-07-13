"""Deterministic effect_id derivation (Hosted Runtime V2 PR A / spec A5).

The linchpin of exactly-once: retries of the SAME logical effect must produce the
SAME id (so a UNIQUE(effect_id) INSERT dedupes them), and distinct effects must
produce distinct ids. Pure function — NO randomness, NO clock. If this ever reads
time or random, retries double-write.
"""
from __future__ import annotations


def derive(*, job_id: int | None, effect_type: str, ordinal: int) -> str:
    """Effect emitted by a turn: keyed by (job, effect_type, execution-order ordinal)."""
    return f"job{int(job_id)}:{effect_type}:{int(ordinal)}"


def derive_control(*, generation: int, effect_type: str, key: str) -> str:
    """Control-plane effect with no owning job (e.g. a cutover-driven cursor advance):
    keyed by (generation, effect_type, caller-stable key)."""
    return f"gen{int(generation)}:{effect_type}:{key}"
