import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception import ingress_v2, service
from perception.ios_contract_v2 import (  # noqa: E402
    EXPECTED_REPORT_KEYS_V2,
    classify_item_v2,
    classify_report_v2,
    missing_expected_keys_v2,
)
from perception.differ_v2 import PerceptionDifferV2  # noqa: E402
from perception.signal_state_v2 import SignalObservationDecision  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures" / "perception_ios_v2"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _by_key(signals):
    return {signal.key: signal for signal in signals}


def test_ios_contract_manifest_tracks_source_and_human_device_gate():
    manifest = _load("manifest.json")

    assert manifest["ios_repo"]["commit"] == "fd60d3f5813128e46f4e68b8cc0ed0b0740a53d0"
    assert manifest["human_device_report"]["status"] == "pending_user_verification"


def test_ios_full_report_fixture_classifies_current_payload_shape():
    payload = _load("ios_report_full_changed.json")
    signals = _by_key(classify_report_v2(payload))

    assert tuple(signals) == EXPECTED_REPORT_KEYS_V2
    assert payload["client_ts"] == "1781874000"

    assert signals["time"].data == {
        "local_time": "2026-06-19T21:00:00+08:00",
        "locale": "zh-Hans-CN",
        "timezone": "Asia/Shanghai",
    }
    assert signals["battery"].data == {"charging": True, "level": 0.82}
    assert signals["broadcast"].data == {"active": True, "state": "broadcasting"}
    assert signals["focus"].data == {
        "authorization_status": "authorized",
        "focused": True,
    }
    assert signals["focus"].differ_inputs == ()
    assert signals["focus"].wake_policy == "pull_only"

    for key in (
        "location_signal",
        "motion_state",
        "calendar_next_event",
        "playback",
        "audio_route",
        "weather",
        "health_sleep",
        "health_workout",
        "health_vitals",
    ):
        signal = signals[key]
        assert signal.status == "changed"
        assert signal.changed is True
        assert signal.encrypted is True
        assert signal.requires_decrypt is True
        assert signal.envelope_id

    assert signals["location_signal"].differ_inputs == (
        "connectivity_anchor",
        "wifi_anchor",
        "place_label",
    )
    assert signals["motion_state"].differ_inputs == ("motion_state",)
    assert signals["calendar_next_event"].differ_inputs == (
        "calendar_presence",
        "calendar_next_event",
    )
    assert signals["playback"].differ_inputs == ("now_playing",)
    assert signals["audio_route"].differ_inputs == ("audio_route",)
    for key in ("audio_route", "weather", "health_sleep", "health_workout", "health_vitals"):
        assert signals[key].differ_inputs == (key,)
        assert signals[key].wake_policy == "pull_only_after_decrypt"
    assert signals["unsupported"].status == "ignored"


def test_ios_unchanged_encrypted_signals_do_not_imply_wake():
    payload = _load("ios_report_unchanged.json")
    signals = _by_key(classify_report_v2(payload))

    for key in (
        "location_signal",
        "motion_state",
        "calendar_next_event",
        "playback",
        "audio_route",
        "weather",
        "health_sleep",
        "health_workout",
        "health_vitals",
    ):
        assert signals[key].status == "unchanged"
        assert signals[key].changed is False
        assert signals[key].requires_decrypt is True


def test_ios_missing_permission_and_unavailable_shapes_are_null_no_wake():
    payload = _load("ios_report_missing_permission_unavailable.json")
    signals = _by_key(classify_report_v2(payload))

    for key in (
        "location_signal",
        "motion_state",
        "focus",
        "calendar_next_event",
        "playback",
        "audio_route",
        "weather",
        "health_sleep",
        "health_workout",
        "health_vitals",
    ):
        assert signals[key].status == "unavailable"
        assert signals[key].wake_policy in {"no_wake", "pull_only"}
        assert signals[key].data is None


def test_ios_dropped_upload_fixture_makes_omission_explicit():
    payload = _load("ios_report_dropped_upload.json")
    manifest = _load("manifest.json")

    assert list(missing_expected_keys_v2(payload)) == manifest["dropped_upload"]["missing_keys"]


