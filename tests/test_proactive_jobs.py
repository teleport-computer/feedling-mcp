import os
import sys
import tempfile
import time
from pathlib import Path

import httpx


_DATA_DIR = tempfile.mkdtemp(prefix="feedling-proactive-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from chat import service as chat_service  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from proactive.controls_v2 import evaluate_wake_control_v2, resolve_settings_v2  # noqa: E402
from proactive import capture_jobs as proactive_capture_jobs  # noqa: E402
from proactive import capture_scheduler as proactive_capture_scheduler  # noqa: E402
from proactive import dashboard as proactive_dashboard  # noqa: E402
from proactive import dream_scheduler as proactive_dream_scheduler  # noqa: E402
from proactive import resident_runtime_v2 as proactive_resident_runtime_v2  # noqa: E402
from perception import service as perception_service  # noqa: E402
from proactive import gate as proactive_gate  # noqa: E402
from proactive import poll_core as proactive_poll_core  # noqa: E402
from proactive import service as proactive_service  # noqa: E402
from push import apns as push_apns  # noqa: E402
from push import service as push_service  # noqa: E402
from screen import frames as screen_frames  # noqa: E402
from hosted import turn as hosted_turn  # noqa: E402
from core import config as core_config  # noqa: E402
from core import reqctx  # noqa: E402
from core import store as core_store  # noqa: E402

from conftest import seed_user  # noqa: E402


def _mark_proactive_activated(user_id: str) -> None:
    core_store.get_store(user_id).mark_first_chat_ok(at_iso="2026-07-03T00:00:00")


AUTONOMY_SWITCH_FIELDS = (
    "dream_enabled",
    "capture_enabled",
    "screen_watch_enabled",
    "photo_wake_enabled",
    "arrival_wake_enabled",
    "unlock_wake_enabled",
)


def _v2_switches(**overrides):
    doc = {
        "ambient": True,
        "scheduled": True,
        "reminders_delivery": True,
        "dream_enabled": True,
        "capture_enabled": True,
        "screen_watch_enabled": True,
        "photo_wake_enabled": True,
        "arrival_wake_enabled": True,
        "unlock_wake_enabled": True,
    }
    doc.update(overrides)
    return doc


def _patch_resident_scheduled_route_dependencies(monkeypatch):
    from proactive import scheduled_wake_v2, store_v2

    scheduled_store = scheduled_wake_v2.InMemoryScheduledWakeStoreV2()
    settings_by_user: dict[str, dict] = {}

    class _SettingsStore:
        def load(self, user_id: str):
            return resolve_settings_v2(settings_by_user.get(user_id))

    monkeypatch.setattr(scheduled_wake_v2, "DBScheduledWakeStoreV2", lambda: scheduled_store)
    monkeypatch.setattr(store_v2, "DBProactiveSettingsStoreV2", _SettingsStore)
    return scheduled_store, settings_by_user


def test_device_event_payload_is_redacted():
    raw = {
        "permission": "authorized",
        "status": "changed",
        "lat": 40.7128,
        "lng": -74.0060,
        "title": "private calendar event",
        "safe_bucket": "home_area",
        "scene_tags": ["work", "reading"],
    }

    event = proactive_service._make_device_event("ios", "permission_changed", raw)

    assert event["event_id"].startswith("evt_")
    assert event["payload"]["permission"] == "authorized"
    assert event["payload"]["safe_bucket"] == "home_area"
    assert event["payload"]["scene_tags"] == ["work", "reading"]
    assert "lat" not in event["payload"]
    assert "lng" not in event["payload"]
    assert "title" not in event["payload"]


def test_manual_proactive_wake_creates_hidden_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_test_proactive")
    seed_user(store.user_id)

    decision = proactive_gate._build_proactive_v2_wake_decision(
        store,
        {
            "force": True,
            "context_hint": "The user has been comparing a product for several minutes.",
            "connections": ["This matches a repeated research pattern."],
            "intent_label": "screen_research",
        },
    )
    store.append_gate_decision(decision)
    job = store.append_proactive_job(proactive_gate._proactive_job_from_decision(decision))

    assert decision["should_reach_out"] is True
    assert decision["schema_version"] == 2
    assert decision["decision_id"].startswith("gd_")
    assert job["job_id"].startswith("pj_")
    assert job["source"] == proactive_service.PROACTIVE_JOB_SOURCE
    assert job["gate_decision_id"] == decision["decision_id"]
    assert job["context_hint"] == ""
    assert job["wake_kind"] == "presence"

    jobs = store.list_proactive_jobs(since_epoch=0)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job["job_id"]


def test_proactive_debug_derives_job_delivery_state(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_test_proactive_delivery")
    seed_user(store.user_id)

    decision = proactive_gate._build_proactive_v2_wake_decision(
        store,
        {
            "force": True,
            "context_hint": "The user is testing proactive delivery.",
            "intent_label": "manual_proactive_test",
            "frames": [{"id": "abcd1234abcd1234"}],
        },
    )
    job = store.append_proactive_job(proactive_gate._proactive_job_from_decision(decision))
    envelope = {
        "id": "msg_proactive_1",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": store.user_id,
    }
    store.append_chat(
        "openclaw",
        proactive_service.PROACTIVE_JOB_SOURCE,
        envelope,
        extra={
            "gate_decision_id": decision["decision_id"],
            "proactive_job_id": job["job_id"],
            "alert_preview": "自然地提醒用户。",
            "alert_status": "delivered",
            "live_activity_status": "delivered",
            "live_activity_activity_id": "la_1",
        },
    )

    snapshot = proactive_dashboard._proactive_debug_snapshot(store)

    assert snapshot["jobs"][0]["derived_status"] == "delivered"
    assert snapshot["jobs"][0]["preview"] == "自然地提醒用户。"
    assert snapshot["jobs"][0]["alert_status"] == "delivered"
    assert snapshot["jobs"][0]["live_activity_status"] == "delivered"


def test_proactive_debug_folds_legacy_no_frame_ticks(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_test_proactive_folded_gate")
    seed_user(store.user_id)

    no_frame = {
        "decision_id": "gd_no_frame",
        "ts": 1000,
        "gate_model": "openrouter:google/gemini-3.1-flash-lite",
        "should_reach_out": False,
        "reason": "no_recent_frames_unit_test",
        "abstention_reason": "no_recent_frames",
        "intent_label": "blocked_before_model",
        "connection": {},
        "frame_ids": [],
        "gate_input": {
            "ocr_chars": 0,
            "sampled_frame_count": 0,
            "decrypt_ok": False,
            "image_count": 0,
        },
    }
    with_frame = {
        "decision_id": "gd_with_frame",
        "ts": 1001,
        "gate_model": "openrouter:google/gemini-3.1-flash-lite",
        "should_reach_out": False,
        "reason": "frame_backed_false_unit_test",
        "abstention_reason": "model_false",
        "intent_label": "reviewable_false",
        "connection": {},
        "frame_ids": [],
        "gate_input": {
            "ocr_chars": 42,
            "sampled_frame_count": 2,
            "decrypt_ok": True,
            "image_count": 1,
        },
    }
    store.append_gate_decision(no_frame)
    store.append_gate_decision(with_frame)

    assert proactive_dashboard._gate_decision_has_frame_context(no_frame) is False
    assert proactive_dashboard._gate_decision_has_frame_context(with_frame) is True

    snapshot = proactive_dashboard._proactive_debug_snapshot(store)
    with reqctx.bind(query_string="key=test&lang=zh"):
        page = proactive_dashboard._render_proactive_dashboard(snapshot)

    assert "主表判定 1" in page
    assert "隐藏空 tick 1" in page
    assert "显示隐藏的旧版无屏幕帧空 tick（1）" in page
    assert "frame_backed_false_unit_test" in page
    assert "no_recent_frames_unit_test" not in page
    assert "显示样本" in page

    with reqctx.bind(query_string="key=test&lang=zh&show_no_frame=1"):
        expanded_page = proactive_dashboard._render_proactive_dashboard(snapshot)

    assert expanded_page.find("frame_backed_false_unit_test") < expanded_page.find("no_recent_frames_unit_test")

    with reqctx.bind(query_string="key=test&lang=en"):
        page_en = proactive_dashboard._render_proactive_dashboard(snapshot)

    assert "visible decisions 1" in page_en
    assert "hidden no-frame ticks 1" in page_en
    assert "Show hidden legacy no-frame ticks (1)" in page_en
    assert "no_recent_frames_unit_test" not in page_en


def test_proactive_debug_dashboard_defaults_to_deep_full_view(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_test_proactive_deep_dashboard")
    seed_user(store.user_id)

    for i in range(12):
        decision_id = f"gd_frame_{i}"
        job_id = f"pj_{i}"
        store.append_gate_decision(
            {
                "decision_id": decision_id,
                "ts": 1000 + i,
                "gate_model": "openrouter:google/gemini-3.1-flash-lite",
                "should_reach_out": bool(i % 2),
                "reason": f"frame_reason_{i}",
                "abstention_reason": "model_false",
                "intent_label": "reviewable_false",
                "connection": {"why": f"connection_{i}"},
                "frame_ids": [f"frame_{i}"],
                "gate_input": {
                    "ocr_chars": 42,
                    "sampled_frame_count": 2,
                    "decrypt_ok": True,
                    "image_count": 1,
                },
            }
        )
        store.append_proactive_job(
            {
                "job_id": job_id,
                "ts": 1000 + i,
                "gate_decision_id": decision_id,
                "status": "completed",
                "intent_label": "reviewable_false",
                "context_hint": f"context_hint_{i}",
                "frame_ids": [f"frame_{i}"],
            }
        )
        envelope = {
            "id": f"msg_proactive_{i}",
            "v": 1,
            "body_ct": "ct",
            "nonce": "nonce",
            "K_user": "k-user",
            "K_enclave": "k-enclave",
            "visibility": "shared",
            "owner_user_id": store.user_id,
        }
        store.append_chat(
            "openclaw",
            proactive_service.PROACTIVE_JOB_SOURCE,
            envelope,
            extra={
                "gate_decision_id": decision_id,
                "proactive_job_id": job_id,
                "alert_preview": f"preview_{i}",
                "alert_status": "delivered",
                "live_activity_status": "delivered",
            },
        )
        store.append_device_event(
            {
                "id": f"ev_{i}",
                "ts": time.time() + i,
                "source": "unit",
                "type": f"event_{i}",
                "payload": {"id": f"payload_{i}"},
            }
        )

    snapshot = proactive_dashboard._proactive_debug_snapshot(store)
    with reqctx.bind(query_string="key=test&lang=en"):
        page = proactive_dashboard._render_proactive_dashboard(snapshot)

    assert "lang-switch" in page
    assert "Full debug mode is on by default" in page
    assert "Compact mode is on" not in page
    assert "hide JSON detail" in page
    assert "frame_reason_0" in page
    assert "pj_0" in page
    assert "preview_0" in page
    assert "event_0" in page


def test_proactive_debug_translates_prose_only_in_zh_view(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_test_proactive_debug_translate")
    seed_user(store.user_id)
    reason = "The screen has a concrete connection to the user's memory garden."
    context_hint = "The user is comparing a dense note and may want one gentle nudge."
    abstention = "The companion has already successfully engaged with the user's current context and provided a relevant response."
    decision = {
        "decision_id": "gd_translate",
        "ts": 1002,
        "gate_model": "openrouter:google/gemini-3.1-flash-lite",
        "should_reach_out": True,
        "reason": reason,
        "abstention_reason": "",
        "intent_label": "manual_proactive_test",
        "connection": {},
        "frame_ids": ["frame_translate"],
        "gate_input": {
            "ocr_chars": 120,
            "sampled_frame_count": 1,
            "decrypt_ok": True,
            "image_count": 1,
        },
        "context_hint": context_hint,
    }
    false_decision = {
        "decision_id": "gd_translate_false",
        "ts": 1003,
        "gate_model": "openrouter:google/gemini-3.1-flash-lite",
        "should_reach_out": False,
        "reason": "already_responded",
        "abstention_reason": abstention,
        "intent_label": "already_responded",
        "connection": {},
        "frame_ids": ["frame_translate_2"],
        "gate_input": {
            "ocr_chars": 120,
            "sampled_frame_count": 1,
            "decrypt_ok": True,
            "image_count": 1,
        },
        "context_hint": "",
    }
    store.append_gate_decision(decision)
    store.append_gate_decision(false_decision)
    snapshot = proactive_dashboard._proactive_debug_snapshot(store)

    monkeypatch.setattr(
        proactive_dashboard,
        "_translate_debug_texts_to_zh",
        lambda texts: {
            reason: "屏幕内容和用户的记忆花园有明确关联。",
            context_hint: "用户正在比较一段密集笔记，可能适合轻轻提醒一句。",
            abstention: "陪伴者已经结合用户当前上下文给过合适回复。",
        },
    )

    with reqctx.bind(query_string="key=test&lang=zh"):
        page_zh = proactive_dashboard._render_proactive_dashboard(snapshot)
    with reqctx.bind(query_string="key=test&lang=en"):
        page_en = proactive_dashboard._render_proactive_dashboard(snapshot)

    assert "屏幕内容和用户的记忆花园有明确关联。" in page_zh
    assert "陪伴者已经结合用户当前上下文给过合适回复。" in page_zh
    assert "已经回应过" in page_zh
    assert "title='The screen has a concrete connection to the user&#x27;s memory garden.'" in page_zh
    assert "title='The companion has already successfully engaged with the user&#x27;s current context" in page_zh
    assert "The screen has a concrete connection" in page_en
    assert "屏幕内容和用户的记忆花园有明确关联。" not in page_en
    assert "陪伴者已经结合用户当前上下文给过合适回复。" not in page_en
    assert snapshot["decisions"][0]["reason"] == reason


def test_proactive_settings_persists_timezone(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_timezone_key"
    user_id = "usr_endpoint_proactive_timezone"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}

    resp = client.post(
        "/v1/proactive/settings",
        headers=headers,
        json={"timezone": "Asia/Tokyo"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["timezone"] == "Asia/Tokyo"

    bad = client.post(
        "/v1/proactive/settings",
        headers=headers,
        json={"timezone": "Not/AZone"},
    )
    assert bad.status_code == 200
    assert bad.get_json()["timezone"] == "Asia/Tokyo"


def test_proactive_state_wake_interval_defaults_clamps_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_wake_interval_key"
    user_id = "usr_endpoint_proactive_wake_interval"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}

    default_state = client.get("/v1/proactive/state", headers=headers)
    assert default_state.status_code == 200
    assert default_state.get_json()["wake_interval_sec"] == 7200
    assert core_store.get_store(user_id).load_proactive_settings()["wake_interval_sec"] == 7200

    db.set_blob(user_id, "proactive_settings", {"wake_interval_sec": "not-a-number"})
    corrupt_state = client.get("/v1/proactive/state", headers=headers)
    assert corrupt_state.status_code == 200
    assert corrupt_state.get_json()["wake_interval_sec"] == 7200

    cases = [
        (899, 900),
        (43201, 43200),
        (7200, 7200),
    ]
    for raw, expected in cases:
        resp = client.post(
            "/v1/proactive/state",
            headers=headers,
            json={"wake_interval_sec": raw},
        )
        assert resp.status_code == 200
        assert resp.get_json()["wake_interval_sec"] == expected
        assert core_store.get_store(user_id).load_proactive_settings()["wake_interval_sec"] == expected

        got = client.get("/v1/proactive/state", headers=headers)
        assert got.status_code == 200
        assert got.get_json()["wake_interval_sec"] == expected

    invalid = client.post(
        "/v1/proactive/state",
        headers=headers,
        json={"wake_interval_sec": "not-a-number"},
    )
    assert invalid.status_code == 200
    assert invalid.get_json()["wake_interval_sec"] == 7200
    assert core_store.get_store(user_id).load_proactive_settings()["wake_interval_sec"] == 7200


def test_proactive_state_three_switch_contract_drives_v2_scheduled_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_three_switch_key"
    user_id = "usr_endpoint_proactive_three_switch"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}

    resp = client.post(
        "/v1/proactive/state",
        headers=headers,
        json={
            "ambient": False,
            "scheduled": False,
            "reminders_delivery": False,
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ambient"] is False
    assert body["scheduled"] is False
    assert body["reminders_delivery"] is False
    assert body["enabled"] is False
    assert body["dnd"] is True

    got = client.get("/v1/proactive/state", headers=headers)
    assert got.status_code == 200
    state = got.get_json()
    assert state["ambient"] is False
    assert state["scheduled"] is False
    assert state["reminders_delivery"] is False

    settings = core_store.get_store(user_id).load_proactive_settings()
    assert settings["enabled"] is False
    assert settings["scheduled"] is False
    assert settings["dnd"] is True

    resolved = resolve_settings_v2(settings)
    assert resolved.switches() == _v2_switches(
        ambient=False,
        scheduled=False,
        reminders_delivery=False,
    )
    decision = evaluate_wake_control_v2("scheduled_wake", settings=resolved)
    assert decision.accepted is False
    assert decision.reason == "scheduled_disabled"
    assert decision.transparency_required is True


def test_proactive_state_autonomy_switches_default_patch_and_legacy_true(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_autonomy_switch_key"
    user_id = "usr_endpoint_proactive_autonomy_switch"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}

    default_state = client.get("/v1/proactive/state", headers=headers)
    assert default_state.status_code == 200
    body = default_state.get_json()
    for key in AUTONOMY_SWITCH_FIELDS:
        assert body[key] is True
        assert core_store.get_store(user_id).load_proactive_settings()[key] is True

    off_payload = {key: False for key in AUTONOMY_SWITCH_FIELDS}
    off = client.post("/v1/proactive/state", headers=headers, json=off_payload)
    assert off.status_code == 200
    off_body = off.get_json()
    settings = core_store.get_store(user_id).load_proactive_settings()
    for key in AUTONOMY_SWITCH_FIELDS:
        assert off_body[key] is False
        assert settings[key] is False

    on_payload = {key: True for key in AUTONOMY_SWITCH_FIELDS}
    on = client.post("/v1/proactive/state", headers=headers, json=on_payload)
    assert on.status_code == 200
    for key in AUTONOMY_SWITCH_FIELDS:
        assert on.get_json()[key] is True

    db.set_blob(user_id, "proactive_settings", {"enabled": False})
    legacy = client.get("/v1/proactive/state", headers=headers)
    assert legacy.status_code == 200
    legacy_body = legacy.get_json()
    assert legacy_body["ambient"] is False
    for key in AUTONOMY_SWITCH_FIELDS:
        assert legacy_body[key] is True


def test_proactive_tick_response_includes_wake_interval_sec(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_tick_wake_interval_key"
    user_id = "usr_endpoint_proactive_tick_wake_interval"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}

    state = client.post(
        "/v1/proactive/state",
        headers=headers,
        json={"wake_interval_sec": 900},
    )
    assert state.status_code == 200
    assert state.get_json()["wake_interval_sec"] == 900

    tick = client.post(
        "/v1/proactive/tick",
        headers=headers,
        json={"trigger": "heartbeat_broadcast_off", "broadcast_state": "off"},
    )

    assert tick.status_code == 200
    body = tick.get_json()
    assert body["decision"]["broadcast_state"] == "off"
    assert body["decision"]["wake_interval_sec"] == 900


def test_proactive_activation_gate_blocks_self_initiated_wakes_until_first_chat_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_activation_gate_key"
    user_id = "usr_endpoint_proactive_activation_gate"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)

    blocked_payloads = [
        {"trigger": "heartbeat_broadcast_off", "broadcast_state": "off"},
        {"trigger": "photo_added"},
        {"trigger": "arrived_at_anchor"},
        {"trigger": "unlock_after_absence"},
        {"job_kind": "screen_watch", "broadcast_state": "on"},
    ]
    for payload in blocked_payloads:
        decision = proactive_gate._build_proactive_v2_wake_decision(store, payload)
        assert decision["should_reach_out"] is False
        assert decision["should_wake_agent"] is False
        assert decision["reason"] == "activation_pending"
        assert decision["gate_input"]["activation_pending"] is True

    client = make_client()
    tick = client.post(
        "/v1/proactive/tick",
        headers={"X-API-Key": api_key},
        json={"trigger": "heartbeat_broadcast_off", "broadcast_state": "off"},
    )
    assert tick.status_code == 200
    body = tick.get_json()
    assert body["enqueued"] is False
    assert body["job"] is None
    assert body["decision"]["reason"] == "activation_pending"

    manual = proactive_gate._build_proactive_v2_wake_decision(store, {"manual": True})
    assert manual["should_wake_agent"] is True
    assert manual["reason"] == "wake_created"

    store.mark_first_chat_ok(at_iso="2026-07-03T00:00:00")
    activated = proactive_gate._build_proactive_v2_wake_decision(
        store,
        {"trigger": "heartbeat_broadcast_off", "broadcast_state": "off"},
    )
    assert activated["should_wake_agent"] is True
    assert activated["reason"] == "wake_created"
    assert activated["gate_input"]["activation_pending"] is False


def test_perception_direct_wake_respects_proactive_activation_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    user_id = "usr_perception_direct_wake_activation"
    seed_user(user_id)
    store = core_store.get_store(user_id)

    perception_service._fire_wake(user_id, "device", "unlock after absence", 1000.0)
    assert store.list_proactive_jobs(since_epoch=0) == []

    store.mark_first_chat_ok(at_iso="2026-07-03T00:00:00")
    perception_service._fire_wake(user_id, "device", "unlock after absence", 1001.0)
    jobs = store.list_proactive_jobs(since_epoch=0)
    assert len(jobs) == 1
    assert jobs[0]["trigger"] == "perception_device"


def test_proactive_tick_delivery_off_still_allows_presence_wake(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_delivery_off_wake_key"
    user_id = "usr_endpoint_proactive_delivery_off_wake"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}
    state = client.post(
        "/v1/proactive/state",
        headers=headers,
        json={"reminders_delivery": False},
    )
    assert state.status_code == 200
    assert state.get_json()["reminders_delivery"] is False
    assert state.get_json()["dnd"] is True

    tick = client.post(
        "/v1/proactive/tick",
        headers=headers,
        json={"trigger": "heartbeat_broadcast_off", "broadcast_state": "off"},
    )

    assert tick.status_code == 200
    body = tick.get_json()
    assert body["enqueued"] is True
    assert body["decision"]["reason"] == "wake_created"
    assert body["decision"]["wake_kind"] == "presence"


def test_proactive_tick_endpoint_enqueues_pollable_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_key"
    user_id = "usr_endpoint_proactive"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}

    tick = client.post(
        "/v1/proactive/tick",
        headers=headers,
        json={
            "force": True,
            "context_hint": "The user has paused on a research screen.",
            "intent_label": "research_pause",
        },
    )
    assert tick.status_code == 200
    body = tick.get_json()
    assert body["enqueued"] is True
    assert body["job"]["source"] == proactive_service.PROACTIVE_JOB_SOURCE
    assert body["decision"]["schema_version"] == 2
    assert body["decision"]["decision_type"] == "wake_event"
    assert body["decision"]["gate_model"] == "proactive_v2:wake"
    assert body["decision"]["gate_input"]["llm_called"] is False
    assert body["job"]["schema_version"] == 2
    assert body["job"]["trigger"] == "manual_wake"
    assert body["job"]["context_hint"] == ""

    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers=headers)
    assert poll.status_code == 200
    jobs = poll.get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == body["job"]["job_id"]

    debug = client.get("/v1/proactive/debug", headers=headers)
    assert debug.status_code == 200
    snapshot = debug.get_json()
    assert snapshot["counts"]["decisions"] == 1
    assert snapshot["counts"]["jobs"] == 1

    page = client.get("/debug/proactive?lang=zh", headers=headers)
    assert page.status_code == 200
    assert b"IO Proactive Harness" in page.data
    assert body["job"]["job_id"].encode() in page.data
    # Section header is always present once the jobs list renders; the
    # previous "frames sent" probe relied on a table column header that
    # the new card layout only emits when a job actually has frames.
    assert "隐藏任务".encode() in page.data

    page_en = client.get("/debug/proactive?lang=en", headers=headers)
    assert page_en.status_code == 200
    assert b"IO Proactive Harness" in page_en.data
    assert b"Hidden Jobs" in page_en.data


def test_screen_watch_tick_preserves_job_kind_and_trigger(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_screen_watch_key"
    user_id = "usr_screen_watch"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)

    client = make_client()
    resp = client.post(
        "/v1/proactive/tick",
        headers={"X-API-Key": api_key},
        json={
            "job_kind": "screen_watch",
            "broadcast_state": "on",
            "frames": [
                {"frame_id": "swframe00000001", "ts": time.time(), "ocr_text": "Settings"},
            ],
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is True
    assert body["decision"]["job_kind"] == "screen_watch"
    assert body["decision"]["trigger"] == "screen_watch"
    assert body["decision"]["wake_kind"] == "screen_watch"
    assert body["decision"]["manual"] is False
    assert body["decision"]["forced"] is False
    assert body["job"]["job_kind"] == "screen_watch"
    assert body["job"]["trigger"] == "screen_watch"
    assert body["job"]["frame_ids"] == ["swframe00000001"]

    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers={"X-API-Key": api_key})
    assert poll.status_code == 200
    jobs = poll.get_json()["jobs"]
    assert jobs[0]["job_kind"] == "screen_watch"
    assert jobs[0]["trigger"] == "screen_watch"


def test_screen_watch_tick_does_not_sample_recent_frames_implicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    monkeypatch.setattr(
        screen_frames,
        "_recent_frame_meta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("screen_watch must not sample frames")),
    )

    api_key = "test_screen_watch_no_sample_key"
    user_id = "usr_screen_watch_no_sample"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)

    client = make_client()
    resp = client.post(
        "/v1/proactive/tick",
        headers={"X-API-Key": api_key},
        json={"job_kind": "screen_watch", "broadcast_state": "on"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is True
    assert body["decision"]["job_kind"] == "screen_watch"
    assert body["decision"]["trigger"] == "screen_watch"
    assert body["decision"]["frame_ids"] == []
    assert body["job"]["job_kind"] == "screen_watch"
    assert body["job"]["frame_ids"] == []


def test_auto_proactive_v2_wake_samples_frames_without_gate_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(proactive_dashboard, "OPENROUTER_API_KEY", "sk-test")
    core_store._stores.clear()

    monkeypatch.setattr(
        screen_frames,
        "_decrypt_frame_metadata_for_gate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("V2 must not decrypt in tick")),
    )
    api_key = "test_proactive_auto_key"
    user_id = "usr_endpoint_proactive_auto"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)
    store = core_store.get_store(user_id)
    store.frames_meta.append({
        "id": "abcd1234abcd1234",
        "filename": "abcd1234abcd1234.env.json",
        "ts": time.time(),
        "encrypted": True,
        "app": None,
        "ocr_text": "",
    })

    client = make_client()
    resp = client.post("/v1/proactive/tick", headers={"X-API-Key": api_key}, json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is True
    assert body["decision"]["gate_model"] == "proactive_v2:wake"
    assert body["decision"]["reason"] == "wake_created"
    assert body["decision"]["trigger"] == "screen_tick"
    assert body["decision"]["gate_input"]["decrypt_ok"] is False
    assert body["decision"]["gate_input"]["image_count"] == 0
    assert body["decision"]["gate_input"]["llm_called"] is False
    assert body["decision"]["connection"] == {}
    assert body["job"]["frame_ids"] == ["abcd1234abcd1234"]
    assert body["job"]["connection"] == {}


def test_auto_proactive_v2_wake_does_not_block_after_recent_user_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(proactive_dashboard, "OPENROUTER_API_KEY", "sk-test")
    core_store._stores.clear()

    api_key = "test_proactive_recent_chat_key"
    user_id = "usr_endpoint_proactive_recent_chat"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)
    store = core_store.get_store(user_id)
    store.append_chat("user", "ios", {
        "id": "msg_recent_user",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "visibility": "shared",
        "owner_user_id": user_id,
    })
    store.frames_meta.append({
        "id": "recentchat123456",
        "filename": "recentchat123456.env.json",
        "ts": time.time(),
        "encrypted": True,
    })

    client = make_client()
    resp = client.post("/v1/proactive/tick", headers={"X-API-Key": api_key}, json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is True
    assert body["decision"]["reason"] == "wake_created"
    assert body["decision"]["gate_input"]["llm_called"] is False
    assert body["job"]["frame_ids"] == ["recentchat123456"]


def test_auto_proactive_v2_wake_does_not_require_gate_model(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(proactive_dashboard, "OPENROUTER_API_KEY", "")
    core_store._stores.clear()

    api_key = "test_proactive_auto_no_model_key"
    user_id = "usr_endpoint_proactive_auto_no_model"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)
    store = core_store.get_store(user_id)
    store.frames_meta.append({
        "id": "efef1234efef1234",
        "filename": "efef1234efef1234.env.json",
        "ts": time.time(),
        "encrypted": True,
    })

    client = make_client()
    resp = client.post("/v1/proactive/tick", headers={"X-API-Key": api_key}, json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is True
    assert body["decision"]["should_reach_out"] is True
    assert body["decision"]["should_wake_agent"] is True
    assert body["decision"]["reason"] == "wake_created"
    assert body["decision"]["gate_input"]["llm_called"] is False


def test_auto_proactive_v2_wake_suppresses_job_without_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_auto_false_key"
    user_id = "usr_endpoint_proactive_auto_false"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)

    client = make_client()
    resp = client.post("/v1/proactive/tick", headers={"X-API-Key": api_key}, json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is False
    assert body["job"] is None
    assert body["decision"]["should_reach_out"] is False
    assert body["decision"]["should_wake_agent"] is False
    assert body["decision"]["reason"] == "no_recent_frames"
    assert body["decision"]["trigger"] == "heartbeat_no_frame"
    assert body["decision"]["frame_ids"] == []
    assert body["decision"]["gate_input"]["llm_called"] is False


def test_auto_proactive_v2_schedule_heartbeats_split_presence_and_screen_wakes(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_auto_schedule_key"
    user_id = "usr_endpoint_proactive_auto_schedule"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}

    off = client.post(
        "/v1/proactive/tick",
        headers=headers,
        json={"trigger": "heartbeat_broadcast_off", "broadcast_state": "off"},
    )
    assert off.status_code == 200
    off_body = off.get_json()
    assert off_body["enqueued"] is True
    assert off_body["decision"]["reason"] == "wake_created"
    assert off_body["decision"]["wake_kind"] == "presence"
    assert off_body["decision"]["screen_context_available"] is False
    assert off_body["job"]["wake_kind"] == "presence"
    assert off_body["job"]["frame_ids"] == []

    opened_without_frame = client.post(
        "/v1/proactive/tick",
        headers=headers,
        json={"trigger": "broadcast_opened", "broadcast_state": "on"},
    )
    assert opened_without_frame.status_code == 200
    opened_body = opened_without_frame.get_json()
    assert opened_body["enqueued"] is False
    assert opened_body["decision"]["reason"] == "no_recent_frames"

    store = core_store.get_store(user_id)
    store.frames_meta.append({
        "id": "frameon123456789",
        "filename": "frameon123456789.env.json",
        "ts": time.time(),
        "encrypted": True,
    })
    on_with_frame = client.post(
        "/v1/proactive/tick",
        headers=headers,
        json={"trigger": "heartbeat_broadcast_on", "broadcast_state": "on"},
    )
    assert on_with_frame.status_code == 200
    on_body = on_with_frame.get_json()
    assert on_body["enqueued"] is True
    assert on_body["decision"]["reason"] == "wake_created"
    assert on_body["decision"]["wake_kind"] == "presence"
    assert on_body["decision"]["screen_context_available"] is False
    assert on_body["job"]["frame_ids"] == []


def test_auto_proactive_v2_away_state_does_not_resurrect_legacy_wake_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_away_key"
    user_id = "usr_endpoint_proactive_away"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    _mark_proactive_activated(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}
    client.post("/v1/proactive/state", headers=headers, json={"user_state": "away"})

    auto = client.post(
        "/v1/proactive/tick",
        headers=headers,
        json={"trigger": "heartbeat_broadcast_off", "broadcast_state": "off"},
    )
    assert auto.status_code == 200
    auto_body = auto.get_json()
    assert auto_body["enqueued"] is True
    assert auto_body["decision"]["should_wake_agent"] is True
    assert auto_body["decision"]["reason"] == "wake_created"
    assert auto_body["decision"]["user_state"] == "away"

    manual = client.post("/v1/proactive/tick", headers=headers, json={"manual": True})
    assert manual.status_code == 200
    manual_body = manual.get_json()
    assert manual_body["enqueued"] is True
    assert manual_body["decision"]["manual"] is True
    assert manual_body["decision"]["reason"] == "wake_created"


def test_gate_review_endpoint_records_human_label(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_gate_review_key"
    user_id = "usr_gate_review"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)

    decision = proactive_gate._build_proactive_v2_wake_decision(
        store,
        {
            "force": True,
            "context_hint": "The user is testing the review harness.",
            "intent_label": "manual_proactive_test",
        },
    )
    store.append_gate_decision(decision)

    client = make_client()
    resp = client.post(
        f"/v1/proactive/decisions/{decision['decision_id']}/review",
        headers={"X-API-Key": api_key},
        json={"label": "good_presence", "notes": "felt natural"},
    )

    assert resp.status_code == 200
    review = resp.get_json()["review"]
    assert review["label"] == "good_presence"
    assert review["label_family"] == "round3"
    assert review["decision_id"] == decision["decision_id"]

    snapshot = proactive_dashboard._proactive_debug_snapshot(store)
    latest = snapshot["latest_review_by_decision"][decision["decision_id"]]
    assert latest["notes"] == "felt natural"

    listing = client.get("/v1/proactive/reviews?since=0", headers={"X-API-Key": api_key})
    assert listing.status_code == 200
    assert listing.get_json()["reviews"][0]["label"] == "good_presence"


def test_proactive_job_claim_and_status_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_proactive_claim_key"
    user_id = "usr_endpoint_proactive_claim"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)

    decision = proactive_gate._build_proactive_v2_wake_decision(
        store,
        {"force": True, "context_hint": "claim test"},
    )
    job = store.append_proactive_job(proactive_gate._proactive_job_from_decision(decision))

    client = make_client()
    headers = {"X-API-Key": api_key}

    claim = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/claim",
        headers=headers,
        json={"consumer_id": "consumer-a"},
    )
    assert claim.status_code == 200
    assert claim.get_json()["claimed"] is True

    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers=headers)
    assert poll.status_code == 200
    assert poll.get_json()["jobs"] == []

    status = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/status",
        headers=headers,
        json={"status": "failed", "reason": "agent_call_failed", "consumer_id": "consumer-a"},
    )
    assert status.status_code == 200

    snapshot = proactive_dashboard._proactive_debug_snapshot(store)
    row = snapshot["jobs"][0]
    assert row["derived_status"] == "failed"
    assert row["status_reason"] == "agent_call_failed"
    assert row["consumer_id"] == "consumer-a"


def test_resident_poll_includes_per_user_runtime_v2_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_resident_runtime_profile_key"
    user_id = "usr_resident_runtime_profile"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    job = store.append_proactive_job({
        "job_id": "pj_runtime_profile",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1000.0,
        "status": "pending",
        "trigger": "heartbeat_broadcast_on",
    })

    monkeypatch.setattr(
        proactive_resident_runtime_v2,
        "resident_runtime_v2_public_profile",
        lambda _store: {proactive_resident_runtime_v2.RESIDENT_WAKE_RUNTIME_V2_FLAG: True},
    )

    client = make_client()
    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers={"X-API-Key": api_key})

    assert poll.status_code == 200
    body = poll.get_json()
    assert body["runtime_v2"][proactive_resident_runtime_v2.RESIDENT_WAKE_RUNTIME_V2_FLAG] is True
    assert body["jobs"][0]["job_id"] == job["job_id"]
    assert body["jobs"][0]["runtime_v2"][proactive_resident_runtime_v2.RESIDENT_WAKE_RUNTIME_V2_FLAG] is True


def test_resident_poll_applies_v2_wake_controls_to_legacy_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_resident_poll_v2_gate_key"
    user_id = "usr_resident_poll_v2_gate"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({
        "ambient": False,
        "scheduled": False,
        "reminders_delivery": False,
        "photo_wake_enabled": False,
    })
    store.append_proactive_job({
        "job_id": "pj_photo",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1000.0,
        "status": "pending",
        "trigger": "photo_added",
    })
    store.append_proactive_job({
        "job_id": "pj_scheduled",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1001.0,
        "status": "pending",
        "trigger": "scheduled_wake",
        "scheduled_note": "check in",
    })
    store.append_proactive_job({
        "job_id": "pj_manual",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1002.0,
        "status": "pending",
        "trigger": "manual_dynamic_island",
    })

    client = make_client()
    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers={"X-API-Key": api_key})

    assert poll.status_code == 200
    body = poll.get_json()
    assert [job["job_id"] for job in body["jobs"]] == ["pj_manual"]
    rows = {row["job_id"]: row for row in store.list_proactive_jobs(since_epoch=0, limit=0)}
    assert rows["pj_photo"]["status"] == "skipped"
    assert rows["pj_photo"]["status_reason"] == "photo_wake_disabled"
    assert rows["pj_scheduled"]["status"] == "skipped"
    assert rows["pj_scheduled"]["status_reason"] == "scheduled_disabled"
    assert rows["pj_manual"]["status"] == "pending"


def test_resident_poll_delivers_introduction_even_when_ambient_off(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_resident_poll_intro_key"
    user_id = "usr_resident_poll_intro"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({
        "ambient": False,
        "scheduled": False,
        "reminders_delivery": False,
    })
    store.append_proactive_job({
        "job_id": "pj_intro",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1000.0,
        "status": "pending",
        "trigger": "post_spawn_genesis",
        "job_kind": "introduction",
    })
    store.append_proactive_job({
        "job_id": "pj_old_normal",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1001.0,
        "status": "pending",
        "trigger": "photo_added",
    })

    client = make_client()
    poll = client.get("/v1/proactive/jobs/poll?since=2000&timeout=0", headers={"X-API-Key": api_key})

    assert poll.status_code == 200
    body = poll.get_json()
    assert [job["job_id"] for job in body["jobs"]] == ["pj_intro"]
    rows = {row["job_id"]: row for row in store.list_proactive_jobs(since_epoch=0, limit=0)}
    assert rows["pj_intro"]["status"] == "pending"


def test_capture_job_polls_and_claims_when_ambient_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_capture_ambient_off_key"
    user_id = "usr_capture_ambient_off"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({
        "ambient": False,
        "scheduled": False,
        "reminders_delivery": False,
    })

    job, enqueued, reason = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="session_break",
        capture_key="window:ambient-off",
        window={
            "after_message_id": "msg_before",
            "until_message_id": "msg_until",
            "until_ts": 1200.0,
            "message_count": 8,
        },
        now=1201.0,
    )

    assert enqueued is True
    assert reason == "enqueued"
    assert job is not None
    assert job["job_id"].startswith("cap_")
    client = make_client()
    headers = {"X-API-Key": api_key}

    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers=headers)

    assert poll.status_code == 200
    body = poll.get_json()
    assert [row["job_id"] for row in body["jobs"]] == [job["job_id"]]
    assert body["jobs"][0]["job_kind"] == "memory_capture"
    assert body["jobs"][0]["source"] == "memory_capture"

    claim = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/claim",
        headers=headers,
        json={"consumer_id": "capture-consumer"},
    )

    assert claim.status_code == 200
    assert claim.get_json()["claimed"] is True
    assert claim.get_json()["job"]["status"] == "claimed"

    status = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/status",
        headers=headers,
        json={
            "status": "completed",
            "consumer_id": "capture-consumer",
            "reason": "nothing_worth_keeping",
            "capture_result": {"status": "noop", "reason": "nothing_worth_keeping"},
            "capture_window": job["window"],
            "memory_action_status": {"status": "not_run"},
            "cards_added": 0,
            "cards_superseded": 0,
            "noop_reason": "nothing_worth_keeping",
        },
    )

    assert status.status_code == 200
    patched = status.get_json()["job"]
    assert patched["capture_result"] == {"status": "noop", "reason": "nothing_worth_keeping"}
    assert patched["capture_window"]["until_message_id"] == "msg_until"
    assert patched["memory_action_status"] == {"status": "not_run"}
    assert patched["cards_added"] == 0
    assert patched["cards_superseded"] == 0
    assert patched["noop_reason"] == "nothing_worth_keeping"


