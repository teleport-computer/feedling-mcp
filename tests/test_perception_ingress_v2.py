import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception import ingress_v2, service  # noqa: E402
from perception.differ_v2 import PerceptionDifferV2  # noqa: E402
from perception.signal_state_v2 import SignalObservationDecision  # noqa: E402
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


def _scripted_state_observer(*steps):
    remaining = iter(steps)

    def observe(
        _user_id,
        _signal,
        _value,
        *,
        observed_at,
        source_event_id=None,
        allow_first_event=False,
    ):
        del source_event_id, allow_first_event
        outcome, last_changed_at = next(remaining)
        seen = datetime.fromtimestamp(float(observed_at), tz=timezone.utc)
        changed = datetime.fromtimestamp(float(last_changed_at), tz=timezone.utc)
        return SignalObservationDecision(
            outcome=outcome,
            changed=outcome == "changed",
            fingerprint="test-fingerprint",
            last_seen_at=seen,
            last_changed_at=changed,
        )

    return observe


class _Store:
    def __init__(self):
        self.events = {}
        self.decrypt_failures = {}
        self.state = {}
        self.config = {}
        self.frames = {}
        self.items = {}

    def append_event(self, uid, event, ts):
        self.events.setdefault(uid, []).append(dict(event))

    def read_events(self, uid, limit=50):
        return list(self.events.get(uid, [])[-limit:])

    # pull_snapshot now includes the TTL-bounded app open/close trajectory.
    # These ingress tests do not seed app events, but their store double must
    # still implement the production read contract used by that snapshot.
    def read_app_opens(self, uid, limit=100, since_epoch=0.0):
        return []

    def read_app_closes(self, uid, limit=100, since_epoch=0.0):
        return []

    def append_decrypt_failure(self, uid, doc, ts):
        self.decrypt_failures.setdefault(uid, []).append(dict(doc))

    def read_decrypt_failures(self, uid, limit=50):
        return list(self.decrypt_failures.get(uid, [])[-limit:])

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
    user_id = "u1"
    observe_state = _scripted_state_observer(
        ("baseline_created", 10.0),
        ("unchanged", 10.0),
        ("changed", 30.0),
    )
    wakes = []

    first = observe_signal_v2(
        user_id,
        "wifi_anchor",
        {"anchor_id": "wifi-home", "label": "home"},
        ts=10.0,
        origin_refs=("ios_report:location_signal",),
        differ=PerceptionDifferV2(observe_state=observe_state),
        submit_wake=wakes.append,
    )
    repeat = observe_signal_v2(
        user_id,
        "wifi_anchor",
        {"anchor_id": "wifi-home", "label": "home"},
        ts=20.0,
        origin_refs=("ios_report:location_signal",),
        differ=PerceptionDifferV2(observe_state=observe_state),
        submit_wake=wakes.append,
    )
    moved = observe_signal_v2(
        user_id,
        "wifi_anchor",
        {"anchor_id": "wifi-work", "label": "work"},
        ts=30.0,
        origin_refs=("ios_report:location_signal",),
        differ=PerceptionDifferV2(observe_state=observe_state),
        submit_wake=wakes.append,
    )

    assert first.wake_events == ()
    assert repeat.wake_events == ()
    assert len(moved.wake_events) == 1
    assert len(wakes) == 1
    assert wakes[0].trigger == "arrived_at_anchor"
    assert wakes[0].origin_refs == ("ios_report:location_signal",)
    assert "wifi_anchor" in wakes[0].change_digest
    assert moved.result.state.last_seen_ts == 30.0
    assert moved.result.state.last_changed_ts == 30.0


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
    user_id = "u_wifi_anchor_decrypt"
    observe_state = _scripted_state_observer(
        ("baseline_created", 300.0),
        ("unchanged", 300.0),
        ("changed", 320.0),
    )
    monkeypatch.setattr(
        ingress_v2,
        "DEFAULT_DIFFER_V2",
        PerceptionDifferV2(observe_state=observe_state),
    )
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
        user_id,
        [{"key": "location_signal", "envelope": {"id": "loc_home_1"}, "changed": True}],
        client_ts=300.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )
    monkeypatch.setattr(
        ingress_v2,
        "DEFAULT_DIFFER_V2",
        PerceptionDifferV2(observe_state=observe_state),
    )
    repeat = service.ingest_snapshot_v2(
        user_id,
        [{"key": "location_signal", "envelope": {"id": "loc_home_2"}, "changed": True}],
        client_ts=310.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )
    monkeypatch.setattr(
        ingress_v2,
        "DEFAULT_DIFFER_V2",
        PerceptionDifferV2(observe_state=observe_state),
    )
    moved = service.ingest_snapshot_v2(
        user_id,
        [{"key": "location_signal", "envelope": {"id": "loc_work"}, "changed": True}],
        client_ts=320.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )

    assert first["location_signal"] == "accepted"
    assert repeat["location_signal"] == "accepted"
    assert moved["location_signal"] == "accepted"
    assert [event.trigger for event in emitted] == ["arrived_at_anchor"]
    assert emitted[0].origin_refs == ("ios_report:location_signal",)
    assert "wifi_anchor" in emitted[0].change_digest
    assert fake.get_state(user_id)["wifi_anchor_id"]["v"] == "wifi-work"


