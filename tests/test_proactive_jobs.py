import importlib
import os
import sys
import tempfile
from pathlib import Path


_DATA_DIR = tempfile.mkdtemp(prefix="feedling-proactive-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

appmod = importlib.import_module("app")
from chat import service as chat_service  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from proactive import dashboard as proactive_dashboard  # noqa: E402
from push import apns as push_apns  # noqa: E402
from core import config as core_config  # noqa: E402


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

    event = appmod._make_device_event("ios", "permission_changed", raw)

    assert event["event_id"].startswith("evt_")
    assert event["payload"]["permission"] == "authorized"
    assert event["payload"]["safe_bucket"] == "home_area"
    assert event["payload"]["scene_tags"] == ["work", "reading"]
    assert "lat" not in event["payload"]
    assert "lng" not in event["payload"]
    assert "title" not in event["payload"]


def test_manual_proactive_wake_creates_hidden_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive")

    decision = appmod._build_proactive_v2_wake_decision(
        store,
        {
            "force": True,
            "context_hint": "The user has been comparing a product for several minutes.",
            "connections": ["This matches a repeated research pattern."],
            "intent_label": "screen_research",
        },
    )
    store.append_gate_decision(decision)
    job = store.append_proactive_job(appmod._proactive_job_from_decision(decision))

    assert decision["should_reach_out"] is True
    assert decision["schema_version"] == 2
    assert decision["decision_id"].startswith("gd_")
    assert job["job_id"].startswith("pj_")
    assert job["source"] == appmod.PROACTIVE_JOB_SOURCE
    assert job["gate_decision_id"] == decision["decision_id"]
    assert job["context_hint"] == ""
    assert job["wake_kind"] == "presence"

    jobs = store.list_proactive_jobs(since_epoch=0)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job["job_id"]


def test_proactive_debug_derives_job_delivery_state(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive_delivery")

    decision = appmod._build_proactive_v2_wake_decision(
        store,
        {
            "force": True,
            "context_hint": "The user is testing proactive delivery.",
            "intent_label": "manual_proactive_test",
            "frames": [{"id": "abcd1234abcd1234"}],
        },
    )
    job = store.append_proactive_job(appmod._proactive_job_from_decision(decision))
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
        appmod.PROACTIVE_JOB_SOURCE,
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

    snapshot = appmod._proactive_debug_snapshot(store)

    assert snapshot["jobs"][0]["derived_status"] == "delivered"
    assert snapshot["jobs"][0]["preview"] == "自然地提醒用户。"
    assert snapshot["jobs"][0]["alert_status"] == "delivered"
    assert snapshot["jobs"][0]["live_activity_status"] == "delivered"


def test_proactive_debug_folds_legacy_no_frame_ticks(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive_folded_gate")

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

    assert appmod._gate_decision_has_frame_context(no_frame) is False
    assert appmod._gate_decision_has_frame_context(with_frame) is True

    snapshot = appmod._proactive_debug_snapshot(store)
    with appmod.app.test_request_context("/debug/proactive?key=test&lang=zh"):
        page = appmod._render_proactive_dashboard(snapshot)

    assert "主表判定 1" in page
    assert "隐藏空 tick 1" in page
    assert "显示隐藏的旧版无屏幕帧空 tick（1）" in page
    assert "frame_backed_false_unit_test" in page
    assert "no_recent_frames_unit_test" not in page
    assert "显示样本" in page

    with appmod.app.test_request_context("/debug/proactive?key=test&lang=zh&show_no_frame=1"):
        expanded_page = appmod._render_proactive_dashboard(snapshot)

    assert expanded_page.find("frame_backed_false_unit_test") < expanded_page.find("no_recent_frames_unit_test")

    with appmod.app.test_request_context("/debug/proactive?key=test&lang=en"):
        page_en = appmod._render_proactive_dashboard(snapshot)

    assert "visible decisions 1" in page_en
    assert "hidden no-frame ticks 1" in page_en
    assert "Show hidden legacy no-frame ticks (1)" in page_en
    assert "no_recent_frames_unit_test" not in page_en


def test_proactive_debug_dashboard_defaults_to_deep_full_view(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive_deep_dashboard")

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
            appmod.PROACTIVE_JOB_SOURCE,
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
                "ts": appmod.time.time() + i,
                "source": "unit",
                "type": f"event_{i}",
                "payload": {"id": f"payload_{i}"},
            }
        )

    snapshot = appmod._proactive_debug_snapshot(store)
    with appmod.app.test_request_context("/debug/proactive?key=test&lang=en"):
        page = appmod._render_proactive_dashboard(snapshot)

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
    store = appmod.UserStore("usr_test_proactive_debug_translate")
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
    snapshot = appmod._proactive_debug_snapshot(store)

    monkeypatch.setattr(
        proactive_dashboard,
        "_translate_debug_texts_to_zh",
        lambda texts: {
            reason: "屏幕内容和用户的记忆花园有明确关联。",
            context_hint: "用户正在比较一段密集笔记，可能适合轻轻提醒一句。",
            abstention: "陪伴者已经结合用户当前上下文给过合适回复。",
        },
    )

    with appmod.app.test_request_context("/debug/proactive?key=test&lang=zh"):
        page_zh = appmod._render_proactive_dashboard(snapshot)
    with appmod.app.test_request_context("/debug/proactive?key=test&lang=en"):
        page_en = appmod._render_proactive_dashboard(snapshot)

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
    appmod._stores.clear()

    api_key = "test_proactive_timezone_key"
    user_id = "usr_endpoint_proactive_timezone"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    client = appmod.app.test_client()
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


def test_proactive_tick_endpoint_enqueues_pollable_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_proactive_key"
    user_id = "usr_endpoint_proactive"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    client = appmod.app.test_client()
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
    assert body["job"]["source"] == appmod.PROACTIVE_JOB_SOURCE
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


def test_auto_proactive_v2_wake_samples_frames_without_gate_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(proactive_dashboard, "OPENROUTER_API_KEY", "sk-test")
    appmod._stores.clear()

    monkeypatch.setattr(
        appmod,
        "_decrypt_frame_metadata_for_gate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("V2 must not decrypt in tick")),
    )
    api_key = "test_proactive_auto_key"
    user_id = "usr_endpoint_proactive_auto"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.frames_meta.append({
        "id": "abcd1234abcd1234",
        "filename": "abcd1234abcd1234.env.json",
        "ts": appmod.time.time(),
        "encrypted": True,
        "app": None,
        "ocr_text": "",
    })

    client = appmod.app.test_client()
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
    appmod._stores.clear()

    api_key = "test_proactive_recent_chat_key"
    user_id = "usr_endpoint_proactive_recent_chat"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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
        "ts": appmod.time.time(),
        "encrypted": True,
    })

    client = appmod.app.test_client()
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
    appmod._stores.clear()

    api_key = "test_proactive_auto_no_model_key"
    user_id = "usr_endpoint_proactive_auto_no_model"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.frames_meta.append({
        "id": "efef1234efef1234",
        "filename": "efef1234efef1234.env.json",
        "ts": appmod.time.time(),
        "encrypted": True,
    })

    client = appmod.app.test_client()
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
    appmod._stores.clear()

    api_key = "test_proactive_auto_false_key"
    user_id = "usr_endpoint_proactive_auto_false"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    client = appmod.app.test_client()
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
    appmod._stores.clear()

    api_key = "test_proactive_auto_schedule_key"
    user_id = "usr_endpoint_proactive_auto_schedule"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    client = appmod.app.test_client()
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

    store = appmod.get_store(user_id)
    store.frames_meta.append({
        "id": "frameon123456789",
        "filename": "frameon123456789.env.json",
        "ts": appmod.time.time(),
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
    assert on_body["decision"]["wake_kind"] == "screen"
    assert on_body["decision"]["screen_context_available"] is True
    assert on_body["job"]["frame_ids"] == ["frameon123456789"]


def test_auto_proactive_v2_away_suppresses_automatic_but_manual_bypasses(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_proactive_away_key"
    user_id = "usr_endpoint_proactive_away"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    client = appmod.app.test_client()
    headers = {"X-API-Key": api_key}
    client.post("/v1/proactive/state", headers=headers, json={"user_state": "away"})

    auto = client.post("/v1/proactive/tick", headers=headers, json={})
    assert auto.status_code == 200
    auto_body = auto.get_json()
    assert auto_body["enqueued"] is False
    assert auto_body["decision"]["should_wake_agent"] is False
    assert auto_body["decision"]["reason"] == "user_away"

    manual = client.post("/v1/proactive/tick", headers=headers, json={"manual": True})
    assert manual.status_code == 200
    manual_body = manual.get_json()
    assert manual_body["enqueued"] is True
    assert manual_body["decision"]["manual"] is True
    assert manual_body["decision"]["reason"] == "wake_created"


def test_gate_review_endpoint_records_human_label(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_gate_review_key"
    user_id = "usr_gate_review"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)

    decision = appmod._build_proactive_v2_wake_decision(
        store,
        {
            "force": True,
            "context_hint": "The user is testing the review harness.",
            "intent_label": "manual_proactive_test",
        },
    )
    store.append_gate_decision(decision)

    client = appmod.app.test_client()
    resp = client.post(
        f"/v1/proactive/decisions/{decision['decision_id']}/review",
        headers={"X-API-Key": api_key},
        json={"label": "great_companion_moment", "notes": "felt natural"},
    )

    assert resp.status_code == 200
    review = resp.get_json()["review"]
    assert review["label"] == "great_companion_moment"
    assert review["decision_id"] == decision["decision_id"]

    snapshot = appmod._proactive_debug_snapshot(store)
    latest = snapshot["latest_review_by_decision"][decision["decision_id"]]
    assert latest["notes"] == "felt natural"

    listing = client.get("/v1/proactive/reviews?since=0", headers={"X-API-Key": api_key})
    assert listing.status_code == 200
    assert listing.get_json()["reviews"][0]["label"] == "great_companion_moment"


def test_proactive_job_claim_and_status_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_proactive_claim_key"
    user_id = "usr_endpoint_proactive_claim"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)

    decision = appmod._build_proactive_v2_wake_decision(
        store,
        {"force": True, "context_hint": "claim test"},
    )
    job = store.append_proactive_job(appmod._proactive_job_from_decision(decision))

    client = appmod.app.test_client()
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

    snapshot = appmod._proactive_debug_snapshot(store)
    row = snapshot["jobs"][0]
    assert row["derived_status"] == "failed"
    assert row["status_reason"] == "agent_call_failed"
    assert row["consumer_id"] == "consumer-a"


def test_proactive_chat_response_records_push_delivery_results(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    appmod._stores.clear()

    sent_push_types = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        sent_push_types.append(push_type)
        return {"status": "delivered"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    api_key = "test_proactive_delivery_key"
    user_id = "usr_endpoint_proactive_delivery"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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

    client = appmod.app.test_client()
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
            "source": appmod.PROACTIVE_JOB_SOURCE,
            "gate_decision_id": "gd_delivery",
            "proactive_job_id": "pj_delivery",
            "alert_body": "我看到你停在这里了。",
            "push_live_activity": True,
            "push_body": "我看到你停在这里了。",
        },
    )

    assert resp.status_code == 200
    assert sent_push_types == ["liveactivity", "alert"]
    snapshot = appmod._proactive_debug_snapshot(store)
    msg = snapshot["proactive_messages"][0]
    assert msg["alert_preview"] == "我看到你停在这里了。"
    assert msg["alert_status"] == "delivered"
    assert msg["live_activity_status"] == "delivered"


def test_ai_chat_response_pushes_when_app_background(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    appmod._stores.clear()

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
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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
    store.append_device_event(appmod._make_device_event("ios", "app_presence", {
        "scene_phase": "background",
        "is_foreground": False,
        "selected_tab": "chat",
        "is_chat_visible": False,
    }))

    resp = appmod.app.test_client().post(
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
    appmod._stores.clear()

    sent = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        sent.append(push_type)
        return {"status": "delivered"}

    monkeypatch.setattr(push_apns, "_send_apns", _fake_send_apns)

    api_key = "test_ai_push_foreground_key"
    user_id = "usr_ai_push_foreground"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.tokens = [
        {"type": "live_activity", "token": "live-token", "activity_id": "activity_1", "status": "active"},
        {"type": "device", "token": "device-token", "status": "active"},
    ]
    store.append_device_event(appmod._make_device_event("ios", "app_presence", {
        "scene_phase": "active",
        "is_foreground": True,
        "selected_tab": "chat",
        "is_chat_visible": True,
    }))

    resp = appmod.app.test_client().post(
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
    appmod._stores.clear()

    api_key = "test_chat_history_lightweight_key"
    user_id = "usr_chat_history_lightweight"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)

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
        appmod.db.chat_append(user_id, msg["id"], msg["ts"], msg, appmod.MAX_CHAT_MESSAGES)
        return msg

    for i in range(1, 6):
        _append(i)
    _append(6, content_type="image", body_ct="x" * 4096)
    _append(7, body_ct="y" * 128)
    for i in range(8, 11):
        _append(i)

    with appmod.app.test_client() as client:
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
    appmod._stores.clear()

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
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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

    client = appmod.app.test_client()
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
            "source": appmod.PROACTIVE_JOB_SOURCE,
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

    snapshot = appmod._proactive_debug_snapshot(store)
    msg = snapshot["proactive_messages"][0]
    assert msg["live_activity_status"] == "delivered"
    assert msg["live_activity_mode"] == "start"
    assert store.live_activity_start_cooldown_remaining_seconds() > 0


def test_proactive_chat_response_uses_update_during_start_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    appmod._stores.clear()

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
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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
    store.live_activity_state["last_start_epoch"] = appmod.time.time()

    client = appmod.app.test_client()
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
            "source": appmod.PROACTIVE_JOB_SOURCE,
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

    snapshot = appmod._proactive_debug_snapshot(store)
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

    monkeypatch.setattr(appmod.httpx, "Client", _Client)

    result = appmod._send_apns(
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

    monkeypatch.setattr(appmod.httpx, "Client", _Client)

    result = appmod._send_apns(
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
    appmod._stores.clear()

    api_key = "test_bad_device_token_key"
    user_id = "usr_bad_device_token"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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

    result = appmod._send_chat_alert(store, "hello", alert_title="Dora")

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
    appmod._stores.clear()

    api_key = "test_bad_live_activity_token_key"
    user_id = "usr_bad_live_activity_token"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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

    with appmod.app.test_client() as client:
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
    appmod._stores.clear()

    api_key = "test_bad_live_activity_env_key"
    user_id = "usr_bad_live_activity_env"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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

    with appmod.app.test_client() as client:
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
    appmod._stores.clear()

    api_key = "test_refresh_bad_live_activity_key"
    user_id = "usr_refresh_bad_live"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
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

    with appmod.app.test_client() as client:
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
    appmod._stores.clear()

    api_key = "test_token_metadata_key"
    user_id = "usr_token_metadata"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    with appmod.app.test_client() as client:
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
    store = appmod.get_store(user_id)
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

    monkeypatch.setattr(appmod.httpx, "Client", _Client)

    result = appmod._send_apns(
        "testflight-token",
        {"aps": {"alert": {"body": "hi"}}},
        push_type="alert",
        topic="com.feedling.mcp",
        preferred_env="production",
    )

    assert result["status"] == "delivered"
    assert result["apns_env"] == "production"
    assert [("sandbox" in url) for url in calls] == [False]