def test_capture_job_recovered_when_created_before_consumer_watermark(tmp_path, monkeypatch):
    # Prod regression: a memory_capture job created *before* the resident /
    # agent-runner consumer's first poll (so its ts is below the consumer's
    # spawn-time `since` watermark) was permanently skipped — pending forever,
    # never claimed — while later dream jobs (ts > watermark) were claimed.
    # Memory-maintenance jobs must be recovered by status, ignoring ts, exactly
    # like the post-spawn introduction job.
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_capture_watermark_key"
    user_id = "usr_capture_watermark"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({
        "ambient": False,
        "scheduled": False,
        "reminders_delivery": False,
    })

    # Capture job created at ts=1000 (== "17:58", before the consumer spawned).
    job, enqueued, reason = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="session_break",
        capture_key="window:pre-watermark",
        window={
            "after_message_id": "msg_before",
            "until_message_id": "msg_until",
            "until_ts": 999.0,
            "message_count": 8,
        },
        now=1000.0,
    )
    assert enqueued is True and job is not None

    client = make_client()
    headers = {"X-API-Key": api_key}

    # Consumer first boot seeds its watermark to "now" (== "18:00", ts=2000),
    # well above the capture job's ts. It must still be delivered.
    poll = client.get("/v1/proactive/jobs/poll?since=2000&timeout=0", headers=headers)

    assert poll.status_code == 200
    body = poll.get_json()
    assert [row["job_id"] for row in body["jobs"]] == [job["job_id"]]
    assert body["jobs"][0]["job_kind"] == "memory_capture"

    claim = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/claim",
        headers=headers,
        json={"consumer_id": "agent-runner:usr_capture_watermark"},
    )
    assert claim.status_code == 200
    assert claim.get_json()["claimed"] is True


