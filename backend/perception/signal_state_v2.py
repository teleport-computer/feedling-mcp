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


def _fingerprint_key_id(secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        b"perception-signal-key-id-v1",
        hashlib.sha256,
    ).hexdigest()


def _unknown_key_rebaseline_enabled() -> bool:
    return os.environ.get(
        "FEEDLING_PERCEPTION_REBASELINE_UNKNOWN_KEYS", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _value_fingerprint(
    secret: str,
    user_id: str,
    signal: str,
    canonical: str,
) -> str:
    message = (
        "perception-signal-v2\0"
        + user_id
        + "\0"
        + signal
        + "\0"
        + canonical
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()


def _observation_material(
    user_id: str,
    signal: str,
    value: Any,
    observed_at: float,
) -> tuple[str, str, tuple[str, str] | None, datetime]:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip()
    if not secret:
        raise ValueError("secret_unset")
    previous_secret = os.environ.get(
        "FEEDLING_RUNTIME_TOKEN_SECRET_PREVIOUS", ""
    ).strip()
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
    current = _value_fingerprint(secret, user_id, signal, canonical)
    current_key_id = _fingerprint_key_id(secret)
    previous = None
    if previous_secret and previous_secret != secret:
        previous = (
            _value_fingerprint(previous_secret, user_id, signal, canonical),
            _fingerprint_key_id(previous_secret),
        )
    return (
        current,
        current_key_id,
        previous,
        datetime.fromtimestamp(timestamp, tz=timezone.utc),
    )


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
        fingerprint, fingerprint_key_id, previous_material, observed = _observation_material(
            user_id, signal, value, observed_at
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return _error(str(exc) or "invalid_observation")

    try:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                previous = conn.execute(
                    "SELECT value_fingerprint, last_seen_at, last_changed_at, "
                    "source_event_id, fingerprint_key_id "
                    "FROM perception_signal_state_v2 "
                    "WHERE user_id=%s AND signal=%s FOR UPDATE",
                    (user_id, signal),
                ).fetchone()
                if previous is not None:
                    (
                        previous_fingerprint,
                        previous_seen,
                        previous_changed,
                        previous_event_id,
                        previous_key_id,
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
                    if previous_key_id == fingerprint_key_id:
                        comparable_fingerprint = fingerprint
                        migrate_key = False
                    elif (
                        previous_material is not None
                        and previous_key_id == previous_material[1]
                    ):
                        comparable_fingerprint = previous_material[0]
                        migrate_key = True
                    else:
                        if (
                            _unknown_key_rebaseline_enabled()
                            and observed > previous_seen
                        ):
                            conn.execute(
                                "UPDATE perception_signal_state_v2 "
                                "SET value_fingerprint=%s, "
                                "fingerprint_key_id=%s, last_seen_at=%s, "
                                "last_changed_at=%s, source_event_id=%s, "
                                "updated_at=now() "
                                "WHERE user_id=%s AND signal=%s",
                                (
                                    fingerprint,
                                    fingerprint_key_id,
                                    observed,
                                    observed,
                                    event_id,
                                    user_id,
                                    signal,
                                ),
                            )
                            return _decision(
                                "baseline_created",
                                fingerprint=fingerprint,
                                last_seen_at=observed,
                                last_changed_at=observed,
                            )
                        return _error("unknown_fingerprint_key")
                    if observed == previous_seen:
                        if not hmac.compare_digest(
                            comparable_fingerprint, previous_fingerprint
                        ):
                            return _decision(
                                "conflict_same_ts",
                                fingerprint=previous_fingerprint,
                                last_seen_at=previous_seen,
                                last_changed_at=previous_changed,
                            )
                        conn.execute(
                            "UPDATE perception_signal_state_v2 "
                            "SET value_fingerprint=%s, fingerprint_key_id=%s, "
                            "source_event_id=%s, updated_at=now() "
                            "WHERE user_id=%s AND signal=%s",
                            (
                                fingerprint,
                                fingerprint_key_id,
                                event_id,
                                user_id,
                                signal,
                            ),
                        )
                        return _decision(
                            "unchanged",
                            fingerprint=(
                                fingerprint if migrate_key else previous_fingerprint
                            ),
                            last_seen_at=previous_seen,
                            last_changed_at=previous_changed,
                        )
                    if hmac.compare_digest(
                        comparable_fingerprint, previous_fingerprint
                    ):
                        conn.execute(
                            "UPDATE perception_signal_state_v2 "
                            "SET value_fingerprint=%s, fingerprint_key_id=%s, "
                            "last_seen_at=%s, source_event_id=%s, "
                            "updated_at=now() "
                            "WHERE user_id=%s AND signal=%s",
                            (
                                fingerprint,
                                fingerprint_key_id,
                                observed,
                                event_id,
                                user_id,
                                signal,
                            ),
                        )
                        return _decision(
                            "unchanged",
                            fingerprint=(
                                fingerprint if migrate_key else previous_fingerprint
                            ),
                            last_seen_at=observed,
                            last_changed_at=previous_changed,
                        )
                    conn.execute(
                        "UPDATE perception_signal_state_v2 "
                        "SET value_fingerprint=%s, fingerprint_key_id=%s, "
                        "last_seen_at=%s, "
                        "last_changed_at=%s, source_event_id=%s, "
                        "updated_at=now() "
                        "WHERE user_id=%s AND signal=%s",
                        (
                            fingerprint,
                            fingerprint_key_id,
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
                    "(user_id, signal, value_fingerprint, fingerprint_key_id, "
                    "last_seen_at, "
                    "last_changed_at, source_event_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (user_id, signal) DO NOTHING RETURNING 1",
                    (
                        user_id,
                        signal,
                        fingerprint,
                        fingerprint_key_id,
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
