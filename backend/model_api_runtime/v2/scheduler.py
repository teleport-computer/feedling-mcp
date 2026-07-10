"""V2 proactive scheduler tick (Task 4 of D3 proactive wake lanes plan).

Pure module: every side effect (which users are due, the wake decision,
enqueueing a heartbeat job, advancing the schedule) is injected via a
`deps` object, so this is fully unit-testable with fakes and carries no
IO or wall-clock reads of its own.

Dependency direction: this module must NOT import `hosted`/`agent_runtime`/
`proactive`. The real wake decision + enqueue implementations are wired in
by serve_worker.py (see test_v2_dependency_direction.py).
"""
import logging

logger = logging.getLogger(__name__)

_DEFAULT_WAKE_INTERVAL_SEC = 7200


def run_scheduler_tick(deps, *, now: float) -> dict:
    """One scheduler pass over all users currently due for a heartbeat wake.

    `deps` provides:
      - due_users() -> list[str]           # users whose heartbeat is due & not in payment cooldown
      - wake_decision(user_id) -> dict      # {"should_wake": bool, "wake_interval_sec": int, "block_reason": str}
      - enqueue_heartbeat(user_id) -> None  # enqueue a heartbeat agent_job (single-flight de-dupes)
      - advance_heartbeat(user_id, next_at_epoch: float) -> None  # persist next_heartbeat_at

    For each due user: consult wake_decision. If should_wake -> enqueue_heartbeat
    AND advance. If NOT should_wake (blocked/weak/unactivated) -> advance ONLY,
    do NOT enqueue (zero-burn: no job, no model call). Always advance so a due
    user isn't reconsidered every tick.

    `now` is an injected epoch float (no wall-clock reads here -- keeps this
    deterministic and unit-testable).

    Returns {"considered": N, "enqueued": E, "skipped": S}.
    """
    considered = 0
    enqueued = 0
    skipped = 0

    for user_id in deps.due_users():
        considered += 1
        try:
            decision = deps.wake_decision(user_id)
            interval = int(decision.get("wake_interval_sec") or _DEFAULT_WAKE_INTERVAL_SEC)
            next_at = now + interval

            if decision.get("should_wake"):
                deps.enqueue_heartbeat(user_id)
                enqueued += 1
            else:
                skipped += 1

            deps.advance_heartbeat(user_id, next_at)
        except Exception:
            skipped += 1
            logger.exception("scheduler: error processing wake tick for user %s", user_id)

    return {"considered": considered, "enqueued": enqueued, "skipped": skipped}