def test_capture_enqueue_single_flight_per_user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_capture_single_flight")
    seed_user(store.user_id)

    first, first_enqueued, first_reason = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="session_break",
        capture_key="window:first",
        window={"until_message_id": "msg_1", "until_ts": 100.0, "message_count": 3},
        now=101.0,
    )
    second, second_enqueued, second_reason = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="quiet_timeout",
        capture_key="window:second",
        window={"until_message_id": "msg_2", "until_ts": 200.0, "message_count": 5},
        now=201.0,
    )

    assert first_enqueued is True
    assert first_reason == "enqueued"
    assert second_enqueued is False
    assert second_reason == "capture_already_pending"
    assert second["job_id"] == first["job_id"]
    jobs = [row for row in store.list_proactive_jobs(since_epoch=0, limit=0) if row.get("job_kind") == "memory_capture"]
    assert len(jobs) == 1


def test_capture_enqueue_is_idempotent_by_capture_key(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_capture_idempotent")
    seed_user(store.user_id)

    first, first_enqueued, _reason = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="session_break",
        capture_key="window:same",
        window={"until_message_id": "msg_1", "until_ts": 100.0, "message_count": 3},
        now=101.0,
    )
    store.update_proactive_job(first["job_id"], {"status": "completed"})
    second, second_enqueued, second_reason = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="quiet_timeout",
        capture_key="window:same",
        window={"until_message_id": "msg_1", "until_ts": 100.0, "message_count": 3},
        now=301.0,
    )

    assert first_enqueued is True
    assert second_enqueued is False
    assert second_reason == "duplicate_capture_key"
    assert second["job_id"] == first["job_id"]
    jobs = [row for row in store.list_proactive_jobs(since_epoch=0, limit=0) if row.get("job_kind") == "memory_capture"]
    assert len(jobs) == 1


