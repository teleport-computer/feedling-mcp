from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent import perception_core  # noqa: E402
from perception import history as perception_history  # noqa: E402
from perception import service as perception_service  # noqa: E402
from perception import store as perception_store  # noqa: E402


# The Flask /v1/agent/perception* routes were deleted in the ASGI cutover; this
# tiny client mimics the old Flask test-client response shape (.status_code /
# .get_json()) while routing straight to the framework-neutral perception_core
# the ASGI routes delegate to — so the test bodies below are unchanged.
class _Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def get_json(self):
        return self._body


class _CoreClient:
    def __init__(self, store):
        self._store = store

    def get(self, path):
        u = urlparse(path)
        q = parse_qs(u.query)
        one = lambda k: (q.get(k) or [None])[0]  # noqa: E731
        try:
            if u.path == "/v1/agent/perception":
                return _Resp(perception_core.agent_perception_payload(self._store, signals_raw=one("signals")))
            if u.path == "/v1/agent/perception/digest":
                return _Resp(perception_core.perception_digest_payload(self._store, days_raw=one("days")))
            raise AssertionError(f"unrouted path: {u.path}")
        except perception_core.AgentRouteError as err:
            return _Resp(err.body, err.status_code)


def _client(monkeypatch, *, settings=None, state=None, snapshot=None, pull=None):
    class Store:
        user_id = "u_agent"

        def load_proactive_settings(self):
            return dict(settings or {})

    monkeypatch.setattr(perception_store, "get_state", lambda uid: dict(state or {}))
    monkeypatch.setattr(perception_service, "snapshot", lambda uid: dict(snapshot or {}))
    monkeypatch.setattr(perception_service, "pull_snapshot", lambda uid: dict(pull or {}))
    monkeypatch.setattr(perception_service, "photos_recent",
                        lambda uid, limit=20: ({"photos": []}, 200))
    return _CoreClient(Store())


def test_agent_perception_returns_requested_fast_signals(monkeypatch):
    client = _client(
        monkeypatch,
        snapshot={
            "local_time": "2026-06-23T12:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "locale": "zh-Hans-CN",
            "battery_level": 0.72,
            "charging": False,
            "place_label": "home",
            "motion_state": "still",
            "now_playing": {"title": "Song"},
            "broadcast_state": "off",
            "broadcast_active": False,
            "user_state": "default",
        },
        pull={
            "place_label": "home",
            "wifi_label": "home_wifi",
            "country": "CN",
            "locality": "深圳市",
            "wifi_anchor_id": "wifi-home",
            "condition": "rain",
            "temperature": 23.4,
            "is_daylight": True,
        },
    )

    resp = client.get("/v1/agent/perception?signals=now,weather,location")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["signals"]["now"]["time"] == "2026-06-23T12:00:00+08:00"
    assert body["signals"]["now"]["battery_level"] == 0.72
    assert body["signals"]["weather"] == {
        "condition": "rain",
        "temperature": 23.4,
        "apparent_temperature": None,
        "humidity": None,
        "precipitation_chance": None,
        "uv_index": None,
        "is_daylight": True,
        "alerts": None,
    }
    assert body["signals"]["location"]["locality"] == "深圳市"
    assert body["signals"]["location"]["wifi_anchor_id"] == "wifi-home"


def test_agent_perception_calendar_returns_event_list(monkeypatch):
    calendar_events = [
        {
            "title": "Yesterday review",
            "next_event_time": "2026-06-22T09:00:00+08:00",
            "end_time": "2026-06-22T09:30:00+08:00",
            "event_kind": "meeting",
            "attendee_count": 2,
            "is_all_day": False,
            "duration_min": 30,
            "minutes_until_start": -1500,
        },
        {
            "title": "1:1",
            "next_event_time": "2026-06-23T10:00:00+08:00",
            "end_time": "2026-06-23T10:30:00+08:00",
            "event_kind": "meeting",
            "attendee_count": 2,
            "is_all_day": False,
            "duration_min": 30,
            "minutes_until_start": 25,
        },
    ]
    client = _client(
        monkeypatch,
        pull={
            "calendar_next_event": calendar_events[1],
            "calendar_events": calendar_events,
            "calendar_events_truncated": False,
        },
    )

    resp = client.get("/v1/agent/perception?signals=calendar")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["calendar"] == {
        "calendar_next_event": calendar_events[1],
        "calendar_events": calendar_events,
        "calendar_events_truncated": False,
    }


