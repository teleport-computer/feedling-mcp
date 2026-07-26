from __future__ import annotations

import base64
from datetime import datetime, timedelta
import itertools
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from memory import service as memory_service  # noqa: E402
from proactive import service as proactive_service  # noqa: E402
from tracking import tracking_core  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "admin-test-token")
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    # Distinct public_key per call: the register endpoint now refuses duplicate
    # content keys (orphan backstop), and these tests need many distinct users.
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "admin-test-token"}


def _env(msg_id: str, user_id: str) -> dict:
    return {
        "id": msg_id,
        "v": 1,
        "body_ct": "ciphertext-that-must-not-leak",
        "nonce": "nonce-that-must-not-leak",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _append_chat_at(user_id: str, msg_id: str, role: str, source: str, ts: float) -> None:
    doc = {
        **_env(msg_id, user_id),
        "role": role,
        "source": source,
        "ts": ts,
    }
    db.chat_append(user_id, msg_id, ts, doc, core_store.MAX_CHAT_MESSAGES)


def test_track_event_scrubs_sensitive_payload(client):
    user_id, api_key = _register(client)

    res = client.post(
        "/v1/track/event",
        headers=_headers(api_key),
        json={
            "type": "onboarding_skill_copied",
            "route": "resident",
            "app_version": "1.0",
            "build": "42",
            "payload": {
                "screen": "chat_empty",
                "characters": 123,
                "prompt": "private prompt",
                "api_key": "sk-private",
                "file_name": "private.txt",
                "nested": {"step": "skill", "token": "private-token"},
            },
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    events = core_store.get_store(user_id).list_tracking_events(limit=0)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["screen"] == "chat_empty"
    assert payload["characters"] == 123
    assert payload["nested"] == {"step": "skill"}
    assert "prompt" not in payload
    assert "api_key" not in payload
    assert "file_name" not in payload
    assert "private" not in json.dumps(events[0])


def test_admin_data_track_requires_admin_token(client, monkeypatch):
    _register(client)

    no_token = client.get("/v1/admin/data-track/users")
    assert no_token.status_code == 401

    good = client.get("/v1/admin/data-track/users", headers=_admin_headers())
    assert good.status_code == 200

    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN")
    disabled = client.get("/v1/admin/data-track/users", headers=_admin_headers())
    assert disabled.status_code == 503


def test_admin_data_track_aggregates_counts_without_content(client):
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    store.append_chat("user", "chat", _env("msg_user_1", user_id))
    store.append_chat("openclaw", "chat", _env("msg_agent_1", user_id))
    store.append_chat(
        "openclaw",
        proactive_service.PROACTIVE_JOB_SOURCE,
        _env("msg_proactive_1", user_id),
        extra={
            "proactive_job_id": "pj_1",
            "live_activity_status": "delivered",
            "alert_status": "delivered",
            "alert_preview": "private alert preview",
        },
    )
    memory_service._save_moments(
        store,
        [
            {"id": "mem_1", "type": "moment", "source": "bootstrap", "created_at": "2026-06-01T01:00:00"},
            {"id": "mem_2", "type": "fact", "source": "chat", "created_at": "2026-06-01T02:00:00"},
        ],
    )
    db.set_blob(store.user_id, "identity", {
        "updated_at": "2026-06-01T03:00:00",
        "relationship_started_at": "2026-06-01",
        "relationship_anchor_evidence": "private evidence",
    })
    store.append_tracking_event(tracking_core._make_tracking_event(
        store,
        "onboarding_connection_copied",
        {"payload": {"screen": "chat_empty", "prompt": "private copied prompt"}},
    ))
    store.append_proactive_job({
        "job_id": "pj_failed_timeout",
        "status": "failed",
        "status_reason": "model_timeout",
        "job_kind": "presence",
    })
    store.append_proactive_job({
        "job_id": "pj_failed_unknown",
        "status": "failed",
        "job_kind": "scheduled_wake",
    })

    res = client.get("/v1/admin/data-track/users", headers=_admin_headers())

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["summary"]["users_total"] == 1
    assert body["summary"]["chat_messages_total"] == 3
    assert body["summary"]["memory_total"] == 2
    row = body["users"][0]
    assert row["chat"]["total"] == 3
    assert row["chat"]["user_messages"] == 1
    assert row["chat"]["agent_messages"] == 2
    assert row["memory"]["by_tab"]["story"] == 1
    assert row["memory"]["by_tab"]["about_me"] == 1
    assert row["proactive"]["proactive_messages"] == 1
    assert row["proactive"]["job_failed_reasons"] == {"model_timeout": 1, "unknown": 1}
    dumped = json.dumps(body)
    assert "ciphertext-that-must-not-leak" not in dumped
    assert "private alert preview" not in dumped
    assert "private copied prompt" not in dumped
    assert "private evidence" not in dumped


def test_admin_data_track_surfaces_provider_health(client):
    user_id, _ = _register(client)
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO provider_health (
              user_id, provider_state, last_provider_success_at,
              last_provider_failure_at, last_provider_error_class,
              last_provider_error_blame, last_probe_at
            )
            VALUES (%s, 'needs_user_action', now() - interval '72 hours',
                    now() - interval '1 hour', 'auth_invalid',
                    'user_provider', now())
            """,
            (user_id,),
        )

    body = client.get(
        "/v1/admin/data-track/users",
        headers=_admin_headers(),
    ).get_json()

    assert body["summary"]["provider_needs_user_action"] == 1
    row = body["users"][0]
    assert row["provider_state"] == "needs_user_action"
    assert row["last_provider_success_at"]
    assert row["last_provider_failure_at"]
    assert row["last_provider_error_class"] == "auth_invalid"


def test_admin_data_track_dau_counts_user_activity_by_beijing_day(client):
    user_a, _ = _register(client)
    user_b, _ = _register(client)
    user_c, _ = _register(client)

    day2_chat_ts = _epoch("2030-06-01T17:30:00Z")  # 2030-06-02 01:30 Beijing
    day2_tracking_ts = _epoch("2030-06-01T18:00:00Z")
    day3_chat_ts = _epoch("2030-06-02T16:30:00Z")  # 2030-06-03 00:30 Beijing

    _append_chat_at(user_a, "dau_user_chat", "user", "chat", day2_chat_ts)
    _append_chat_at(user_a, "dau_agent_reply", "openclaw", "chat", day2_chat_ts + 1)
    _append_chat_at(user_c, "dau_verify_ping", "user", "verify_ping", day2_chat_ts + 2)
    _append_chat_at(user_b, "dau_next_day_chat", "user", "chat", day3_chat_ts)

    db.log_append(
        user_a,
        "tracking_events",
        {"event_id": "trk_day2_a", "type": "app_open", "ts": day2_tracking_ts},
        ts=day2_tracking_ts,
    )
    db.log_append(
        user_b,
        "tracking_events",
        {"event_id": "trk_day2_b", "type": "onboarding_view", "ts": day2_tracking_ts + 10},
        ts=day2_tracking_ts + 10,
    )

    res = client.get(
        "/v1/admin/data-track/dau?since=2030-06-01T17:00:00Z&days=10",
        headers=_admin_headers(),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    by_day = {row["day"]: row for row in body["rows"]}
    assert body["definition"]["timezone"] == "Asia/Shanghai"
    assert body["summary"]["snapshot_first_day"] == ""
    assert body["summary"]["snapshot_last_day"] == ""
    assert body["summary"]["snapshot_days"] == 0
    assert all(row["frozen"] is False for row in body["rows"])
    assert by_day["2030-06-03"]["dau"] == 1
    assert by_day["2030-06-03"]["chat_dau"] == 1
    assert by_day["2030-06-03"]["tracking_dau"] == 0
    assert by_day["2030-06-02"]["dau"] == 2
    assert by_day["2030-06-02"]["chat_dau"] == 1
    assert by_day["2030-06-02"]["tracking_dau"] == 2
    assert by_day["2030-06-02"]["user_messages"] == 1
    assert by_day["2030-06-02"]["tracking_events"] == 2
    assert by_day["2030-06-02"]["active_events"] == 3

    page = client.get(
        "/admin/data-track?view=dau&since=2030-06-01T17:00:00Z&days=10",
        headers=_admin_headers(),
    )
    assert page.status_code == 200, page.get_data(as_text=True)
    html = page.get_data(as_text=True)
    assert "Daily Active Users" in html
    assert "Chat DAU" in html
    assert "2030-06-02" in html


def test_admin_data_track_dau_histogram_json_and_page(client):
    users = [_register(client)[0] for _ in range(3)]
    event_ts = _epoch("2030-06-01T18:00:00Z")  # 2030-06-02 Beijing
    for uid, duration in zip(users, (59, 60, 3600)):
        db.log_append(
            uid,
            "tracking_events",
            {"type": "app_session_end", "payload": {"duration_sec": duration}},
            ts=event_ts,
        )

    res = client.get(
        "/v1/admin/data-track/dau?day=2030-06-02&days=10",
        headers=_admin_headers(),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    histogram = body["usage_histogram"]
    assert body["filters"]["day"] == "2030-06-02"
    assert histogram["day"] == "2030-06-02"
    assert histogram["total_users"] == 3
    assert [bucket["users"] for bucket in histogram["buckets"]] == [
        1, 1, 0, 0, 0, 1, 0, 0,
    ]
    assert histogram["median_sec"] == 60.0

    page = client.get(
        "/admin/data-track?view=dau&day=2030-06-02&days=10",
        headers=_admin_headers(),
    )
    assert page.status_code == 200, page.get_data(as_text=True)
    page_html = page.get_data(as_text=True)
    assert "使用时长分布 · 2030-06-02" in page_html
    assert "样本 3 人" in page_html
    assert "样本=当天有上报的 3 位用户" in page_html


def test_user_detail_daily_usage_json_page_and_events_limit(client):
    user_id, _ = _register(client)
    zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).date()
    first_day = today - timedelta(days=2)
    first_midnight = datetime.combine(
        first_day, datetime.min.time(), tzinfo=zone
    ).timestamp()
    today_midnight = datetime.combine(
        today, datetime.min.time(), tzinfo=zone
    ).timestamp()
    for index, (ts, duration) in enumerate((
        (first_midnight, 40),
        (today_midnight, 80),
        (today_midnight + 1, "bad"),
        (today_midnight + 2, 20),
        (today_midnight + 3, 30),
    )):
        db.log_append(
            user_id,
            "tracking_events",
            {
                "event_id": f"daily_{index}",
                "type": "app_session_end",
                "ts": ts,
                "payload": {"duration_sec": duration},
            },
            ts=ts,
        )

    res = client.get(
        f"/v1/admin/data-track/users/{user_id}?days=3&events_limit=2",
        headers=_admin_headers(),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    user = res.get_json()["user"]
    assert user["daily_usage_days"] == 3
    assert len(user["daily_usage"]) == 3
    assert user["daily_usage"][0]["foreground_sec"] == 40
    assert user["daily_usage"][1]["foreground_sec"] == 0
    assert user["daily_usage"][1]["sessions"] == 0
    assert user["daily_usage"][2]["foreground_sec"] == 130
    assert user["daily_usage"][2]["sessions"] == 4
    assert sum(row["foreground_sec"] for row in user["daily_usage"]) == user["app_usage"]["foreground_sec"] == 170
    assert sum(row["sessions"] for row in user["daily_usage"]) == user["app_usage"]["sessions"] == 5
    assert user["tracking"]["events_limit"] == 2
    assert len(user["tracking"]["latest"]) == 2

    capped = client.get(
        f"/v1/admin/data-track/users/{user_id}?days=999&events_limit=999",
        headers=_admin_headers(),
    ).get_json()["user"]
    assert capped["daily_usage_days"] == 90
    assert len(capped["daily_usage"]) == 90
    assert capped["tracking"]["events_limit"] == 500
    assert len(capped["tracking"]["latest"]) == 5

    page = client.get(
        f"/admin/data-track/users/{user_id}?days=3&events_limit=2",
        headers=_admin_headers(),
    )
    assert page.status_code == 200, page.get_data(as_text=True)
    body = page.get_data(as_text=True)
    assert "最近 3 天使用时长" in body
    assert "窗口合计" in body and "全时段合计" in body
    assert "未打开" in body
    assert first_day.isoformat() in body
    assert (first_day + timedelta(days=1)).isoformat() in body


def test_uid_lookup_form_strip_validation_and_admin_key_passthrough(client):
    user_id, _ = _register(client)
    page = client.get(
        "/admin/data-track?admin_key=admin-test-token",
        headers=_admin_headers(),
    )
    body = page.get_data(as_text=True)
    assert 'action="/admin/data-track/users"' in body
    assert 'name="uid"' in body
    assert 'name="admin_key" type="hidden" value="admin-test-token"' in body

    lookup = client.get(
        f"/admin/data-track/users?uid=%20%0A{user_id}%09&admin_key=admin-test-token",
        headers=_admin_headers(),
    )
    assert lookup.status_code == 303
    assert lookup.headers["location"] == (
        f"/admin/data-track/users/{user_id}?admin_key=admin-test-token"
    )

    invalid_json = client.get(
        "/v1/admin/data-track/users/not-a-user",
        headers=_admin_headers(),
    )
    assert invalid_json.status_code == 400
    assert invalid_json.get_json() == {"error": "invalid_user_id"}

    invalid_page = client.get(
        "/admin/data-track/users?uid=%3Cscript%3E",
        headers=_admin_headers(),
    )
    assert invalid_page.status_code == 400
    invalid_body = invalid_page.get_data(as_text=True)
    assert "UID 格式不正确" in invalid_body
    assert "<script>" not in invalid_body
    assert "&lt;script&gt;" in invalid_body

    invalid_path = client.get(
        f"/admin/data-track/users/{quote('<script>', safe='')}",
        headers=_admin_headers(),
    )
    assert invalid_path.status_code == 400
    assert "UID 格式不正确" in invalid_path.get_data(as_text=True)


def test_tracking_stats_events_limit_clamps_to_500():
    events = [
        {"event_id": str(index), "type": "app_open", "ts": float(index)}
        for index in range(600)
    ]
    store = SimpleNamespace(list_tracking_events=lambda limit=0: events)

    short = _dt._tracking_stats(store, include_events=True, events_limit=3)
    assert short["events_limit"] == 3
    assert [row["event_id"] for row in short["latest"]] == ["599", "598", "597"]

    capped = _dt._tracking_stats(store, include_events=True, events_limit=999)
    assert capped["events_limit"] == 500
    assert len(capped["latest"]) == 500


def test_admin_data_track_supports_since_filter_and_pagination(client):
    old_user, _ = _register(client)
    new_user, _ = _register(client)

    with registry._users_lock:
        for entry in registry._users:
            if entry["user_id"] == old_user:
                entry["created_at"] = "2026-06-01T17:00:00+00:00"
            elif entry["user_id"] == new_user:
                entry["created_at"] = "2026-06-01T19:00:00+00:00"
        registry._save_users()

    summary = client.get(
        "/v1/admin/data-track/summary?since=2026-06-01T18:00:00Z",
        headers=_admin_headers(),
    )
    assert summary.status_code == 200, summary.get_data(as_text=True)
    summary_body = summary.get_json()
    assert summary_body["summary"]["users_total"] == 1
    assert "users" not in summary_body

    users = client.get(
        "/v1/admin/data-track/users?since=2026-06-01T18:00:00Z&limit=1",
        headers=_admin_headers(),
    )
    assert users.status_code == 200, users.get_data(as_text=True)
    body = users.get_json()
    assert body["pagination"] == {
        "limit": 1,
        "offset": 0,
        "returned": 1,
        "total": 1,
        "next_offset": None,
        "prev_offset": None,
    }
    assert [row["user_id"] for row in body["users"]] == [new_user]


def test_admin_data_track_sorts_before_pagination(client):
    low_chat_high_memory, _ = _register(client)
    mid_chat_mid_memory, _ = _register(client)
    high_chat_low_memory, _ = _register(client)

    def add_chat(user_id: str, *, regular: int, proactive: int) -> None:
        store = core_store.get_store(user_id)
        for idx in range(regular):
            role = "user" if idx % 2 == 0 else "openclaw"
            store.append_chat(role, "chat", _env(f"{user_id}_chat_{idx}", user_id))
        for idx in range(proactive):
            store.append_chat(
                "openclaw",
                proactive_service.PROACTIVE_JOB_SOURCE,
                _env(f"{user_id}_proactive_{idx}", user_id),
            )

    def add_memories(user_id: str, count: int) -> None:
        store = core_store.get_store(user_id)
        memory_service._save_moments(
            store,
            [
                {
                    "id": f"{user_id}_mem_{idx}",
                    "type": "moment" if idx % 2 == 0 else "fact",
                    "source": "test",
                    "created_at": f"2026-06-01T00:{idx:02d}:00",
                }
                for idx in range(count)
            ],
        )

    add_chat(low_chat_high_memory, regular=1, proactive=0)
    add_memories(low_chat_high_memory, 5)
    add_chat(mid_chat_mid_memory, regular=2, proactive=1)
    add_memories(mid_chat_mid_memory, 3)
    add_chat(high_chat_low_memory, regular=3, proactive=2)
    add_memories(high_chat_low_memory, 1)

    def sorted_ids(sort: str, direction: str) -> list[str]:
        res = client.get(
            f"/v1/admin/data-track/users?sort={sort}&dir={direction}&limit=10",
            headers=_admin_headers(),
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        return [row["user_id"] for row in res.get_json()["users"]]

    assert sorted_ids("chat", "desc") == [
        high_chat_low_memory,
        mid_chat_mid_memory,
        low_chat_high_memory,
    ]
    assert sorted_ids("chat", "asc") == [
        low_chat_high_memory,
        mid_chat_mid_memory,
        high_chat_low_memory,
    ]
    assert sorted_ids("memory", "desc") == [
        low_chat_high_memory,
        mid_chat_mid_memory,
        high_chat_low_memory,
    ]
    assert sorted_ids("proactive", "desc") == [
        high_chat_low_memory,
        mid_chat_mid_memory,
        low_chat_high_memory,
    ]

    page = client.get("/admin/data-track?sort=chat&dir=desc", headers=_admin_headers())
    assert page.status_code == 200, page.get_data(as_text=True)
    html = page.get_data(as_text=True)
    assert "Chat desc" in html
    assert "DAU" in html
    assert "Memory asc" in html
    assert "Proactive desc" in html


# --- 2026-07 data-track redo: genesis-aware stage + activation funnel ---------
# Regression guards for the fix that stopped counting genesis (bucket-based)
# users as stuck at memory_garden. Pure-function tests on _data_track_fast_validation.
from admin import data_track as _dt  # noqa: E402


def _genesis_model_api_memory():
    # Genesis writes by bucket, so the retired by_tab counters are all zero even
    # though the garden has cards. This is exactly the shape that used to break.
    return {"total": 7, "by_tab": {"story": 0, "about_me": 0, "ta_thinking": 0, "unknown": 7},
            "by_source": {"genesis_import": 7}}


def test_fast_validation_genesis_user_is_complete_despite_empty_tabs():
    v = _dt._data_track_fast_validation(
        route="model_api",
        chat={"model_api_greetings": 1, "model_api_user_messages": 2, "model_api_agent_messages": 2,
              "user_messages": 2, "agent_messages": 2},
        memory=_genesis_model_api_memory(),
        identity={"relationship_started_at": "2026-06-25", "relationship_anchor_evidence": "x",
                  "relationship_anchor_source": "genesis_import", "updated_at": "2026-06-30"},
        history_import={"has_job": True, "status": "completed", "chat_ready": True},
        model_api_config={"test_status": "ok"},
        consumer_state=None,
        bootstrap_events={"by_type": {}},
    )
    assert v["passing"] is True
    assert v["stage"] == "complete"
    mg = next(s for s in v["steps"] if s["id"] == "memory")
    assert mg["passing"] is True  # cards exist -> garden satisfied (bucket-agnostic)


# App usage-duration rendering (app_session_end aggregation surfaced in the overview).
def test_fmt_duration_sec_compact_human_readable():
    assert _dt._fmt_duration_sec(0) == "0s"
    assert _dt._fmt_duration_sec(45) == "45s"
    assert _dt._fmt_duration_sec(137) == "2m17s"
    assert _dt._fmt_duration_sec(120) == "2m"
    assert _dt._fmt_duration_sec(5400) == "1h30m"
    assert _dt._fmt_duration_sec(3600) == "1h"
    assert _dt._fmt_duration_sec(None) == "—"
    assert _dt._fmt_duration_sec("nope") == "—"
    # rounds and never negative
    assert _dt._fmt_duration_sec(59.6) == "1m"


def test_beijing_time_display_helpers():
    # Display-only Beijing (UTC+8) conversion; storage stays UTC. Inputs are an
    # epoch or an explicit-UTC value so the assertion is host-timezone-independent.
    import calendar
    utc_midnight = calendar.timegm((2026, 7, 13, 0, 0, 0, 0, 0, 0))  # 2026-07-13 00:00:00 UTC
    # epoch -> Beijing wall clock is 08:00 the same day
    assert _dt._debug_time(utc_midnight) == "07-13 08:00:00"
    assert _dt._bj_iso(utc_midnight) == "2026-07-13 08:00:00"
    # a stored (naive) UTC ISO string is treated as UTC, then shifted +8
    assert _dt._bj_iso("2026-07-13T00:00:00") == "2026-07-13 08:00:00"
    assert _dt._bj_iso("2026-07-12T20:30:00") == "2026-07-13 04:30:00"  # crosses the day
    # empties stay empty; zero epoch is the "no time" sentinel
    assert _dt._bj_iso("") == ""
    assert _dt._bj_iso(None) == ""
    assert _dt._debug_time(0) == "—"
    # fail-soft: a wildly out-of-range epoch must not raise (would 500 the page)
    assert isinstance(_dt._bj_iso(10 ** 30), str)
    assert _dt._debug_time(10 ** 30) == "—"


def test_dau_page_marks_frozen_vs_live_and_cutover_note():
    # Each day shows 🔒已冻结 (snapshot, immutable) or ⏱实时 (live, can shrink on
    # deletion); the history note names the snapshot cutover day.
    summary = {
        "latest_dau": 2, "latest_day": "2026-07-14", "max_dau": 5, "avg_dau": 3.5,
        "user_messages": 10, "tracking_events": 20, "days_returned": 2,
        "timezone": "Asia/Shanghai", "generated_at": "2026-07-14T00:00:00",
        "snapshot_first_day": "2026-07-13", "snapshot_last_day": "2026-07-13", "snapshot_days": 1,
    }
    rows = [
        {"day": "2026-07-14", "frozen": False, "dau": 2, "chat_dau": 1, "tracking_dau": 2,
         "active_events": 3, "user_messages": 4, "tracking_events": 5, "session_dau": 1,
         "avg_session_sec": 60, "session_count": 3, "last_at": "2026-07-14T00:00:00"},
        {"day": "2026-07-13", "frozen": True, "dau": 5, "chat_dau": 3, "tracking_dau": 5,
         "active_events": 9, "user_messages": 6, "tracking_events": 7, "session_dau": 2,
         "avg_session_sec": 90, "session_count": 8, "last_at": "2026-07-13T10:00:00"},
    ]
    out = _dt._render_data_track_dau_page(
        {"summary": summary, "filters": {}, "definition": {"dau": "", "excluded": ""}, "rows": rows}
    )
    assert "🔒 已冻结" in out          # the frozen day
    assert "⏱ 实时" in out            # today (live)
    assert "首个冻结日是 <b>2026-07-13</b>" in out  # cutover named in the note
    assert "<b>今天</b>仍是实时数据" in out
    assert "<th>状态</th>" in out       # status column present


def test_dau_page_renders_usage_histogram_with_day_links_and_sample_copy():
    summary = {
        "latest_dau": 5, "latest_day": "2026-07-24", "max_dau": 5, "avg_dau": 4.0,
        "user_messages": 10, "tracking_events": 20, "days_returned": 2,
        "timezone": "Asia/Shanghai", "generated_at": "2026-07-25T00:00:00",
        "snapshot_first_day": "", "snapshot_last_day": "", "snapshot_days": 0,
    }
    rows = [
        {"day": "2026-07-25", "frozen": False},
        {"day": "2026-07-24", "frozen": True},
    ]
    histogram = {
        "day": "2026-07-24",
        "buckets": [
            {"label": "0-1min", "lo_sec": 0, "hi_sec": 60, "users": 2},
            {"label": "1-5min", "lo_sec": 60, "hi_sec": 300, "users": 3},
        ],
        "total_users": 5,
        "median_sec": 120,
        "mean_sec": 180,
        "p90_sec": 300,
        "max_sec": 600,
    }
    out = _dt._render_data_track_dau_page({
        "summary": summary,
        "filters": {"day": "2026-07-24"},
        "definition": {"dau": "", "excluded": ""},
        "rows": rows,
        "usage_histogram": histogram,
    })

    assert "使用时长分布 · 2026-07-24" in out
    assert "width:66.67%" in out
    assert "width:100.00%" in out
    assert "2 人 · 40.0%" in out
    assert "样本 5 人 · 中位数 2m · 均值 3m · P90 5m · 最大值 10m" in out
    assert "day=2026-07-25" in out
    assert "day=2026-07-24" in out
    assert "样本=当天有上报的 5 位用户" in out
    assert "没有 app_session_end 上报的用户不计入" in out


def test_bj_deep_converts_only_iso_datetime_strings():
    # The user-detail <pre> JSON clone shows every timestamp in Beijing; non-time
    # strings and other types are untouched. Display-only — JSON API stays UTC.
    src = {
        "user_id": "usr_x",
        "registered_at": "2026-07-13T00:00:00",          # -> +8
        "route": "model_api",                             # not a time
        "nested": {"last_activity_at": "2026-07-12T20:30:00.500000"},  # crosses day
        "list": ["2026-07-13T00:00:00", "not-a-time", 42],
        "count": 7,
    }
    out = _dt._bj_deep(src)
    assert out["registered_at"] == "2026-07-13 08:00:00"
    assert out["route"] == "model_api"
    assert out["nested"]["last_activity_at"] == "2026-07-13 04:30:00"
    assert out["list"][0] == "2026-07-13 08:00:00"
    assert out["list"][1] == "not-a-time"
    assert out["list"][2] == 42
    assert out["count"] == 7
    # source dict is not mutated (clone semantics)
    assert src["registered_at"] == "2026-07-13T00:00:00"


def _post_session_end(client, api_key: str, duration_sec: int) -> None:
    res = client.post(
        "/v1/track/event",
        headers=_headers(api_key),
        json={
            "type": "app_session_end",
            "source": "ios",
            "platform": "ios",
            "route": "model_api",
            "app_version": "1.4.0",
            "build": "312",
            "payload": {"duration_sec": duration_sec},
        },
    )
    assert res.status_code == 200, res.get_data(as_text=True)


def test_admin_data_track_app_usage_rollup(client):
    # No app_session_end events yet -> app_usage present but zeroed.
    u1, k1 = _register(client)
    empty = client.get("/v1/admin/data-track/users", headers=_admin_headers()).get_json()
    au0 = empty["summary"]["app_usage"]
    assert au0 == {
        "foreground_sec_total": 0, "sessions_total": 0,
        "avg_session_sec": 0, "users_active": 0, "dau_today": 0,
    }

    # Two sessions for u1 (137 + 63 = 200s), one for u2 (100s).
    _post_session_end(client, k1, 137)
    _post_session_end(client, k1, 63)
    u2, k2 = _register(client)
    _post_session_end(client, k2, 100)

    body = client.get("/v1/admin/data-track/users", headers=_admin_headers()).get_json()
    au = body["summary"]["app_usage"]
    assert au["foreground_sec_total"] == 300
    assert au["sessions_total"] == 3
    assert au["avg_session_sec"] == 100
    assert au["users_active"] == 2        # both users had >=1 session
    assert au["dau_today"] == 2           # events ingested "now" -> today in Shanghai

    # Per-user app_usage contract: active users carry their totals; a fresh
    # user with no app_session_end events defaults to zeros.
    rows = {r["user_id"]: r for r in body["users"]}
    assert rows[u1]["app_usage"]["sessions"] == 2
    assert rows[u1]["app_usage"]["foreground_sec"] == 200
    assert rows[u1]["app_usage"]["last_at"]  # non-empty iso
    assert rows[u2]["app_usage"]["sessions"] == 1
    u_none, _ = _register(client)
    rows2 = {
        r["user_id"]: r
        for r in client.get("/v1/admin/data-track/users", headers=_admin_headers()).get_json()["users"]
    }
    assert rows2[u_none]["app_usage"] == {
        "foreground_sec": 0, "sessions": 0, "last_at_epoch": 0.0, "last_at": "",
    }


def test_admin_data_track_page_uses_plain_language(client, monkeypatch):
    # The overview HTML leads with 激活用户, de-emphasizes 注册, explains both,
    # and renders the App-usage section — no more "已激活 / 原始行" jargon.
    user_id, _ = _register(client)
    core_store.get_store(user_id).append_chat(
        "user", "chat", _env("msg_dashboard_active", user_id)
    )
    monkeypatch.setattr(
        _dt,
        "_runtime_token_usage_summary",
        lambda **_kw: {
            "window_days": 30,
            "sampled_turns": 7,
            "users": 1,
            "model_calls": 8,
            "usage_reported_calls": 6,
            "cache_reported_calls": 6,
            "usage_telemetry_coverage": 0.75,
            "cache_telemetry_coverage": 0.75,
            "prompt_tokens": 12_000,
            "completion_tokens": 345,
            "total_tokens": 12_345,
            "cache_read_tokens": 6_000,
            "cache_write_tokens": 1_000,
            "cache_miss_tokens": 4_000,
        },
    )
    page = client.get("/admin/data-track", headers=_admin_headers()).get_data(as_text=True)
    assert "激活用户（真正用起来的人）" in page
    assert "累计注册行（含重装孤儿·非人数）" in page
    assert "怎么读这些数" in page          # the explainer note-box
    assert "没有、也无法有「已删除账户数」" in page
    assert "App 使用时长" in page
    assert "运营 Telemetry" in page
    assert "全站 V2 token 总量（近 30 天）" in page
    assert "12,345" in page
    assert "自托管激活账号（当前筛选）" in page
    assert "可观测账号覆盖数" in page
    assert "已激活 / 原始行" not in page    # old jargon gone


def test_admin_data_track_app_usage_dau_is_shanghai_day(client, monkeypatch):
    # now = 2030-06-01T16:30Z == Asia/Shanghai 2030-06-02 00:30 (just past midnight).
    # A session at 16:10Z is Shanghai 06-02 00:10 (today); 15:50Z is 06-01 23:50
    # (yesterday) — dau_today must count only the Shanghai-today one.
    now = _epoch("2030-06-01T16:30:00Z")
    monkeypatch.setattr(_dt.time, "time", lambda: now)

    u_today, _ = _register(client)
    u_yesterday, _ = _register(client)
    for uid, ts_iso, dur in [
        (u_today, "2030-06-01T16:10:00Z", 40),
        (u_yesterday, "2030-06-01T15:50:00Z", 50),
    ]:
        ev = {"type": "app_session_end", "payload": {"duration_sec": dur}, "ts": _epoch(ts_iso)}
        db.log_append(uid, "tracking_events", ev, ts=_epoch(ts_iso))

    au = client.get("/v1/admin/data-track/users", headers=_admin_headers()).get_json()["summary"]["app_usage"]
    assert au["sessions_total"] == 2
    assert au["users_active"] == 2
    assert au["dau_today"] == 1  # only the Shanghai-today session, not the day-boundary one


def test_fast_validation_no_memories_still_blocks_memory_garden():
    v = _dt._data_track_fast_validation(
        route="model_api",
        chat={},
        memory={"total": 0, "by_tab": {}, "by_source": {}},
        identity=None,
        history_import={"has_job": True, "status": "processing", "chat_ready": False},
        model_api_config={"test_status": "ok"},
        consumer_state=None,
        bootstrap_events={"by_type": {}},
    )
    assert v["passing"] is False
    mg = next(s for s in v["steps"] if s["id"] == "memory")
    assert mg["passing"] is False  # genuinely empty garden must still flag


def test_detail_payload_runtime_includes_reasoning_effort(client):
    from admin import data_track as data_track

    user_id, _api_key = _register(client)
    # Config lives in the routes/credentials tables now (was a model_api blob).
    from conftest import configure_model_api_route
    configure_model_api_route(
        user_id, provider="openrouter", model="anthropic/claude-sonnet-4.6",
        reasoning_effort="medium", test_status="ok")
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    row = data_track._build_data_track_user(user_entry, include_detail=True)

    assert row["runtime"]["reasoning_effort"] == "medium"


def test_provider_attempts_detail_is_bounded_and_reports_more(monkeypatch):
    rows = [{"attempt_n": n} for n in range(1, 202)]
    calls = []

    def fake_log_read(user_id, stream, limit):
        calls.append((user_id, stream, limit))
        return rows

    monkeypatch.setattr(_dt.db, "log_read", fake_log_read)

    detail = _dt._provider_attempts_detail(SimpleNamespace(user_id="usr_ledger"))

    assert calls == [("usr_ledger", "provider_attempts", 201)]
    assert detail["coverage"] == "chat_turns_only"
    assert detail["has_more"] is True
    assert len(detail["attempts"]) == 200
    assert detail["attempts"][0]["attempt_n"] == 2
    assert detail["attempts"][-1]["attempt_n"] == 201


def test_perception_permissions_block_renders_granted_denied_and_switches():
    # The user-detail page shows a readable 感知授权 & 主动开关 block so "can't use
    # album/screen" is answerable on sight (granted vs not vs unknown).
    user = {
        "perception_permissions": {
            "permission_states": {"photos": "authorized", "screen": "denied", "location": "notDetermined"},
            "switches": {"photo_wake_照片唤醒": True, "screen_watch_屏幕观察": False},
            "wake_directive_configured": True,
            "wake_interval_sec": 7200,
        }
    }
    out = _dt._render_perception_permissions(user)
    assert "photos" in out and "已授权" in out          # granted
    assert "screen" in out and "未授权" in out           # denied
    assert "location" in out and "notDetermined" in out  # unknown -> raw state shown
    assert "photo_wake_照片唤醒" in out and "开" in out
    assert "screen_watch_屏幕观察" in out and "关" in out
    assert "自定义 wake 指令：已配置" in out
    assert "间隔：7200 秒" in out
    # empty permission_states -> explicit "no report" hint, not silence
    empty = _dt._render_perception_permissions({"perception_permissions": {"permission_states": {}, "switches": {}}})
    assert "permission_states 为空" in empty
    # no block at all when the user has no perception_permissions key
    assert _dt._render_perception_permissions({}) == ""


def test_detail_payload_exposes_permission_metadata_without_private_directive(client):
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({
        "permission_states": {"photos": "authorized", "<script>": "<private>"},
        "photo_wake_enabled": False,
        "wake_directive": "private user-authored instruction",
        "wake_interval_sec": 3600,
    })
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    row = _dt._build_data_track_user(user_entry, include_detail=True)
    pp = row["perception_permissions"]
    assert pp["permission_states"]["photos"] == "authorized"
    assert pp["switches"]["ambient_心跳"] is True
    assert "ambient_陪伴" not in pp["switches"]
    assert pp["switches"]["photo_wake_照片唤醒"] is False
    assert pp["wake_directive_configured"] is True
    assert pp["wake_interval_sec"] == 3600
    assert "private user-authored instruction" not in json.dumps(row)

    rendered = _dt._render_perception_permissions(row)
    assert "<script>" not in rendered
    assert "<private>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;private&gt;" in rendered