def test_capture_failed_window_retries_same_key(tmp_path, monkeypatch):
    # Regression: a FAILED capture window must be retryable. Previously the
    # terminal failed job matched as duplicate_capture_key forever (and re-armed
    # pending_capture_key) -> permanent capture_already_pending, window never
    # re-captured.
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_capture_failed_retry")
    seed_user(store.user_id)

    first, first_enqueued, _ = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="session_break",
        capture_key="window:same",
        window={"until_message_id": "msg_1", "until_ts": 100.0, "message_count": 3},
        now=101.0,
    )
    store.update_proactive_job(first["job_id"], {"status": "failed"})

    second, second_enqueued, second_reason = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="quiet_timeout",
        capture_key="window:same",
        window={"until_message_id": "msg_1", "until_ts": 100.0, "message_count": 3},
        now=301.0,
    )
    assert first_enqueued is True
    assert second_enqueued is True  # failed window IS retried
    assert second_reason == "enqueued"
    assert second["job_id"] != first["job_id"]  # a fresh job …
    assert second["capture_key"] == first["capture_key"]  # … for the same window

    # While the retry is in flight it's the latest same-key job -> single-flighted,
    # so retries don't pile up.
    third, third_enqueued, third_reason = proactive_capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="quiet_timeout",
        capture_key="window:same",
        window={"until_message_id": "msg_1", "until_ts": 100.0, "message_count": 3},
        now=401.0,
    )
    assert third_enqueued is False
    assert third_reason == "duplicate_capture_key"
    assert third["job_id"] == second["job_id"]
    jobs = [row for row in store.list_proactive_jobs(since_epoch=0, limit=0) if row.get("job_kind") == "memory_capture"]
    assert len(jobs) == 2  # first(failed) + second(pending) — no pile-up


