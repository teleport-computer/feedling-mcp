"""L1: the Memory Garden step's non-blocking below-floor hint.

Root cause it guards against: a 37-day relationship finished onboarding with
2 cards because nothing surfaced the day-scaled floor to the agent. The hint
makes "below floor" visible WITHOUT turning memory into a gate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from hosted.onboarding_validation import _memory_floor_fields


def _st(count, total_floor):
    return {"memory_count": count, "memory_floor": total_floor}


def test_below_floor_sets_flag_and_hint():
    f = _memory_floor_fields(_st(2, 38))
    assert f["memory_floor"] == 38
    assert f["memory_below_floor"] is True
    assert f["hint"]
    # hint names both the actual count and the expected floor so the agent
    # knows the gap concretely.
    assert "2" in f["hint"] and "38" in f["hint"]
    # never a fabrication instruction — restraint stays intact.
    assert "NEVER fabricate" in f["hint"]


def test_at_or_above_floor_no_hint():
    at = _memory_floor_fields(_st(38, 38))
    above = _memory_floor_fields(_st(40, 38))
    for f in (at, above):
        assert f["memory_below_floor"] is False
        assert f["hint"] == ""


def test_zero_floor_is_never_below():
    # legacy / missing floor must not spuriously flag or block.
    f = _memory_floor_fields(_st(0, 0))
    assert f["memory_below_floor"] is False
    assert f["hint"] == ""


def test_below_floor_never_flips_passing():
    # The helper only produces informational fields; it must not carry a
    # `passing` / `blocking` key that could gate onboarding.
    f = _memory_floor_fields(_st(2, 38))
    assert "passing" not in f
    assert "blocking" not in f


def test_memory_floor_reads_flat_field():
    # `_memory_floor_fields` reads `memory_floor` directly off bootstrap_st
    # (Batch 4: the old per-tab `floors` dict no longer exists there).
    f = _memory_floor_fields({"memory_count": 1, "memory_floor": 13})
    assert f["memory_floor"] == 13
    assert f["memory_below_floor"] is True
