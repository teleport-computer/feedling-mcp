"""Integration-style unit tests for the batch-2 perceptkit wiring inside
perception/service.py: `_apply`'s Tier 2 rollup block and the decrypt-failure
UNAVAILABLE path in `ingest_snapshot_v2`.

Uses an in-memory fake perception store (no DB) that implements
`merge_perception_daily`/`list_perception_daily` the same way
`perception/store.py` does (read-modify-write under a "lock" — single
threaded here, so a plain dict suffices), so the full attribution/dedup/
cutover/observed-state path in `health_measurement.py` runs exactly as it
would in production, without touching Postgres.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import perception.service as service  # noqa: E402


class FakeStore:
    """In-memory perception store double covering exactly what these tests
    exercise: state cells + the perception_daily rollup table."""

    def __init__(self):
        self.state = {}
        self.config = {}
        self.daily = {}          # (uid, date, signal) -> doc
        self.decrypt_failures = {}

    # --- perception_state ---
    def get_state(self, uid):
        return {k: dict(v) for k, v in self.state.get(uid, {}).items()}

    def merge_state_guarded(self, uid, patch):
        cur = self.state.setdefault(uid, {})
        written = set()
        for f, cell in patch.items():
            old = cur.get(f)
            old_ts = old.get("ts") if isinstance(old, dict) else None
            new_ts = cell.get("ts")
            if old_ts is None or new_ts is None or float(new_ts) >= float(old_ts):
                cur[f] = dict(cell)
                written.add(f)
        return written

    def get_config(self, uid):
        return dict(self.config.get(uid, {}))

    # --- perception_daily (Tier 2 rollup) ---
    def merge_perception_daily(self, uid, date, signal, merge_fn, ts):
        key = (uid, date, signal)
        prev = self.daily.get(key, {})
        new_doc = merge_fn(prev) or {}
        self.daily[key] = new_doc
        return new_doc

    def list_perception_daily(self, uid, signal, days=30):
        rows = [
            {"date": d, "doc": doc}
            for (u, d, s), doc in self.daily.items()
            if u == uid and s == signal
        ]
        rows.sort(key=lambda r: r["date"])
        return rows[-days:]

    # --- decrypt-failure audit (unrelated to these tests but read by
    # _record_decrypt_failure_v2, which every decrypt-failure path calls) ---
    def append_decrypt_failure(self, uid, doc, ts):
        self.decrypt_failures.setdefault(uid, []).append(dict(doc))


def _env(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_fire_wake", lambda *a, **k: None)
    monkeypatch.setattr(service, "_app_proactive_settings", lambda uid: {})
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: None)
    monkeypatch.setattr(service, "_proactive_activation_ready", lambda uid: True)
    return fake


UID = "u_health_measurement"


# ---------------------------------------------------------------------------
# Additive guarantee: a report with NO new fields behaves exactly as before.
# ---------------------------------------------------------------------------

def test_no_metadata_report_uses_todays_date_and_folds_normally(monkeypatch):
    fake = _env(monkeypatch)
    service.ingest(UID, {"health_vitals": {"resting_heart_rate": 60}}, client_ts=1_000.0)
    rows = fake.list_perception_daily(UID, "health_vitals")
    assert len(rows) == 1
    doc = rows[0]["doc"]
    assert doc["resting_heart_rate"] == {"min": 60.0, "max": 60.0, "sum": 60.0, "count": 1}
    # Legacy path never introduces the new bookkeeping keys.
    assert "_ts_kind" not in doc
    assert "_seen" not in doc
    assert "_observed" not in doc


# ---------------------------------------------------------------------------
# Attribution: a measured_at in the past routes to that day, not today.
# ---------------------------------------------------------------------------

def test_measured_at_attributes_to_the_real_day_not_report_day(monkeypatch):
    fake = _env(monkeypatch)
    service.ingest(
        UID,
        {"health_body": {
            "weight_kg": 70.2,
            "weight_kg_measured_at": "2026-01-15T08:00:00-08:00",
            "weight_kg_sample_id": "sample-weight-1",
        }},
        client_ts=1_756_000_000.0,  # "today" is nowhere near January.
    )
    # health_body has 4 groups; only weight_kg carried metadata this report.
    # The other 3 groups (no metadata this call) fall back to today's date —
    # that's a separate row recording their (correct) NO_OBSERVATION state,
    # it must not affect the measured, real-day row for weight_kg.
    rows = fake.list_perception_daily(UID, "health_body")
    by_date = {r["date"]: r["doc"] for r in rows}
    assert "2026-01-15" in by_date
    real_day = by_date["2026-01-15"]
    assert real_day["weight_kg"] == 70.2
    assert real_day["_ts_kind"] == "measured"
    assert "bmi" not in real_day  # bmi's own (missing) sample never lands here


def test_blood_pressure_halves_attribute_together_glucose_independently(monkeypatch):
    fake = _env(monkeypatch)
    service.ingest(
        UID,
        {"health_metabolic": {
            "blood_pressure_systolic": 118,
            "blood_pressure_diastolic": 76,
            "blood_pressure_measured_at": "2026-02-01T07:00:00+08:00",
            "blood_pressure_sample_id": "bp-1",
            "blood_glucose_mmol_l": 5.1,
            "blood_glucose_mmol_l_measured_at": "2026-02-03T07:00:00+08:00",
            "blood_glucose_mmol_l_sample_id": "glucose-1",
        }},
        client_ts=1_756_000_000.0,
    )
    rows = fake.list_perception_daily(UID, "health_metabolic")
    by_date = {r["date"]: r["doc"] for r in rows}
    assert set(by_date) == {"2026-02-01", "2026-02-03"}
    assert "blood_pressure_systolic" in by_date["2026-02-01"]
    assert "blood_glucose_mmol_l" in by_date["2026-02-03"]
    assert "blood_glucose_mmol_l" not in by_date["2026-02-01"]


# ---------------------------------------------------------------------------
# Dedup: a re-upload of the same sample must not double-count.
# ---------------------------------------------------------------------------

def test_reupload_of_same_sample_does_not_double_count(monkeypatch):
    fake = _env(monkeypatch)
    payload = {
        "health_body": {
            "weight_kg": 70.2,
            "weight_kg_measured_at": "2026-01-15T08:00:00-08:00",
            "weight_kg_sample_id": "sample-weight-1",
        }
    }
    # The phone re-uploads the same unchanged sample on the next two reports.
    service.ingest(UID, payload, client_ts=1_756_000_000.0)
    service.ingest(UID, payload, client_ts=1_756_000_300.0)
    service.ingest(UID, payload, client_ts=1_756_000_600.0)
    rows = fake.list_perception_daily(UID, "health_body")
    by_date = {r["date"]: r["doc"] for r in rows}
    real_day = by_date["2026-01-15"]
    assert real_day["weight_kg"] == 70.2
    # One real sample, uploaded 3x -> exactly one identity key remembered,
    # not one row per upload attempt.
    assert len(real_day["_seen"]) == 1


# ---------------------------------------------------------------------------
# The cutover: an old arrival-tagged row is replaced, not folded onto.
# ---------------------------------------------------------------------------

def test_cutover_replaces_poisoned_arrival_tagged_row(monkeypatch):
    fake = _env(monkeypatch)
    # Simulate 10 days of the OLD bug: the same January sample re-uploaded
    # under arrival-time semantics, keyed by "today" with no `_ts_kind` tag.
    fake.daily[(UID, "2026-01-15", "health_vitals")] = {
        "resting_heart_rate": {"min": 60.0, "max": 60.0, "sum": 600.0, "count": 10},
    }
    service.ingest(
        UID,
        {"health_vitals": {
            "resting_heart_rate": 58,
            "resting_heart_rate_measured_at": "2026-01-15T08:00:00-08:00",
            "resting_heart_rate_sample_id": "hr-real-1",
        }},
        client_ts=1_756_000_000.0,
    )
    doc = fake.daily[(UID, "2026-01-15", "health_vitals")]
    assert doc["resting_heart_rate"] == {"min": 58.0, "max": 58.0, "sum": 58.0, "count": 1}
    assert doc["_ts_kind"] == "measured"


def test_stale_resample_after_cutover_is_rejected_not_refolded(monkeypatch):
    # Cutover already happened for this day (first measurement-aware report
    # landed and replaced the old arrival-tagged aggregate outright).
    fake = _env(monkeypatch)
    payload = {
        "health_vitals": {
            "resting_heart_rate": 58,
            "resting_heart_rate_measured_at": "2026-01-15T08:00:00-08:00",
            "resting_heart_rate_sample_id": "hr-real-1",
        }
    }
    service.ingest(UID, payload, client_ts=1_756_000_000.0)
    doc_after_cutover = fake.daily[(UID, "2026-01-15", "health_vitals")]
    assert doc_after_cutover["_ts_kind"] == "measured"
    assert doc_after_cutover["resting_heart_rate"]["count"] == 1

    # The phone keeps finding the SAME January sample as "newest within N
    # days" on every later report and resends it, now correctly stamped with
    # its true (unchanged) measured_at. This is genuinely stale re-traffic,
    # not a new reading -> must be deduped, never refolded a second time.
    for later_ts in (1_756_100_000.0, 1_756_200_000.0):
        service.ingest(UID, payload, client_ts=later_ts)
    doc_final = fake.daily[(UID, "2026-01-15", "health_vitals")]
    assert doc_final["resting_heart_rate"] == {"min": 58.0, "max": 58.0, "sum": 58.0, "count": 1}
    assert len(doc_final["_seen"]) == 1


# ---------------------------------------------------------------------------
# Old app versions: no measurement metadata at all -> unchanged behavior,
# even for a signal this module knows how to group.
# ---------------------------------------------------------------------------

def test_old_app_build_without_any_metadata_keeps_legacy_behavior(monkeypatch):
    fake = _env(monkeypatch)
    service.ingest(UID, {"health_body": {"weight_kg": 71.0, "bmi": 22.4}}, client_ts=1_756_000_000.0)
    rows = fake.list_perception_daily(UID, "health_body")
    assert len(rows) == 1
    doc = rows[0]["doc"]
    assert doc["weight_kg"] == 71.0
    assert doc["bmi"] == 22.4
    assert "_ts_kind" not in doc  # legacy path — no new bookkeeping at all.


# ---------------------------------------------------------------------------
# Observation state: no-observation vs unavailable stay distinguishable.
# ---------------------------------------------------------------------------

def test_no_observation_recorded_when_a_measurement_aware_group_is_null(monkeypatch):
    fake = _env(monkeypatch)
    service.ingest(
        UID,
        {"health_vitals": {
            "resting_heart_rate": None,
            "step_count": 9000,
            "step_count_measured_at": "2026-04-01T08:00:00-08:00",
            "step_count_sample_id": "steps-1",
        }},
        client_ts=1_756_000_000.0,
    )
    rows = fake.list_perception_daily(UID, "health_vitals")
    by_date = {r["date"]: r["doc"] for r in rows}
    steps_doc = by_date["2026-04-01"]
    assert steps_doc["_observed"]["step_count"] == "observed"
    # resting_heart_rate has no metadata this call -> falls back to today's
    # date bucket, but is still measurement-aware overall (step_count carried
    # metadata this report), so it gets observed-state bookkeeping too.
    today_docs = [d for date, d in by_date.items() if date != "2026-04-01"]
    assert any(d.get("_observed", {}).get("resting_heart_rate") == "no_observation" for d in today_docs)


def test_decrypt_failure_records_unavailable_not_no_observation(monkeypatch):
    fake = _env(monkeypatch)

    def boom(envelope, api_key, *, purpose):
        raise RuntimeError("enclave unreachable")

    results = service.ingest_snapshot_v2(
        UID,
        [{"key": "health_vitals", "envelope": {"id": "env1", "body_ct": "Y3Q="}, "changed": True}],
        client_ts=1_756_000_000.0,
        api_key="api-key",
        decrypt_envelope=boom,
    )
    assert results["health_vitals"] == "accepted"  # envelope taken; value withheld
    rows = fake.list_perception_daily(UID, "health_vitals")
    assert len(rows) == 1
    observed = rows[0]["doc"]["_observed"]
    assert observed["resting_heart_rate"] == "unavailable"
    assert "_ts_kind" not in rows[0]["doc"]  # no measurement time -> no cutover forced


# ---------------------------------------------------------------------------
# Sleep / workout: interval fields on the real wire names (`sleep_start`/
# `sleep_end`, `workout_start`/`workout_end` — no `_interval_` infix), end to
# end through service.ingest. Regression coverage for a naming mismatch that
# would otherwise leave both signals silently on the legacy arrival-date path.
# ---------------------------------------------------------------------------

def test_sleep_interval_attributes_to_wake_up_day(monkeypatch):
    fake = _env(monkeypatch)
    service.ingest(
        UID,
        {"health_sleep": {
            "asleep_minutes": 410,
            "core_minutes": 200,
            "deep_minutes": 90,
            "rem_minutes": 120,
            "sleep_start": "2026-03-01T23:00:00-05:00",
            "sleep_end": "2026-03-02T07:00:00-05:00",
            "sleep_sample_id": "sleep-1",
        }},
        client_ts=1_756_000_000.0,  # "today" is nowhere near March 2026.
    )
    rows = fake.list_perception_daily(UID, "health_sleep")
    by_date = {r["date"]: r["doc"] for r in rows}
    assert "2026-03-02" in by_date  # wake-up day, not the day sleep started.
    assert by_date["2026-03-02"]["_ts_kind"] == "measured"
    assert by_date["2026-03-02"]["asleep_minutes"] == 410


def test_workout_interval_attributes_and_dedups(monkeypatch):
    fake = _env(monkeypatch)
    payload = {
        "health_workout": {
            "workout_type": "run",
            "duration_min": 32,
            "count_today": 1,
            "workout_start": "2026-04-05T06:00:00-07:00",
            "workout_end": "2026-04-05T06:32:00-07:00",
            "workout_sample_id": "workout-1",
        }
    }
    service.ingest(UID, payload, client_ts=1_756_000_000.0)
    # Re-upload of the same workout sample must not double-count.
    service.ingest(UID, payload, client_ts=1_756_000_300.0)
    rows = fake.list_perception_daily(UID, "health_workout")
    by_date = {r["date"]: r["doc"] for r in rows}
    assert "2026-04-05" in by_date
    doc = by_date["2026-04-05"]
    # health_workout's rollup shape is an event list, not a numeric aggregate.
    assert len(doc["events"]) == 1
    assert doc["events"][0]["duration_min"] == 32
    assert len(doc["_seen"]) == 1


# ---------------------------------------------------------------------------
# Malformed metadata must never drop the value itself — only the bookkeeping
# falls back to today's report-arrival date.
# ---------------------------------------------------------------------------

def test_malformed_measured_at_still_folds_the_value(monkeypatch):
    fake = _env(monkeypatch)
    service.ingest(
        UID,
        {"health_body": {
            "weight_kg": 70.2,
            # No UTC offset — perceptkit.attribution refuses to guess one.
            "weight_kg_measured_at": "2026-01-15T08:00:00",
            "weight_kg_sample_id": "sample-weight-1",
        }},
        client_ts=1_756_000_000.0,
    )
    rows = fake.list_perception_daily(UID, "health_body")
    assert len(rows) == 1  # falls back to today's date bucket, not dropped.
    doc = rows[0]["doc"]
    assert doc["weight_kg"] == 70.2
    assert doc["_ts_kind"] == "measured"  # still measurement-aware (had a sample_id)