def test_dream_failed_window_retries_same_key(tmp_path, monkeypatch):
    # Same scheduler-correctness fix as capture, for dream: a failed dream must be
    # retryable and must not permanently lock dream_already_pending.
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_dream_failed_retry")
    seed_user(store.user_id)

    first, first_enqueued, _ = proactive_capture_jobs.enqueue_memory_dream_job(
        store, trigger="nightly_dream", dream_key="dream:same", now=101.0,
    )
    store.update_proactive_job(first["job_id"], {"status": "failed"})

    second, second_enqueued, second_reason = proactive_capture_jobs.enqueue_memory_dream_job(
        store, trigger="nightly_dream", dream_key="dream:same", now=301.0,
    )
    assert first_enqueued is True
    assert second_enqueued is True  # failed dream IS retried
    assert second_reason == "enqueued"
    assert second["job_id"] != first["job_id"]

    third, third_enqueued, third_reason = proactive_capture_jobs.enqueue_memory_dream_job(
        store, trigger="nightly_dream", dream_key="dream:same", now=401.0,
    )
    assert third_enqueued is False  # retry in flight -> single-flighted
    assert third_reason == "duplicate_dream_key"
    assert third["job_id"] == second["job_id"]
    jobs = [row for row in store.list_proactive_jobs(since_epoch=0, limit=0) if row.get("job_kind") == "memory_dream"]
    assert len(jobs) == 2  # no pile-up


def _capture_test_envelope(user_id: str, msg_id: str) -> dict:
    return {
        "id": msg_id,
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def _memory_capture_jobs(store) -> list[dict]:
    return [
        row for row in store.list_proactive_jobs(since_epoch=0, limit=0)
        if row.get("job_kind") == "memory_capture"
    ]


def _memory_dream_jobs(store) -> list[dict]:
    return [
        row for row in store.list_proactive_jobs(since_epoch=0, limit=0)
        if row.get("job_kind") == "memory_dream"
    ]


def _dream_test_memory(user_id: str, memory_id: str, *, occurred_at: str = "2026-06-20T00:00:00Z") -> dict:
    return {
        "v": 1,
        "id": memory_id,
        "type": "fact",
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": f"ct_{memory_id}",
        "nonce": f"nonce_{memory_id}",
        "K_user": f"ku_{memory_id}",
        "K_enclave": f"ke_{memory_id}",
        "occurred_at": occurred_at,
        "created_at": occurred_at,
        "updated_at": occurred_at,
        "status": "active",
        "importance": 0.6,
        "pulse": 0.3,
    }


def test_dream_tick_threshold_single_flight_and_ambient_off_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_NEW_CARDS", "3")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()

    api_key = "test_dream_tick_key"
    user_id = "usr_dream_tick"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({
        "ambient": False,
        "scheduled": False,
        "reminders_delivery": False,
    })
    db.memory_replace_all(user_id, [
        _dream_test_memory(user_id, "mem_a"),
        _dream_test_memory(user_id, "mem_b"),
    ])
    client = make_client()
    headers = {"X-API-Key": api_key}

    not_due = client.post("/v1/dream/tick", headers=headers, json={"now": 1000.0})

    assert not_due.status_code == 200
    assert not_due.get_json()["enqueued"] is False
    assert not_due.get_json()["reason"] == "not_enough_new_cards"
    assert _memory_dream_jobs(store) == []

    db.memory_replace_all(user_id, [
        _dream_test_memory(user_id, "mem_a"),
        _dream_test_memory(user_id, "mem_b"),
        _dream_test_memory(user_id, "mem_c"),
    ])
    queued = client.post("/v1/dream/tick", headers=headers, json={"now": 1100.0})
    duplicate = client.post("/v1/dream/tick", headers=headers, json={"now": 1101.0})
    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers=headers)

    assert queued.status_code == 200
    assert queued.get_json()["enqueued"] is True
    assert queued.get_json()["job"]["job_kind"] == "memory_dream"
    assert duplicate.status_code == 200
    assert duplicate.get_json()["enqueued"] is False
    assert duplicate.get_json()["reason"] == "dream_already_pending"
    assert len(_memory_dream_jobs(store)) == 1
    assert poll.status_code == 200
    assert [job["job_id"] for job in poll.get_json()["jobs"]] == [queued.get_json()["job"]["job_id"]]
    assert poll.get_json()["jobs"][0]["source"] == "memory_dream"


def test_dream_tick_respects_dream_enabled_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_NEW_CARDS", "1")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()

    user_id = "usr_dream_enabled_switch"
    seed_user(user_id)
    store = core_store.UserStore(user_id)
    db.memory_replace_all(user_id, [_dream_test_memory(user_id, "mem_switch")])

    store.save_proactive_settings({"dream_enabled": False})
    disabled = proactive_dream_scheduler.tick_memory_dream(store, now=1000.0)
    assert disabled["enqueued"] is False
    assert disabled["reason"] == "dream_disabled"
    assert _memory_dream_jobs(store) == []

    store.save_proactive_settings({"dream_enabled": True})
    enabled = proactive_dream_scheduler.tick_memory_dream(store, now=1001.0)
    assert enabled["enqueued"] is True
    assert enabled["job"]["job_kind"] == "memory_dream"


def test_dream_enqueue_idempotent_by_key_and_single_flight(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = core_store.UserStore("usr_dream_idempotent")
    seed_user(store.user_id)

    first, first_enqueued, first_reason = proactive_capture_jobs.enqueue_memory_dream_job(
        store,
        trigger="nightly_dream",
        dream_key="dream:same",
        dream_until={"signature": "sig_a"},
        dream_stats={"card_count": 3, "signature": "sig_a"},
        now=100.0,
    )
    store.update_proactive_job(first["job_id"], {"status": "completed"})
    duplicate, duplicate_enqueued, duplicate_reason = proactive_capture_jobs.enqueue_memory_dream_job(
        store,
        trigger="nightly_dream",
        dream_key="dream:same",
        dream_until={"signature": "sig_a"},
        dream_stats={"card_count": 3, "signature": "sig_a"},
        now=200.0,
    )
    second, second_enqueued, second_reason = proactive_capture_jobs.enqueue_memory_dream_job(
        store,
        trigger="nightly_dream",
        dream_key="dream:second",
        dream_until={"signature": "sig_b"},
        dream_stats={"card_count": 4, "signature": "sig_b"},
        now=300.0,
    )
    third, third_enqueued, third_reason = proactive_capture_jobs.enqueue_memory_dream_job(
        store,
        trigger="nightly_dream",
        dream_key="dream:third",
        dream_until={"signature": "sig_c"},
        dream_stats={"card_count": 5, "signature": "sig_c"},
        now=400.0,
    )

    assert first_enqueued is True
    assert first_reason == "enqueued"
    assert duplicate_enqueued is False
    assert duplicate_reason == "duplicate_dream_key"
    assert duplicate["job_id"] == first["job_id"]
    assert second_enqueued is True
    assert second_reason == "enqueued"
    assert third_enqueued is False
    assert third_reason == "dream_already_pending"
    assert third["job_id"] == second["job_id"]
    assert len(_memory_dream_jobs(store)) == 2


def test_dream_completion_advances_state(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_NEW_CARDS", "1")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()

    api_key = "test_dream_completion_key"
    user_id = "usr_dream_completion"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    db.memory_replace_all(user_id, [_dream_test_memory(user_id, "mem_done")])
    client = make_client()
    headers = {"X-API-Key": api_key}

    first = client.post("/v1/dream/tick", headers=headers, json={"now": 2000.0})
    job = first.get_json()["job"]
    done = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/status",
        headers=headers,
        json={
            "status": "completed",
            "reason": "dream_nothing_to_consolidate",
            "dream_result": {"status": "noop"},
            "cards_merged": 0,
            "cards_superseded": 0,
            "questions": ["ask later"],
            "noop_reason": "dream_nothing_to_consolidate",
        },
    )
    second = client.post("/v1/dream/tick", headers=headers, json={"now": 2100.0})

    assert first.status_code == 200
    assert first.get_json()["enqueued"] is True
    assert done.status_code == 200
    patched = done.get_json()["job"]
    assert patched["dream_result"] == {"status": "noop"}
    assert patched["questions"] == ["ask later"]
    state = proactive_dream_scheduler.load_dream_state(store)
    assert state["pending_dream_key"] == ""
    assert state["last_dream_completed_at"] > 0
    assert state["last_dreamed_card_count"] == 1
    assert second.status_code == 200
    assert second.get_json()["enqueued"] is False
    assert second.get_json()["reason"] == "already_dreamed"


def test_capture_coordinator_dedupes_same_window_across_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_CAPTURE_TURN_BACKSTOP", "1")
    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "0")
    monkeypatch.setenv("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()

    api_key = "test_capture_dedupe_key"
    user_id = "usr_capture_dedupe"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)

    msg = store.append_chat("user", "chat", _capture_test_envelope(user_id, "msg_capture_dedupe"))
    first_jobs = _memory_capture_jobs(store)
    assert len(first_jobs) == 1
    assert first_jobs[0]["trigger"] == "turn_backstop"

    client = make_client()
    headers = {"X-API-Key": api_key}
    background = client.post(
        "/v1/device/events",
        headers=headers,
        json={
            "source": "ios",
            "type": "app_presence",
            "payload": {"scene_phase": "background", "is_chat_visible": False},
        },
    )
    quiet = client.post(
        "/v1/capture/tick",
        headers=headers,
        json={"now": float(msg["ts"]) + 30.0},
    )

    assert background.status_code == 200
    assert quiet.status_code == 200
    assert background.get_json()["capture"]["enqueued"] is False
    assert quiet.get_json()["enqueued"] is False
    assert len(_memory_capture_jobs(store)) == 1