def test_unknown_ios_signal_fails_explicitly_instead_of_waking():
    signal = classify_item_v2({"key": "future_sensor", "data": "{\"value\":1}"})

    assert signal.status == "unknown_signal"
    assert signal.wake_policy == "error"


def test_contract_mapped_continuous_signals_produce_zero_differ_events():
    differ = PerceptionDifferV2()
    samples = (
        ("time", {"local_time": "2026-06-19T21:00:00+08:00"}),
        ("battery", {"level": 0.82, "charging": True}),
        ("motion_state", {"state": "walking", "confidence": "high"}),
        ("now_playing", {"playback_state": "playing", "title": "Song"}),
        ("audio_route", {"output_type": "speaker", "is_bluetooth": False}),
        ("place_label", "home"),
        ("weather", {"condition": "rain", "temperature": 23.4}),
        ("health_sleep", {"asleep_minutes": 420}),
        ("health_workout", {"workout_type": "running", "count_today": 1}),
        ("health_vitals", {"resting_heart_rate": 60, "step_count": 3500}),
    )

    for signal, value in samples:
        result = differ.observe("u_ios_contract", signal, value, ts=100.0)
        assert result.events == ()


class _PhotoStore:
    def __init__(self):
        self.frames = {}
        self.items = {}
        self.events = []

    def get_config(self, uid):
        return {}

    def get_user_state_doc(self, uid):
        return {}

    def put_photo_envelope(self, uid, frame_id, ts, env):
        self.frames[(uid, frame_id)] = dict(env)

    def item_upsert(self, uid, kind, item_id, ts, doc, expires_at=None):
        self.items[(uid, kind, item_id)] = dict(doc)

    def item_list(self, uid, kind, limit=20, now=None):
        rows = [
            doc for (row_uid, row_kind, _), doc in self.items.items()
            if row_uid == uid and row_kind == kind
        ]
        return rows[:limit]

    def item_get(self, uid, kind, item_id, now=None):
        return self.items.get((uid, kind, item_id))

    def read_events(self, uid, limit=50):
        return list(self.events[-limit:])

    def append_event(self, uid, event, ts):
        self.events.append(dict(event))


def test_ios_photo_fixture_stores_sensitive_scene_without_hard_block(monkeypatch):
    payload = _load("ios_photo_evaluate_document.json")
    fake = _PhotoStore()
    monkeypatch.setattr(service, "store", fake)
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled", lambda user_or_store: True)
    monkeypatch.setattr(service, "_proactive_activation_ready", lambda uid: True)
    monkeypatch.setattr(
        ingress_v2,
        "DEFAULT_DIFFER_V2",
        PerceptionDifferV2(
            observe_state=lambda _uid, _signal, _value, *, observed_at, **_kwargs: (
                SignalObservationDecision(
                    outcome="changed",
                    changed=True,
                    fingerprint="test-fingerprint",
                    last_seen_at=datetime.fromtimestamp(
                        observed_at, tz=timezone.utc
                    ),
                    last_changed_at=datetime.fromtimestamp(
                        observed_at, tz=timezone.utc
                    ),
                )
            )
        ),
    )
    wakes = []
    monkeypatch.setattr(service, "_fire_wake_event_v2", lambda event: wakes.append(event))

    out, code = service.photo_evaluate(
        "u_ios_contract",
        payload["metadata"],
        payload["content_envelope"],
        meta_envelope=payload["meta_envelope"],
    )

    assert code == 200
    assert out["status"] == "stored"
    assert out["usable"] is True
    assert out["sensitive"] is True
    assert fake.frames[("u_ios_contract", payload["content_envelope"]["id"])] == payload["content_envelope"]

    listed = service.photos_recent("u_ios_contract")[0]["photos"]
    assert "meta_envelope" not in listed[0]
    content, content_code = service.photo_content("u_ios_contract", payload["content_envelope"]["id"])
    assert content_code == 200
    assert content["meta_envelope"] == payload["meta_envelope"]
    assert wakes and wakes[0].trigger == "photo_added"
