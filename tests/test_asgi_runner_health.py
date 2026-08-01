"""Pure health-policy tests for the runner fleet check."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
import db  # noqa: E402
from asgi import runner_health  # noqa: E402
from hosted import agent_runtime_cutover  # noqa: E402


def _asgi_get(path: str):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.get(path)
            return response.status_code, response.json()

    return asyncio.run(go())


@pytest.fixture()
def runner_health_dependencies(monkeypatch):
    monkeypatch.setattr(runner_health, "time", time, raising=False)
    monkeypatch.setattr(
        runner_health,
        "agent_runtime_cutover",
        agent_runtime_cutover,
        raising=False,
    )
    monkeypatch.setattr(runner_health.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        runner_health.agent_runtime_cutover,
        "supervisor_heartbeat_max_age",
        lambda: 90.0,
    )


def test_runner_health_route_returns_200_for_exact_healthy_fleet(
    monkeypatch, runner_health_dependencies,
):
    monkeypatch.setenv("FEEDLING_EXPECTED_RUNNER_COUNT", "1")
    monkeypatch.setattr(db, "list_supervisor_instance_heartbeats", lambda: [
        {"ts": 995.0, "host_all": True, "owner": "private-owner"},
    ])

    status, body = _asgi_get("/healthz/runner")

    assert status == 200
    assert body == {
        "ok": True,
        "status": "healthy",
        "checks": {"runner_fleet": {
            "status": "ok", "expected": 1, "healthy": 1,
            "observed": 1, "max_age_seconds": 90.0,
        }},
    }
    assert "private-owner" not in json.dumps(body)


def test_runner_health_route_returns_503_for_runner_count_mismatch(
    monkeypatch, runner_health_dependencies,
):
    monkeypatch.setenv("FEEDLING_EXPECTED_RUNNER_COUNT", "1")
    monkeypatch.setattr(db, "list_supervisor_instance_heartbeats", lambda: [])

    status, body = _asgi_get("/healthz/runner")

    assert status == 503
    assert body == {
        "ok": False,
        "status": "unhealthy",
        "checks": {"runner_fleet": {
            "status": "down", "reason": "runner_count_mismatch",
            "expected": 1, "healthy": 0, "observed": 0,
            "max_age_seconds": 90.0,
        }},
    }


@pytest.mark.parametrize("raw", [None, "not-an-integer"])
def test_runner_health_route_returns_503_for_invalid_expected_count(
    monkeypatch, runner_health_dependencies, raw,
):
    if raw is None:
        monkeypatch.delenv("FEEDLING_EXPECTED_RUNNER_COUNT", raising=False)
    else:
        monkeypatch.setenv("FEEDLING_EXPECTED_RUNNER_COUNT", raw)

    status, body = _asgi_get("/healthz/runner")

    assert status == 503
    assert body == {
        "ok": False,
        "status": "unhealthy",
        "checks": {"runner_fleet": {
            "status": "down", "reason": "invalid_expected_runner_count",
            "expected": None, "healthy": 0, "observed": 0,
            "max_age_seconds": 90.0,
        }},
    }


def test_runner_health_route_returns_503_for_heartbeat_query_error(
    monkeypatch, runner_health_dependencies,
):
    monkeypatch.setenv("FEEDLING_EXPECTED_RUNNER_COUNT", "1")

    def raise_db_error():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "list_supervisor_instance_heartbeats", raise_db_error)

    status, body = _asgi_get("/healthz/runner")

    assert status == 503
    assert body == {
        "ok": False,
        "status": "unhealthy",
        "checks": {"runner_fleet": {
            "status": "down", "reason": "runner_health_check_error",
            "expected": 1, "healthy": 0, "observed": 0,
            "max_age_seconds": 90.0,
        }},
    }


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
