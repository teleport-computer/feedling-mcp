"""Unit tests for the field-agnostic quantitative history aggregator.

Pure functions — no DB. Covers each shape + the key property that NEW fields
flow through with zero code change (docs/PERCEPTION_HISTORY_SPEC, since removed — see git history; principle #2)."""
from datetime import datetime, timezone

import pytest

from perception import catalog, history


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


def test_notable_changes_discovers_numeric_fields_sorts_and_caps():
    rows_by_signal = {
        "health_vitals": [
            {"date": "2026-06-23", "doc": {"resting_heart_rate": {"sum": 120, "count": 2, "min": 58, "max": 62}}},
            {"date": "2026-06-24", "doc": {"resting_heart_rate": {"sum": 120, "count": 2, "min": 59, "max": 61}}},
            {"date": "2026-06-25", "doc": {"resting_heart_rate": {"sum": 132, "count": 2, "min": 65, "max": 67}}},
        ],
        "health_activity": [
            {"date": "2026-06-23", "doc": {"active_energy_kcal": {"total": 200}}},
            {"date": "2026-06-24", "doc": {"active_energy_kcal": {"total": 200}}},
            {"date": "2026-06-25", "doc": {"active_energy_kcal": {"total": 500}}},
        ],
        "weather": [
            {"date": "2026-06-23", "doc": {"temperature": {"sum": 40, "count": 2, "min": 19, "max": 21}}},
            {"date": "2026-06-24", "doc": {"temperature": {"sum": 40, "count": 2, "min": 19, "max": 21}}},
            {"date": "2026-06-25", "doc": {"temperature": {"sum": 60, "count": 2, "min": 29, "max": 31}}},
        ],
        "health_body": [
            {"date": "2026-06-23", "doc": {"weight_kg": 50.0, "_at": 1.0}},
            {"date": "2026-06-24", "doc": {"weight_kg": 50.0, "_at": 2.0}},
            {"date": "2026-06-25", "doc": {"weight_kg": 40.0, "_at": 3.0}},
        ],
    }

    now = 1_000_000.0
    changes = history.notable_changes(
        rows_by_signal,
        last_report_ts_by_signal={
            "health_activity": {"active_energy_kcal": now},
            "weather": {"temperature": now},
        },
        now=now,
        timezone_name="Asia/Shanghai",
        max_changes=2,
    )

    assert [(c["signal"], c["field"]) for c in changes] == [
        ("health_activity", "active_energy_kcal"),
        ("weather", "temperature"),
    ]
    assert changes[0]["current"] == 500.0
    assert changes[0]["baseline_median"] == 200.0
    assert changes[0]["delta"] == 300.0
    assert changes[0]["direction"] == "up"
    assert changes[0]["magnitude"] == 1.5


def test_notable_changes_requires_two_baseline_values_and_current_delta():
    rows_by_signal = {
        "health_vitals": [
            {"date": "2026-06-24", "doc": {"resting_heart_rate": {"sum": 120, "count": 2, "min": 58, "max": 62}}},
            {"date": "2026-06-25", "doc": {"resting_heart_rate": {"sum": 132, "count": 2, "min": 65, "max": 67}}},
        ],
        "weather": [
            {"date": "2026-06-23", "doc": {"condition": "rain"}},
            {"date": "2026-06-24", "doc": {"condition": "sun"}},
            {"date": "2026-06-25", "doc": {"condition": "sun"}},
        ],
    }

    assert history.notable_changes(
        rows_by_signal,
        last_report_ts_by_signal={},
        now=1_000_000.0,
        timezone_name=None,
    ) == []


def _step_count_rows():
    return {
        "health_vitals": [
            {"date": "2026-08-23", "doc": {"step_count": {"max": 1000}}},
            {"date": "2026-08-24", "doc": {"step_count": {"max": 1000}}},
            {"date": "2026-08-25", "doc": {"step_count": {"max": 1800}}},
        ]
    }


def test_notable_changes_mirrors_fresh_and_stale_using_catalog_ttl():
    report_ts = datetime(2026, 8, 25, 15, 41, tzinfo=timezone.utc).timestamp()
    ttl = catalog.SIGNALS["health_vitals"].ttl_sec
    reports = {"health_vitals": {"step_count": report_ts}}

    fresh = history.notable_changes(
        _step_count_rows(),
        last_report_ts_by_signal=reports,
        now=report_ts + ttl,
        timezone_name="Asia/Shanghai",
    )[0]
    stale = history.notable_changes(
        _step_count_rows(),
        last_report_ts_by_signal=reports,
        now=report_ts + ttl + 1,
        timezone_name="Asia/Shanghai",
    )[0]

    assert fresh["current"] == 1800.0
    assert "last_known" not in fresh
    assert "as_of" not in fresh
    assert "current" not in stale
    assert stale["last_known"] == 1800.0
    assert stale["as_of"] == "2026-08-25 23:41"
    for field in ("signal", "field", "baseline_median", "delta", "direction", "magnitude"):
        assert stale[field] == fresh[field]


def test_notable_changes_missing_field_timestamp_is_last_known_without_as_of():
    change = history.notable_changes(
        _step_count_rows(),
        last_report_ts_by_signal={},
        now=1_000_000.0,
        timezone_name="Asia/Shanghai",
    )[0]

    assert "current" not in change
    assert change["last_known"] == 1800.0
    assert change["as_of"] is None


