"""Native /healthz router — process liveness + in-memory readiness probe.

Public, no-auth. Backward compatible: the legacy ``ok`` / ``mode`` fields are
still present so old probes keep working. On top of that it now reports the
process-local state so an external heartbeat can distinguish healthy from
degraded without SSHing into the CVM:

- ``status``: "healthy" | "degraded" — the process-local roll-up.
- HTTP code: 200 whenever the process can serve the probe.
- ``checks.*``: per-component detail for the heartbeat to key on.

Status policy:
- Degraded (still 200): the in-memory user registry loaded 0 users (the past
  lifespan-missing-load_users → global-401 failure mode), or the wake-bus
  listener is enabled but not actually listening.

The probe deliberately performs no database I/O and does not inspect the
business connection pool. A saturated pool must not make the liveness endpoint
wait behind user traffic or return a false outage.
"""

from __future__ import annotations

import os
import time

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
    """Return the process-local checks without touching external dependencies."""
    return {
        "registry": _registry_check(),
        "wake_bus": _wake_bus_check(),
    }


@router.get("/healthz")
async def healthz():
    """Process liveness probe. Public, no auth — used by heartbeats/compose."""
    checks = _gather_checks()

    degraded = (
        checks["registry"].get("status") == "empty"
        or checks["wake_bus"].get("status") == "not_listening"
    )
    status = "degraded" if degraded else "healthy"

    body = {
        "ok": True,
        "mode": "multi_tenant",  # legacy field
        "status": status,
        "release": _release(),
        "uptime_s": round(time.time() - _STARTED_AT, 1),
        "worker": _worker(),
        "checks": checks,
    }
    return JSONResponse(body, status_code=200)