def test_capture_quiet_tick_noops_without_new_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_capture_quiet_noop_key"
    user_id = "usr_capture_quiet_noop"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    client = make_client()

    resp = client.post(
        "/v1/capture/tick",
        headers={"X-API-Key": api_key},
        json={"now": 2000.0},
    )

    assert resp.status_code == 200
    assert resp.get_json()["enqueued"] is False
    assert resp.get_json()["reason"] == "no_new_messages"
    assert _memory_capture_jobs(core_store.get_store(user_id)) == []


def test_capture_turn_backstop_enqueues_only_when_due(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_CAPTURE_TURN_BACKSTOP", "2")
    monkeypatch.setenv("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", "0")
    store = core_store.UserStore("usr_capture_turn_backstop")
    seed_user(store.user_id)

    store.append_chat("user", "chat", _capture_test_envelope(store.user_id, "msg_turn_1"))
    assert _memory_capture_jobs(store) == []

    store.append_chat("user", "chat", _capture_test_envelope(store.user_id, "msg_turn_2"))
    jobs = _memory_capture_jobs(store)

    assert len(jobs) == 1
    assert jobs[0]["trigger"] == "turn_backstop"
    assert jobs[0]["window"]["until_message_id"] == "msg_turn_2"
    assert jobs[0]["window"]["message_count"] == 2


def test_quiet_capture_respects_capture_enabled_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "0")
    monkeypatch.setenv("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()

    user_id = "usr_capture_enabled_switch"
    seed_user(user_id)
    store = core_store.UserStore(user_id)
    msg = store.append_chat("user", "chat", _capture_test_envelope(user_id, "msg_capture_switch"))

    store.save_proactive_settings({"capture_enabled": False})
    disabled = proactive_capture_scheduler.tick_quiet_capture(store, now=float(msg["ts"]) + 1.0)
    assert disabled["enqueued"] is False
    assert disabled["reason"] == "capture_disabled"
    assert _memory_capture_jobs(store) == []

    store.save_proactive_settings({"capture_enabled": True})
    enabled = proactive_capture_scheduler.tick_quiet_capture(store, now=float(msg["ts"]) + 2.0)
    assert enabled["enqueued"] is True
    assert enabled["job"]["job_kind"] == "memory_capture"


def test_model_api_capture_and_recap_respect_capture_enabled_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    user_id = "usr_model_api_capture_enabled_switch"
    seed_user(user_id)
    store = core_store.UserStore(user_id)
    store.save_proactive_settings({"capture_enabled": False})
    monkeypatch.setattr(
        hosted_turn,
        "_model_api_turn_count",
        lambda _store: hosted_turn.MODEL_API_CAPTURE_TURN_INTERVAL,
    )

    capture = hosted_turn._model_api_maybe_run_memory_capture(
        store,
        api_key=None,
        runtime=None,
        user_message="hello",
        assistant_reply="hi",
        user_message_id="msg_user",
        assistant_message_id="msg_assistant",
        context_payload={},
        effects=[],
    )
    recap_due, recap_reason = hosted_turn._model_api_recap_due(
        store,
        hosted_turn.MODEL_API_CONSOLIDATE_TURN_INTERVAL,
    )

    assert capture["status"] == "skipped"
    assert capture["reason"] == "capture_disabled"
    assert recap_due is False
    assert recap_reason == "capture_disabled"


def test_capture_device_boundary_ignores_proactive_switches(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_CAPTURE_TURN_BACKSTOP", "999")
    monkeypatch.setenv("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()

    api_key = "test_capture_switches_off_key"
    user_id = "usr_capture_switches_off"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({
        "ambient": False,
        "scheduled": False,
        "reminders_delivery": False,
    })
    store.append_chat("user", "chat", _capture_test_envelope(user_id, "msg_switches_off"))

    resp = make_client().post(
        "/v1/device/events",
        headers={"X-API-Key": api_key},
        json={
            "source": "ios",
            "type": "app_presence",
            "payload": {"scene_phase": "background", "is_chat_visible": False},
        },
    )

    assert resp.status_code == 200
    assert resp.get_json()["capture"]["enqueued"] is True
    assert len(_memory_capture_jobs(store)) == 1


def test_capture_completion_advances_state_and_blocks_same_window(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_CAPTURE_TURN_BACKSTOP", "999")
    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "0")
    monkeypatch.setenv("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()

    api_key = "test_capture_completion_key"
    user_id = "usr_capture_completion"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    msg = store.append_chat("user", "chat", _capture_test_envelope(user_id, "msg_capture_done"))
    client = make_client()
    headers = {"X-API-Key": api_key}

    first = client.post(
        "/v1/device/events",
        headers=headers,
        json={
            "source": "ios",
            "type": "app_presence",
            "payload": {"scene_phase": "background", "is_chat_visible": False},
        },
    )
    job = first.get_json()["capture"]["job"]
    full_job = _memory_capture_jobs(store)[0]
    done = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/status",
        headers=headers,
        json={
            "status": "completed",
            "reason": "nothing_worth_keeping",
            "capture_window": full_job["window"],
            "capture_result": {"status": "noop", "reason": "nothing_worth_keeping"},
        },
    )
    second = client.post(
        "/v1/capture/tick",
        headers=headers,
        json={"now": float(msg["ts"]) + 30.0},
    )

    assert first.status_code == 200
    assert first.get_json()["capture"]["enqueued"] is True
    assert done.status_code == 200
    state = proactive_capture_scheduler.load_capture_state(store)
    assert state["pending_capture_key"] == ""
    assert state["last_captured_until_message_id"] == "msg_capture_done"
    assert state["turns_since_capture"] == 0
    assert second.status_code == 200
    assert second.get_json()["enqueued"] is False
    assert second.get_json()["reason"] in {"no_new_messages", "already_captured"}
    assert len(_memory_capture_jobs(store)) == 1


def test_resident_scheduled_fire_endpoint_queues_due_timer_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()
    scheduled_store, _settings_by_user = _patch_resident_scheduled_route_dependencies(monkeypatch)

    api_key = "test_resident_scheduled_fire_key"
    user_id = "usr_resident_scheduled_fire"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}
    scheduled = client.post(
        "/v1/proactive/scheduled/actions",
        headers=headers,
        json={
            "actions": [{
                "type": "schedule_wake",
                "at": "2000-01-01T00:00:00+00:00",
                "tz": "UTC",
                "note": "take meds",
            }],
            "turn_id": "turn_test",
            "wake_ids": ["wake_original"],
        },
    )
    assert scheduled.status_code == 200
    timer_id = scheduled.get_json()["results"][0]["timer_id"]

    fired = client.post("/v1/proactive/scheduled/fire", headers=headers, json={})

    assert fired.status_code == 200
    body = fired.get_json()
    assert body["queued"] == 1
    assert body["results"][0]["status"] == "fired"
    assert body["results"][0]["timer_id"] == timer_id
    job = body["jobs"][0]
    assert job["trigger"] == "scheduled_wake"
    assert job["wake_kind"] == "scheduled_wake"
    assert job["scheduled_note"] == "take meds"
    assert job["payload"]["v2_wake"]["scheduled_wake"]["wake_id"] == timer_id
    record = scheduled_store.list_records(user_id)[0]
    assert record.status == "fired"
    assert record.fired_wake_id == body["results"][0]["wake_id"]


def test_resident_scheduled_fire_endpoint_transparency_when_scheduled_off(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()
    scheduled_store, settings_by_user = _patch_resident_scheduled_route_dependencies(monkeypatch)

    api_key = "test_resident_scheduled_fire_off_key"
    user_id = "usr_resident_scheduled_fire_off"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}
    scheduled = client.post(
        "/v1/proactive/scheduled/actions",
        headers=headers,
        json={
            "actions": [{
                "type": "schedule_wake",
                "at": "2000-01-01T00:00:00+00:00",
                "tz": "UTC",
                "note": "check in",
            }],
        },
    )
    assert scheduled.status_code == 200
    timer_id = scheduled.get_json()["results"][0]["timer_id"]
    settings_by_user[user_id] = {"scheduled": False}

    fired = client.post("/v1/proactive/scheduled/fire", headers=headers, json={})

    assert fired.status_code == 200
    body = fired.get_json()
    assert body["queued"] == 1
    assert body["results"][0]["status"] == "blocked"
    assert body["results"][0]["reason"] == "scheduled_disabled"
    assert body["results"][0]["transparency_wake_id"]
    job = body["jobs"][0]
    assert job["trigger"] == "background_result"
    assert job["intent_label"] == "scheduled_transparency"
    assert job["wake_kind"] == "background_result"
    assert job["background_payload"]["reason"] == "scheduled_disabled"
    assert job["background_payload"]["timer"]["wake_id"] == timer_id
    record = scheduled_store.list_records(user_id)[0]
    assert record.status == "blocked"
    assert record.block_reason == "scheduled_disabled"
    assert record.transparency_wake_id == body["results"][0]["transparency_wake_id"]


def test_resident_scheduled_fire_endpoint_ignores_future_timer(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()
    scheduled_store, _settings_by_user = _patch_resident_scheduled_route_dependencies(monkeypatch)

    api_key = "test_resident_scheduled_future_key"
    user_id = "usr_resident_scheduled_future"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    client = make_client()
    headers = {"X-API-Key": api_key}
    scheduled = client.post(
        "/v1/proactive/scheduled/actions",
        headers=headers,
        json={
            "actions": [{
                "type": "schedule_wake",
                "at": "2999-01-01T00:00:00+00:00",
                "tz": "UTC",
                "note": "future",
            }],
        },
    )
    assert scheduled.status_code == 200

    fired = client.post("/v1/proactive/scheduled/fire", headers=headers, json={})

    assert fired.status_code == 200
    body = fired.get_json()
    assert body["queued"] == 0
    assert body["results"] == []
    assert body["jobs"] == []
    record = scheduled_store.list_records(user_id)[0]
    assert record.status == "pending"


def test_resident_stale_claim_is_recovered_and_old_consumer_cannot_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    now = 5000.0
    api_key = "test_resident_stale_reclaim_key"
    user_id = "usr_resident_stale_reclaim"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.append_proactive_job({
        "job_id": "pj_stale_resident",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 100.0,
        "status": "claimed",
        "consumer_id": "resident-a",
        "claimed_at": str(now - proactive_poll_core.RESIDENT_WAKE_LEASE_SEC - 1),
        "trigger": "heartbeat_broadcast_on",
    })
    monkeypatch.setattr(proactive_poll_core.time, "time", lambda: now)

    client = make_client()
    headers = {"X-API-Key": api_key}
    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers=headers)

    assert poll.status_code == 200
    jobs = poll.get_json()["jobs"]
    assert [job["job_id"] for job in jobs] == ["pj_stale_resident"]
    recovered = jobs[0]
    assert recovered["status"] == "pending"
    assert recovered["status_reason"] == "resident_stale_claim_recovered"
    assert recovered["consumer_id"] == "recovered:resident-a"
    assert recovered.get("recovered_at")

    stale_status = client.post(
        "/v1/proactive/jobs/pj_stale_resident/status",
        headers=headers,
        json={"status": "posted", "consumer_id": "resident-a"},
    )
    assert stale_status.status_code == 409
    assert stale_status.get_json()["error"] == "consumer_mismatch"


def test_resident_reaper_does_not_reclaim_hosted_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    now = 6000.0
    api_key = "test_resident_reaper_hosted_key"
    user_id = "usr_resident_reaper_hosted"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.append_proactive_job({
        "job_id": "pj_hosted_claim",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 100.0,
        "status": "claimed",
        "consumer_id": "hosted_runtime_v2",
        "claimed_at": str(now - proactive_poll_core.RESIDENT_WAKE_LEASE_SEC - 1),
        "trigger": "heartbeat_broadcast_on",
    })
    monkeypatch.setattr(proactive_poll_core.time, "time", lambda: now)

    client = make_client()
    poll = client.get(
        "/v1/proactive/jobs/poll?since=0&timeout=0",
        headers={"X-API-Key": api_key},
    )

    assert poll.status_code == 200
    assert poll.get_json()["jobs"] == []
    row = store.list_proactive_jobs(since_epoch=0, limit=0)[0]
    assert row["status"] == "claimed"
    assert row["consumer_id"] == "hosted_runtime_v2"


def test_proactive_chat_response_records_push_delivery_results(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    core_store._stores.clear()

    sent_push_types = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        sent_push_types.append(push_type)
        return {"status": "delivered"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    api_key = "test_proactive_delivery_key"
    user_id = "usr_endpoint_proactive_delivery"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "live-token",
            "activity_id": "activity_1",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "device",
            "token": "device-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
    ]

    client = make_client()
    headers = {"X-API-Key": api_key}
    envelope = {
        "id": "msg_delivery_1",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
    }

    resp = client.post(
        "/v1/chat/response",
        headers=headers,
        json={
            "envelope": envelope,
            "source": proactive_service.PROACTIVE_JOB_SOURCE,
            "gate_decision_id": "gd_delivery",
            "proactive_job_id": "pj_delivery",
            "alert_body": "我看到你停在这里了。",
            "push_live_activity": True,
            "push_body": "我看到你停在这里了。",
        },
    )

    assert resp.status_code == 200
    assert sent_push_types == ["liveactivity", "alert"]
    snapshot = proactive_dashboard._proactive_debug_snapshot(store)
    msg = snapshot["proactive_messages"][0]
    assert msg["alert_preview"] == "我看到你停在这里了。"
    assert msg["alert_status"] == "delivered"
    assert msg["live_activity_status"] == "delivered"


def test_proactive_chat_response_delivery_off_writes_chat_without_push(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    core_store._stores.clear()

    sent_push_types = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        sent_push_types.append(push_type)
        return {"status": "delivered"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    api_key = "test_proactive_delivery_off_key"
    user_id = "usr_endpoint_proactive_delivery_off"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({"reminders_delivery": False})
    store.tokens = [
        {
            "type": "live_activity",
            "token": "live-token",
            "activity_id": "activity_1",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "device",
            "token": "device-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
    ]

    client = make_client()
    headers = {"X-API-Key": api_key}
    envelope = {
        "id": "msg_delivery_off_1",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
    }

    resp = client.post(
        "/v1/chat/response",
        headers=headers,
        json={
            "envelope": envelope,
            "source": proactive_service.PROACTIVE_JOB_SOURCE,
            "gate_decision_id": "gd_delivery_off",
            "proactive_job_id": "pj_delivery_off",
            "alert_body": "这条应该静默写入。",
            "push_live_activity": True,
            "push_body": "这条应该静默写入。",
        },
    )

    assert resp.status_code == 200
    assert sent_push_types == []
    snapshot = proactive_dashboard._proactive_debug_snapshot(store)
    msg = snapshot["proactive_messages"][0]
    assert msg["alert_preview"] == "这条应该静默写入。"
    assert msg["push_decision"] == "suppressed"
    assert msg["push_reason"] == "reminders_delivery_disabled"
    assert msg["alert_status"] == "suppressed"
    assert msg["live_activity_status"] == "suppressed"


def test_ai_chat_response_pushes_when_app_background(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    core_store._stores.clear()

    sent = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        sent.append({
            "token": device_token,
            "push_type": push_type,
            "event": (payload.get("aps") or {}).get("event"),
        })
        return {"status": "delivered"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    api_key = "test_ai_push_background_key"
    user_id = "usr_ai_push_background"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "live-token",
            "activity_id": "activity_1",
            "status": "active",
            "registered_at": "2026-06-01T00:00:00",
        },
        {
            "type": "device",
            "token": "device-token",
            "status": "active",
            "registered_at": "2026-06-01T00:00:01",
        },
    ]
    store.append_device_event(proactive_service._make_device_event("ios", "app_presence", {
        "scene_phase": "background",
        "is_foreground": False,
        "selected_tab": "chat",
        "is_chat_visible": False,
    }))

    resp = make_client().post(
        "/v1/chat/response",
        headers={"X-API-Key": api_key},
        json={
            "envelope": {
                "id": "msg_ai_background",
                "v": 1,
                "body_ct": "ct",
                "nonce": "nonce",
                "K_user": "k-user",
                "K_enclave": "k-enclave",
                "visibility": "shared",
                "owner_user_id": user_id,
            },
            "source": "chat",
            "alert_body": "后台时每条 AI 消息都要推送。",
        },
    )

    assert resp.status_code == 200
    assert [(row["push_type"], row["event"]) for row in sent] == [
        ("liveactivity", "update"),
        ("alert", None),
    ]
    msg = store.chat_messages[-1]
    assert msg["push_decision"] == "send"
    assert msg["push_reason"] == "app_background"
    assert msg["live_activity_status"] == "delivered"
    assert msg["alert_status"] == "delivered"


def test_ai_chat_response_suppresses_push_when_app_foreground(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    core_store._stores.clear()

    sent = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        sent.append(push_type)
        return {"status": "delivered"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    api_key = "test_ai_push_foreground_key"
    user_id = "usr_ai_push_foreground"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {"type": "live_activity", "token": "live-token", "activity_id": "activity_1", "status": "active"},
        {"type": "device", "token": "device-token", "status": "active"},
    ]
    store.append_device_event(proactive_service._make_device_event("ios", "app_presence", {
        "scene_phase": "active",
        "is_foreground": True,
        "selected_tab": "chat",
        "is_chat_visible": True,
    }))

    resp = make_client().post(
        "/v1/chat/response",
        headers={"X-API-Key": api_key},
        json={
            "envelope": {
                "id": "msg_ai_foreground",
                "v": 1,
                "body_ct": "ct",
                "nonce": "nonce",
                "K_user": "k-user",
                "K_enclave": "k-enclave",
                "visibility": "shared",
                "owner_user_id": user_id,
            },
            "source": "chat",
            "alert_body": "前台时不要打断用户。",
        },
    )

    assert resp.status_code == 200
    assert sent == []
    msg = store.chat_messages[-1]
    assert msg["push_decision"] == "suppress"
    assert msg["push_reason"] == "app_foreground_chat_visible"
    assert msg["live_activity_status"] == "suppressed"
    assert msg["alert_status"] == "suppressed"


def test_chat_history_supports_lightweight_images_and_before_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(chat_service, "CHAT_HISTORY_INLINE_BODY_CT_MAX", 64)
    core_store._stores.clear()

    api_key = "test_chat_history_lightweight_key"
    user_id = "usr_chat_history_lightweight"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)

    def _append(idx: int, *, content_type: str = "text", body_ct: str = "ct"):
        msg = store.append_chat(
            "user",
            "chat",
            {
                "id": f"msg_{idx}",
                "v": 1,
                "body_ct": body_ct,
                "nonce": "nonce",
                "K_user": "k-user",
                "K_enclave": "k-enclave",
                "visibility": "shared",
                "owner_user_id": user_id,
            },
            content_type=content_type,
        )
        msg["ts"] = float(idx)
        # Persist the deterministic ts override through the DB layer (the old
        # file-based store._persist_chat() is gone — chat is row-per-message now).
        db.chat_append(user_id, msg["id"], msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)
        return msg

    for i in range(1, 6):
        _append(i)
    _append(6, content_type="image", body_ct="x" * 4096)
    _append(7, body_ct="y" * 128)
    for i in range(8, 11):
        _append(i)

    with make_client() as client:
        latest = client.get(
            "/v1/chat/history?limit=6&include_image_body=false",
            headers={"X-API-Key": api_key},
        )
        older = client.get(
            "/v1/chat/history?before=5&limit=3&include_image_body=false",
            headers={"X-API-Key": api_key},
        )
        image_body = client.get(
            "/v1/chat/messages/msg_6/body",
            headers={"X-API-Key": api_key},
        )
        large_text_body = client.get(
            "/v1/chat/messages/msg_7/body",
            headers={"X-API-Key": api_key},
        )

    assert latest.status_code == 200
    latest_body = latest.get_json()
    assert [m["id"] for m in latest_body["messages"]] == [
        "msg_5",
        "msg_6",
        "msg_7",
        "msg_8",
        "msg_9",
        "msg_10",
    ]
    assert latest_body["has_more_older"] is True
    assert latest_body["image_bodies_omitted"] == 1
    assert latest_body["bodies_omitted"] == 2
    assert latest_body["body_omit_inline_max"] == 64
    image_row = [m for m in latest_body["messages"] if m["id"] == "msg_6"][0]
    assert image_row["body_omitted"] is True
    assert image_row["body_omitted_reason"] == "image_body"
    assert image_row["body_ct_len"] == 4096
    assert "body_ct" not in image_row
    assert "K_user" not in image_row
    large_text_row = [m for m in latest_body["messages"] if m["id"] == "msg_7"][0]
    assert large_text_row["content_type"] == "text"
    assert large_text_row["body_omitted"] is True
    assert large_text_row["body_omitted_reason"] == "large_body_ct"
    assert large_text_row["body_ct_len"] == 128
    assert "body_ct" not in large_text_row
    assert "K_user" not in large_text_row

    assert older.status_code == 200
    older_body = older.get_json()
    assert [m["id"] for m in older_body["messages"]] == ["msg_2", "msg_3", "msg_4"]
    assert older_body["has_more_older"] is True

    assert image_body.status_code == 200
    full_image = image_body.get_json()["message"]
    assert full_image["id"] == "msg_6"
    assert full_image["body_ct"] == "x" * 4096
    assert full_image["body_omitted"] is False

    assert large_text_body.status_code == 200
    full_large_text = large_text_body.get_json()["message"]
    assert full_large_text["id"] == "msg_7"
    assert full_large_text["body_ct"] == "y" * 128
    assert full_large_text["body_omitted"] is False


def test_proactive_chat_response_uses_push_to_start_when_start_window_open(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    core_store._stores.clear()

    sent = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        sent.append({
            "token": device_token,
            "push_type": push_type,
            "event": (payload.get("aps") or {}).get("event"),
            "content_state": (payload.get("aps") or {}).get("content-state", {}),
            "alert": (payload.get("aps") or {}).get("alert", {}),
        })
        return {"status": "delivered"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    api_key = "test_proactive_start_key"
    user_id = "usr_endpoint_proactive_start"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "live-token",
            "activity_id": "activity_1",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "push_to_start",
            "token": "start-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:01",
        },
        {
            "type": "device",
            "token": "device-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:02",
        },
    ]

    client = make_client()
    headers = {"X-API-Key": api_key}
    envelope = {
        "id": "msg_start_1",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
    }

    resp = client.post(
        "/v1/chat/response",
        headers=headers,
        json={
            "envelope": envelope,
            "source": proactive_service.PROACTIVE_JOB_SOURCE,
            "gate_decision_id": "gd_start",
            "proactive_job_id": "pj_start",
            "alert_body": "我看到你停在这里了。",
            "push_live_activity": True,
            "push_body": "我看到你停在这里了。",
        },
    )

    assert resp.status_code == 200
    assert [(row["push_type"], row["event"]) for row in sent] == [
        ("liveactivity", "end"),
        ("liveactivity", "start"),
        ("alert", None),
    ]
    start_state = sent[1]["content_state"]
    assert start_state["visualState"] == "reply"
    assert start_state["name"] == "IO"
    assert start_state["desc"] == "我看到你停在这里了。"
    assert start_state["body"] == "我看到你停在这里了。"

    snapshot = proactive_dashboard._proactive_debug_snapshot(store)
    msg = snapshot["proactive_messages"][0]
    assert msg["live_activity_status"] == "delivered"
    assert msg["live_activity_mode"] == "start"
    assert store.live_activity_start_cooldown_remaining_seconds() > 0


def test_proactive_chat_response_uses_update_during_start_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    core_store._stores.clear()

    sent = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        sent.append({
            "token": device_token,
            "push_type": push_type,
            "event": (payload.get("aps") or {}).get("event"),
            "content_state": (payload.get("aps") or {}).get("content-state", {}),
            "alert": (payload.get("aps") or {}).get("alert", {}),
        })
        return {"status": "delivered"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    api_key = "test_proactive_update_key"
    user_id = "usr_endpoint_proactive_update"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "live-token",
            "activity_id": "activity_1",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "push_to_start",
            "token": "start-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:01",
        },
        {
            "type": "device",
            "token": "device-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:02",
        },
    ]
    store.live_activity_state["last_start_epoch"] = time.time()

    client = make_client()
    headers = {"X-API-Key": api_key}
    envelope = {
        "id": "msg_update_1",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
    }

    resp = client.post(
        "/v1/chat/response",
        headers=headers,
        json={
            "envelope": envelope,
            "source": proactive_service.PROACTIVE_JOB_SOURCE,
            "gate_decision_id": "gd_update",
            "proactive_job_id": "pj_update",
            "alert_body": "继续看一下这里。",
            "push_live_activity": True,
            "push_body": "继续看一下这里。",
        },
    )

    assert resp.status_code == 200
    assert [(row["push_type"], row["event"]) for row in sent] == [
        ("liveactivity", "update"),
        ("alert", None),
    ]
    update_state = sent[0]["content_state"]
    assert update_state["visualState"] == "reply"
    assert update_state["desc"] == "继续看一下这里。"
    assert sent[0]["alert"] == {"title": "IO", "body": "继续看一下这里。"}

    snapshot = proactive_dashboard._proactive_debug_snapshot(store)
    msg = snapshot["proactive_messages"][0]
    assert msg["live_activity_status"] == "delivered"
    assert msg["live_activity_mode"] == "update"


def test_apns_retries_production_when_sandbox_rejects_testflight_token(monkeypatch):
    monkeypatch.setattr(push_apns, "APNS_KEY", "test-key")
    monkeypatch.setattr(push_apns, "APNS_SANDBOX", True)
    monkeypatch.setattr(push_apns, "_make_apns_jwt", lambda: "jwt")

    calls = []

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json, headers):
            calls.append(url)
            if "sandbox" in url:
                return _Resp(400, '{"reason":"BadDeviceToken"}')
            return _Resp(200)

    monkeypatch.setattr(httpx, "Client", _Client)

    result = push_apns._send_apns(
        "testflight-token",
        {"aps": {"alert": {"body": "hi"}}},
        push_type="alert",
        topic="com.feedling.mcp",
    )

    assert [("sandbox" in url) for url in calls] == [True, False]
    assert result["status"] == "delivered"
    assert result["apns_env"] == "production"
    assert result["fallback_attempted"] is True
    assert result["fallback_from"] == "sandbox"


def test_apns_retries_production_when_sandbox_returns_bad_environment_key(monkeypatch):
    monkeypatch.setattr(push_apns, "APNS_KEY", "test-key")
    monkeypatch.setattr(push_apns, "APNS_SANDBOX", True)
    monkeypatch.setattr(push_apns, "_make_apns_jwt", lambda: "jwt")

    calls = []

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json, headers):
            calls.append(url)
            if "sandbox" in url:
                return _Resp(400, '{"reason":"BadEnvironmentKeyInToken"}')
            return _Resp(200)

    monkeypatch.setattr(httpx, "Client", _Client)

    result = push_apns._send_apns(
        "testflight-token",
        {"aps": {"alert": {"body": "hi"}}},
        push_type="alert",
        topic="com.feedling.mcp",
    )

    assert [("sandbox" in url) for url in calls] == [True, False]
    assert result["status"] == "delivered"
    assert result["apns_env"] == "production"
    assert result["fallback_attempted"] is True
    assert result["fallback_from"] == "sandbox"


def test_chat_alert_falls_back_from_bad_latest_device_token(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_bad_device_token_key"
    user_id = "usr_bad_device_token"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {
            "type": "device",
            "token": "old-device-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "device",
            "token": "new-device-token",
            "status": "active",
            "registered_at": "2026-05-29T00:00:00",
        },
    ]

    seen = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        seen.append(device_token)
        if device_token == "old-device-token":
            return {"status": "delivered", "apns_env": "production"}
        return {"status": "error", "code": 400, "reason": '{"reason":"BadDeviceToken"}', "apns_env": "production"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    result = push_service._send_chat_alert(store, "hello", alert_title="Dora")

    assert result["status"] == "delivered"
    assert seen == ["new-device-token", "old-device-token"]
    latest = [t for t in store.tokens if t["token"] == "new-device-token"][0]
    assert latest["status"] == "expired"
    assert "BadDeviceToken" in latest["last_error"]
    older = [t for t in store.tokens if t["token"] == "old-device-token"][0]
    assert older["status"] == "active"
    assert older["last_success_at"]


def test_live_activity_falls_back_from_topic_mismatch_token(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_bad_live_activity_token_key"
    user_id = "usr_bad_live_activity_token"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "old-live-token",
            "status": "active",
            "activity_id": "old_activity",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "live_activity",
            "token": "new-live-token",
            "status": "active",
            "activity_id": "new_activity",
            "registered_at": "2026-05-29T00:00:00",
        },
    ]

    seen = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        seen.append(device_token)
        if device_token == "old-live-token":
            return {"status": "delivered", "apns_env": "production"}
        return {
            "status": "error",
            "code": 400,
            "reason": '{"reason":"DeviceTokenNotForTopic"}',
            "apns_env": "production",
        }

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    with make_client() as client:
        resp = client.post(
            "/v1/push/live-activity",
            headers={"X-API-Key": api_key},
            json={"body": "hello"},
        )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "delivered"
    assert seen == ["new-live-token", "old-live-token"]
    latest = [t for t in store.tokens if t["token"] == "new-live-token"][0]
    assert latest["status"] == "expired"
    assert "DeviceTokenNotForTopic" in latest["last_error"]


def test_live_activity_expires_environment_mismatch_token(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_bad_live_activity_env_key"
    user_id = "usr_bad_live_activity_env"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "old-live-token",
            "status": "active",
            "activity_id": "old_activity",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "live_activity",
            "token": "new-live-token",
            "status": "active",
            "activity_id": "new_activity",
            "registered_at": "2026-05-29T00:00:00",
        },
    ]

    seen = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        seen.append(device_token)
        if device_token == "old-live-token":
            return {"status": "delivered", "apns_env": "production"}
        return {
            "status": "error",
            "code": 400,
            "reason": '{"reason":"BadEnvironmentKeyInToken"}',
            "apns_env": "production",
        }

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    with make_client() as client:
        resp = client.post(
            "/v1/push/live-activity",
            headers={"X-API-Key": api_key},
            json={"body": "hello"},
        )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "delivered"
    assert seen == ["new-live-token", "old-live-token"]
    latest = [t for t in store.tokens if t["token"] == "new-live-token"][0]
    assert latest["status"] == "expired"
    assert latest["expired_at"]
    assert "BadEnvironmentKeyInToken" in latest["last_error"]


def test_live_activity_expiring_error_requests_token_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_refresh_bad_live_activity_key"
    user_id = "usr_refresh_bad_live"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    store = core_store.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "bad-live-token",
            "status": "active",
            "activity_id": "activity_1",
            "registered_at": "2026-05-29T00:00:00",
        }
    ]

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        return {
            "status": "error",
            "code": 400,
            "reason": '{"reason":"BadDeviceToken"}',
            "apns_env": "production",
        }

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    with make_client() as client:
        resp = client.post(
            "/v1/push/live-activity",
            headers={"X-API-Key": api_key},
            json={"body": "hello"},
        )

    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "error"
    assert body["needs_refresh"] is True
    assert body["reason"] == "BadDeviceToken"
    latest = store.tokens[0]
    assert latest["status"] == "expired"
    assert latest["expired_at"]


