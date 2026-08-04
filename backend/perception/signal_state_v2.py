"""Durable decision boundary for wake-capable Runtime V2 perception signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import math
import os
from typing import Any, Literal

import db


log = logging.getLogger("perception.signal_state_v2")


DecisionOutcome = Literal[
    "baseline_created",
    "duplicate",
    "stale",
    "conflict_same_ts",
    "unchanged",
    "changed",
    "error",
]


@dataclass(frozen=True)
class SignalObservationDecision:
    outcome: DecisionOutcome
    changed: bool
    fingerprint: str | None
    last_seen_at: datetime | None
    last_changed_at: datetime | None
    error_code: str = ""


def _error(code: str) -> SignalObservationDecision:
    return SignalObservationDecision(
        outcome="error",
        changed=False,
        fingerprint=None,
        last_seen_at=None,
        last_changed_at=None,
        error_code=code,
    )


def _decision(
    outcome: DecisionOutcome,
    *,
    fingerprint: str,
    last_seen_at: datetime,
    last_changed_at: datetime,
) -> SignalObservationDecision:
    return SignalObservationDecision(
        outcome=outcome,
        changed=outcome == "changed",
        fingerprint=fingerprint,
        last_seen_at=last_seen_at,
        last_changed_at=last_changed_at,
    )


def _observation_material(
    user_id: str,
    signal: str,
    value: Any,
    observed_at: float,
) -> tuple[str, datetime]:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip()
    if not secret:
        raise ValueError("secret_unset")
    timestamp = float(observed_at)
    if not math.isfinite(timestamp):
        raise ValueError("invalid_timestamp")
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    message = (
        "perception-signal-v2\0"
        + user_id
        + "\0"
        + signal
        + "\0"
        + canonical
    ).encode("utf-8")
    fingerprint = hmac.new(
        secret.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
    return fingerprint, datetime.fromtimestamp(timestamp, tz=timezone.utc)


def observe_signal_state(
    user_id: str,
    signal: str,
    value: Any,
    *,
    observed_at: float,
    source_event_id: str | None = None,
    allow_first_event: bool = False,
) -> SignalObservationDecision:
    try:
        fingerprint, observed = _observation_material(
            user_id, signal, value, observed_at
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return _error(str(exc) or "invalid_observation")

    try:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                previous = conn.execute(
                    "SELECT value_fingerprint, last_seen_at, last_changed_at, "
                    "source_event_id FROM perception_signal_state_v2 "
                    "WHERE user_id=%s AND signal=%s FOR UPDATE",
                    (user_id, signal),
                ).fetchone()
                if previous is not None:
                    (
                        previous_fingerprint,
                        previous_seen,
                        previous_changed,
                        previous_event_id,
                    ) = previous
                    event_id = source_event_id or None
                    if event_id is not None and event_id == previous_event_id:
                        return _decision(
                            "duplicate",
                            fingerprint=previous_fingerprint,
                            last_seen_at=previous_seen,
                            last_changed_at=previous_changed,
                        )
                    if observed < previous_seen:
                        return _decision(
                            "stale",
                            fingerprint=previous_fingerprint,
                            last_seen_at=previous_seen,
                            last_changed_at=previous_changed,
                        )
                    if observed == previous_seen:
                        if fingerprint != previous_fingerprint:
                            return _decision(
                                "conflict_same_ts",
                                fingerprint=previous_fingerprint,
                                last_seen_at=previous_seen,
                                last_changed_at=previous_changed,
                            )
                        conn.execute(
                            "UPDATE perception_signal_state_v2 "
                            "SET source_event_id=%s, updated_at=now() "
                            "WHERE user_id=%s AND signal=%s",
                            (event_id, user_id, signal),
                        )
                        return _decision(
                            "unchanged",
                            fingerprint=previous_fingerprint,
                            last_seen_at=previous_seen,
                            last_changed_at=previous_changed,
                        )
                    if fingerprint == previous_fingerprint:
                        conn.execute(
                            "UPDATE perception_signal_state_v2 "
                            "SET last_seen_at=%s, source_event_id=%s, "
                            "updated_at=now() "
                            "WHERE user_id=%s AND signal=%s",
                            (observed, event_id, user_id, signal),
                        )
                        return _decision(
                            "unchanged",
                            fingerprint=previous_fingerprint,
                            last_seen_at=observed,
                            last_changed_at=previous_changed,
                        )
                    conn.execute(
                        "UPDATE perception_signal_state_v2 "
                        "SET value_fingerprint=%s, last_seen_at=%s, "
                        "last_changed_at=%s, source_event_id=%s, "
                        "updated_at=now() "
                        "WHERE user_id=%s AND signal=%s",
                        (
                            fingerprint,
                            observed,
                            observed,
                            event_id,
                            user_id,
                            signal,
                        ),
                    )
                    return _decision(
                        "changed",
                        fingerprint=fingerprint,
                        last_seen_at=observed,
                        last_changed_at=observed,
                    )
                inserted = conn.execute(
                    "INSERT INTO perception_signal_state_v2 "
                    "(user_id, signal, value_fingerprint, last_seen_at, "
                    "last_changed_at, source_event_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (user_id, signal) DO NOTHING RETURNING 1",
                    (
                        user_id,
                        signal,
                        fingerprint,
                        observed,
                        observed,
                        source_event_id or None,
                    ),
                ).fetchone()
    except Exception as exc:  # fail closed at the storage boundary
        log.warning(
            "perception signal baseline write failed user_id=%s signal=%s: %s",
            user_id,
            signal,
            exc,
        )
        return _error("storage_error")

    if inserted is None:
        return observe_signal_state(
            user_id,
            signal,
            value,
            observed_at=observed_at,
            source_event_id=source_event_id,
            allow_first_event=allow_first_event,
        )
    return _decision(
        "changed" if allow_first_event else "baseline_created",
        fingerprint=fingerprint,
        last_seen_at=observed,
        last_changed_at=observed,
    )
