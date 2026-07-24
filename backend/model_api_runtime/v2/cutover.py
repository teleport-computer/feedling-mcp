"""Cutover state machine constants + pure transition table (spec A2).

Pure (no DB) so it stays import-clean under the dependency-direction guard. The
DB CAS lives in db.advance_runtime_state; this module only names states and says
which transitions are legal. draining is a mandatory barrier: you cannot jump
resident<->v2 directly.
"""
from __future__ import annotations

RESIDENT = "resident"
DRAINING = "draining"
V2 = "v2"

VALID_TRANSITIONS: set[tuple[str, str]] = {
    (RESIDENT, DRAINING),
    (DRAINING, V2),
    (V2, DRAINING),
    (DRAINING, RESIDENT),
}


def is_valid_transition(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in VALID_TRANSITIONS
