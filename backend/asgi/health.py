"""Native /healthz router — liveness + dependency readiness probe.

Public, no-auth. Backward compatible: the legacy ``ok`` / ``mode`` fields are
still present so old probes keep working. On top of that it now reports the
real state of the backend's critical dependencies so an external heartbeat can
distinguish healthy / degraded / down without SSHing into the CVM:

- ``status``: "healthy" | "degraded" | "unhealthy" — the tri-state roll-up.
- HTTP code: 200 when the process can serve, 503 when a *critical* dependency
  (the primary Postgres) is down. Prod's backend container has no orchestrator
  healthcheck (see deploy/docker-compose.phala.yaml), so a 503 here reports an
  outage — it does NOT trigger a container restart.
- ``checks.*``: per-component detail for the heartbeat to key on.

Status policy:
- ``db`` is the only *critical* check. SELECT 1 failing (or the pool unable to
  hand out a connection within 2s) → unhealthy → 503.
- Degraded (still 200): pool saturated (a request is waiting or 0 available),
  the in-memory user registry loaded 0 users (the past lifespan-missing-
  load_users → global-401 failure mode), or the wake-bus listener is enabled
  but not actually listening.

The synchronous DB work runs in the threadpool so the 2s probe never blocks the
event loop of this async worker.
"""

from __future__ import annotations

import os
import time

import db
from asgi import health_executor
from fastapi import APIRouter
from starlette.responses import JSONResponse

router = APIRouter()

# Process start, captured at import so uptime reflects this worker's lifetime.
_STARTED_AT = time.time()


def _release() -> dict:
    """Build/release metadata, same env source as enclave config.RELEASE.
    Defaults to obvious ``dev`` placeholders so an un-injected build is honest
    rather than fabricated."""
    return {
        "git_commit": os.environ.get("FEEDLING_GIT_COMMIT", "dev"),
        "image_digest": os.environ.get("FEEDLING_IMAGE_DIGEST", "sha256:dev"),
        "built_at": os.environ.get("FEEDLING_BUILT_AT", "dev"),
    }


def _worker() -> dict:
    wid = None
    try:
        from core import wake_bus

        wid = wake_bus.WORKER_ID
    except Exception:  # noqa: BLE001 — worker id is cosmetic, never fail health
        pass
    return {"id": wid, "pid": os.getpid()}


def _db_checks() -> tuple[dict, dict]:
    """Return ``(db_check, db_pool_check)``. ``db`` is critical; pool saturation
    is a degraded signal. Runs synchronous DB I/O — call from the threadpool."""
    probe = db.health_probe(
        timeout=db.HEALTH_DB_ACQUIRE_TIMEOUT_SECONDS,
        statement_timeout_ms=db.HEALTH_DB_STATEMENT_TIMEOUT_MS,
    )
    db_check: dict = {
        "status": "ok" if probe["ok"] else "down",
        "latency_ms": probe["latency_ms"],
    }
    if not probe["ok"]:
        db_check["error"] = probe["error"]

    try:
        stats = db.get_pool().get_stats()
        available = stats.get("pool_available")
        waiting = stats.get("requests_waiting", 0) or 0
        saturated = waiting > 0 or available == 0
        pool_check: dict = {
            "status": "saturated" if saturated else "ok",
            "size": stats.get("pool_size"),
            "available": available,
            "waiting": waiting,
            "max": stats.get("pool_max"),
        }
    except Exception as e:  # noqa: BLE001
        pool_check = {"status": "unknown", "error": str(e)[:200]}
    return db_check, pool_check


def _registry_check() -> dict:
    try:
        from accounts import registry

        n = len(registry._users)
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "error": str(e)[:200]}
    # 0 users loaded means load_users() never ran / failed — every key resolves
    # to None → global 401s. Surface it as degraded, not silently ok.
    return {"status": "ok" if n > 0 else "empty", "users_loaded": n}


def _wake_bus_check() -> dict:
    try:
        from core import wake_bus

        enabled = wake_bus._enabled()
        listening = bool(wake_bus._listener_started)
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "error": str(e)[:200]}
    if not enabled:
        return {"status": "disabled", "enabled": False, "listening": listening}
    return {
        "status": "ok" if listening else "not_listening",
        "enabled": True,
        "listening": listening,
    }


def _gather_checks() -> dict:
    """All synchronous checks in one threadpool hop (keeps the event loop free)."""
    db_check, pool_check = _db_checks()
    return {
        "db": db_check,
        "db_pool": pool_check,
        "registry": _registry_check(),
        "wake_bus": _wake_bus_check(),
    }


def _deadline_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "mode": "multi_tenant",
            "status": "unhealthy",
            "release": _release(),
            "uptime_s": round(time.time() - _STARTED_AT, 1),
            "worker": _worker(),
            "checks": {
                "db": {"status": "down", "error": "health_check_timeout"},
            },
        },
        status_code=503,
    )


@router.get("/healthz")
async def healthz():
    """Liveness + readiness probe. Public, no auth — used by heartbeats/compose."""
    try:
        checks = await health_executor.run(_gather_checks)
    except health_executor.HealthCheckTimeout:
        return _deadline_response()

    critical_ok = checks["db"]["status"] == "ok"
    degraded = (
        checks["db_pool"].get("status") == "saturated"
        or checks["registry"].get("status") == "empty"
        or checks["wake_bus"].get("status") == "not_listening"
    )
    if not critical_ok:
        status = "unhealthy"
    elif degraded:
        status = "degraded"
    else:
        status = "healthy"

    body = {
        "ok": critical_ok,  # legacy field: true iff serving (200)
        "mode": "multi_tenant",  # legacy field
        "status": status,
        "release": _release(),
        "uptime_s": round(time.time() - _STARTED_AT, 1),
        "worker": _worker(),
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if critical_ok else 503)