@pytest.mark.parametrize("timezone_name", [None, "not/a-zone"])
def test_notable_changes_unknown_timezone_labels_utc(timezone_name):
    report_ts = datetime(2026, 8, 25, 15, 41, tzinfo=timezone.utc).timestamp()
    change = history.notable_changes(
        _step_count_rows(),
        last_report_ts_by_signal={"health_vitals": {"step_count": report_ts}},
        now=report_ts + catalog.SIGNALS["health_vitals"].ttl_sec + 1,
        timezone_name=timezone_name,
    )[0]

    assert change["as_of"] == "2026-08-25 15:41 UTC"


def test_cross_domain_recent_balances_domains_and_folds_health():
    rows_by_signal = {
        # health: two comparable signals -> folded into ONE health domain entry
        "health_vitals": [
            {"date": "2026-06-23", "doc": {"resting_heart_rate": {"sum": 120, "count": 2, "min": 58, "max": 62}}},
            {"date": "2026-06-24", "doc": {"resting_heart_rate": {"sum": 120, "count": 2, "min": 59, "max": 61}}},
            {"date": "2026-06-25", "doc": {"resting_heart_rate": {"sum": 132, "count": 2, "min": 65, "max": 67}}},
        ],
        "health_activity": [
            {"date": "2026-06-23", "doc": {"active_energy_kcal": {"total": 200}}},
            {"date": "2026-06-24", "doc": {"active_energy_kcal": {"total": 200}}},
            {"date": "2026-06-25", "doc": {"active_energy_kcal": {"total": 500}}},
        ],
        # media tally: today's artist is new vs. prior days -> novelty new_artist
        "playback": [
            {"date": "2026-06-24", "doc": {"by_artist": {"The National": 30.0}, "total_minutes": 30.0,
                                            "distinct": ["Fake Empire"]}},
            {"date": "2026-06-25", "doc": {"by_artist": {"Phoebe Bridgers": 90.0}, "total_minutes": 90.0,
                                            "distinct": ["Motion Sickness", "Garden Song"]}},
        ],
        # place dwell: >=4h at the current place today -> novelty long_dwell
        "location_signal": [
            {"date": "2026-06-25", "doc": {"minutes": {"公司": 300.0}, "visited": ["家", "公司"]}},
        ],
    }
    snapshot = {
        "now_playing": {"title": "Motion Sickness", "artist": "Phoebe Bridgers"},
        "place_label": "公司",
        "broadcast_state": "off",
        "calendar_next_event": {"title": "站会", "start_time": "2026-06-26T10:00"},
        "recent_apps": [
            {"app": "Spotify", "ts": 30.0}, {"app": "Messages", "ts": 20.0}, {"app": "Spotify", "ts": 10.0},
        ],
    }
    pull = {"condition": "sunny", "temperature": 24.0}  # weather present; mood/reminders absent
    photos = [{"photo_id": "p1", "metadata": {"scene_hint": "food", "time_of_day": "evening"}}]

    board = history.cross_domain_recent(
        snapshot=snapshot, pull_snapshot=pull, rows_by_signal=rows_by_signal,
        last_report_ts_by_signal={
            "health_vitals": {"resting_heart_rate": 1_000_000.0},
            "health_activity": {"active_energy_kcal": 1_000_000.0},
        },
        now=1_000_000.0,
        timezone_name="Asia/Shanghai",
        photos=photos, max_health_notable=8,
    )

    # All life-context domains laid out; health is exactly ONE entry, not the headline.
    assert set(board) == {"location", "media", "app", "health", "weather",
                          "mood", "reminders", "calendar", "photos", "screen"}
    assert len(board["health"]["notable"]) == 2  # folded, both health signals as plain rows
    # media + place novelty surface as light factual hints
    assert board["media"]["now"] == {"title": "Motion Sickness", "artist": "Phoebe Bridgers"}
    assert board["media"]["novelty"] == "new_artist"
    assert board["location"]["now"] == "公司"
    assert board["location"]["novelty"] == "long_dwell"
    # app: most-recent first, deduped
    assert board["app"]["now"] == "Spotify"
    assert board["app"]["recent"] == ["Spotify", "Messages"]
    # honest-empty domains when data is missing
    assert board["weather"] == {"condition": "sunny", "temperature": 24.0}
    assert board["mood"] == {"status": "none"}
    assert board["photos"]["scenes"] == ["food"]
    assert board["screen"] == {"state": "off"}


def test_cross_domain_recent_degrades_to_empty_on_no_inputs():
    board = history.cross_domain_recent(
        snapshot=None, pull_snapshot=None, rows_by_signal=None, photos=None,
        last_report_ts_by_signal={}, now=1_000_000.0, timezone_name=None,
    )
    assert board["health"]["notable"] == []
    assert board["media"]["now"] is None
    assert board["weather"] == {"status": "none"}
    assert board["photos"] == {"recent_count": 0, "scenes": []}


def test_record_unhistorized_signal_raises():
    try:
        history.record_daily(None, "now", {"battery_level": 0.5})
        assert False, "expected ValueError"
    except ValueError:
        pass
