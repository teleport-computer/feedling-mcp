import json
import logging
import pathlib
import sys
import threading
import types
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from screen import screen_read_core  # noqa: E402
from screen.screen_read_core import ScreenResult  # noqa: E402
from capabilities import screen as cap_screen  # noqa: E402


def test_shared_screen_grounding_uses_latest_frame_clock(monkeypatch):
    monkeypatch.setattr(screen_read_core.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        screen_read_core.db,
        "frame_list_meta",
        lambda user_id: [{"id": "f1", "ts": 910.0}] if user_id == "u1" else [],
    )

    assert screen_read_core.screen_share_grounding("u1") == {
        "active": True,
        "latest_frame_age_sec": 90,
    }
    assert screen_read_core.screen_share_grounding("unknown") == {}


def test_shared_screen_grounding_rejects_stale_or_future_frames(monkeypatch):
    monkeypatch.setattr(screen_read_core.time, "time", lambda: 1_000.0)
    for frame_ts in (909.9, 1_000.1):
        monkeypatch.setattr(
            screen_read_core.db,
            "frame_list_meta",
            lambda _user_id, ts=frame_ts: [{"id": "f1", "ts": ts}],
        )
        assert screen_read_core.screen_share_grounding("u1") == {}


def test_shared_screen_grounding_reports_fresh_broadcast_without_recent_frames(
    monkeypatch,
):
    monkeypatch.setattr(screen_read_core.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        screen_read_core.db,
        "frame_list_meta",
        lambda _user_id: [{"id": "f1", "ts": 699.0}],
    )
    monkeypatch.setattr(
        screen_read_core.db,
        "perception_broadcast_meta",
        lambda _user_id: {
            "broadcast_state": "broadcasting",
            "broadcast_state_ts": "999.0",
            "broadcast_active": "true",
            "broadcast_active_ts": "999.0",
        },
    )

    assert screen_read_core.screen_share_grounding("u1") == {
        "active": False,
        "stalled": True,
        "status": "broadcast_on_without_recent_frames",
        "latest_frame_age_sec": 301,
        "suggested_action": "Ask the user to stop and restart screen sharing.",
    }


def test_shared_screen_grounding_ignores_old_or_inactive_broadcast(monkeypatch):
    monkeypatch.setattr(screen_read_core.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        screen_read_core.db,
        "frame_list_meta",
        lambda _user_id: [{"id": "f1", "ts": 600.0}],
    )
    reports = (
        {
            "broadcast_state": "on",
            "broadcast_state_ts": 699.0,
            "broadcast_active": True,
            "broadcast_active_ts": 699.0,
        },
        {
            "broadcast_state": "off",
            "broadcast_state_ts": 999.0,
            "broadcast_active": False,
            "broadcast_active_ts": 999.0,
        },
    )
    for report in reports:
        monkeypatch.setattr(
            screen_read_core.db,
            "perception_broadcast_meta",
            lambda _user_id, value=report: value,
        )
        assert screen_read_core.screen_share_grounding("u1") == {}


def test_broadcast_freshness_uses_latest_recognized_status():
    assert screen_read_core.broadcast_report_is_recently_active(
        {
            "broadcast_state": "on",
            "broadcast_state_ts": 600.0,
            "broadcast_active": False,
            "broadcast_active_ts": 999.0,
        },
        now=1_000.0,
    ) is False
    assert screen_read_core.broadcast_report_is_recently_active(
        {
            "broadcast_state": "on",
            "broadcast_state_ts": 999.0,
            "broadcast_active": False,
            "broadcast_active_ts": 1_000.0,
        },
        now=1_000.0,
    ) is False


def test_broadcast_freshness_tolerates_small_future_device_clock_skew(caplog):
    report = {
        "broadcast_state": "on",
        "broadcast_state_ts": "1060.0",
        "broadcast_active": "true",
        "broadcast_active_ts": "1060.0",
    }

    assert screen_read_core.broadcast_report_is_recently_active(
        report, now=1_000.0
    ) is True

    report["broadcast_state_ts"] = "1121.0"
    report["broadcast_active_ts"] = "1121.0"
    with caplog.at_level(logging.DEBUG, logger=screen_read_core.__name__):
        assert screen_read_core.broadcast_report_is_recently_active(
            report, now=1_000.0
        ) is False
    assert "device timestamp is 121.0s ahead" in caplog.text


