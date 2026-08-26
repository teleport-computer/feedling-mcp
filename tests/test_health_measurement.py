"""Unit tests for perception/health_measurement.py — attribution, dedup
identity, observation-state, and the arrival->measured cutover.

Pure functions only (no DB, no service.py) — mirrors the sensegate modules
they wrap (sensegate.attribution / sensegate.identity / sensegate.observation)
being pure/no-I/O themselves.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception import health_measurement as hm  # noqa: E402


# ---------------------------------------------------------------------------
# extract_group_metadata
# ---------------------------------------------------------------------------

def test_extract_group_metadata_per_field_groups_are_independent():
    raw = {
        "weight_kg": 70.2,
        "weight_kg_measured_at": "2026-01-15T08:00:00-08:00",
        "weight_kg_sample_id": "sample-weight-1",
        "bmi": 22.1,
        # bmi carries no metadata this report — old-style field.
    }
    metas = {m.group.name: m for m in hm.extract_group_metadata("health_body", raw)}
    assert metas["weight_kg"].measured_at == "2026-01-15T08:00:00-08:00"
    assert metas["weight_kg"].sample_id == "sample-weight-1"
    assert metas["weight_kg"].has_measurement_time
    assert metas["bmi"].measured_at is None
    assert not metas["bmi"].has_measurement_time
    assert not metas["bmi"].is_measurement_aware


def test_extract_group_metadata_blood_pressure_shares_one_pair():
    raw = {
        "blood_pressure_systolic": 118,
        "blood_pressure_diastolic": 76,
        "blood_pressure_measured_at": "2026-02-01T07:00:00+08:00",
        "blood_pressure_sample_id": "bp-1",
        "blood_glucose_mmol_l": 5.1,
        "blood_glucose_mmol_l_measured_at": "2026-02-02T07:00:00+08:00",
        "blood_glucose_mmol_l_sample_id": "glucose-1",
    }
    metas = {m.group.name: m for m in hm.extract_group_metadata("health_metabolic", raw)}
    assert metas["blood_pressure"].sample_id == "bp-1"
    assert metas["blood_pressure"].group.fields == (
        "blood_pressure_systolic", "blood_pressure_diastolic",
    )
    assert metas["blood_glucose_mmol_l"].sample_id == "glucose-1"
    # two independent groups -> two independent dates when attributed.
    d_bp = hm.attributed_date(metas["blood_pressure"], fallback="1970-01-01")
    d_glucose = hm.attributed_date(metas["blood_glucose_mmol_l"], fallback="1970-01-01")
    assert d_bp == "2026-02-01"
    assert d_glucose == "2026-02-02"


def test_extract_group_metadata_sleep_uses_start_end_not_interval_infix():
    # Wire contract: sleep sends `sleep_start`/`sleep_end` (no `_interval_`
    # infix) — this pins that naming so it can't silently regress again.
    raw = {
        "asleep_minutes": 410,
        "core_minutes": 200,
        "deep_minutes": 90,
        "rem_minutes": 120,
        "sleep_start": "2026-03-01T23:00:00-05:00",
        "sleep_end": "2026-03-02T07:00:00-05:00",
        "sleep_sample_id": "sleep-1",
    }
    metas = {m.group.name: m for m in hm.extract_group_metadata("health_sleep", raw)}
    meta = metas["sleep"]
    assert meta.interval_start == "2026-03-01T23:00:00-05:00"
    assert meta.interval_end == "2026-03-02T07:00:00-05:00"
    assert meta.sample_id == "sleep-1"
    assert meta.has_measurement_time
    # Sleep attributes to the day it ends (wake-up day), not the day it started.
    assert hm.attributed_date(meta, fallback="1970-01-01") == "2026-03-02"


def test_extract_group_metadata_workout_uses_start_end_not_interval_infix():
    raw = {
        "workout_type": "run",
        "duration_min": 32,
        "count_today": 1,
        "workout_start": "2026-04-05T06:00:00-07:00",
        "workout_end": "2026-04-05T06:32:00-07:00",
        "workout_sample_id": "workout-1",
    }
    metas = {m.group.name: m for m in hm.extract_group_metadata("health_workout", raw)}
    meta = metas["workout"]
    assert meta.interval_start == "2026-04-05T06:00:00-07:00"
    assert meta.interval_end == "2026-04-05T06:32:00-07:00"
    assert meta.sample_id == "workout-1"
    assert meta.has_measurement_time
    assert hm.attributed_date(meta, fallback="1970-01-01") == "2026-04-05"


def test_extract_group_metadata_returns_empty_for_unmapped_signal():
    assert hm.extract_group_metadata("health_cycle", {"flow_level": "light"}) == []


def test_extract_group_metadata_tolerates_non_mapping_raw_value():
    assert hm.extract_group_metadata("health_body", None) == []
    assert hm.extract_group_metadata("health_body", "not-a-dict") == []


# ---------------------------------------------------------------------------
# attributed_date
# ---------------------------------------------------------------------------

def test_attributed_date_instant_uses_its_own_offset_not_utc():
    group = hm.MeasurementGroup("weight_kg", ("weight_kg",), hm.INSTANT)
    # 23:30 in UTC-08:00 is still Jan 15 there, even though UTC has rolled to Jan 16.
    meta = hm.GroupMeta(group, measured_at="2026-01-15T23:30:00-08:00", sample_id="s1")
    assert hm.attributed_date(meta, fallback="2026-08-26") == "2026-01-15"


def test_attributed_date_episode_end_for_sleep_crossing_midnight():
    group = hm.MeasurementGroup("sleep", ("asleep_minutes",), hm.INTERVAL)
    meta = hm.GroupMeta(
        group,
        interval_start="2026-03-01T23:00:00-05:00",
        interval_end="2026-03-02T07:00:00-05:00",
        sample_id="sleep-1",
    )
    # Sleep 23:00 -> 07:00 attributes to the day it ENDS (wake-up day).
    assert hm.attributed_date(meta, fallback="1970-01-01") == "2026-03-02"


def test_attributed_date_falls_back_when_no_measurement_time():
    group = hm.MeasurementGroup("weight_kg", ("weight_kg",), hm.INSTANT)
    meta = hm.GroupMeta(group)  # nothing reported — old app build.
    assert hm.attributed_date(meta, fallback="2026-08-26") == "2026-08-26"


def test_attributed_date_falls_back_on_malformed_timestamp_without_raising():
    group = hm.MeasurementGroup("weight_kg", ("weight_kg",), hm.INSTANT)
    meta = hm.GroupMeta(group, measured_at="not-a-timestamp", sample_id="s1")
    assert hm.attributed_date(meta, fallback="2026-08-26") == "2026-08-26"
    # No offset -> sensegate.attribution refuses to guess; must still fall back.
    meta_naive = hm.GroupMeta(group, measured_at="2026-01-15T08:00:00", sample_id="s1")
    assert hm.attributed_date(meta_naive, fallback="2026-08-26") == "2026-08-26"


# ---------------------------------------------------------------------------
# identity_key
# ---------------------------------------------------------------------------

def test_identity_key_stable_across_calls_and_none_without_sample_id():
    group = hm.MeasurementGroup("weight_kg", ("weight_kg",), hm.INSTANT)
    meta = hm.GroupMeta(group, measured_at="2026-01-15T08:00:00-08:00", sample_id="sample-1")
    k1 = hm.identity_key("health_body", meta)
    k2 = hm.identity_key("health_body", meta)
    assert k1 == k2
    assert k1 is not None

    meta_no_id = hm.GroupMeta(group, measured_at="2026-01-15T08:00:00-08:00")
    assert hm.identity_key("health_body", meta_no_id) is None


def test_identity_key_differs_by_group_and_signal():
    weight_group = hm.MeasurementGroup("weight_kg", ("weight_kg",), hm.INSTANT)
    bmi_group = hm.MeasurementGroup("bmi", ("bmi",), hm.INSTANT)
    meta_weight = hm.GroupMeta(weight_group, measured_at="2026-01-15T08:00:00-08:00", sample_id="s1")
    meta_bmi = hm.GroupMeta(bmi_group, measured_at="2026-01-15T08:00:00-08:00", sample_id="s1")
    # Same sample_id, different group name -> different key (never collide).
    assert hm.identity_key("health_body", meta_weight) != hm.identity_key("health_body", meta_bmi)


# ---------------------------------------------------------------------------
# apply_group_update — dedup + cutover + observed-state
# ---------------------------------------------------------------------------

def _hr_group():
    return hm.MeasurementGroup("resting_heart_rate", ("resting_heart_rate",), hm.INSTANT)


def test_apply_group_update_folds_a_fresh_sample():
    doc = hm.apply_group_update(
        None, signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": 60}, identity_key="key-1", ts=100.0,
    )
    assert doc["resting_heart_rate"] == {"min": 60.0, "max": 60.0, "sum": 60.0, "count": 1}
    assert doc["_ts_kind"] == "measured"
    assert doc["_seen"] == {"key-1": 100.0}
    assert doc["_observed"]["resting_heart_rate"] == "observed"


def test_apply_group_update_dedups_reupload_of_same_sample():
    doc = hm.apply_group_update(
        None, signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": 60}, identity_key="key-1", ts=100.0,
    )
    # Same sample re-uploaded (same identity_key) a moment later — must NOT
    # inflate sum/count a second time.
    doc2 = hm.apply_group_update(
        doc, signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": 60}, identity_key="key-1", ts=130.0,
    )
    assert doc2["resting_heart_rate"] == {"min": 60.0, "max": 60.0, "sum": 60.0, "count": 1}

    # A genuinely NEW sample (different identity_key) DOES fold in.
    doc3 = hm.apply_group_update(
        doc2, signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": 64}, identity_key="key-2", ts=200.0,
    )
    assert doc3["resting_heart_rate"] == {"min": 60.0, "max": 64.0, "sum": 124.0, "count": 2}


def test_apply_group_update_without_identity_key_never_dedups():
    # Old app build: no sample_id at all -> identity_key is None -> every call
    # folds (today's behavior, unchanged).
    doc = hm.apply_group_update(
        None, signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": 60}, identity_key=None, ts=100.0,
    )
    doc2 = hm.apply_group_update(
        doc, signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": 60}, identity_key=None, ts=130.0,
    )
    assert doc2["resting_heart_rate"]["count"] == 2


def test_apply_group_update_cutover_replaces_arrival_tagged_doc_outright():
    # Simulate a pre-cutover row: inflated by repeated same-sample re-uploads
    # under the OLD (arrival-time) semantics, no `_ts_kind` tag at all.
    poisoned_doc = {"resting_heart_rate": {"min": 50.0, "max": 90.0, "sum": 700.0, "count": 10}}
    doc = hm.apply_group_update(
        poisoned_doc, signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": 62}, identity_key="key-real-1", ts=500.0,
    )
    # The old aggregate must be wiped, not folded onto — a single fresh sample.
    assert doc["resting_heart_rate"] == {"min": 62.0, "max": 62.0, "sum": 62.0, "count": 1}
    assert doc["_ts_kind"] == "measured"


def test_apply_group_update_second_measured_write_folds_normally():
    doc = hm.apply_group_update(
        {"_ts_kind": "measured", "resting_heart_rate": {"min": 60.0, "max": 60.0, "sum": 60.0, "count": 1},
         "_seen": {"key-1": 100.0}},
        signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": 64}, identity_key="key-2", ts=200.0,
    )
    # Row already tagged "measured" -> fold normally, no reset.
    assert doc["resting_heart_rate"] == {"min": 60.0, "max": 64.0, "sum": 124.0, "count": 2}
    assert set(doc["_seen"]) == {"key-1", "key-2"}


def test_apply_group_update_observed_state_no_observation_when_value_missing():
    doc = hm.apply_group_update(
        None, signal="health_vitals", group=_hr_group(),
        field_values={"resting_heart_rate": None}, identity_key=None, ts=100.0,
    )
    assert doc["_observed"]["resting_heart_rate"] == "no_observation"
    assert "resting_heart_rate" not in doc  # nothing numeric folded when value is None


def test_apply_group_update_seen_set_is_bounded():
    doc = None
    for i in range(hm.SEEN_CAP + 10):
        doc = hm.apply_group_update(
            doc, signal="health_vitals", group=_hr_group(),
            field_values={"resting_heart_rate": 60 + i}, identity_key=f"key-{i}", ts=float(i),
        )
    assert len(doc["_seen"]) <= hm.SEEN_CAP


# ---------------------------------------------------------------------------
# apply_unavailable
# ---------------------------------------------------------------------------

def test_apply_unavailable_marks_state_without_touching_numeric_data():
    prev = {
        "_ts_kind": "measured",
        "resting_heart_rate": {"min": 60.0, "max": 64.0, "sum": 124.0, "count": 2},
        "_seen": {"key-1": 100.0},
    }
    doc = hm.apply_unavailable(prev, group=_hr_group())
    assert doc["_observed"]["resting_heart_rate"] == "unavailable"
    assert doc["resting_heart_rate"] == prev["resting_heart_rate"]
    assert doc["_ts_kind"] == "measured"  # untouched, not force-reset
    assert doc["_seen"] == prev["_seen"]


def test_apply_unavailable_on_missing_prev_doc():
    doc = hm.apply_unavailable(None, group=_hr_group())
    assert doc["_observed"]["resting_heart_rate"] == "unavailable"
    assert "_ts_kind" not in doc


# ---------------------------------------------------------------------------
# select_rollup_rows_after_cutover — read-side half of the cutover: keeps old
# (arrival-tagged) and new (measured-tagged) rows from ever being pooled into
# one series.
# ---------------------------------------------------------------------------

def test_select_rollup_rows_keeps_only_measured_once_any_row_is_measured():
    rows = [
        {"date": "2026-01-15", "doc": {"weight_kg": 68.4}},               # old-meaning
        {"date": "2026-03-01", "doc": {"weight_kg": 68.4}},               # old-meaning
        {"date": "2026-01-14", "doc": {"weight_kg": 68.4, "_ts_kind": "measured"}},  # new-meaning
    ]
    kept = hm.select_rollup_rows_after_cutover(rows)
    assert kept == [rows[2]]


def test_select_rollup_rows_is_a_no_op_when_nothing_is_measurement_aware():
    rows = [
        {"date": "2026-01-15", "doc": {"weight_kg": 68.4}},
        {"date": "2026-03-01", "doc": {"weight_kg": 68.4}},
    ]
    assert hm.select_rollup_rows_after_cutover(rows) == rows


def test_select_rollup_rows_tolerates_empty_and_malformed_input():
    assert hm.select_rollup_rows_after_cutover([]) == []
    assert hm.select_rollup_rows_after_cutover(None) == []
