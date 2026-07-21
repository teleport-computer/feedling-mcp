import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception import service  # noqa: E402
from perception.differ_v2 import PerceptionDifferV2  # noqa: E402
from perception.ingress_v2 import (  # noqa: E402
    device_event_observations_v2,
    observe_signal_v2,
)
from proactive.adapters_v2 import wake_event_v2_from_legacy_job  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures" / "perception_ios_v2"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _activate_proactive(monkeypatch) -> None:
    monkeypatch.setattr(service, "_proactive_activation_ready", lambda uid: True)


class _Store:
    def __init__(self):
        self.events = {}
        self.state = {}
        self.config = {}
        self.frames = {}
        self.items = {}

    def append_event(self, uid, event, ts):
        self.events.setdefault(uid, []).append(dict(event))

    def read_events(self, uid, limit=50):
        return list(self.events.get(uid, [])[-limit:])

    def get_config(self, uid):
        return dict(self.config.get(uid, {}))

    def get_state(self, uid):
        return {k: dict(v) for k, v in self.state.get(uid, {}).items()}

    def merge_state_guarded(self, uid, patch):
        cur = self.state.setdefault(uid, {})
        cur.update({k: dict(v) for k, v in patch.items()})
        return set(patch)

    def put_photo_envelope(self, uid, frame_id, ts, env):
        self.frames[(uid, frame_id)] = dict(env)

    def item_upsert(self, uid, kind, item_id, ts, doc, expires_at=None):
        self.items[(uid, kind, item_id)] = dict(doc)

    def item_get(self, uid, kind, item_id, now=None):
        return self.items.get((uid, kind, item_id))

    def item_list(self, uid, kind, limit=20, now=None):
        return [
            doc for (row_uid, row_kind, _), doc in self.items.items()
            if row_uid == uid and row_kind == kind
        ][:limit]


def test_anchor_transition_wakes_once_and_repeat_only_updates_seen():
    differ = PerceptionDifferV2()
    wakes = []

    first = observe_signal_v2(
        "u1",
        "wifi_anchor",
        {"anchor_id": "wifi-home", "label": "home"},
        ts=10.0,
        origin_refs=("ios_report:location_signal",),
        differ=differ,
        submit_wake=wakes.append,
    )
    repeat = observe_signal_v2(
        "u1",
        "wifi_anchor",
        {"anchor_id": "wifi-home", "label": "home"},
        ts=20.0,
        origin_refs=("ios_report:location_signal",),
        differ=differ,
        submit_wake=wakes.append,
    )

    assert len(first.wake_events) == 1
    assert repeat.wake_events == ()
    assert len(wakes) == 1
    assert wakes[0].trigger == "arrived_at_anchor"
    assert wakes[0].origin_refs == ("ios_report:location_signal",)
    assert "wifi_anchor" in wakes[0].change_digest
    assert repeat.result.state.last_seen_ts == 20.0
    assert repeat.result.state.last_changed_ts == 10.0


def test_continuous_signals_produce_zero_wakes_through_ingress():
    differ = PerceptionDifferV2()
    wakes = []

    for signal, value in (
        ("motion_state", {"state": "walking"}),
        ("battery", {"level": 0.7}),
        ("now_playing", {"title": "Song"}),
        ("time", {"local_time": "2026-06-19T21:00:00+08:00"}),
        ("place_label", "home"),
    ):
        observed = observe_signal_v2(
            "u1",
            signal,
            value,
            ts=30.0,
            origin_refs=(f"test:{signal}",),
            differ=differ,
            submit_wake=wakes.append,
        )
        assert observed.wake_events == ()

    assert wakes == []


def test_pr6b_real_ios_report_fixture_is_accepted_without_wake_or_plaintext_state(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: emitted.append(event))

    results = service.ingest_snapshot_v2(
        "u1",
        _load_fixture("ios_report_full_changed.json")["context_snapshot"],
        client_ts=1781874000,
    )

    assert results["location_signal"] == "accepted"
    assert results["motion_state"] == "accepted"
    assert results["calendar_next_event"] == "accepted"
    assert results["playback"] == "accepted"
    assert results["focus"] == "accepted"
    assert emitted == []
    state = fake.get_state("u1")
    assert "local_time" in state
    assert "motion_state" not in state
    assert "now_playing" not in state


