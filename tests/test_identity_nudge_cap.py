"""Pure-unit tests for nudge sum cap validation (no DB required)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from identity import card_policy  # noqa: E402


def test_single_nudge_within_cap():
    """Single nudge of +8 should pass."""
    ok, err = card_policy.validate_nudge_sum([("dimension", 8)])
    assert ok, f"Expected pass but got error: {err}"
    assert err == ""


def test_single_nudge_exceeds_cap():
    """Single nudge of +11 should be rejected."""
    ok, err = card_policy.validate_nudge_sum([("dimension", 11)])
    assert not ok, "Expected rejection for delta > 10"
    assert err == "nudge_delta_exceeds_cap"


def test_negative_nudge_within_cap():
    """Negative nudge of -8 should pass."""
    ok, err = card_policy.validate_nudge_sum([("dimension", -8)])
    assert ok, f"Expected pass but got error: {err}"


def test_negative_nudge_exceeds_cap():
    """Negative nudge of -11 should be rejected."""
    ok, err = card_policy.validate_nudge_sum([("dimension", -11)])
    assert not ok, "Expected rejection for |delta| > 10"
    assert err == "nudge_delta_exceeds_cap"


def test_same_dimension_sum_within_cap():
    """Two nudges (+8 and -3) for same dimension sum to 5, within cap."""
    ok, err = card_policy.validate_nudge_sum([("dimension", 8), ("dimension", -3)])
    assert ok, f"Expected pass but got error: {err}"


def test_same_dimension_sum_exceeds_cap():
    """Two nudges (+8 and +8) for same dimension sum to 16, exceeds cap."""
    ok, err = card_policy.validate_nudge_sum([("dimension", 8), ("dimension", 8)])
    assert not ok, "Expected rejection for sum > 10"
    assert err == "nudge_delta_exceeds_cap"


def test_whitespace_normalization():
    """Dimension names with whitespace should normalize; '幽默' and '幽默 ' are the same."""
    ok, err = card_policy.validate_nudge_sum([("幽默", 8), ("幽默 ", 8)])
    assert not ok, "Expected rejection for normalized sum > 10"
    assert err == "nudge_delta_exceeds_cap"


def test_case_insensitive_normalization():
    """Dimension names should normalize case-insensitively."""
    ok, err = card_policy.validate_nudge_sum([("Dimension", 8), ("dimension", 8)])
    assert not ok, "Expected rejection for normalized sum > 10"
    assert err == "nudge_delta_exceeds_cap"


def test_multiple_dimensions_each_within_cap():
    """Multiple different dimensions, each within cap."""
    ok, err = card_policy.validate_nudge_sum([("dim1", 5), ("dim2", 8), ("dim3", 3)])
    assert ok, f"Expected pass but got error: {err}"


def test_multiple_dimensions_one_exceeds_cap():
    """Multiple dimensions where one exceeds cap."""
    ok, err = card_policy.validate_nudge_sum([("dim1", 5), ("dim2", 11), ("dim3", 3)])
    assert not ok, "Expected rejection when one dimension exceeds cap"
    assert err == "nudge_delta_exceeds_cap"


def test_empty_list():
    """Empty nudge list should pass."""
    ok, err = card_policy.validate_nudge_sum([])
    assert ok, f"Expected pass for empty list but got error: {err}"


def test_boundary_10_positive():
    """Boundary: exactly +10 should pass."""
    ok, err = card_policy.validate_nudge_sum([("dimension", 10)])
    assert ok, f"Expected pass at boundary +10 but got error: {err}"


def test_boundary_10_negative():
    """Boundary: exactly -10 should pass."""
    ok, err = card_policy.validate_nudge_sum([("dimension", -10)])
    assert ok, f"Expected pass at boundary -10 but got error: {err}"


def test_boundary_plus_0_1():
    """Boundary: sum exactly +10 (multiple parts) should pass."""
    ok, err = card_policy.validate_nudge_sum([("dimension", 5), ("dimension", 5)])
    assert ok, f"Expected pass at sum boundary +10 but got error: {err}"


def test_boundary_minus_0_1():
    """Boundary: sum exactly -10 (multiple parts) should pass."""
    ok, err = card_policy.validate_nudge_sum([("dimension", -5), ("dimension", -5)])
    assert ok, f"Expected pass at sum boundary -10 but got error: {err}"