def test_register_token_persists_client_push_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()

    api_key = "test_token_metadata_key"
    user_id = "usr_token_metadata"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)

    with make_client() as client:
        resp = client.post(
            "/v1/push/register-token",
            headers={"X-API-Key": api_key},
            json={
                "type": "device",
                "token": "device-token",
                "apns_env": "production",
                "bundle_id": "com.feedling.mcp",
                "app_version": "1.0.0",
                "app_build": "42",
                "build_configuration": "release",
                "device_model": "iPhone",
                "system_version": "26.4.1",
            },
        )

    assert resp.status_code == 200
    store = core_store.get_store(user_id)
    token = store.tokens[0]
    assert token["status"] == "active"
    assert token["apns_env"] == "production"
    assert token["bundle_id"] == "com.feedling.mcp"
    assert token["app_build"] == "42"


def test_apns_prefers_token_recorded_environment(monkeypatch):
    monkeypatch.setattr(push_apns, "APNS_KEY", "test-key")
    monkeypatch.setattr(push_apns, "APNS_SANDBOX", True)
    monkeypatch.setattr(push_apns, "_make_apns_jwt", lambda: "jwt")

    calls = []

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json, headers):
            calls.append(url)
            return _Resp(200)

    monkeypatch.setattr(httpx, "Client", _Client)

    result = push_apns._send_apns(
        "testflight-token",
        {"aps": {"alert": {"body": "hi"}}},
        push_type="alert",
        topic="com.feedling.mcp",
        preferred_env="production",
    )

    assert result["status"] == "delivered"
    assert result["apns_env"] == "production"
    assert [("sandbox" in url) for url in calls] == [False]