def test_weather_health_and_focus_ingress_are_pull_only_after_decrypt(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_submit_wake_event_v2_compat", lambda event: emitted.append(event))

    plaintext_by_id = {
        "env_audio": {
            "values": {"output_type": "bluetooth", "is_bluetooth": True, "device_name": "Headphones"},
            "message": "audio fresh",
        },
        "env_weather": {
            "values": {"condition": "rain", "temperature": 23.4, "is_daylight": False},
            "message": "weather fresh",
        },
        "env_sleep": {"values": {"asleep_minutes": 420}, "message": "sleep fresh"},
        "env_workout": {
            "values": {"workout_type": "running", "duration_min": 30, "count_today": 1},
            "message": "workout fresh",
        },
        "env_vitals": {
            "values": {"resting_heart_rate": 60, "step_count": 3500},
            "message": "vitals fresh",
        },
    }

    def decrypt(envelope, api_key, *, purpose):
        assert api_key == "api-key"
        assert purpose.startswith("perception:")
        return json.dumps(plaintext_by_id[envelope["id"]]).encode("utf-8")

    results = service.ingest_snapshot_v2(
        "u_weather_health",
        [
            {"key": "focus", "data": json.dumps({"authorization_status": "authorized", "focused": True})},
            {"key": "audio_route", "envelope": {"id": "env_audio"}, "changed": True},
            {"key": "weather", "envelope": {"id": "env_weather"}, "changed": True},
            {"key": "health_sleep", "envelope": {"id": "env_sleep"}, "changed": True},
            {"key": "health_workout", "envelope": {"id": "env_workout"}, "changed": True},
            {"key": "health_vitals", "envelope": {"id": "env_vitals"}, "changed": True},
        ],
        client_ts=200.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )

    assert results["focus"] == "accepted"
    for key in ("audio_route", "weather", "health_sleep", "health_workout", "health_vitals"):
        assert results[key] == "accepted"
    state = fake.get_state("u_weather_health")
    assert state["focus_authorization_status"]["v"] == "authorized"
    assert state["in_focus"]["v"] is True
    assert state["output_type"]["v"] == "bluetooth"
    assert state["is_bluetooth"]["v"] is True
    assert state["device_name"]["v"] == "Headphones"
    assert state["condition"]["v"] == "rain"
    assert state["condition"]["msg"] == "weather fresh"
    assert state["temperature"]["v"] == 23.4
    assert state["is_daylight"]["v"] is False
    assert state["asleep_minutes"]["v"] == 420
    assert state["workout_type"]["v"] == "running"
    assert state["duration_min"]["v"] == 30
    assert state["count_today"]["v"] == 1
    assert state["resting_heart_rate"]["v"] == 60
    assert state["step_count"]["v"] == 3500
    assert service.pull_snapshot("u_weather_health", now=200.0)["in_focus"] is True
    assert service.pull_snapshot("u_weather_health", now=200.0)["output_type"] == "bluetooth"
    assert emitted == []


def test_encrypted_body_output_key_values_are_unwrapped_before_storage(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_submit_wake_event_v2_compat", lambda event: emitted.append(event))

    plaintext_by_id = {
        "env_motion": {
            "values": {"motion_state": {"state": "walking", "confidence": 0.9, "started_at": 100.0}},
            "message": "motion fresh",
        },
        "env_calendar": {
            "values": {
                "calendar_next_event": {"title": "1:1", "starts_in_min": 25},
                "calendar_events": [
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
                ],
                "calendar_events_truncated": False,
            },
            "message": "calendar fresh",
        },
        "env_playback": {
            "values": {"now_playing": {"title": "Song", "artist": "Artist"}},
            "message": "playback fresh",
        },
    }

    def decrypt(envelope, api_key, *, purpose):
        assert api_key == "api-key"
        assert purpose.startswith("perception:")
        return json.dumps(plaintext_by_id[envelope["id"]]).encode("utf-8")

    results = service.ingest_snapshot_v2(
        "u_output_key_values",
        [
            {"key": "motion_state", "envelope": {"id": "env_motion"}, "changed": True},
            {"key": "calendar_next_event", "envelope": {"id": "env_calendar"}, "changed": True},
            {"key": "playback", "envelope": {"id": "env_playback"}, "changed": True},
        ],
        client_ts=250.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )

    assert results["motion_state"] == "accepted"
    assert results["calendar_next_event"] == "accepted"
    assert results["playback"] == "accepted"
    state = fake.get_state("u_output_key_values")
    assert state["motion_state"]["v"] == {"state": "walking", "confidence": 0.9, "started_at": 100.0}
    assert state["motion_state"]["msg"] == "motion fresh"
    assert state["calendar_next_event"]["v"] == {"title": "1:1", "starts_in_min": 25}
    assert [event["title"] for event in state["calendar_events"]["v"]] == ["Yesterday review", "1:1"]
    assert state["calendar_events_truncated"]["v"] is False
    assert state["now_playing"]["v"] == {"title": "Song", "artist": "Artist"}
    assert "values" not in state["motion_state"]["v"]
    assert "motion_state" not in state["motion_state"]["v"]
    snapshot = service.pull_snapshot("u_output_key_values", now=250.0)
    assert snapshot["motion_state"] == {"state": "walking", "confidence": 0.9, "started_at": 100.0}
    assert snapshot["calendar_next_event"] == {"title": "1:1", "starts_in_min": 25}
    assert [event["title"] for event in snapshot["calendar_events"]] == ["Yesterday review", "1:1"]
    assert snapshot["calendar_events_truncated"] is False
    assert snapshot["now_playing"] == {"title": "Song", "artist": "Artist"}
    assert emitted == []


def test_calendar_encrypted_body_missing_next_event_clears_old_next_event(monkeypatch):
    fake = _Store()
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_submit_wake_event_v2_compat", lambda event: None)

    fake.merge_state_guarded("u_calendar_clear", {
        "calendar_next_event": {"v": {"title": "old event"}, "ts": 100.0, "msg": "old"},
    })

    plaintext = {
        "values": {
            "calendar_events": [
                {
                    "title": "All hands",
                    "next_event_time": "2026-06-24T12:00:00+08:00",
                    "end_time": "2026-06-24T13:00:00+08:00",
                    "event_kind": "meeting",
                    "attendee_count": 10,
                    "is_all_day": False,
                    "duration_min": 60,
                    "minutes_until_start": 120,
                },
            ],
            "calendar_events_truncated": False,
        },
        "message": "calendar fresh",
    }

    def decrypt(envelope, api_key, *, purpose):
        assert purpose == "perception:calendar_next_event"
        return json.dumps(plaintext).encode("utf-8")

    results = service.ingest_snapshot_v2(
        "u_calendar_clear",
        [{"key": "calendar_next_event", "envelope": {"id": "calendar_no_next"}, "changed": True}],
        client_ts=300.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )

    assert results["calendar_next_event"] == "accepted"
    state = fake.get_state("u_calendar_clear")
    assert state["calendar_next_event"]["v"] is None
    assert [event["title"] for event in state["calendar_events"]["v"]] == ["All hands"]
    assert state["calendar_events_truncated"]["v"] is False


def test_location_signal_decrypt_feeds_wifi_anchor_differ_once(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_submit_wake_event_v2_compat", lambda event: emitted.append(event))

    plaintext_by_id = {
        "loc_home_1": {
            "values": {"place_label": "unknown", "wifi_label": None, "country": "US", "wifi_anchor_id": "wifi-home"},
            "message": "location fresh",
        },
        "loc_home_2": {
            "values": {"place_label": "unknown", "wifi_label": None, "country": "US", "wifi_anchor_id": "wifi-home"},
            "message": "location fresh",
        },
        "loc_work": {
            "values": {"place_label": "unknown", "wifi_label": None, "country": "US", "wifi_anchor_id": "wifi-work"},
            "message": "location fresh",
        },
    }

    def decrypt(envelope, api_key, *, purpose):
        assert api_key == "api-key"
        assert purpose == "perception:location_signal"
        return json.dumps(plaintext_by_id[envelope["id"]]).encode("utf-8")

    first = service.ingest_snapshot_v2(
        "u_wifi_anchor_decrypt",
        [{"key": "location_signal", "envelope": {"id": "loc_home_1"}, "changed": True}],
        client_ts=300.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )
    repeat = service.ingest_snapshot_v2(
        "u_wifi_anchor_decrypt",
        [{"key": "location_signal", "envelope": {"id": "loc_home_2"}, "changed": True}],
        client_ts=310.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )
    moved = service.ingest_snapshot_v2(
        "u_wifi_anchor_decrypt",
        [{"key": "location_signal", "envelope": {"id": "loc_work"}, "changed": True}],
        client_ts=320.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )

    assert first["location_signal"] == "accepted"
    assert repeat["location_signal"] == "accepted"
    assert moved["location_signal"] == "accepted"
    assert [event.trigger for event in emitted] == ["arrived_at_anchor", "arrived_at_anchor"]
    assert emitted[0].origin_refs == ("ios_report:location_signal",)
    assert "wifi_anchor" in emitted[0].change_digest
    assert fake.get_state("u_wifi_anchor_decrypt")["wifi_anchor_id"]["v"] == "wifi-work"


def test_location_signal_null_or_unchanged_anchor_does_not_wake(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_submit_wake_event_v2_compat", lambda event: emitted.append(event))

    plaintext_by_id = {
        "loc_null": {
            "values": {"place_label": "unknown", "wifi_label": None, "country": "US", "wifi_anchor_id": None},
            "message": "location fresh",
        },
        "loc_unchanged": {
            "values": {"place_label": "unknown", "wifi_label": None, "country": "US", "wifi_anchor_id": "wifi-home"},
            "message": "location fresh",
        },
    }

    def decrypt(envelope, api_key, *, purpose):
        return json.dumps(plaintext_by_id[envelope["id"]]).encode("utf-8")

    service.ingest_snapshot_v2(
        "u_wifi_anchor_noop",
        [{"key": "location_signal", "envelope": {"id": "loc_null"}, "changed": True}],
        client_ts=300.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )
    service.ingest_snapshot_v2(
        "u_wifi_anchor_unchanged",
        [{"key": "location_signal", "envelope": {"id": "loc_unchanged"}, "changed": False}],
        client_ts=300.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )

    assert emitted == []


def test_ingress_flag_follows_hosted_runtime_fence_not_an_independent_flag(monkeypatch):
    """Dual-runtime coexistence spec §6: perception ingress follows the same
    per-user fence chat send routes on, so it can never drift from chat mode.
    The old independent ``perception_ingress_runtime_v2_enabled`` profile/config
    flag and the env-gated baseline are both vestigial now — only the fence
    (``hosted.config_store.get_hosted_runtime_mode_strict``) decides."""
    from hosted import config_store as hosted_config_store

    user_store = SimpleNamespace(user_id="u_flag")

    # A stale/legacy flag value in the profile no longer has any effect —
    # only the fence mode does.
    monkeypatch.setattr(
        hosted_config_store,
        "get_hosted_runtime_mode_strict",
        lambda store: hosted_config_store.HOSTED_RUNTIME_MODE_RESIDENT,
    )
    assert service.perception_ingress_runtime_v2_enabled(user_store) is False

    monkeypatch.setattr(
        hosted_config_store,
        "get_hosted_runtime_mode_strict",
        lambda store: hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    )
    assert service.perception_ingress_runtime_v2_enabled(user_store) is True

    def _raise(store):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(hosted_config_store, "get_hosted_runtime_mode_strict", _raise)
    assert service.perception_ingress_runtime_v2_enabled(user_store) is False


def test_photo_added_wake_is_differ_event_with_digest_and_origin_refs(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: emitted.append(event))
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled", lambda user_or_store: True)
    _activate_proactive(monkeypatch)

    out, code = service.photo_evaluate(
        "u1",
        {"scene_hint": "food"},
        {"id": "photo_1", "body_ct": "cipher"},
    )

    assert code == 200 and out["status"] == "stored"
    assert len(emitted) == 1
    assert emitted[0].trigger == "photo_added"
    assert emitted[0].origin_refs == ("photo:photo_1",)
    assert "photo_added" in emitted[0].change_digest


def test_device_event_route_only_runs_perception_ingress_when_flag_on(monkeypatch):
    # The Flask /v1/device/events route was deleted in the ASGI cutover; this
    # exercises the framework-neutral core the route (and its ASGI counterpart)
    # delegate to — proactive_core.device_events_append — directly.
    from proactive import proactive_core

    class DeviceStore:
        user_id = "u_device"

        def __init__(self):
            self.events = []

        def append_device_event(self, event):
            self.events.append(dict(event))

        def list_device_events(self, since_epoch=0.0, limit=100):
            return list(self.events)[-limit:]

    fake_store = DeviceStore()
    calls = []

    monkeypatch.setattr(service, "ingest_device_event_v2", lambda uid, event: calls.append((uid, event)) or {
        "observations": 1,
        "wake_events": 1,
    })

    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled", lambda user_or_store: False)
    off = proactive_core.device_events_append(fake_store, {
        "type": "screen_frame",
        "payload": {"safe_screen_phash": "hash_a", "broadcast_state": "on"},
    })
    assert "perception_v2" not in off
    assert calls == []

    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled", lambda user_or_store: True)
    on = proactive_core.device_events_append(fake_store, {
        "type": "screen_frame",
        "payload": {"safe_screen_phash": "hash_b", "broadcast_state": "on"},
    })
    assert on["perception_v2"] == {"observations": 1, "wake_events": 1}
    assert calls and calls[-1][0] == "u_device"


def test_device_event_phash_respects_broadcast_state(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: emitted.append(event))
    _activate_proactive(monkeypatch)

    off_event = {
        "event_id": "evt_off",
        "ts": 100.0,
        "type": "screen_frame",
        "payload": {"safe_screen_phash": "hash_a", "broadcast_state": "off"},
    }
    on_event = {
        "event_id": "evt_on",
        "ts": 101.0,
        "type": "screen_frame",
        "payload": {"safe_screen_phash": "hash_b", "broadcast_state": "on"},
    }

    assert device_event_observations_v2(off_event) == ()
    assert service.ingest_device_event_v2("u1", off_event) == {"observations": 0, "wake_events": 0}
    assert service.ingest_device_event_v2("u1", on_event)["wake_events"] == 1
    assert emitted[0].source == "scene_change"
    assert emitted[0].origin_refs == ("device_event:evt_on",)


def test_device_event_unlock_after_absence_wakes(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: emitted.append(event))
    _activate_proactive(monkeypatch)

    out = service.ingest_device_event_v2("u_unlock_after_absence", {
        "event_id": "evt_unlock",
        "ts": 400.0,
        "type": "unlock_after_absence",
        "payload": {"wake_trigger": "unlock_after_absence", "idle_sec": 3600},
    })

    assert out == {"observations": 1, "wake_events": 1}
    assert emitted[0].trigger == "unlock_after_absence"
    assert emitted[0].origin_refs == ("device_event:evt_unlock",)


def test_compatibility_job_adapter_preserves_presence_hints():
    event = wake_event_v2_from_legacy_job(
        "u1",
        {
            "job_id": "pj_1",
            "trigger": "arrived_at_anchor",
            "ts": 100.0,
            "change_digest": "wifi_anchor: none -> home",
            "presence_hints": {"entered_anchor": "home"},
            "origin_refs": ["ios_report:location_signal"],
        },
    )

    assert event.source == "perception_event"
    assert event.presence_hints == {"entered_anchor": "home"}
    assert event.origin_refs == ("ios_report:location_signal",)
