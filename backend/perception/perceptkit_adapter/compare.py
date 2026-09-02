"""Compare what PerceptKit concluded against what the live path concluded.

The shadow's whole purpose. Without this, a shadow run only proves the kit
does not crash on real data -- which is worth something, but it is not the
question. The question is whether the kit reaches the *same conclusions* the
code we already trust reaches, on the same input, and where it does not.

## What is being compared

Both sides keep a "current" projection of the user, and both were just
updated from the same report:

    live path   perception_state       field -> {v, ts, msg}
    kit         perceptkit_current     (signal, dimension) -> typed_value

So the comparison is field-by-field between two dictionaries -- but only
after two problems are handled, and both of them are why this is a module
rather than a dict comprehension.

**The names do not line up.** iOS says ``level``, the live path stores
``battery_level``, the manifest declares ``level_ratio``. Every pair is
written down in ``COMPARABLE`` below. Nothing is matched by guessing at
similar names: ``temperature`` and ``temperature_c`` happen to be the same
number today, and ``body_fat_pct`` and ``body_fat_ratio`` differ by 100x --
those two look equally similar.

**Absence is not disagreement.** The live path writes ``v: None`` for a
signal the user withheld; the kit writes ``availability="unavailable"`` and
deliberately keeps the last reliable value beside it. Compared naively, every
permission-denied signal in the system reads as a mismatch, and the report
becomes noise nobody scrolls past.

## Only the signals in this report

Both projections are cumulative, but they did not start on the same day. The
live path has months of state; the kit has whatever the shadow has seen since
it was switched on. Comparing everything means comparing the kit's silence
against the live path's history and calling the kit wrong -- thousands of
findings, none of them real. So each run compares only the signals that
arrived in the report it just processed.

## What a verdict means

    agree          both have a value, and it is the same value
    differ         both have a value, and it is not          <- the interesting one
    only_live      the live path kept something the kit did not
    only_kit       the kit kept something the live path did not
    both_absent    neither has it (not recorded; it is not news)
    declared_gap   a pair we have decided not to compare, and why

``declared_gap`` exists so that "we never compared this" and "we compared
this and it matched" cannot look the same from the outside. A comparison
report whose coverage is implicit is a comparison report that quietly stops
covering things.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: kit signal -> {kit field: live field}. A live field written as
#: ``("now_playing", "title")`` means "look inside that live field's object".
#:
#: Everything here is a deliberate pair. Adding one says "these two are the
#: same fact under two names"; that claim is what makes a `differ` verdict
#: mean something.
COMPARABLE: dict[str, dict[str, Any]] = {
    "battery": {
        "level_ratio": "battery_level",
        "is_charging": "charging",
        "is_low_power_mode_enabled": "low_power_mode",
    },
    "time_context": {
        "time_zone_id": "timezone",
        "locale": "locale",
    },
    "broadcast": {
        "is_active": "broadcast_active",
    },
    "focus_state": {
        "is_active": "in_focus",
    },
    "audio_route": {
        "output_type": "output_type",
        "is_bluetooth": "is_bluetooth",
        "device_label": "device_name",
    },
    "weather": {
        "condition": "condition",
        "temperature_c": "temperature",
        "apparent_temperature_c": "apparent_temperature",
        "humidity_ratio": "humidity",
        "precipitation_probability": "precipitation_chance",
        "uv_index": "uv_index",
        "is_daylight": "is_daylight",
        "alerts": "alerts",
    },
    "motion_state": {
        # The live path stores whatever iOS sent under one field, which is the
        # whole object when iOS sends one and the bare label when the decrypt
        # unwrapped a single-output signal. Both shapes are read -- see
        # SCALAR_FALLBACK.
        "state": ("motion_state", "state"),
    },
    # The live path keeps the whole playback object in one field, so each kit
    # field is compared against a key inside it.
    "music_playback": {
        "title": ("now_playing", "title"),
        "artist": ("now_playing", "artist"),
        # The live path keeps iOS's own key, which is album_title.
        "album": ("now_playing", "album_title"),
        "playback_state": ("now_playing", "playback_state"),
    },
    "steps": {
        # iOS sends this inside health_vitals; the manifest gives steps its own
        # signal. If nothing routes it across, this reads only_live -- which is
        # the correct thing for it to read.
        "step_count": "step_count",
    },
    "health_vitals": {
        "resting_heart_rate": "resting_heart_rate",
        "current_heart_rate": "current_heart_rate",
        "hrv_sdnn_ms": "hrv_sdnn_ms",
        "respiratory_rate": "respiratory_rate",
        "oxygen_saturation_pct": "oxygen_saturation_pct",
        "vo2_max": "vo2_max",
    },
    "health_activity": {
        "active_energy_kcal": "active_energy_kcal",
        "exercise_minutes": "exercise_minutes",
        "stand_minutes": "stand_minutes",
        "mindful_minutes": "mindful_minutes",
    },
    "health_body": {
        "weight_kg": "weight_kg",
        "bmi": "bmi",
        "height_cm": "height_cm",
        # Paired across a real unit difference; the conversion is declared in
        # UNIT_BRIDGE below rather than assumed here.
        "body_fat_ratio": "body_fat_pct",
    },
    "health_metabolic": {
        "blood_glucose_mmol_l": "blood_glucose_mmol_l",
        "blood_pressure_systolic_mmhg": "blood_pressure_systolic",
        "blood_pressure_diastolic_mmhg": "blood_pressure_diastolic",
    },
    "health_cycle": {
        "is_active_period": "is_active_period",
        "flow_level": "flow_level",
    },
    "health_workout": {
        "workout_type": "workout_type",
        "duration_minutes": "duration_min",
    },
    "app_usage": {
        # The Shortcut automations report a typed display name and no bundle
        # id, so both sides hold the same string under two names.
        "app_name": "app_name",
        "category": "app_category",
        # The live path stores where the app is, the kit stores what happened.
        # Same fact, two vocabularies -- translated by LIVE_VOCABULARY below.
        "action": "app_state",
    },
    "location_city": {
        "locality": "locality",
        "country_code": "country",
    },
    "proximity_anchor": {
        "anchor_id": "wifi_anchor_id",
        "label": "wifi_label",
    },
}

#: Kit fields we have decided not to compare, and why. Recorded rather than
#: omitted: a pair missing from both maps is an oversight, and an oversight
#: that looks like coverage is the failure this file is trying to avoid.
KIT_ONLY: dict[tuple[str, str], str] = {
    ("time_context", "utc_offset_seconds"): "live keeps the zone id only, never the offset",
    ("weather", "valid_at"): "live stores no forecast validity time",
    ("weather", "location_scope"): "live stores no forecast scope",
    ("motion_state", "confidence"): "iOS sends low/medium/high; the manifest declares a 0-1 number. Dropped by the adapter rather than invented -- sevenfloor's call",
    ("music_playback", "track_key"): "computed by the adapter; nothing to compare against",
    ("music_playback", "position_seconds"): "iOS reports the track's total duration, not the playback position; neither side has this",
    ("music_playback", "edge_quality"): "computed by the adapter; nothing to compare against",
    ("health_workout", "active_energy_kcal"): "live health_workout does not carry it",
    ("health_workout", "distance_m"): "live health_workout does not carry it",
    ("health_workout", "start_at"): "live stores no workout interval",
    ("health_workout", "end_at"): "live stores no workout interval",
    ("health_cycle", "start_at"): "live stores no cycle interval",
    ("health_cycle", "end_at"): "live stores no cycle interval",
    ("health_mood", "labels"): "live stores a count, the kit stores the labels",
    ("health_mood", "recorded_at"): "live stores a same-day boolean, not a time",
    ("app_usage", "app_id"): "no bundle id on the Shortcut path; same string as app_name",
    ("app_usage", "open_count"): "the kit counts opens by aggregating; the live path keeps no counter",
    ("location_city", "region"): "iOS releases the locality and country, not the administrative region",
    ("location_city", "accuracy_m"): "derived from the fix, which never leaves the device",
    ("location_city", "placemark_source"): "set by this adapter; nothing to compare against",
    ("location_city", "coordinate"): "privacy_class=restricted -- never populated, never stored",
    ("proximity_anchor", "anchor_type"): "always wifi from iOS; bluetooth is not available to a third-party app",
    ("proximity_anchor", "is_connected"): "an anchor is only reported while connected",
    ("proximity_anchor", "raw_identifier"): "privacy_class=restricted -- the SSID stays on the device",
    ("photo_library_added", "count"): "the live path keeps no photo cell in state",
    ("photo_library_added", "added_at"): "the live path keeps no photo cell in state",
    ("screen_change", "changed"): "the live path compares a phash and wakes; it keeps no flag",
    ("presence_recovery", "recovered_at"): "the live path fires a wake; it keeps no recovery cell",
    ("presence_recovery", "absence_seconds"): "iOS reports that an absence ended, not how long it was",
    ("presence_recovery", "absence_quality"): "set by this adapter from whether a duration came with it",
}

#: Live fields with no kit counterpart, and why. Same reasoning as above from
#: the other side.
LIVE_ONLY: dict[str, str] = {
    "local_time": "derivable from the zone id and occurred_at; the manifest declines to store it",
    "focus_authorization_status": "the kit expresses authorization as availability, not a field",
    "broadcast_state": "the manifest models the boolean only",
    "count_today": "live health_workout counts workouts per day; the kit aggregates instead",
    "asleep_minutes": "sleep shape differs: live totals per day, the kit one record per stage",
    "core_minutes": "sleep shape differs",
    "deep_minutes": "sleep shape differs",
    "rem_minutes": "sleep shape differs",
    "label_count": "live stores a count, the kit stores the labels",
    "recorded_today": "live stores a same-day boolean, not a time",
}

#: Signals whose two sides are not the same shape at all, so no field pairing
#: is meaningful. Compared at the signal level or not at all.
SHAPE_DIFFERS: dict[str, str] = {
    "health_sleep": (
        "live keeps four per-day minute totals; the kit keeps one record per "
        "sleep stage with its own interval. Neither is a renaming of the other."
    ),
    "health_mood": (
        "live keeps label_count and a same-day boolean; the kit keeps the "
        "labels themselves and a recorded_at."
    ),
}

#: Pairs whose live holder may be the bare value rather than an object.
#:
#: Derived from the adapter's own table so the two cannot drift: those are
#: exactly the signals whose decrypted payload the live path unwraps to a
#: scalar when they declare a single output. Reading `.get(key)` on a string
#: returns nothing, and the field silently reads as absent on both sides.
def _scalar_fallback() -> frozenset:
    from .ios_report import SCALAR_FIELD
    return frozenset(SCALAR_FIELD.items())


SCALAR_FALLBACK = _scalar_fallback()


#: Live-path vocabulary translated into the manifest's, per pair.
#:
#: Distinct from the adapter's ``VALUE_MAPS``, which translates what iOS sends.
#: This one translates what the *live path* chose to store: it keeps where the
#: app is (`foreground` / `closed`) while the manifest records what happened
#: (`open` / `close`). Same fact, two vocabularies, and neither side is wrong --
#: so the mapping is declared here rather than either side being changed.
LIVE_VOCABULARY: dict[tuple[str, str], dict[str, str]] = {
    ("app_usage", "action"): {"foreground": "open", "closed": "close"},
}


#: The live value converted into the kit's unit, per pair, where the two sides
#: genuinely store the same fact in different units.
#:
#: Written down one pair at a time on purpose. A comparison that silently
#: normalizes units cannot tell "the adapter converts correctly" from "the
#: adapter forgot to convert" -- both come out as agreement. Here the
#: conversion is a claim about that one pair, and if the adapter stops doing it
#: the comparison goes red.
UNIT_BRIDGE: dict[tuple[str, str], Any] = {
    # iOS reports body fat as a percentage; the manifest declares a ratio.
    ("health_body", "body_fat_ratio"): lambda v: v / 100.0,
}


#: Signals the shadow never sees, and why. **This is the shadow's coverage
#: hole written down.**
#:
#: The shadow taps one entry point: the v2 snapshot ingest. Perception has
#: several others -- photo evaluation, device events, app open/close, and the
#: calendar/reminder source mirror -- and the signals below only ever arrive
#: through those. Pairing their fields here would produce a steady stream of
#: `only_live` findings that mean nothing except "the shadow is not plugged in
#: there", which is noise dressed as a defect.
#:
#: Listing them instead of omitting them is the point. ``coverage()`` reads
#: this, so "the kit agrees on everything we compare" can always be checked
#: against "and here is what we do not compare".
NOT_SHADOWED: dict[str, str] = {
    # Empty, and that took work: these six were all here, one line each, saying
    # the shadow never saw them. Every perception entry point is tapped now
    # (see events.py). Anything added back belongs here with its reason rather
    # than left to look like agreement.
}


#: Signals the shadow observes but has nothing on the live side to compare
#: against, and why. Different from a `NOT_SHADOWED` entry: the kit *does* get
#: these, they land in its tables and its aggregates -- there is simply no
#: field in `perception_state` holding the same fact, because the live path
#: models them as events that fire rather than as state that persists.
NO_LIVE_COUNTERPART: dict[str, str] = {
    "photo_library_added": "the live path wakes on a photo; it keeps no photo cell in state",
    "screen_change": "the live path compares a screen phash and wakes; it keeps no changed flag",
    "presence_recovery": "the live path fires an unlock-after-absence wake; it keeps no recovery cell",
}


#: How close two floats have to be to count as the same number. Tight on
#: purpose: this is here to absorb JSON round-trip noise, not to smooth over a
#: unit conversion or a rounding change, which are exactly what we are looking
#: for.
FLOAT_REL_TOL = 1e-9

#: Sample values are truncated before they are stored. These are real readings
#: from a real person, kept only to make a divergence debuggable.
MAX_SAMPLE_CHARS = 200


@dataclass(frozen=True)
class Divergence:
    """One field, one verdict, and the two values behind it."""

    signal: str
    field: str
    verdict: str
    live: Any = None
    kit: Any = None
    note: str = ""

    @property
    def interesting(self) -> bool:
        return self.verdict != "agree"


def _same(live: Any, kit: Any) -> bool:
    """Are these the same value?

    Deliberately strict. Enum casing is not folded and numbers are not
    rounded: if one side lower-cases a state name or drops a decimal, that
    reaches every consumer downstream and the comparison should say so rather
    than absorb it.
    """
    if live is None or kit is None:
        return live is None and kit is None
    if isinstance(live, bool) or isinstance(kit, bool):
        return live is kit
    if isinstance(live, (int, float)) and isinstance(kit, (int, float)):
        return math.isclose(float(live), float(kit), rel_tol=FLOAT_REL_TOL,
                            abs_tol=0.0)
    if isinstance(live, (list, tuple)) and isinstance(kit, (list, tuple)):
        return len(live) == len(kit) and all(
            _same(a, b) for a, b in zip(live, kit))
    return live == kit


def _live_value(state: Mapping[str, Any], where: Any, signal: str = "") -> Any:
    """Pull one live value out of ``perception_state``.

    ``where`` is a field name, or ``(field, key)`` to reach inside the object
    the live path stores in that field.
    """
    if isinstance(where, tuple):
        field, key = where
        holder = (state.get(field) or {}).get("v")
        if isinstance(holder, Mapping):
            return holder.get(key)
        if holder is not None and (signal, key) in SCALAR_FALLBACK:
            return holder            # the unwrapped single-output shape
        return None
    return (state.get(where) or {}).get("v")


def _kit_projection(rows: Sequence[Any]) -> Any:
    """The projection to compare against, when a signal has more than one.

    Signals with a dimension (playback, app usage) keep one row per dimension.
    The live path keeps exactly one cell, and it holds the most recent thing
    it saw -- so the newest projection is the one that answers the same
    question. Which one was picked is recorded on the finding, because
    "the kit disagreed" and "the kit is tracking three things and one of them
    disagreed" are different news.
    """
    if not rows:
        return None
    return max(rows, key=lambda p: (p.observed_at, p.dimension_key))


def compare(
    live_state: Mapping[str, Any],
    kit_current: Mapping[str, Sequence[Any]],
    *,
    signals: Iterable[str],
) -> list[Divergence]:
    """Compare the two projections for the given signals.

    ``signals`` is what arrived in the report being processed -- see the
    module docstring for why the comparison is scoped to it and not to
    everything both sides hold.
    """
    from .ios_report import VALUE_MAPS as vocabularies

    findings: list[Divergence] = []
    for signal in sorted(set(signals)):
        reason = SHAPE_DIFFERS.get(signal)
        if reason:
            findings.append(Divergence(signal, "*", "declared_gap", note=reason))
            continue

        pairs = COMPARABLE.get(signal)
        if pairs is None:
            findings.append(Divergence(
                signal, "*", "declared_gap",
                note="no field pairing declared for this signal"))
            continue

        projection = _kit_projection(kit_current.get(signal) or ())
        multi = len(kit_current.get(signal) or ()) > 1
        observed = projection is not None and projection.availability == "observed"
        typed = (projection.typed_value or {}) if projection else {}
        if not isinstance(typed, Mapping):
            typed = {}

        for kit_field, live_where in sorted(pairs.items()):
            live = _live_value(live_state, live_where, signal)
            convert = UNIT_BRIDGE.get((signal, kit_field))
            if convert is not None and isinstance(live, (int, float)) \
                    and not isinstance(live, bool):
                live = convert(live)
            # The adapter translates producer vocabulary into the manifest's
            # (iOS "still" -> "stationary"). Apply the same declared map here,
            # so the comparison checks that the adapter *did* translate rather
            # than reporting the translation itself as a disagreement -- and
            # still goes red if the adapter ever stops.
            vocab = (vocabularies.get(signal, {}).get(kit_field)
                     or LIVE_VOCABULARY.get((signal, kit_field)))
            if vocab and isinstance(live, str):
                live = vocab.get(live, live)
            kit = typed.get(kit_field) if observed else None
            note = "kit has several dimensions; compared the newest" if multi else ""

            if live is None and kit is None:
                continue                        # both_absent -- not news
            if live is None:
                verdict = "only_kit"
            elif kit is None:
                verdict = "only_live"
            elif _same(live, kit):
                verdict = "agree"
            else:
                verdict = "differ"
            findings.append(Divergence(signal, kit_field, verdict, live, kit, note))

        for kit_field in sorted(typed):
            if kit_field in pairs:
                continue
            gap = KIT_ONLY.get((signal, kit_field))
            findings.append(Divergence(
                signal, kit_field, "declared_gap",
                note=gap or "kit field with no declared live counterpart"))
    return findings


def sample(value: Any) -> str | None:
    """Render one value for storage, truncated.

    Truncation is not cosmetic. These are a real person's readings, kept only
    so a divergence can be diagnosed; there is no reason for the diagnostic
    copy to be complete.
    """
    if value is None:
        return None
    text = repr(value)
    if len(text) > MAX_SAMPLE_CHARS:
        text = text[:MAX_SAMPLE_CHARS - 1] + "…"
    return text


def undeclared_pairs(signals: Mapping[str, Any]) -> list[str]:
    """Manifest fields this module has an opinion about neither way.

    Called by a test. A field that is in neither ``COMPARABLE`` nor
    ``KIT_ONLY`` is not "not compared" -- it is *unnoticed*, and it will stay
    unnoticed as the manifest grows unless something fails when it appears.
    """
    missing: list[str] = []
    for key, definition in signals.items():
        if key in SHAPE_DIFFERS or key in NOT_SHADOWED:
            continue
        pairs = COMPARABLE.get(key, {})
        for fd in definition.fields:
            if fd.key in pairs or (key, fd.key) in KIT_ONLY:
                continue
            missing.append(f"{key}.{fd.key}")
    return missing


#: One row per (subject, signal, field, verdict), counter bumped in place.
#: ``occurrences`` answers "how often", the ``last_*`` columns answer "what did
#: it look like" -- and an ``agree`` row deliberately stores neither sample,
#: because there is nothing to diagnose and no reason to keep the reading.
_UPSERT = """
INSERT INTO perceptkit_shadow_divergence
  (subject_id, signal, field, verdict, occurrences,
   first_seen_at, last_seen_at, last_live, last_kit, last_report_id, note)
VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s)
ON CONFLICT (subject_id, signal, field, verdict) DO UPDATE SET
  occurrences    = perceptkit_shadow_divergence.occurrences + 1,
  last_seen_at   = EXCLUDED.last_seen_at,
  last_live      = EXCLUDED.last_live,
  last_kit       = EXCLUDED.last_kit,
  last_report_id = EXCLUDED.last_report_id,
  note           = EXCLUDED.note
"""


def record(conn: Any, subject_id: str, findings: Sequence[Divergence], *,
           now: Any, report_id: str | None = None) -> int:
    """Fold one run's findings into the running tally. Returns rows touched."""
    if not findings:
        return 0
    rows = []
    for d in findings:
        keep = d.verdict not in ("agree", "declared_gap")
        rows.append((
            subject_id, d.signal, d.field, d.verdict, now, now,
            sample(d.live) if keep else None,
            sample(d.kit) if keep else None,
            report_id if keep else None,
            d.note or None,
        ))
    with conn.cursor() as cur:
        cur.executemany(_UPSERT, rows)
    return len(rows)


def summarize(conn: Any, *, subject_id: str | None = None,
              verdicts: Sequence[str] = ("differ", "only_live", "only_kit"),
              ) -> list[dict[str, Any]]:
    """Read the tally back, worst first.

    Aggregated across subjects unless one is named: a field the kit gets wrong
    is almost never wrong for one person only, and per-subject rows bury that
    under whoever reports most often.
    """
    where = ["verdict = ANY(%s)"]
    params: list[Any] = [list(verdicts)]
    if subject_id is not None:
        where.append("subject_id = %s")
        params.append(subject_id)
    sql = (
        "SELECT signal, field, verdict, SUM(occurrences) AS n, "
        "       COUNT(DISTINCT subject_id) AS subjects, "
        "       MAX(last_seen_at) AS last_seen, "
        "       (array_agg(last_live ORDER BY last_seen_at DESC))[1] AS live, "
        "       (array_agg(last_kit  ORDER BY last_seen_at DESC))[1] AS kit "
        "FROM perceptkit_shadow_divergence "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY signal, field, verdict "
        "ORDER BY n DESC, signal, field"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def coverage(signals: Mapping[str, Any]) -> dict[str, Any]:
    """What the comparison actually covers, and what it does not.

    Written to be read next to a clean divergence report. "The kit agrees on
    everything" is only reassuring alongside how much "everything" is, and a
    coverage number that has to be recomputed by hand is a coverage number
    nobody recomputes.
    """
    compared = sorted(
        key for key in signals
        if key not in NOT_SHADOWED and key not in SHAPE_DIFFERS
        and COMPARABLE.get(key)
    )
    observed_only = sorted(
        key for key in signals
        if key not in NOT_SHADOWED and key not in SHAPE_DIFFERS
        and not COMPARABLE.get(key)
    )
    return {
        "signals_total": len(signals),
        "signals_compared": compared,
        "fields_compared": sum(len(COMPARABLE.get(k, {})) for k in compared),
        "signals_observed_only": observed_only,
        "not_shadowed": dict(NOT_SHADOWED),
        "no_live_counterpart": dict(NO_LIVE_COUNTERPART),
        "shape_differs": dict(SHAPE_DIFFERS),
        "kit_only_fields": {f"{s}.{f}": why for (s, f), why in KIT_ONLY.items()},
        "live_only_fields": dict(LIVE_ONLY),
    }


__all__ = [
    "COMPARABLE", "KIT_ONLY", "LIVE_ONLY", "SHAPE_DIFFERS", "NOT_SHADOWED", "NO_LIVE_COUNTERPART",
    "UNIT_BRIDGE", "LIVE_VOCABULARY", "SCALAR_FALLBACK",
    "FLOAT_REL_TOL", "MAX_SAMPLE_CHARS",
    "Divergence", "compare", "record", "summarize", "sample",
    "undeclared_pairs", "coverage",
]
