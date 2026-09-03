"""Pure runner-fleet health policy for public health reporting."""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future
import logging
import os
import threading
import time

import db
from asgi import health_executor
from fastapi import APIRouter
from hosted import agent_runtime_cutover
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Reject timestamps beyond a small allowance for ordinary clock skew. Without a
# lower age bound, a corrupt far-future row remains "fresh" for arbitrarily long
# and can mask a dead runner.
MAX_HEARTBEAT_FUTURE_SKEW_SECONDS = 5.0
RUNNER_HEALTH_PROBE_BACKOFF_SECONDS = 15.0

# Keep only the public fields needed to re-evaluate heartbeat freshness. A
# successful snapshot naturally expires under the same heartbeat max-age rule,
# so a transient database read failure cannot mask a dead runner indefinitely.
_runner_health_state = {
    "rows": None,
    "probe_in_flight": False,
    "retry_after": 0.0,
    "failure_reason": None,
}
_runner_health_state_lock = threading.Lock()


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
        age = now - ts
        if (
            ts > 0
            and -MAX_HEARTBEAT_FUTURE_SKEW_SECONDS <= age <= max_age
            and bool(heartbeat.get("host_all"))
        ):
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


def _unavailable(reason: str, *, expected: int) -> JSONResponse:
    """Report an unavailable observation without claiming the runner is down."""
    check = {
        "status": "unknown",
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


def _begin_probe() -> tuple[bool, str | None]:
    now = time.monotonic()
    with _runner_health_state_lock:
        if _runner_health_state["probe_in_flight"]:
            return False, str(
                _runner_health_state["failure_reason"]
                or "runner_health_check_in_progress"
            )
        retry_after = float(_runner_health_state["retry_after"] or 0.0)
        if now < retry_after:
            return False, str(
                _runner_health_state["failure_reason"]
                or "runner_health_check_backoff"
            )
        _runner_health_state["probe_in_flight"] = True
    return True, None


def _finish_probe() -> None:
    with _runner_health_state_lock:
        _runner_health_state["probe_in_flight"] = False


def _record_probe_success(instances) -> None:
    rows = tuple(
        {
            "ts": heartbeat.get("ts"),
            "host_all": bool(heartbeat.get("host_all")),
        }
        for heartbeat in instances
    )
    with _runner_health_state_lock:
        _runner_health_state["rows"] = rows
        _runner_health_state["retry_after"] = 0.0
        _runner_health_state["failure_reason"] = None


def _record_probe_failure(reason: str) -> None:
    with _runner_health_state_lock:
        _runner_health_state["retry_after"] = (
            time.monotonic() + RUNNER_HEALTH_PROBE_BACKOFF_SECONDS
        )
        _runner_health_state["failure_reason"] = reason


def _cached_rows():
    with _runner_health_state_lock:
        rows = _runner_health_state["rows"]
        if rows is None:
            return None
        return [dict(row) for row in rows]


def _probe_failure_response(reason: str, *, expected: int) -> JSONResponse:
    rows = _cached_rows()
    if rows is None:
        return _unavailable(reason, expected=expected)

    check = evaluate_runner_fleet(
        rows,
        expected=expected,
        now=time.time(),
        max_age=agent_runtime_cutover.supervisor_heartbeat_max_age(),
    )
    if check["status"] != "ok":
        return _unavailable(reason, expected=expected)

    check["source"] = "cached"
    check["probe_reason"] = reason
    return JSONResponse(
        {
            "ok": True,
            "status": "degraded",
            "checks": {"runner_fleet": check},
        },
        status_code=200,
    )


def _complete_probe(future: Future[list[dict]]) -> None:
    """Finalize probe state when its blocking callable really stops running."""
    try:
        instances = future.result()
    except FutureCancelledError:
        pass
    except Exception:  # noqa: BLE001 - caller maps public probe failures
        _record_probe_failure("runner_health_check_error")
    else:
        _record_probe_success(instances)
    finally:
        _finish_probe()


@router.get("/healthz/runner")
async def runner_healthz():
    """Public aggregate health signal for the expected runner fleet."""
    try:
        expected = parse_expected_runner_count(
            os.environ.get("FEEDLING_EXPECTED_RUNNER_COUNT")
        )
    except ValueError:
        return _unhealthy("invalid_expected_runner_count")

    should_probe, fallback_reason = _begin_probe()
    if not should_probe:
        return _probe_failure_response(str(fallback_reason), expected=expected)

    try:
        instances = await health_executor.run(
            db.list_supervisor_instance_heartbeats_for_health,
            timeout=db.HEALTH_DB_ACQUIRE_TIMEOUT_SECONDS,
            statement_timeout_ms=db.HEALTH_DB_STATEMENT_TIMEOUT_MS,
            completion_callback=_complete_probe,
        )
    except asyncio.CancelledError:
        _record_probe_failure("runner_health_check_cancelled")
        raise
    except health_executor.HealthCheckSaturated:
        reason = "runner_health_check_timeout"
        _record_probe_failure(reason)
        _finish_probe()
        return _probe_failure_response(reason, expected=expected)
    except health_executor.HealthCheckTimeout:
        reason = "runner_health_check_timeout"
        _record_probe_failure(reason)
        return _probe_failure_response(reason, expected=expected)
    except Exception:  # noqa: BLE001 - a probe must never expose DB internals
        logger.exception("runner health heartbeat query failed")
        reason = "runner_health_check_error"
        _record_probe_failure(reason)
        _finish_probe()
        return _probe_failure_response(reason, expected=expected)

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
