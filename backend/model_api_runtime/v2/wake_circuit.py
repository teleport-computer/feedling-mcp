"""Persistent V2 automatic-wake circuit, reset only by user input/config saves.

Counters share the owned job's terminal transaction. Existing payment cooldown
and heartbeat backoff remain independent. Notification/trace publication uses
the existing best-effort sinks after commit; it is not a durable outbox.
"""
from __future__ import annotations

import logging

import db
import debug_trace
from accounts import registry as accounts_registry
from core import store as core_store
from notices import catalog, core as notices_core

FAILURE_THRESHOLD = 3
ERROR_CLASSES = frozenset({
    "quota_insufficient", "auth_invalid", "provider_account_expired",
})
LANES = frozenset({"heartbeat", "screen_watch"})
RESET_REASONS = frozenset({"new_chat", "credential_saved", "route_activated", "setup_saved"})
NOTICE_CODE = "wake_provider_circuit_open"
NOTICE_KEY = "v2_wake_provider_circuit"
log = logging.getLogger(__name__)


def record_result_on_cursor(cur, *, user_id: str, lane: str, job_id: int,
                            error_class: str = "") -> bool:
    """Return True only on a closed→open transition, while owning job terminalization.

    A job that began before the last explicit reset cannot re-open the circuit.
    Successful/non-target outcomes clear a closed streak, never an open circuit.
    Works with tuple and dict cursors (the RETURNING value is only presence).
    """
    if lane not in LANES:
        return False
    targeted = error_class in ERROR_CLASSES
    if targeted:
        cur.execute(
            "INSERT INTO v2_wake_schedule (user_id) VALUES (%s) "
            "ON CONFLICT (user_id) DO NOTHING", (user_id,),
        )
    cur.execute(
        "UPDATE v2_wake_schedule AS schedule SET "
        "provider_fail_streak=CASE WHEN %s THEN LEAST(provider_fail_streak+1,%s) ELSE 0 END, "
        "wake_circuit_opened_at=CASE WHEN %s AND provider_fail_streak+1 >= %s "
        "THEN clock_timestamp() ELSE NULL END, "
        "wake_circuit_reason=CASE WHEN %s AND provider_fail_streak+1 >= %s "
        "THEN %s ELSE '' END, updated_at=now() "
        "WHERE schedule.user_id=%s AND wake_circuit_opened_at IS NULL "
        "AND (wake_circuit_reset_at IS NULL OR wake_circuit_reset_at < "
        "(SELECT COALESCE(started_at,claimed_at,created_at) FROM agent_jobs WHERE id=%s)) "
        "RETURNING wake_circuit_opened_at",
        (targeted, FAILURE_THRESHOLD, targeted, FAILURE_THRESHOLD,
         targeted, FAILURE_THRESHOLD, error_class if targeted else "", user_id, job_id),
    )
    row = cur.fetchone()
    if row is None:
        return False
    opened = row.get("wake_circuit_opened_at") if isinstance(row, dict) else row[0]
    return opened is not None


def is_open(user_id: str) -> bool:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT wake_circuit_opened_at IS NOT NULL FROM v2_wake_schedule WHERE user_id=%s",
            (user_id,),
        ).fetchone()
    return bool(row and row[0])


def reset(user_id: str, *, reason: str) -> bool:
    """Best-effort reset after a primary save; V1 gets no schedule row/write.

    Fence old results even when closed. A failed reset is logged and may leave
    the circuit open; it must not turn an already persisted save into an error.
    """
    if reason not in RESET_REASONS:
        raise ValueError("invalid wake circuit reset reason")
    try:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                cur = conn.execute(
                    "INSERT INTO v2_wake_schedule (user_id) "
                    "SELECT user_id FROM v2_runtime_state WHERE user_id=%s "
                    "AND hosted_runtime_state='v2' ON CONFLICT (user_id) DO NOTHING",
                    (user_id,),
                )
                cur.execute(
                    "SELECT wake_circuit_opened_at FROM v2_wake_schedule "
                    "WHERE user_id=%s AND EXISTS (SELECT 1 FROM v2_runtime_state "
                    "WHERE user_id=%s AND hosted_runtime_state='v2') FOR UPDATE",
                    (user_id, user_id),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                was_open = row[0] is not None
                cur.execute(
                    "UPDATE v2_wake_schedule SET provider_fail_streak=0, "
                    "wake_circuit_opened_at=NULL,wake_circuit_reason='', "
                    "wake_circuit_reset_at=clock_timestamp(), "
                    "payment_cooldown_until=NULL,proactive_backoff_until=NULL, "
                    "proactive_fail_streak=0,updated_at=now() WHERE user_id=%s",
                    (user_id,),
                )
    except Exception:
        # The primary Chat/config write already succeeded; never overturn it.
        # Keep database details and user data out of this diagnostic.
        log.warning("wake circuit reset failed")
        return False
    if was_open:
        publish(user_id, opened=False, reason=reason)
    return was_open


def publish(user_id: str, *, opened: bool, reason: str = "") -> None:
    """Serialize best-effort notice updates against reset/new failure transitions.

    Re-read under the schedule lock so a delayed publisher cannot leave an open
    notice after recovery. A process crash between commit and this call can omit
    the notice; the persistent scheduler/worker gate remains authoritative.
    """
    try:
        store = core_store.get_store_per_load_mode(
            user_id,
            reason="wake circuit notice and trace sinks use direct DB reads, no store sections",
            bypass_legacy_hydration=True,
        )
        language = accounts_registry._get_user_archive_language(user_id) or ""
        with db.get_pool().connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT wake_circuit_opened_at,wake_circuit_reason,provider_fail_streak "
                    "FROM v2_wake_schedule WHERE user_id=%s FOR UPDATE", (user_id,),
                ).fetchone()
                if row is None or (row[0] is not None) != opened:
                    return
                if opened:
                    notices_core.emit(
                        store, source="model_api", error_class=NOTICE_CODE,
                        blame="user_provider", severity="warning",
                        user_text=catalog.user_text_for(NOTICE_CODE, language=language),
                        dedupe_key=NOTICE_KEY,
                    )
                else:
                    notices_core.resolve(store, NOTICE_KEY)
                event_type = "wake.circuit.open" if opened else "wake.circuit.recover"
                debug_trace.trace_event(
                    store, subsystem="v2", type=event_type,
                    status="warning" if opened else "ok",
                    detail={"reason": row[1] if opened else reason,
                            "streak": int(row[2]), "threshold": FAILURE_THRESHOLD},
                )
    except Exception:
        # No raw exception text: a notice/store failure may contain user data.
        log.warning("wake circuit visibility publication failed")
