"""Pure health-policy tests for the runner fleet check."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi import runner_health  # noqa: E402


def test_parse_expected_runner_count_requires_positive_integer():
    assert runner_health.parse_expected_runner_count("2") == 2
    for raw in (None, "", "0", "-1", "1.5", "many"):
        with pytest.raises(ValueError):
            runner_health.parse_expected_runner_count(raw)


def test_runner_fleet_is_healthy_only_when_observed_and_healthy_equal_expected():
    rows = [{"ts": 995.0, "host_all": True}]
    assert runner_health.evaluate_runner_fleet(
        rows, expected=1, now=1000.0, max_age=90.0
    ) == {
        "status": "ok", "expected": 1, "healthy": 1,
        "observed": 1, "max_age_seconds": 90.0,
    }


@pytest.mark.parametrize("rows", [
    [],
    [{"ts": 800.0, "host_all": True}],
    [{"ts": 995.0, "host_all": False}],
    [{"ts": 995.0, "host_all": True}, {"ts": 994.0, "host_all": True}],
])
def test_runner_fleet_count_or_health_drift_is_down(rows):
    check = runner_health.evaluate_runner_fleet(
        rows, expected=1, now=1000.0, max_age=90.0
    )
    assert check["status"] == "down"
    assert check["reason"] == "runner_count_mismatch"
    assert set(check) == {
        "status", "reason", "expected", "healthy", "observed",
        "max_age_seconds",
    }


@pytest.mark.parametrize("row", [
    {"host_all": True},
    {"ts": "not-a-timestamp", "host_all": True},
])
def test_runner_fleet_malformed_or_absent_timestamp_is_observed_but_not_healthy(row):
    assert runner_health.evaluate_runner_fleet(
        [row], expected=1, now=1000.0, max_age=90.0
    ) == {
        "status": "down", "reason": "runner_count_mismatch", "expected": 1,
        "healthy": 0, "observed": 1, "max_age_seconds": 90.0,
    }


def test_runner_fleet_result_does_not_expose_instance_identity_fields():
    check = runner_health.evaluate_runner_fleet(
        [{"ts": 995.0, "host_all": True, "owner": "runner-7", "host": "secret-host"}],
        expected=1,
        now=1000.0,
        max_age=90.0,
    )
    assert "owner" not in check
    assert "host" not in check