def test_agent_perception_slow_signals_return_inline_without_background(monkeypatch):
    client = _client(
        monkeypatch,
        pull={
            "step_count": 6500,
            "asleep_minutes": 420,
            "workout_type": "run",
            "duration_min": 30,
            "count_today": 1,
            "resting_heart_rate": 60,
        },
    )

    resp = client.get("/v1/agent/perception?signals=steps,sleep,workout,vitals")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["steps"] == {"step_count": 6500}
    assert body["signals"]["sleep"] == {
        "asleep_minutes": 420,
        "core_minutes": None,
        "deep_minutes": None,
        "rem_minutes": None,
    }
    assert body["signals"]["workout"] == {
        "workout_type": "run",
        "duration_min": 30,
        "count_today": 1,
    }
    assert body["signals"]["vitals"] == {
        "resting_heart_rate": 60,
        "step_count": 6500,
        "current_heart_rate": None,
        "hrv_sdnn_ms": None,
        "respiratory_rate": None,
        "oxygen_saturation_pct": None,
        "vo2_max": None,
    }
    assert "needs_background" not in str(body)


def test_agent_perception_pull_only_signals_return_inline(monkeypatch):
    client = _client(
        monkeypatch,
        pull={
            "focus_authorization_status": "authorized",
            "in_focus": True,
            "output_type": "bluetooth",
            "is_bluetooth": True,
            "device_name": "AirPods",
        },
    )

    resp = client.get("/v1/agent/perception?signals=focus,audio_route")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["focus"] == {
        "focus_authorization_status": "authorized",
        "in_focus": True,
    }
    assert body["signals"]["audio_route"] == {
        "output_type": "bluetooth",
        "is_bluetooth": True,
        "device_name": "AirPods",
    }


def test_agent_perception_app_signal_reads_shortcut_snapshot(monkeypatch):
    client = _client(
        monkeypatch,
        snapshot={
            "app_name": "Spotify",
            "app_category": "music",
        },
        pull={
            "app_name": "Stale Pull App",
            "app_category": "stale",
        },
    )

    resp = client.get("/v1/agent/perception?signals=app")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["app"] == {
        "app_name": "Spotify",
        "app_category": "music",
        "app_state": None,
    }


def test_agent_perception_pull_only_null_permission_messages_return_disabled(monkeypatch):
    client = _client(
        monkeypatch,
        state={
            "focus_authorization_status": {
                "v": None,
                "ts": 10.0,
                "msg": "专注模式未授权",
            },
            "output_type": {
                "v": None,
                "ts": 10.0,
                "msg": "audio route permission denied",
            },
        },
        pull={
            "focus_authorization_status": None,
            "in_focus": None,
            "output_type": None,
            "is_bluetooth": None,
            "device_name": None,
        },
    )

    resp = client.get("/v1/agent/perception?signals=focus,audio_route")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["focus"] == {"disabled": True, "reason": "not_permitted"}
    assert body["signals"]["audio_route"] == {"disabled": True, "reason": "not_permitted"}


def test_agent_perception_permission_states_return_disabled(monkeypatch):
    client = _client(
        monkeypatch,
        settings={
            "permission_states": {
                "weather": "off",
                "location": "not_permitted",
                "focus": "off",
                "audio_route": "not_permitted",
            }
        },
        pull={
            "condition": "rain",
            "place_label": "home",
            "focus_authorization_status": "authorized",
            "in_focus": True,
            "output_type": "bluetooth",
        },
    )

    resp = client.get("/v1/agent/perception?signals=weather,location,focus,audio_route")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["weather"] == {"disabled": True, "reason": "switch_off"}
    assert body["signals"]["location"] == {"disabled": True, "reason": "not_permitted"}
    assert body["signals"]["focus"] == {"disabled": True, "reason": "switch_off"}
    assert body["signals"]["audio_route"] == {"disabled": True, "reason": "not_permitted"}


