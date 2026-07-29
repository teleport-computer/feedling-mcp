"""Hysteresis anchor for the V2 verbatim chat tail.

The tail's start seq used to be recomputed every turn from the newest
``max_turns`` genuine user turns, so the window slid forward on every new
message and the prompt prefix after the summary changed each round — provider
prompt caches require an exact prefix match, so that is a guaranteed miss.

This module keeps the start seq *pinned* until enough new turns accumulate,
then advances it once.  Most turns are therefore pure appends behind a
byte-identical prefix; the cost is that the verbatim tail floats between
``target_turns`` and ``max_turns_before_advance``.

Deliberately pure: no DB, no envelope, no provider.  The advance policy can be
tested without a running service, matching ``summary_frontier``/
``prompt_frontier``'s layering.
"""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_TARGET_TURNS = 40
DEFAULT_MAX_TURNS_BEFORE_ADVANCE = 60


@dataclass(frozen=True)
class TailAnchorDecision:
    """Where the verbatim tail starts this turn, and whether it just moved."""

    anchor_seq: int
    advanced: bool
    reason: str


def decide_anchor(
    *,
    current_anchor: int | None,
    turns_after_anchor: int,
    boundary_seq_for_target: int | None,
    target_turns: int = DEFAULT_TARGET_TURNS,
    max_turns_before_advance: int = DEFAULT_MAX_TURNS_BEFORE_ADVANCE,
) -> TailAnchorDecision:
    """Pin the tail start until the hysteresis band is crossed.

    ``boundary_seq_for_target`` is the oldest seed seq among the newest
    ``target_turns`` genuine user turns (what the old per-turn computation
    returned).  It is consulted only when an advance is actually due.
    """

    target = int(target_turns)
    ceiling = int(max_turns_before_advance)
    if target <= 0 or ceiling <= target:
        raise ValueError(
            "max_turns_before_advance must be greater than target_turns, "
            "and target_turns must be positive"
        )

    boundary = (
        int(boundary_seq_for_target)
        if boundary_seq_for_target is not None
        else None
    )

    if current_anchor is None:
        if boundary is None:
            return TailAnchorDecision(0, False, "no_boundary")
        return TailAnchorDecision(boundary, True, "bootstrap")

    anchor = int(current_anchor)
    if int(turns_after_anchor) < ceiling:
        return TailAnchorDecision(anchor, False, "hysteresis_hold")
    if boundary is None:
        return TailAnchorDecision(anchor, False, "no_boundary")
    # seq is monotonic; an older boundary would lengthen the tail AND reorder
    # the prefix — strictly worse than holding.
    if boundary <= anchor:
        return TailAnchorDecision(anchor, False, "boundary_not_newer")
    return TailAnchorDecision(boundary, True, "threshold_advance")