def test_list_frames_exposes_shared_grounding_contract(monkeypatch):
    store = types.SimpleNamespace(
        user_id="u1",
        frames_lock=threading.RLock(),
        frames_meta=[{"id": "f1", "filename": "f1.jpg", "ts": 699.0}],
    )
    stalled = {
        "active": False,
        "stalled": True,
        "status": "broadcast_on_without_recent_frames",
        "latest_frame_age_sec": 301,
        "suggested_action": "Ask the user to stop and restart screen sharing.",
    }
    seen = {}

    def fake_grounding(user_id, *, latest_frame_ts=None):
        seen.update(user_id=user_id, latest_frame_ts=latest_frame_ts)
        return stalled

    monkeypatch.setattr(screen_read_core, "screen_share_grounding", fake_grounding)
    monkeypatch.setattr(
        screen_read_core.frames, "_frame_url", lambda _store, filename: f"/{filename}"
    )

    result = screen_read_core.list_frames(store, 20)

    assert result.status == 200
    assert result.json_body["screen_share"] == stalled
    assert seen == {"user_id": "u1", "latest_frame_ts": 699.0}


def test_recent_wraps_json_body(monkeypatch):
    monkeypatch.setattr(screen_read_core, "list_frames",
                        lambda store, limit: ScreenResult(status=200, json_body={"frames": [], "total": 0}))
    r = cap_screen.recent("STORE", params={"limit": 5})
    assert r.ok is True and r.data == {"frames": [], "total": 0}


def test_read_resolves_latest_then_decrypts(monkeypatch):
    monkeypatch.setattr(screen_read_core, "latest_frame",
                        lambda store: ScreenResult(status=200, json_body={"id": "f1"}))
    seen = {}
    def fake_decrypt(store, frame_id, *, include_image, api_key, runtime_token):
        seen.update(frame_id=frame_id, include_image=include_image)
        return ScreenResult(status=200, json_body={"caption": "a cat"})
    monkeypatch.setattr(screen_read_core, "frame_decrypt", fake_decrypt)
    r = cap_screen.read("STORE", params={})  # no frame_id → resolve latest
    assert r.ok is True and r.data == {
        "caption": "a cat",
        "image_omitted_reason": "not_requested",
    }
    assert seen == {"frame_id": "f1", "include_image": "false"}


def test_active_share_defaults_to_pixels_when_include_image_is_omitted(monkeypatch):
    store = types.SimpleNamespace(user_id="u_live")
    monkeypatch.setattr(
        screen_read_core,
        "screen_share_grounding",
        lambda user_id: {"active": True, "latest_frame_age_sec": 3},
    )
    seen = {}

    def fake_decrypt(store, frame_id, *, include_image, api_key, runtime_token):
        seen.update(frame_id=frame_id, include_image=include_image)
        payload = {
            "id": frame_id,
            "ocr_text": "visible text",
            "image_b64": "a" * 4000,
            "image_mime": "image/png",
        }
        return ScreenResult(
            status=200,
            raw_body=json.dumps(payload).encode(),
            media_type="application/json",
        )

    monkeypatch.setattr(screen_read_core, "frame_decrypt", fake_decrypt)

    result = cap_screen.read(store, params={"frame_id": "f-live"})

    assert seen == {"frame_id": "f-live", "include_image": "true"}
    assert result.ok is True
    assert result.data["image_b64"] == "a" * 4000
    assert result.data["image_mime"] == "image/png"
    assert result.data["has_image"] is True


def test_inactive_share_default_explains_that_pixels_were_not_requested(monkeypatch):
    store = types.SimpleNamespace(user_id="u_idle")
    monkeypatch.setattr(
        screen_read_core, "screen_share_grounding", lambda _user_id: {}
    )
    seen = {}

    def fake_decrypt(store, frame_id, *, include_image, api_key, runtime_token):
        seen["include_image"] = include_image
        payload = {
            "frame_id": frame_id,
            "ocr_text": "",
            "image_b64": None,
            "image_bytes_omitted": True,
        }
        return ScreenResult(
            status=200,
            raw_body=json.dumps(payload).encode(),
            media_type="application/json; charset=utf-8",
        )

    monkeypatch.setattr(screen_read_core, "frame_decrypt", fake_decrypt)

    result = cap_screen.read(store, params={"frame_id": "f-idle"})

    assert seen["include_image"] == "false"
    assert result.data["image_omitted_reason"] == "not_requested"