def test_agent_perception_null_permission_message_returns_disabled_but_no_event_does_not(monkeypatch):
    client = _client(
        monkeypatch,
        state={
            "asleep_minutes": {
                "v": None,
                "ts": 10.0,
                "msg": "HealthKit 不可用，无法读取睡眠趋势",
            },
            "calendar_next_event": {
                "v": None,
                "ts": 10.0,
                "msg": "未来 24 小时内没有可用日程",
            },
        },
        pull={
            "asleep_minutes": None,
            "calendar_next_event": None,
        },
    )

    resp = client.get("/v1/agent/perception?signals=sleep,calendar")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["sleep"] == {"disabled": True, "reason": "not_permitted"}
    assert body["signals"]["calendar"] == {
        "calendar_next_event": None,
        "calendar_events": None,
        "calendar_events_truncated": None,
    }


def test_agent_perception_rejects_unknown_signals(monkeypatch):
    client = _client(monkeypatch)

    resp = client.get("/v1/agent/perception?signals=now,nope")
    body = resp.get_json()

    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"] == "unknown_signals"
    assert body["unknown"] == ["nope"]


def test_agent_perception_digest_returns_top_notable_changes(monkeypatch):
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
    }
    calls = []

    def fake_list(user_id, signal, days):
        calls.append((user_id, signal, days))
        return rows_by_signal.get(signal, [])

    monkeypatch.setenv("FEEDLING_DIGEST_NOTABLE_MAX", "1")
    monkeypatch.setattr(perception_store, "list_perception_daily", fake_list)
    client = _client(monkeypatch)

    resp = client.get("/v1/agent/perception/digest?days=7")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["days"] == 7
    assert body["changes"] == [{
        "signal": "health_activity",
        "field": "active_energy_kcal",
        "current": 500.0,
        "baseline_median": 200.0,
        "delta": 300.0,
        "direction": "up",
        "magnitude": 1.5,
    }]
    assert all(call[0] == "u_agent" and call[2] == 7 for call in calls)
    # The board also reads the two non-comparable history shapes it renders directly.
    assert {call[1] for call in calls} == (
        set(perception_history.comparable_signals()) | {"playback", "location_signal"}
    )
    # The balanced board ships alongside legacy changes; health is one folded entry
    # and (same rows, same cap) matches the legacy top-N here.
    assert body["domains"]["health"]["notable"] == body["changes"]
    assert set(body["domains"]) >= {
        "location", "media", "app", "health", "weather",
        "mood", "reminders", "calendar", "photos", "screen",
    }


def test_agent_perception_digest_empty_without_baseline(monkeypatch):
    monkeypatch.setattr(
        perception_store,
        "list_perception_daily",
        lambda user_id, signal, days: [
            {"date": "2026-06-24", "doc": {"resting_heart_rate": {"sum": 120, "count": 2, "min": 58, "max": 62}}},
            {"date": "2026-06-25", "doc": {"resting_heart_rate": {"sum": 132, "count": 2, "min": 65, "max": 67}}},
        ] if signal == "health_vitals" else [],
    )
    client = _client(monkeypatch)

    resp = client.get("/v1/agent/perception/digest")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["days"] == 30
    assert body["changes"] == []
    assert body["domains"]["health"]["notable"] == []
    assert "media" in body["domains"] and "location" in body["domains"]


def test_agent_perception_digest_rejects_invalid_days(monkeypatch):
    client = _client(monkeypatch)

    resp = client.get("/v1/agent/perception/digest?days=nope")
    body = resp.get_json()

    assert resp.status_code == 400
    assert body == {"ok": False, "error": "invalid_days"}
