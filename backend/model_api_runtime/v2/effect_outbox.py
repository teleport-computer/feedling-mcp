"""Ownership/generation-fenced effect applier (spec A4). Pure of sinks: dispatch
is injected so this module never imports hosted/capabilities. Each pending row
is handled in its own transaction that locks the user's v2_runtime_state row
FOR UPDATE, so a concurrent cutover cannot slip between the authoritative
``state == v2`` + generation check and the apply.
"""
from __future__ import annotations
import json
import logging
from typing import Callable
import db
from model_api_runtime.v2 import effect_id as _effect_id


log = logging.getLogger("feedling.v2.effect_outbox")

_LEGACY_SENSITIVE_EFFECT_TYPES = frozenset({"memory", "identity", "schedule"})
_SCRUBBED_LEGACY_PAYLOAD = json.dumps({"legacy_payload_scrubbed": True})


def _sanitized_dispatch_error(exc: Exception) -> str:
    """Return an operator-useful error that cannot contain payload secrets."""
    if isinstance(exc, db.EffectDeliveryUncertainError):
        return "delivery_uncertain: unresolved sink claim requires reconciliation"
    return f"dispatch_failed:{type(exc).__name__}"


def enqueue_effect(*, job_id, user_id, effect_type, ordinal, expected_generation, payload) -> str:
    """Producer-side entry to the generation-fenced outbox (spec C5 / PR A A4). Derives the
    deterministic effect_id, enqueues (ON CONFLICT DO NOTHING = retry-idempotent), returns the id.
    PR C's tool loop is the first caller; the already-wired apply_pending_effects drains it."""
    eid = _effect_id.derive(job_id=job_id, effect_type=effect_type, ordinal=ordinal)
    db.effect_enqueue(eid, user_id, job_id, effect_type, expected_generation, payload)
    return eid


