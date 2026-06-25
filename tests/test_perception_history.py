"""Unit tests for the field-agnostic quantitative history aggregator.

Pure functions — no DB. Covers each shape + the key property that NEW fields
flow through with zero code change (PERCEPTION_HISTORY_SPEC principle #2)."""
from perception import history


def test_is_historized_skips_pure_instant():
    assert history.is_historized("health_vitals")
    assert not history.is_historized("now")
    assert not history.is_historized("battery")


def test_numeric_dist_accumulates_min_max_sum_count():
    d = history.record_daily(None, "health_vitals", {"current_heart_rate": 60})
    d = history.record_daily(d, "health_vitals", {"current_heart_rate": 80})
    d = history.record_daily(d, "health_vitals", {"current_heart_rate": 70})
    hr = d["current_heart_rate"]
    assert hr == {"min": 60.0, "max": 80.0, "sum": 210.0, "count": 3}


def test_numeric_dist_is_field_agnostic_new_field_flows():
    # A field NOT named anywhere in history.py still aggregates.
    d = history.record_daily(None, "health_vitals", {"brand_new_metric": 1.0})
    d = history.record_daily(d, "health_vitals", {"brand_new_metric": 3.0})
    assert d["brand_new_metric"] == {"min": 1.0, "max": 3.0, "sum": 4.0, "count": 2}


def test_step_count_aggregates_and_reads_as_daily_total():
    # step_count is cumulative-within-day: aggregated via numeric_dist, but the
    # trend representative is max (= daily total), not the average.
    d = history.record_daily(None, "health_vitals", {"step_count": 1000, "current_heart_rate": 65})
    d = history.record_daily(d, "health_vitals", {"step_count": 1801, "current_heart_rate": 68})
    assert d["step_count"]["max"] == 1801.0
    rows = [{"date": "2026-06-25", "doc": d}]
    t = history.read_trend(rows, "health_vitals", "step_count")
    assert t["current"] == 1801.0  # daily total, not avg


def test_cumulative_takes_running_max():
    d = history.record_daily(None, "health_activity", {"active_energy_kcal": 120})
    d = history.record_daily(d, "health_activity", {"active_energy_kcal": 90})   # stale lower
    d = history.record_daily(d, "health_activity", {"active_energy_kcal": 200})
    assert d["active_energy_kcal"] == {"total": 200.0}


def test_main_of_day_replaces_with_latest_nonnull():
    d = history.record_daily(None, "health_body", {"weight_kg": 52.0, "bmi": None}, ts=1.0)
    d = history.record_daily(d, "health_body", {"weight_kg": 52.4, "bmi": 21.1}, ts=2.0)
    assert d["weight_kg"] == 52.4
    assert d["bmi"] == 21.1
    assert d["_at"] == 2.0


def test_duration_by_state_accumulates_on_prior_state():
    # at t=0 still; at t=600s (10min) walking -> 10min credited to still
    d = history.record_daily(None, "motion_state",
                             {"motion_state": {"state": "still"}}, ts=0.0)
    d = history.record_daily(d, "motion_state",
                             {"motion_state": {"state": "walking"}}, ts=600.0)
    d = history.record_daily(d, "motion_state",
                             {"motion_state": {"state": "walking"}}, ts=1200.0)
    assert d["minutes"]["still"] == 10.0
    assert d["minutes"]["walking"] == 10.0


def test_focus_duration_uses_bool_state():
    d = history.record_daily(None, "focus", {"in_focus": True}, ts=0.0)
    d = history.record_daily(d, "focus", {"in_focus": False}, ts=300.0)  # 5min focused
    assert d["minutes"]["focused"] == 5.0


def test_place_dwell_tracks_minutes_and_visited():
    d = history.record_daily(None, "location_signal", {"place_label": "home"}, ts=0.0)
    d = history.record_daily(d, "location_signal", {"place_label": "work"}, ts=1800.0)  # 30m home
    assert d["minutes"]["home"] == 30.0
    assert d["visited"] == ["home", "work"]


def test_event_list_dedups_by_id():
    ev = {"id": "evt1", "title": "1:1"}
    d = history.record_daily(None, "calendar_next_event", {"calendar_events": [ev]})
    d = history.record_daily(d, "calendar_next_event",
                             {"calendar_events": [ev, {"id": "evt2", "title": "standup"}]})
    titles = sorted(e["title"] for e in d["events"])
    assert titles == ["1:1", "standup"]


def test_event_list_workout_single_event():
    d = history.record_daily(None, "health_workout",
                             {"workout_type": "run", "duration_min": 30, "count_today": 1})
    assert len(d["events"]) == 1
    assert d["events"][0]["workout_type"] == "run"


def test_subjective_appends_each_entry():
    d = history.record_daily(None, "health_mood",
                             {"valence": 0.4, "valence_classification": "slightly_pleasant"}, ts=1.0)
    d = history.record_daily(d, "health_mood",
                             {"valence": -0.2, "valence_classification": "slightly_unpleasant"}, ts=2.0)
    assert len(d["entries"]) == 2
    assert d["entries"][0]["valence"] == 0.4


def test_tally_music_digest_minutes_and_top():
    # play artist A for 10min, then B for 5min
    d = history.record_daily(None, "playback",
                             {"now_playing": {"playback_state": "playing", "title": "t1", "artist": "A"}}, ts=0.0)
    d = history.record_daily(d, "playback",
                             {"now_playing": {"playback_state": "playing", "title": "t2", "artist": "B"}}, ts=600.0)
    d = history.record_daily(d, "playback",
                             {"now_playing": {"playback_state": "paused", "title": "t2", "artist": "B"}}, ts=900.0)
    assert d["total_minutes"] == 15.0
    assert d["by_artist"]["A"] == 10.0
    assert d["by_artist"]["B"] == 5.0
    assert set(d["distinct"]) == {"t1", "t2"}
    # total_minutes is trendable
    t = history.read_trend([{"date": "2026-06-25", "doc": d}], "playback", "total_minutes")
    assert t["current"] == 15.0


def test_tally_caps_top_n():
    d = None
    # 40 distinct artists each 1min -> by_artist capped to 30
    for i in range(41):
        d = history.record_daily(d, "playback",
                                 {"now_playing": {"playback_state": "playing", "title": f"t{i}", "artist": f"a{i}"}},
                                 ts=float(i * 60))
    assert len(d["by_artist"]) <= 30


def test_read_trend_numeric_dist_baseline_and_delta():
    # 3 baseline days avg ~58, current day avg 70 -> up
    rows = [
        {"date": "2026-06-23", "doc": {"resting_heart_rate": {"min": 56, "max": 60, "sum": 116, "count": 2}}},
        {"date": "2026-06-24", "doc": {"resting_heart_rate": {"min": 57, "max": 59, "sum": 116, "count": 2}}},
        {"date": "2026-06-25", "doc": {"resting_heart_rate": {"min": 68, "max": 72, "sum": 140, "count": 2}}},
    ]
    t = history.read_trend(rows, "health_vitals", "resting_heart_rate")
    assert [d["value"] for d in t["daily"]] == [58.0, 58.0, 70.0]
    assert t["current"] == 70.0
    assert t["baseline"]["median"] == 58.0
    assert t["delta"] == 12.0
    assert t["direction"] == "up"


def test_record_unhistorized_signal_raises():
    try:
        history.record_daily(None, "now", {"battery_level": 0.5})
        assert False, "expected ValueError"
    except ValueError:
        pass