def test_stalled_share_does_not_default_to_old_pixels(monkeypatch):
    store = types.SimpleNamespace(user_id="u_stalled")
    monkeypatch.setattr(
        screen_read_core,
        "screen_share_grounding",
        lambda _user_id: {
            "active": False,
            "stalled": True,
            "latest_frame_age_sec": 600,
        },
    )
    seen = {}

    def fake_decrypt(store, frame_id, *, include_image, api_key, runtime_token):
        seen["include_image"] = include_image
        return ScreenResult(status=200, json_body={"ocr_text": "old text"})

    monkeypatch.setattr(screen_read_core, "frame_decrypt", fake_decrypt)

    result = cap_screen.read(store, params={"frame_id": "f-old"})

    assert seen["include_image"] == "false"
    assert result.data["image_omitted_reason"] == "not_requested"


def test_active_share_without_plaintext_image_explains_absence(monkeypatch):
    store = types.SimpleNamespace(user_id="u_empty")
    monkeypatch.setattr(
        screen_read_core,
        "screen_share_grounding",
        lambda _user_id: {"active": True, "latest_frame_age_sec": 1},
    )
    monkeypatch.setattr(
        screen_read_core,
        "frame_decrypt",
        lambda *_args, **_kwargs: ScreenResult(
            status=200,
            raw_body=json.dumps(
                {
                    "frame_id": "f-empty",
                    "image_b64": "",
                    "image_mime": "image/jpeg",
                }
            ).encode(),
            media_type="application/json",
        ),
    )

    result = cap_screen.read(store, params={"frame_id": "f-empty"})

    assert result.data["image_omitted_reason"] == "absent_in_plaintext"


def test_explicit_false_keeps_active_share_text_only(monkeypatch):
    store = types.SimpleNamespace(user_id="u_live_text")
    monkeypatch.setattr(
        screen_read_core,
        "screen_share_grounding",
        lambda _user_id: {"active": True, "latest_frame_age_sec": 1},
    )
    seen = {}

    def fake_decrypt(store, frame_id, *, include_image, api_key, runtime_token):
        seen["include_image"] = include_image
        return ScreenResult(status=200, json_body={"ocr_text": "only text"})

    monkeypatch.setattr(screen_read_core, "frame_decrypt", fake_decrypt)

    result = cap_screen.read(
        store, params={"frame_id": "f-text", "include_image": False}
    )

    assert seen["include_image"] == "false"
    assert result.data["image_omitted_reason"] == "not_requested"


def test_unexpected_image_is_never_silently_truncated(monkeypatch):
    payload = {
        "frame_id": "f-unexpected",
        "image_b64": "b" * 4000,
        "image_mime": "image/jpeg",
    }
    monkeypatch.setattr(
        screen_read_core,
        "frame_decrypt",
        lambda *_args, **_kwargs: ScreenResult(
            status=200,
            raw_body=json.dumps(payload).encode(),
            media_type="application/json",
        ),
    )

    result = cap_screen.read(
        "STORE", params={"frame_id": "f-unexpected", "include_image": False}
    )

    assert result.ok is True
    assert result.data["image_b64"] == "b" * 4000
    assert result.data["has_image"] is True
    assert "image_omitted_reason" not in result.data


def test_read_binary_body_exposes_b64_only_for_native_vision_bridge(monkeypatch):
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(status=200, raw_body=b"\xff\xd8", media_type="image/jpeg"))
    r = cap_screen.read("STORE", params={"frame_id": "f2", "include_image": True})
    assert r.ok is True
    assert r.data == {
        "frame_id": "f2",
        "media_type": "image/jpeg",
        "has_image": True,
        "image_b64": "/9g=",
    }


def test_recent_caps_large_frame_list(monkeypatch):
    monkeypatch.setattr(screen_read_core, "list_frames",
                        lambda store, limit: ScreenResult(status=200, json_body={"frames": list(range(1000)), "total": 1000}))
    r = cap_screen.recent("STORE", params={})
    assert r.ok is True and len(r.data["frames"]) == 50