def apply_pending_effects(user_id: str, *, dispatch: Callable[[str, dict], None]) -> dict:
    applied = discarded = 0
    for row in db.effect_pending(user_id, due_prefix_only=True):
        eid = row["effect_id"]
        dispatch_failed = False
        dispatch_error_committed = False
        deferred_dispatch_error: Exception | None = None
        try:
            with db.get_pool().connection() as conn:
                # Pool connections are autocommit=True. Without this explicit
                # transaction the FOR UPDATE lock was released immediately after the
                # SELECT, allowing cutover to race between generation validation and
                # dispatch.
                error_staged_in_transaction = False
                with conn.transaction():
                    with conn.cursor() as cur:
                        # Shared across ordinary operations and nested sink
                        # connections; account deletion takes the exclusive form.
                        # Taking it before runtime-state closes the delete-vs-
                        # outer-state-vs-inner-lifecycle deadlock cycle.
                        db._lock_chat_user_fence_on_cursor(cur, user_id)
                        # Be robust to an effect enqueued before any runtime
                        # state read initialized this user.  Initializing and
                        # locking in the same transaction closes that first-
                        # cutover edge as well.
                        cur.execute(
                            "INSERT INTO v2_runtime_state (user_id) "
                            "SELECT %s WHERE EXISTS ("
                            "  SELECT 1 FROM users u WHERE u.user_id=%s"
                            ") ON CONFLICT (user_id) DO NOTHING",
                            (user_id, user_id),
                        )
                        cur.execute(
                            "SELECT hosted_runtime_state, runtime_generation "
                            "FROM v2_runtime_state "
                            "WHERE user_id=%s FOR UPDATE", (user_id,))
                        control = cur.fetchone()
                        current_state = str(control[0]) if control else "resident"
                        current = int(control[1]) if control else 0
                        # effect_pending() is only an ordered work-list hint.
                        # Re-read all dispatch fields while locking the row so
                        # a concurrent applier cannot leave us using a stale
                        # snapshot after it terminalized this effect.
                        cur.execute(
                            "SELECT effect_type, expected_generation, payload, status, "
                            "       next_attempt_at <= now() AS is_due "
                            "FROM v2_effect_outbox WHERE effect_id=%s FOR UPDATE",
                            (eid,))
                        effect = cur.fetchone()
                        if not effect or effect[3] != "pending":
                            continue
                        if not bool(effect[4]):
                            # Another applier may have failed this ordered head
                            # after our work-list snapshot. Do not bypass its
                            # retry delay or any effect queued behind it.
                            break
                        effect_type, expected_generation, payload, _status, _is_due = effect
                        # Holding this row lock across dispatch makes an
                        # in-flight V2 sink finish before cutover can commit;
                        # cutover bumps the generation, so late effects are
                        # discarded without touching a sink.
                        if (
                            current_state == "v2"
                            and int(expected_generation) == current
                        ):
                            try:
                                if isinstance(payload, str):
                                    payload = json.loads(payload)
                                if not isinstance(payload, dict):
                                    raise RuntimeError(
                                        "effect payload must be an object"
                                    )
                                # Never mutate the stored dict in place.
                                payload = dict(payload)
                            except RuntimeError as exc:
                                deferred_dispatch_error = exc
                            except Exception:
                                deferred_dispatch_error = RuntimeError(
                                    "invalid effect payload"
                                )
                            if deferred_dispatch_error is None:
                                payload["effect_id"] = eid
                                # The outer transaction already owns the
                                # same-user shared account-deletion fence. Mark
                                # that ownership while the synchronous nested
                                # sink commits so it does not queue a redundant
                                # shared advisory lock behind a waiting delete.
                                try:
                                    with db._chat_user_fence_held_by_outer_transaction(
                                        user_id
                                    ):
                                        dispatch(effect_type, payload)
                                except Exception as exc:
                                    deferred_dispatch_error = exc
                            if deferred_dispatch_error is not None:
                                dispatch_failed = True
                                # Persist retry/manual-reconciliation state in
                                # the SAME transaction that still owns the
                                # effect row lock. Releasing the lock first
                                # would let a concurrent sweeper replay an
                                # ambiguously delivered effect before its
                                # backoff/terminal marker became visible.
                                db._effect_record_error_on_cursor(
                                    cur,
                                    eid,
                                    _sanitized_dispatch_error(
                                        deferred_dispatch_error
                                    ),
                                    reconciliation_required=isinstance(
                                        deferred_dispatch_error,
                                        db.EffectDeliveryUncertainError,
                                    ),
                                )
                                error_staged_in_transaction = True
                            if deferred_dispatch_error is None:
                                if effect_type in _LEGACY_SENSITIVE_EFFECT_TYPES:
                                    cur.execute(
                                        "UPDATE v2_effect_outbox SET status='applied', "
                                        "applied_at=now(), last_error='', payload=%s::jsonb "
                                        "WHERE effect_id=%s",
                                        (_SCRUBBED_LEGACY_PAYLOAD, eid),
                                    )
                                else:
                                    cur.execute(
                                        "UPDATE v2_effect_outbox SET status='applied', applied_at=now(), "
                                        "last_error='' WHERE effect_id=%s", (eid,))
                                applied += 1
                        else:
                            if effect_type in _LEGACY_SENSITIVE_EFFECT_TYPES:
                                cur.execute(
                                    "UPDATE v2_effect_outbox SET status='discarded', "
                                    "payload=%s::jsonb WHERE effect_id=%s",
                                    (_SCRUBBED_LEGACY_PAYLOAD, eid),
                                )
                            else:
                                cur.execute(
                                    "UPDATE v2_effect_outbox SET status='discarded' WHERE effect_id=%s",
                                    (eid,))
                            discarded += 1
                # Reaching here means the transaction commit succeeded.
                dispatch_error_committed = error_staged_in_transaction
                if deferred_dispatch_error is not None:
                    raise deferred_dispatch_error
        except Exception as exc:
            if dispatch_failed and not dispatch_error_committed:
                try:
                    db.effect_record_error(
                        eid,
                        _sanitized_dispatch_error(exc),
                        reconciliation_required=isinstance(
                            exc, db.EffectDeliveryUncertainError),
                    )
                except Exception:  # preserve the original delivery failure
                    log.exception(
                        "[v2.effect_outbox] failed to persist dispatch error effect=%s",
                        eid,
                    )
            raise
    return {"applied": applied, "discarded": discarded}
