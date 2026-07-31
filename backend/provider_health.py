"""Runtime-neutral provider health state and proactive admission policy."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import db
from core import util as core_util
from notices import catalog as notices_catalog


log = logging.getLogger(__name__)

PROVIDER_STATE_OK = "ok"
PROVIDER_STATE_NEEDS_USER_ACTION = "needs_user_action"
PROVIDER_NEEDS_USER_ACTION_REASON = "provider_needs_user_action"
UNHEALTHY_AFTER_SEC = 48 * 60 * 60
USER_PROVIDER_CONFIRM_SEC = 60 * 60
PROBE_INTERVAL_SEC = 24 * 60 * 60

# A provider that answers slowly never fails, so none of the fields above can
# describe it.  60 s is compaction's own per-call timeout: once the smoothed
# round-trip crosses it, background folds start timing out and a backlog can
# only grow.  The average is exponential so one slow answer cannot trip it and
# one fast answer cannot clear it.
SLOW_PROVIDER_MS = 60_000
_LATENCY_EWMA_ALPHA = 0.3


@dataclass(frozen=True)
class ProactiveAdmission:
    allowed: bool
    block_reason: str = ""
    probe: bool = False


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return core_util._to_epoch(value)


def _utc_datetime(epoch: float) -> datetime:
    return datetime.fromtimestamp(float(epoch), timezone.utc)


def _current_dict(row: Any) -> dict:
    if row is None:
        return {}
    return {
        "provider_state": str(row[0] or PROVIDER_STATE_OK),
        "last_provider_success_at": _epoch(row[1]),
        "last_provider_failure_at": _epoch(row[2]),
        "last_provider_error_class": str(row[3] or ""),
        "last_provider_error_blame": str(row[4] or ""),
        "user_provider_failure_started_at": _epoch(row[5]),
        "last_probe_at": _epoch(row[6]),
        # Optional tail: rows read by callers that predate the latency column
        # (or select fewer fields) simply carry no sample.
        "recent_latency_ms": float(row[7] or 0.0) if len(row) > 7 else 0.0,
    }


def evolve_failure(
    current: dict,
    *,
    error_class: str,
    blame: str,
    now: float,
    route_selected_at: float,
) -> dict:
    """Pure failure transition used by persistence and boundary tests.

    A user-provider failure starts or extends the latest homogeneous failure
    segment.  The 48-hour clock is still based on the last real success (or the
    route-selection time when no success has ever been recorded), never on a
    failure count or on agent-message timestamps.  After any transient/system
    failure, the user-provider segment must remain homogeneous for an hour so a
    single recovery-time 401 cannot shift blame to the user.
    """
    out = dict(current or {})
    previous_state = str(out.get("provider_state") or PROVIDER_STATE_OK)
    previous_blame = str(out.get("last_provider_error_blame") or "")
    last_success = _epoch(out.get("last_provider_success_at"))
    previous_failure_at = _epoch(out.get("last_provider_failure_at"))
    baseline = last_success or max(0.0, float(route_selected_at or 0.0))
    if baseline <= 0:
        baseline = float(now)

    out.update(
        {
            "provider_state": previous_state,
            "last_provider_failure_at": float(now),
            "last_provider_error_class": str(error_class or "unknown")[:64],
            "last_provider_error_blame": str(blame or "system")[:64],
        }
    )

    if blame == "user_provider":
        segment_started = _epoch(out.get("user_provider_failure_started_at"))
        if previous_blame != "user_provider" or segment_started <= 0:
            # With no prior failure evidence, every observed failure in the
            # 48-hour window is user-provider, so the no-success baseline is
            # also the homogeneous-segment baseline.  A preceding transient
            # failure starts a fresh confirmation segment at this event.
            segment_started = (
                baseline if previous_failure_at <= baseline else float(now)
            )
        out["user_provider_failure_started_at"] = segment_started
        if (
            float(now) - baseline >= UNHEALTHY_AFTER_SEC
            and float(now) - segment_started >= USER_PROVIDER_CONFIRM_SEC
        ):
            out["provider_state"] = PROVIDER_STATE_NEEDS_USER_ACTION
            if previous_state != PROVIDER_STATE_NEEDS_USER_ACTION:
                # Entering the state consumes the current failed attempt as the
                # initial probe.  The next automatic probe is due in 24 hours.
                out["last_probe_at"] = float(now)
    else:
        out["user_provider_failure_started_at"] = 0.0

    return out


def _latency_ewma(previous: Any, sample_ms: float) -> float:
    prior = float(previous or 0.0)
    if prior <= 0:
        return float(sample_ms)
    return _LATENCY_EWMA_ALPHA * float(sample_ms) + (1.0 - _LATENCY_EWMA_ALPHA) * prior


def provider_is_slow(state: dict) -> bool:
    """Whether this route's smoothed round-trip is past the fold timeout.

    Deliberately independent of ``provider_state``: a slow provider is still
    usable and must not be blocked from admission.  This answers "why is
    everything taking minutes", which the failure fields cannot.
    """
    return float((state or {}).get("recent_latency_ms") or 0.0) > SLOW_PROVIDER_MS


def evolve_success(
    current: dict, *, now: float, latency_ms: float | None = None
) -> dict:
    out = dict(current or {})
    out.update(
        {
            "provider_state": PROVIDER_STATE_OK,
            "last_provider_success_at": float(now),
            "user_provider_failure_started_at": 0.0,
            "last_probe_at": 0.0,
        }
    )
    if latency_ms is not None and float(latency_ms) >= 0:
        out["recent_latency_ms"] = _latency_ewma(
            out.get("recent_latency_ms"), float(latency_ms)
        )
    return out


def _route_selected_at(user_id: str) -> float:
    route = db.get_blob(user_id, "onboarding_route") or {}
    selected = core_util._to_epoch(route.get("selected_at"))
    if selected > 0:
        return selected
    # Pre-routing rows may not have onboarding_route.  Route creation is a
    # conservative fallback; never use last_agent_at because historical
    # fallback bubbles polluted that signal.
    active = db.model_api_active_route(user_id) or {}
    return core_util._to_epoch(active.get("created_at"))


def record_success(
    user_id: str, *, now: float | None = None, latency_ms: float | None = None
) -> bool:
    """Record one usable provider response and immediately restore health.

    ``latency_ms`` folds into an exponential average (see :func:`evolve_success`)
    so a slow-but-working route is visible. Callers that cannot measure omit it,
    and the stored average is then carried forward untouched — a caller without
    a stopwatch must not be able to erase the signal.
    """
    ts = float(time.time() if now is None else now)
    sample = None if latency_ms is None else max(0.0, float(latency_ms))
    # The EWMA is folded inside the UPSERT rather than read-modify-written, so
    # concurrent turns for one user cannot lose a sample to a lost update.
    latency_clause = "recent_latency_ms = provider_health.recent_latency_ms"
    params: tuple = (user_id, _utc_datetime(ts), _utc_datetime(ts), None)
    if sample is not None:
        latency_clause = (
            "recent_latency_ms = CASE"
            "  WHEN COALESCE(provider_health.recent_latency_ms, 0) <= 0 THEN %s"
            "  ELSE %s * %s + (1 - %s) * provider_health.recent_latency_ms"
            " END"
        )
        params = (
            user_id,
            _utc_datetime(ts),
            _utc_datetime(ts),
            sample,
            sample,
            _LATENCY_EWMA_ALPHA,
            sample,
            _LATENCY_EWMA_ALPHA,
        )
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                f"""
                INSERT INTO provider_health (
                  user_id, provider_state, last_provider_success_at,
                  user_provider_failure_started_at, last_probe_at, updated_at,
                  recent_latency_ms
                )
                VALUES (%s, 'ok', %s, NULL, NULL, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                  provider_state = 'ok',
                  last_provider_success_at = EXCLUDED.last_provider_success_at,
                  user_provider_failure_started_at = NULL,
                  last_probe_at = NULL,
                  updated_at = EXCLUDED.updated_at,
                  {latency_clause}
                """,
                params,
            )
        return True
    except Exception as exc:  # observability must never fail a provider turn
        log.error("provider health success write failed user=%s: %s", user_id, exc)
        return False


def record_failure(
    user_id: str,
    *,
    error_class: str,
    now: float | None = None,
    route_selected_at: float | None = None,
) -> bool:
    """Record a classified provider failure and apply the 48-hour policy."""
    ts = float(time.time() if now is None else now)
    klass = str(error_class or "unknown")[:64]
    blame = notices_catalog.blame_for(klass)
    selected_at = (
        float(route_selected_at)
        if route_selected_at is not None
        else _route_selected_at(user_id)
    )
    try:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                # Lock a real row even for the first observation.  Without this
                # insert-first step, concurrent first success/failure writes can
                # both observe "no row" and let the later UPSERT erase the
                # success baseline.
                conn.execute(
                    """
                    INSERT INTO provider_health (user_id)
                    VALUES (%s)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (user_id,),
                )
                row = conn.execute(
                    """
                    SELECT provider_state, last_provider_success_at,
                           last_provider_failure_at, last_provider_error_class,
                           last_provider_error_blame,
                           user_provider_failure_started_at, last_probe_at,
                           recent_latency_ms
                    FROM provider_health
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (user_id,),
                ).fetchone()
                next_state = evolve_failure(
                    _current_dict(row),
                    error_class=klass,
                    blame=blame,
                    now=ts,
                    route_selected_at=selected_at,
                )
                conn.execute(
                    """
                    INSERT INTO provider_health (
                      user_id, provider_state, last_provider_success_at,
                      last_provider_failure_at, last_provider_error_class,
                      last_provider_error_blame,
                      user_provider_failure_started_at, last_probe_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                      provider_state = EXCLUDED.provider_state,
                      last_provider_success_at = EXCLUDED.last_provider_success_at,
                      last_provider_failure_at = EXCLUDED.last_provider_failure_at,
                      last_provider_error_class = EXCLUDED.last_provider_error_class,
                      last_provider_error_blame = EXCLUDED.last_provider_error_blame,
                      user_provider_failure_started_at =
                        EXCLUDED.user_provider_failure_started_at,
                      last_probe_at = EXCLUDED.last_probe_at,
                      updated_at = EXCLUDED.updated_at
                    """,
                    (
                        user_id,
                        next_state["provider_state"],
                        (
                            _utc_datetime(_epoch(next_state.get("last_provider_success_at")))
                            if _epoch(next_state.get("last_provider_success_at")) > 0
                            else None
                        ),
                        _utc_datetime(ts),
                        next_state["last_provider_error_class"],
                        next_state["last_provider_error_blame"],
                        (
                            _utc_datetime(
                                _epoch(
                                    next_state.get(
                                        "user_provider_failure_started_at"
                                    )
                                )
                            )
                            if _epoch(
                                next_state.get("user_provider_failure_started_at")
                            )
                            > 0
                            else None
                        ),
                        (
                            _utc_datetime(_epoch(next_state.get("last_probe_at")))
                            if _epoch(next_state.get("last_probe_at")) > 0
                            else None
                        ),
                        _utc_datetime(ts),
                    ),
                )
        return True
    except Exception as exc:  # observability must never fail a provider turn
        log.error("provider health failure write failed user=%s: %s", user_id, exc)
        return False


def error_class_for_exception(exc: BaseException) -> str:
    """Classify a V2 provider exception with the shared backend catalog."""
    return notices_catalog.classify_upstream(str(exc or "")) or "unknown"


def proactive_admission(
    user_id: str,
    *,
    now: float | None = None,
) -> ProactiveAdmission:
    """Block unhealthy proactive wakes, except one atomically claimed daily probe.

    Database failures fail open.  This policy is a resource optimization, not an
    authorization boundary, so an observability outage must not become a fleet
    wide proactive outage.
    """
    ts = float(time.time() if now is None else now)
    try:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT provider_state, last_probe_at
                    FROM provider_health
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (user_id,),
                ).fetchone()
                if row is None or str(row[0] or "") != PROVIDER_STATE_NEEDS_USER_ACTION:
                    return ProactiveAdmission(allowed=True)
                last_probe_at = _epoch(row[1])
                if last_probe_at <= 0 or ts - last_probe_at >= PROBE_INTERVAL_SEC:
                    conn.execute(
                        """
                        UPDATE provider_health
                        SET last_probe_at = %s, updated_at = %s
                        WHERE user_id = %s
                        """,
                        (_utc_datetime(ts), _utc_datetime(ts), user_id),
                    )
                    return ProactiveAdmission(allowed=True, probe=True)
                return ProactiveAdmission(
                    allowed=False,
                    block_reason=PROVIDER_NEEDS_USER_ACTION_REASON,
                )
    except Exception as exc:
        log.error("provider health gate read failed user=%s: %s", user_id, exc)
        return ProactiveAdmission(allowed=True)
