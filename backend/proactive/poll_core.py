"""Framework-neutral proactive job long-poll core (ASGI-migration plan §7.4).

The "reclaim stale claims, compute the pollable pending jobs, and shape the
response" logic for `/v1/proactive/jobs/poll`, lifted out of the Flask route so
the forthcoming FastAPI async poll route (plan §9.2) reuses **identical**
payload / claim / clamp semantics. Only the *waiting* primitive stays in the
route (Flask `threading.Event` today, an asyncio waiter under ASGI); everything
here is pure store access with no Flask/FastAPI request object.

This code was relocated verbatim from `proactive/routes.py` — behavior is
unchanged; `routes.py` now calls in here.
"""

from __future__ import annotations

import time
from datetime import datetime

from proactive import capture_jobs, resident_runtime_v2, service

# A stale resident wake claim older than this is reclaimed to `pending` so a
# survivor consumer can pick it up (hosted consumers are exempt — they manage
# their own lease).
RESIDENT_WAKE_LEASE_SEC = 600.0
_HOSTED_CONSUMER_IDS = frozenset({"hosted_runtime", "hosted_runtime_v2"})


def _job_age_ref_epoch(job: dict) -> float:
    for key in ("realizing_at", "claimed_at", "ts"):
        raw = job.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw).timestamp()
            except ValueError:
                pass
    return 0.0


def reclaim_stale_resident_jobs(store, *, now: float | None = None) -> int:
    now = time.time() if now is None else float(now)
    reclaimed = 0
    for job in store.list_proactive_jobs(limit=100):
        status = str(job.get("status") or "")
        if status not in {"claimed", "realizing"}:
            continue
        consumer_id = str(job.get("consumer_id") or "")
        if consumer_id in _HOSTED_CONSUMER_IDS:
            continue
        age_ref = _job_age_ref_epoch(job)
        if not age_ref or now - age_ref <= RESIDENT_WAKE_LEASE_SEC:
            continue
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        patched = store.update_proactive_job(job_id, {
            "status": "pending",
            "status_reason": "resident_stale_claim_recovered",
            "consumer_id": f"recovered:{consumer_id}"[:160] if consumer_id else "recovered:resident",
            "recovered_at": datetime.fromtimestamp(now).isoformat(),
        }, only_if_status=status)
        if patched is not None:
            reclaimed += 1
    return reclaimed


def _with_resident_runtime_v2(job: dict, runtime_profile: dict) -> dict:
    out = dict(job or {})
    out["runtime_v2"] = dict(runtime_profile or {})
    return out


def _is_introduction_job(job: dict) -> bool:
    return str((job or {}).get("job_kind") or "").strip() == "introduction"


def _is_ts_watermark_exempt_job(job: dict) -> bool:
    # Jobs that must be recovered by status, ignoring the consumer's ts
    # watermark. The resident / agent-runner consumer seeds its proactive
    # checkpoint to "now" on first boot, so any pending job created before that
    # first poll (introduction posted at spawn; memory_capture/dream/migrate
    # enqueued while the user was chatting before the consumer came up) has a ts
    # below `since` and would otherwise be skipped forever (prod: a 17:58 capture
    # job stayed pending while later dream jobs were claimed).
    return _is_introduction_job(job) or capture_jobs.is_memory_maintenance_job(job)


def _resident_pending_watermark_exempt_jobs(store, *, limit: int, runtime_profile: dict) -> list[dict]:
    out: list[dict] = []
    for job in store.list_proactive_jobs(since_epoch=0, limit=0):
        if str(job.get("status") or "pending") != "pending":
            continue
        if not _is_ts_watermark_exempt_job(job):
            continue
        out.append(_with_resident_runtime_v2(job, runtime_profile))
        if len(out) >= limit:
            break
    return out


def _settings_v2_for_store(store):
    try:
        from proactive.store_v2 import DBProactiveSettingsStoreV2

        return DBProactiveSettingsStoreV2().load(store.user_id)
    except Exception:
        return store.load_proactive_settings()


def _resident_wake_control_decision_v2(store, job: dict):
    if str((job or {}).get("source") or service.PROACTIVE_JOB_SOURCE) != service.PROACTIVE_JOB_SOURCE:
        return None
    try:
        from proactive.adapters_v2 import wake_event_v2_from_legacy_job
        from proactive.controls_v2 import evaluate_wake_control_v2

        event = wake_event_v2_from_legacy_job(store.user_id, job)
        return evaluate_wake_control_v2(
            event.source,
            trigger=event.trigger,
            manual=event.manual,
            settings=_settings_v2_for_store(store),
        )
    except Exception:
        return None


def resident_pollable_pending_jobs(store, *, since: float, limit: int, runtime_profile: dict) -> list[dict]:
    # The post-spawn introduction job is intentionally created by the supervisor
    # right after process spawn. On a first boot the resident consumer seeds its
    # proactive checkpoint to "now" to avoid replaying historical hidden jobs, so
    # this one bootstrap job must be recovered by status rather than by ts > since.
    out: list[dict] = _resident_pending_watermark_exempt_jobs(
        store,
        limit=limit,
        runtime_profile=runtime_profile,
    )
    seen = {str(job.get("job_id") or "") for job in out}
    if len(out) >= limit:
        return out
    read_limit = max(limit, 100)
    for job in store.list_proactive_jobs(since_epoch=since, limit=read_limit):
        job_id = str(job.get("job_id") or "")
        if job_id and job_id in seen:
            continue
        if str(job.get("status") or "pending") != "pending":
            continue
        # introduction + memory-maintenance jobs are already recovered above
        # (status-based, ts-watermark-exempt); skip them here so they aren't
        # wake-gated by the v2 controls below.
        if _is_ts_watermark_exempt_job(job):
            continue
        decision = _resident_wake_control_decision_v2(store, job)
        if decision is not None and not decision.accepted:
            job_id = str(job.get("job_id") or "")
            if job_id:
                store.update_proactive_job(
                    job_id,
                    {
                        "status": "skipped",
                        "status_reason": decision.reason,
                        "wake_result": decision.reason,
                        "agent_action": decision.reason,
                        "agent_action_status": "resident_poll_wake_gate_v2",
                    },
                    only_if_status="pending",
                )
            continue
        out.append(_with_resident_runtime_v2(job, runtime_profile))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Poll orchestration primitives shared by the Flask route and the ASGI route.
# --------------------------------------------------------------------------- #

# clamp bounds for the `limit` query arg (kept identical to the legacy route).
LIMIT_DEFAULT = 20
LIMIT_MIN = 1
LIMIT_MAX = 100


def clamp_limit(limit: int) -> int:
    return max(LIMIT_MIN, min(int(limit), LIMIT_MAX))


def runtime_profile(store) -> dict:
    return resident_runtime_v2.resident_runtime_v2_public_profile(store)


def build_response(*, jobs: list, runtime_profile: dict, timed_out: bool) -> dict:
    """The `/v1/proactive/jobs/poll` response contract (locked for parity)."""
    return {"jobs": jobs, "runtime_v2": runtime_profile, "timed_out": timed_out}
