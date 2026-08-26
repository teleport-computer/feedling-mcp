"""Health measurement-time wiring: attribution, dedup identity, and the
arrival-time -> measured-time cutover for the perception_daily rollup.

Background: the iOS client fetches "the newest sample in the last N days" per
health metric and re-uploads it on every report. Historically that value was
folded into the rollup keyed by upload time, so a January weight looked new
every day and a re-upload of the same sample could be double-counted forever
(no raw samples are kept to correct it later).

The client now OPTIONALLY sends, per metric, when it was actually measured and
a stable id for that sample. Every one of these fields is optional; when a
report carries none of them (old app build, or a signal this module has not
been extended to cover) the caller must fall back to today's report-arrival
behavior untouched — see the `measurement_aware` gate at each call site in
`service.py`.

This module is pure (no I/O, no DB) so it is unit-testable without a database,
matching the rest of `perception/history.py` / `perceptkit.history`.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from perceptkit import attribution as sg_attribution
from perceptkit import identity as sg_identity
from perceptkit import observation as sg_observation

from . import history

INSTANT = "instant"
INTERVAL = "interval"

# A stable dedup/attribution "source" namespace for perceptkit.identity. Not a
# per-user value — the perception_daily table is already keyed by user_id, so
# this only needs to distinguish "this came from the iOS HealthKit report
# pipeline" from any other future measurement source.
SOURCE = "ios_health"

# Cap on how many identity keys a single (user, date, signal) rollup doc keeps
# in its `_seen` dedup set. A real day sees at most a handful of distinct
# HealthKit samples per group (re-uploads of the SAME sample don't grow this
# set — they hit the dedup and are skipped); this is headroom against a bug
# or a malicious client replaying many distinct fake sample_ids, not a limit
# anyone should hit in normal use.
SEEN_CAP = 32


class MeasurementGroup(NamedTuple):
    """One or more catalog output fields that share a single measured_at /
    sample_id pair (e.g. blood pressure's systolic+diastolic halves)."""
    name: str
    fields: tuple[str, ...]
    kind: str  # INSTANT | INTERVAL


# Per historized health signal: which output fields require their own
# measurement identity, and which share one. Most fields default to their own
# group because the phone fetches "the newest sample" per metric independently
# — a weight sample and a BMI sample are not necessarily the same HealthKit
# write. Blood pressure is the one documented exception (one cuff reading
# produces both halves at once).
#
# health_activity / health_cycle / health_mood are intentionally NOT covered
# here yet — the brief's worked examples are body measurements, sleep, workout
# and blood pressure. Extending coverage is adding one more dict entry here
# once their wire fields are confirmed; see docs/NOTES-measured-at-ingest.md.
MEASUREMENT_GROUPS: dict[str, tuple[MeasurementGroup, ...]] = {
    "health_body": (
        MeasurementGroup("weight_kg", ("weight_kg",), INSTANT),
        MeasurementGroup("bmi", ("bmi",), INSTANT),
        MeasurementGroup("body_fat_pct", ("body_fat_pct",), INSTANT),
        MeasurementGroup("height_cm", ("height_cm",), INSTANT),
    ),
    "health_vitals": (
        MeasurementGroup("resting_heart_rate", ("resting_heart_rate",), INSTANT),
        MeasurementGroup("step_count", ("step_count",), INSTANT),
        MeasurementGroup("current_heart_rate", ("current_heart_rate",), INSTANT),
        MeasurementGroup("hrv_sdnn_ms", ("hrv_sdnn_ms",), INSTANT),
        MeasurementGroup("respiratory_rate", ("respiratory_rate",), INSTANT),
        MeasurementGroup("oxygen_saturation_pct", ("oxygen_saturation_pct",), INSTANT),
        MeasurementGroup("vo2_max", ("vo2_max",), INSTANT),
    ),
    "health_metabolic": (
        MeasurementGroup("blood_glucose_mmol_l", ("blood_glucose_mmol_l",), INSTANT),
        MeasurementGroup(
            "blood_pressure",
            ("blood_pressure_systolic", "blood_pressure_diastolic"),
            INSTANT,
        ),
    ),
    "health_sleep": (
        MeasurementGroup(
            "sleep",
            ("asleep_minutes", "core_minutes", "deep_minutes", "rem_minutes"),
            INTERVAL,
        ),
    ),
    "health_workout": (
        MeasurementGroup("workout", ("workout_type", "duration_min", "count_today"), INTERVAL),
    ),
}


class GroupMeta(NamedTuple):
    group: MeasurementGroup
    measured_at: str | None = None
    interval_start: str | None = None
    interval_end: str | None = None
    sample_id: str | None = None

    @property
    def has_measurement_time(self) -> bool:
        if self.group.kind == INTERVAL:
            return bool(self.interval_start and self.interval_end)
        return bool(self.measured_at)

    @property
    def is_measurement_aware(self) -> bool:
        """True the moment the client sent ANY new-contract field for this
        group (a time, or a sample id) — used to decide whether a report
        touches the new cutover path at all."""
        return bool(self.has_measurement_time or self.sample_id)


def extract_group_metadata(signal: str, raw_value: Any) -> list[GroupMeta]:
    """Read the optional metadata fields for every measurement group declared
    for `signal` out of the RAW (pre-resolver) reported mapping.

    Naming convention: ``"<group>_measured_at"`` / ``"<group>_sample_id"`` for
    instant groups, ``"<group>_start"`` / ``"<group>_end"`` / ``"<group>_sample_id"``
    for interval groups (matches the iOS wire contract exactly — sleep sends
    ``sleep_start``/``sleep_end``, workout sends ``workout_start``/``workout_end``;
    there is no ``_interval_`` infix on the wire). Never raises — a malformed or
    absent field simply comes back as None so the caller falls back to
    today's behavior.
    """
    groups = MEASUREMENT_GROUPS.get(signal, ())
    if not groups or not isinstance(raw_value, Mapping):
        return []
    out: list[GroupMeta] = []
    for group in groups:
        sample_id = raw_value.get(f"{group.name}_sample_id")
        sample_id = str(sample_id).strip() if sample_id is not None else None
        if group.kind == INTERVAL:
            out.append(GroupMeta(
                group,
                interval_start=raw_value.get(f"{group.name}_start"),
                interval_end=raw_value.get(f"{group.name}_end"),
                sample_id=sample_id or None,
            ))
        else:
            out.append(GroupMeta(
                group,
                measured_at=raw_value.get(f"{group.name}_measured_at"),
                sample_id=sample_id or None,
            ))
    return out


def attributed_date(meta: GroupMeta, *, fallback: str) -> str:
    """The device-local date this group's sample should be filed under.

    Single readings attribute by their own offset's local date (INSTANT);
    sleep/workout attribute by the day they END (EPISODE_END) — both delegate
    to perceptkit.attribution so the "which day" rule lives in exactly one
    place. Falls back to `fallback` (today's report-arrival date) when no
    measurement time was reported, or when it fails to parse — a malformed
    client payload must never crash ingest, it should just behave like an old
    client that sent nothing.
    """
    if not meta.has_measurement_time:
        return fallback
    try:
        if meta.group.kind == INTERVAL:
            return sg_attribution.attribute_episode(meta.interval_start, meta.interval_end)
        return sg_attribution.attribute_instant(meta.measured_at)
    except (ValueError, TypeError):
        return fallback


def identity_key(signal: str, meta: GroupMeta) -> str | None:
    """A stable dedup key for this group's sample, or None when we can't build
    one safely. Never invents a key — an untracked group is simply never
    deduped (matches perceptkit.identity's own "missing identity -> refuse"
    stance)."""
    if not meta.sample_id:
        return None
    try:
        return sg_identity.measurement_key(
            source=SOURCE,
            metric=f"{signal}:{meta.group.name}",
            sample_id=meta.sample_id,
        )
    except sg_identity.MissingIdentity:
        return None


def _strip_reserved(doc: Mapping) -> dict:
    return {k: v for k, v in doc.items() if not (isinstance(k, str) and k.startswith("_"))}


def _reserved(doc: Mapping) -> dict:
    return {k: v for k, v in doc.items() if isinstance(k, str) and k.startswith("_")}


def apply_group_update(
    prev_doc: Mapping | None,
    *,
    signal: str,
    group: MeasurementGroup,
    field_values: Mapping[str, Any],
    identity_key: str | None,
    ts: float,
) -> dict:
    """Fold one measurement group's fields into its (already date-bucketed)
    day-doc. Pure — the caller buckets by attributed date and runs this INSIDE
    the row lock (`store.merge_perception_daily`'s merge_fn) so the dedup
    check-and-write is atomic with the rest of the row.

    Cutover: a day-doc not yet tagged ``_ts_kind == "measured"`` is REPLACED
    outright (not folded onto) the moment a measurement-time-aware report
    lands on it. This is deliberate — see docs/NOTES-measured-at-ingest.md
    "the cutover": we never compare an old arrival-tagged aggregate (which may
    already be inflated by repeated same-sample re-uploads) against new,
    correctly-deduped data. The old row is simply superseded.
    """
    doc = dict(prev_doc or {})
    if doc.get("_ts_kind") != "measured":
        doc = {}
    doc["_ts_kind"] = "measured"

    seen: dict[str, float] = dict(doc.get("_seen") or {})
    is_dup = bool(identity_key) and identity_key in seen
    if identity_key and not is_dup:
        seen[identity_key] = ts
        if len(seen) > SEEN_CAP:
            for k, _ in sorted(seen.items(), key=lambda kv: kv[1])[: len(seen) - SEEN_CAP]:
                seen.pop(k, None)
    doc["_seen"] = seen

    observed: dict[str, str] = dict(doc.get("_observed") or {})
    for field in group.fields:
        observed[field] = sg_observation.classify(field_values.get(field), available=True)
    doc["_observed"] = observed

    any_value = any(v is not None for v in field_values.values())
    if any_value and not is_dup:
        folded = history.record_daily(_strip_reserved(doc), signal, field_values, ts=ts)
        folded.update(_reserved(doc))
        doc = folded
    return doc


def apply_unavailable(prev_doc: Mapping | None, *, group: MeasurementGroup) -> dict:
    """Record that this group's sample could not be observed at all this
    report (decrypt failure / no envelope / no authorization) — distinct from
    "queried successfully, no sample" (NO_OBSERVATION, produced by
    `apply_group_update` when every field in the group is None).

    Deliberately does NOT force the `_ts_kind` cutover — there is no
    measurement time here to cut over WITH, so an unavailable report must
    never wipe an already-measured day's numeric data. It only ever touches
    the `_observed` sidecar.
    """
    doc = dict(prev_doc or {})
    observed: dict[str, str] = dict(doc.get("_observed") or {})
    for field in group.fields:
        observed[field] = sg_observation.classify(None, available=False)
    doc["_observed"] = observed
    return doc


def select_rollup_rows_after_cutover(rows: list[Mapping]) -> list[Mapping]:
    """Read-side half of the cutover: keep old-meaning (arrival-tagged) rows
    OUT of any multi-day series computation once this (user, signal) has at
    least one measurement-time-aware row.

    ``apply_group_update`` stops the poison from *growing* (a day-doc's first
    measured write replaces its old aggregate outright), but it cannot erase
    arrival-tagged rows already sitting under OTHER dates in
    ``perception_daily`` — every day the phone re-uploaded the same stale
    sample before this rollout got its own (fabricated-fresh) row. Those rows
    are a different quantity from measured-time rows (arrival instant vs. true
    measurement instant) and must never be pooled into one trend/baseline/
    delta computation — see docs/NOTES-cutover.md.

    Policy: if ANY row for this series carries ``_ts_kind == "measured"``,
    keep ONLY the measured-tagged rows for series math. If none do (old app
    traffic only, or a signal this module doesn't cover), every row lacks the
    tag and this is a no-op — byte-identical to pre-cutover behavior. Rows
    are never dropped from the raw per-day history read (``perception.
    history`` capability) — only from series-shaped reads (trend/drift/
    digest) that fold multiple days together; "stay readable" means the raw
    endpoint keeps showing everything, tag included.
    """
    rows = list(rows or [])

    def _is_measured(row: Mapping) -> bool:
        doc = row.get("doc") if isinstance(row, Mapping) else None
        return isinstance(doc, Mapping) and doc.get("_ts_kind") == "measured"

    if not any(_is_measured(r) for r in rows):
        return rows
    return [r for r in rows if _is_measured(r)]
