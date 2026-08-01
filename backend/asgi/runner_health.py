"""Pure runner-fleet health policy for public health reporting."""

from __future__ import annotations

import logging
import os
import time

import db
from fastapi import APIRouter
from hosted import agent_runtime_cutover
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def parse_expected_runner_count(raw: str | None) -> int:
    try:
        value = int(raw or "")
    except (TypeError, ValueError) as exc:
        raise ValueError("expected runner count must be a positive integer") from exc
    if value < 1 or str(value) != str(raw).strip():
        raise ValueError("expected runner count must be a positive integer")
    return value


def evaluate_runner_fleet(instances, *, expected, now, max_age):
    observed = len(instances)
    healthy = 0
    for heartbeat in instances:
        try:
            ts = float(heartbeat.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0 and now - ts <= max_age and bool(heartbeat.get("host_all")):
            healthy += 1
    ok = observed == expected and healthy == expected
    result = {
        "status": "ok" if ok else "down",
        "expected": expected,
        "healthy": healthy,
        "observed": observed,
        "max_age_seconds": float(max_age),
    }
    if not ok:
        result["reason"] = "runner_count_mismatch"
    return result


def _unhealthy(reason: str, *, expected: int | None = None) -> JSONResponse:
    """Return a fixed public failure response without runner identifiers."""
    check = {
        "status": "down",
        "reason": reason,
        "expected": expected,
        "healthy": 0,
        "observed": 0,
        "max_age_seconds": float(agent_runtime_cutover.supervisor_heartbeat_max_age()),
    }
    return JSONResponse(
        {
            "ok": False,
            "status": "unhealthy",
            "checks": {"runner_fleet": check},
        },
        status_code=503,
    )


@router.get("/healthz/runner")
async def runner_healthz():
    """Public aggregate health signal for the expected runner fleet."""
    try:
        expected = parse_expected_runner_count(
            os.environ.get("FEEDLING_EXPECTED_RUNNER_COUNT")
        )
    except ValueError:
        return _unhealthy("invalid_expected_runner_count")

    try:
        instances = await run_in_threadpool(db.list_supervisor_instance_heartbeats)
    except Exception:  # noqa: BLE001 - a probe must never expose DB internals
        logger.exception("runner health heartbeat query failed")
        return _unhealthy("runner_health_check_error", expected=expected)

    check = evaluate_runner_fleet(
        instances,
        expected=expected,
        now=time.time(),
        max_age=agent_runtime_cutover.supervisor_heartbeat_max_age(),
    )
    ok = check["status"] == "ok"
    return JSONResponse(
        {
            "ok": ok,
            "status": "healthy" if ok else "unhealthy",
            "checks": {"runner_fleet": check},
        },
        status_code=200 if ok else 503,
    )