def test_location_snapshot_still_stores_when_durable_decision_fails_closed(monkeypatch):
    """A DB decision error may suppress a wake, but must not reject the upload."""
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(
        service, "_submit_wake_event_v2_compat", lambda event: emitted.append(event)
    )

    def decision_error(_uid, _signal, _value, *, observed_at, **_kwargs):
        del observed_at
        return SignalObservationDecision(
            outcome="error",
            changed=False,
            fingerprint=None,
            last_seen_at=None,
            last_changed_at=None,
            error_code="storage_error",
        )

    monkeypatch.setattr(
        ingress_v2,
        "DEFAULT_DIFFER_V2",
        PerceptionDifferV2(observe_state=decision_error),
    )

    def decrypt(_envelope, _api_key, *, purpose):
        assert purpose == "perception:location_signal"
        return json.dumps({
            "values": {"wifi_anchor_id": "wifi-home", "place_label": "unknown"}
        }).encode("utf-8")

    result = service.ingest_snapshot_v2(
        "u_location_fail_closed",
        [{
            "key": "location_signal",
            "envelope": {"id": "location-1"},
            "changed": True,
        }],
        client_ts=500.0,
        api_key="api-key",
        decrypt_envelope=decrypt,
    )

    assert result["location_signal"] == "accepted"
    assert (
        fake.get_state("u_location_fail_closed")["wifi_anchor_id"]["v"]
        == "wifi-home"
    )
    assert emitted == []


def test_snapshot_v2_records_an_audit_event_when_a_signal_fails_to_decrypt(monkeypatch):
    """A failed envelope decrypt must leave a trace.

    ``results[key]`` is set to "accepted" BEFORE the decrypt runs (the report
    contract is "we took your envelope"), and a failed decrypt then silently
    skipped the state write: no log, no counter, no event. A fleet-wide enclave
    hiccup or a caller that forgot to forward the api key therefore looks
    exactly like "this user has no reading" — the same invisibility that let
    the 2026-07-24 null-perception regression run for hours. The value write
    stays skipped (never store a guess); only observability is added.
    """
    fake = _Store()
    monkeypatch.setattr(service, "store", fake)

    def boom(envelope, api_key, *, purpose):
        raise RuntimeError("enclave unreachable")

    results = service.ingest_snapshot_v2(
        "u_decrypt_failure",
        [{"key": "location_signal", "envelope": {"id": "loc_1"}, "changed": True}],
        client_ts=500.0,
        api_key="api-key",
        decrypt_envelope=boom,
    )

    assert results["location_signal"] == "accepted"      # client contract unchanged
    assert fake.get_state("u_decrypt_failure") == {}      # no value invented

    failures = fake.read_decrypt_failures("u_decrypt_failure")
    assert len(failures) == 1
    assert failures[0]["key"] == "location_signal"
    assert failures[0]["reason"] == "decrypt_failed:RuntimeError"
    # NOT in the wake-audit stream: service._last_wake_ts / _last_v2_wake_ts scan
    # only the newest 50 rows there, so a burst of failures would push the last
    # "wake" row out of the window and silently disable burst/cluster dedup.
    assert fake.read_events("u_decrypt_failure") == []


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
    monkeypatch.setattr(
        ingress_v2,
        "DEFAULT_DIFFER_V2",
        PerceptionDifferV2(
            observe_state=_scripted_state_observer(("changed", service._now()))
        ),
    )
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


def test_device_event_route_surfaces_the_perception_ingress_result(monkeypatch):
    # The Flask /v1/device/events route was deleted in the ASGI cutover; this
    # exercises the framework-neutral core the route (and its ASGI counterpart)
    # delegate to — proactive_core.device_events_append — directly.
    #
    # 2026-07-25: this test used to assert the mirror case ("fence off => no
    # ingest"). That fork was the sibling of the /report regression and is
    # gone; the fence-independence of the ingest is now pinned by
    # test_device_event_ingress_runs_even_when_the_chat_fence_says_legacy.
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

    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled", lambda user_or_store: True)
    on = proactive_core.device_events_append(fake_store, {
        "type": "screen_frame",
        "payload": {"safe_screen_phash": "hash_b", "broadcast_state": "on"},
    })
    assert on["perception_v2"] == {"observations": 1, "wake_events": 1}
    assert calls and calls[-1][0] == "u_device"


