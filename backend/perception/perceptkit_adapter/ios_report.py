"""FeedlingIOSReportAdapter -- turn one iOS snapshot into a ``ReportEnvelope``.

The spec (§16) names this as one of the three adapters that belong to the
host rather than to the kit: it has to understand one specific producer's
shape, and the kit only knows the standard envelope. It started life as the
worked example shipped with the package; this is the host's copy of it.

**It is fed already-decrypted values.** The live v2 ingest decrypts the
sensitive signals through the enclave and collects everything into
``storage_items`` as ``{key, data}`` pairs -- exactly the shape this adapter
reads. Running from that point costs no extra enclave calls, which matters:
one report is roughly seven decrypts already.

## The one thing here that really matters: absence must not become zero

iOS encodes three states in ``data``:

    an object   a real reading        -> observed
    ``""``      authorized, nothing read this round -> no_data
    ``null``    permission withheld   -> unavailable

Collapse the last two into 0 and every layer downstream processes a
fabrication faithfully: rules fire, trends compute, and the agent says "you
didn't walk at all today". Nothing crashes and nothing is logged. Only the
user knows the sentence is wrong.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

#: iOS snapshot key -> kit signal. Names that differ get reconciled here,
#: never in the kit: every producer calls things something else, and changing
#: the kit would be deciding for every other host.
KEY_TO_SIGNAL: dict[str, str] = {
    "time": "time_context",
    "battery": "battery",
    "broadcast": "broadcast",
    "motion_state": "motion_state",
    "focus": "focus_state",
    "audio_route": "audio_route",
    "weather": "weather",
    "playback": "music_playback",
    "health_vitals": "health_vitals",
    "health_sleep": "health_sleep",
    "health_workout": "health_workout",
    "health_activity": "health_activity",
    "health_body": "health_body",
    "health_metabolic": "health_metabolic",
    "health_cycle": "health_cycle",
    "health_mood": "health_mood",
}

#: Keys that do not enter the standard pipeline. Written down so nobody
#: later reads their absence as an oversight.
IGNORED_KEYS = {
    # What third-party apps cannot read. iOS sends this to say so explicitly.
    "unsupported",
    # Calendar and reminders take the source-mirror path, not the signal path
    # -- which is what §7.13 specifies too.
    "calendar_next_event", "reminders",
    # Location is resolved to a city or anchor on the device; coordinates never
    # leave it, so this is not a pass-through observation.
    "location_signal",
}

#: Per-signal field renames, where iOS's name differs from the manifest's.
FIELD_ALIASES: dict[str, dict[str, str]] = {
    "battery": {"level": "level_ratio", "charging": "is_charging",
                "low_power_mode": "is_low_power_mode_enabled"},
    "focus_state": {"focused": "is_active"},
    "time_context": {"timezone": "time_zone_id", "local_time": None},
    "weather": {"temperature": "temperature_c",
                "apparent_temperature": "apparent_temperature_c",
                "humidity": "humidity_ratio",
                "precipitation_chance": "precipitation_probability"},
}

#: Fields iOS sends that the manifest does not declare. Dropped explicitly
#: rather than silently filtered, so "why can't I find the
#: authorization_status I sent" has an answer.
DROPPED_FIELDS: dict[str, set[str]] = {
    # Authorization is expressed through availability, not as its own field.
    "focus_state": {"authorization_status"},
    # Local time is derivable from time_zone_id plus occurred_at.
    "time_context": {"local_time"},
}


#: Some signals put the authorization state in a field inside `data` rather
#: than using `data: null`. Focus does:
#: `{"authorization_status": "denied", "focused": null}`.
#: Read naively that is an observed reading whose required field is null, so
#: the pipeline rejects it as malformed -- reported as "bad data format" when
#: the truth is "the user withheld permission", which sends anyone debugging
#: it entirely the wrong way.
AUTH_STATUS_FIELDS: dict[str, str] = {
    "focus_state": "authorization_status",
}

#: Which values of that field count as authorized.
AUTHORIZED_VALUES = {"authorized", "granted", "allowed"}


def _availability(data: Any, signal: str | None = None) -> str:
    """The three-state decision -- the most important few lines in here."""
    if data is None:
        return "unavailable"        # permission withheld
    if data == "":
        return "no_data"            # authorized, nothing read this round
    field = AUTH_STATUS_FIELDS.get(signal or "")
    if field and isinstance(data, Mapping):
        status = data.get(field)
        if status is not None and str(status).lower() not in AUTHORIZED_VALUES:
            return "unavailable"    # the in-payload authorization case
    return "observed"


def _rename(signal: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """iOS field names -> manifest field names.

    Get this wrong and the pipeline rejects the whole observation for a
    missing required field. That is the good outcome: a loud rejection beats
    quietly storing rows whose fields nothing will ever query.
    """
    alias = FIELD_ALIASES.get(signal, {})
    dropped = DROPPED_FIELDS.get(signal, set())
    out: dict[str, Any] = {}
    for k, v in value.items():
        if v is None or k in dropped:
            continue
        out[alias.get(k, k)] = v
    return out


def report_id_for(payload: Mapping[str, Any]) -> str:
    """Derived from the payload, with no clock and no randomness.

    Mixing either in makes every retransmission look like a new report and
    idempotency stops meaning anything -- and retransmission is normal: a
    flaky network or a suspended app produces one.
    """
    canonical = repr(sorted(
        (i.get("key"), repr(i.get("data")))
        for i in payload.get("context_snapshot", [])
    )) + str(payload.get("client_ts", ""))
    return "ios-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def to_envelope(payload: Mapping[str, Any], *, occurred_at: str) -> dict[str, Any]:
    """Turn one iOS snapshot into a standard report envelope.

    ``occurred_at`` comes from the caller: the whole snapshot is sampled at one
    moment, and iOS carries the device local time in its ``time`` item. Use
    that, not the host's own clock.
    """
    observations: list[dict[str, Any]] = []
    for item in payload.get("context_snapshot", []):
        key = item.get("key")
        if key in IGNORED_KEYS:
            continue
        signal = KEY_TO_SIGNAL.get(key)
        if signal is None:
            # An unknown key is skipped, not an error: iOS may ship a new field
            # a release before the backend understands it. Erroring instead
            # would fail the whole report.
            continue

        data = item.get("data")
        availability = _availability(data, signal)
        obs: dict[str, Any] = {
            "signal": signal,
            "signal_schema_version": 1,
            "occurred_at": occurred_at,
            "availability": availability,
        }
        if availability == "observed" and isinstance(data, Mapping):
            obs["value"] = _rename(signal, data)
        observations.append(obs)

    return {
        "schema_version": 1,
        "report_id": report_id_for(payload),
        "producer": "ios",
        "observations": observations,
    }


__all__ = ["KEY_TO_SIGNAL", "IGNORED_KEYS", "FIELD_ALIASES", "DROPPED_FIELDS",
           "AUTH_STATUS_FIELDS", "AUTHORIZED_VALUES", "report_id_for", "to_envelope"]
