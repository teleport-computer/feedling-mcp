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


def derive_batch_item(*, parent_effect_id: str, ordinal: int) -> str:
    """Child sink identity for one operation inside an atomic outbox batch.

    The parent row is still the generation/order fence.  Children need their
    own deterministic identities because disjoint workspace writes may commit
    independently before a sibling fails.  Replaying the parent can then skip
    completed children and retry only operations that provably did not land.
    """
    parent = str(parent_effect_id or "")
    if not parent:
        raise ValueError("parent_effect_id is required")
    index = int(ordinal)
    if index < 0:
        raise ValueError("ordinal must be non-negative")
    return f"{parent}:item:{index}"