def test_device_event_ingress_runs_even_when_the_chat_fence_says_legacy(monkeypatch):
    """Sibling of the 2026-07-25 /report hotfix (the test above).

    PR #107 tied ``perception_ingress_runtime_v2_enabled`` to the hosted chat
    runtime fence. ``/report`` was un-tied by the hotfix, but this second call
    site was missed — and it has NO legacy branch at all, so every
    resident-chat user (≈ all of prod) stopped producing device-event
    observations. That killed the only producers of the ``unlock_after_absence``
    and ``screen_phash`` wakes (perception/differ_v2.py): prod unlock wakes went
    from ~13/h to 0/h at the 07-24 10:12Z deploy and did NOT recover with the
    /report hotfix. Producing an observation is a data-integrity concern, not a
    runtime-lane concern — it must not depend on which chat runtime owns the
    user.
    """
    from proactive import proactive_core

    class DeviceStore:
        user_id = "u_device_legacy_fence"

        def __init__(self):
            self.events = []

        def append_device_event(self, event):
            self.events.append(dict(event))

        def list_device_events(self, since_epoch=0.0, limit=100):
            return list(self.events)[-limit:]

    calls = []
    monkeypatch.setattr(service, "ingest_device_event_v2", lambda uid, event: calls.append((uid, event)) or {
        "observations": 1,
        "wake_events": 1,
    })
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled", lambda user_or_store: False)

    # A non-capture-boundary event keeps this focused on the ingress fork
    # (a boundary event would drag the capture scheduler's DB path in).
    out = proactive_core.device_events_append(DeviceStore(), {
        "type": "screen_frame",
        "payload": {"safe_screen_phash": "hash_legacy_fence", "broadcast_state": "on"},
    })

    assert out["perception_v2"] == {"observations": 1, "wake_events": 1}
    assert calls and calls[-1][0] == "u_device_legacy_fence"


def test_device_event_phash_respects_broadcast_state(monkeypatch):
    user_id = "u1"
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: emitted.append(event))
    monkeypatch.setattr(
        ingress_v2,
        "DEFAULT_DIFFER_V2",
        PerceptionDifferV2(
            observe_state=_scripted_state_observer(
                ("changed", 101.0), ("duplicate", 101.0)
            )
        ),
    )
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
    observations = device_event_observations_v2(on_event)
    assert len(observations) == 1
    assert observations[0].source_event_id == "evt_on"
    assert observations[0].allow_first_event is True
    assert service.ingest_device_event_v2(user_id, off_event) == {"observations": 0, "wake_events": 0}
    assert service.ingest_device_event_v2(user_id, on_event)["wake_events"] == 1
    assert service.ingest_device_event_v2(user_id, on_event)["wake_events"] == 0
    assert len(emitted) == 1
    assert emitted[0].source == "scene_change"
    assert emitted[0].origin_refs == ("device_event:evt_on",)


def test_device_event_unlock_after_absence_wakes(monkeypatch):
    fake = _Store()
    emitted = []
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: emitted.append(event))
    monkeypatch.setattr(
        ingress_v2,
        "DEFAULT_DIFFER_V2",
        PerceptionDifferV2(
            observe_state=_scripted_state_observer(("changed", 400.0))
        ),
    )
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


def test_report_decrypts_context_snapshot_even_for_resident_chat_users(monkeypatch):
    """HOTFIX 2026-07-25 回归钉。

    PR #107 把 report 的 context_snapshot 分叉绑到 chat runtime fence——
    resident-chat 用户(≈全 prod)掉进 legacy 不解密路径:加密信封(location/
    calendar/playback/health)没人解密,state 值全空而 freshness ts 照走,
    agent 看到 null(usr_450e 报障,全 prod trace 零 perception:* 解密)。
    上报合同是全量 v2 加密的——解密永远发生,与 chat fence 无关。此测试钉住:
    即使 fence 判 legacy,report 也必须走 v2 ingest 并解出明文进 state。
    """
    from perception import perception_read_core

    fake = _Store()
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: None)
    # chat fence 说 resident/legacy —— 解密必须照样发生
    monkeypatch.setattr(
        service, "perception_ingress_runtime_v2_enabled", lambda s: False)

    calls = []

    def fake_decrypt(envelope, api_key, *, purpose):
        calls.append((purpose, api_key))
        return json.dumps({
            "values": {
                "output_type": "bluetooth",
                "is_bluetooth": True,
                "device_name": "AirPods",
            },
            "message": "audio fresh",
        }).encode("utf-8")

    monkeypatch.setattr(
        service.core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    user_store = SimpleNamespace(user_id="u_resident_decrypt")
    body, status = perception_read_core.report(
        user_store,
        {
            "context_snapshot": [
                {"key": "audio_route", "envelope": {"id": "env_a1"}, "changed": True},
            ],
            "client_ts": 100.0,
        },
        api_key="user-api-key",
    )

    assert status == 200
    assert body["results"]["audio_route"] == "accepted"
    # 解密真的发生了(经 enclave,带用户 key、按 purpose 归因)
    assert calls == [("perception:audio_route", "user-api-key")]
    # 明文值展开进 state —— agent 拉 snapshot 不再是 null
    state = fake.get_state("u_resident_decrypt")
    assert state["output_type"]["v"] == "bluetooth"
    assert state["device_name"]["v"] == "AirPods"
