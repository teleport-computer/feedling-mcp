from __future__ import annotations

import base64
from datetime import datetime
import itertools
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402
from core import config as core_config  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "admin-test-token")
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    appmod.app.config.update(TESTING=True)
    with appmod.app.test_client() as c:
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
    appmod.db.chat_append(user_id, msg_id, ts, doc, appmod.MAX_CHAT_MESSAGES)


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
    events = appmod.get_store(user_id).list_tracking_events(limit=0)
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
    store = appmod.get_store(user_id)

    store.append_chat("user", "chat", _env("msg_user_1", user_id))
    store.append_chat("openclaw", "chat", _env("msg_agent_1", user_id))
    store.append_chat(
        "openclaw",
        appmod.PROACTIVE_JOB_SOURCE,
        _env("msg_proactive_1", user_id),
        extra={
            "proactive_job_id": "pj_1",
            "live_activity_status": "delivered",
            "alert_status": "delivered",
            "alert_preview": "private alert preview",
        },
    )
    appmod._save_moments(
        store,
        [
            {"id": "mem_1", "type": "moment", "source": "bootstrap", "created_at": "2026-06-01T01:00:00"},
            {"id": "mem_2", "type": "fact", "source": "chat", "created_at": "2026-06-01T02:00:00"},
        ],
    )
    appmod.db.set_blob(store.user_id, "identity", {
        "updated_at": "2026-06-01T03:00:00",
        "relationship_started_at": "2026-06-01",
        "relationship_anchor_evidence": "private evidence",
    })
    store.append_tracking_event(appmod._make_tracking_event(
        store,
        "onboarding_connection_copied",
        {"payload": {"screen": "chat_empty", "prompt": "private copied prompt"}},
    ))

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
    dumped = json.dumps(body)
    assert "ciphertext-that-must-not-leak" not in dumped
    assert "private alert preview" not in dumped
    assert "private copied prompt" not in dumped
    assert "private evidence" not in dumped


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

    appmod.db.log_append(
        user_a,
        "tracking_events",
        {"event_id": "trk_day2_a", "type": "app_open", "ts": day2_tracking_ts},
        ts=day2_tracking_ts,
    )
    appmod.db.log_append(
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


def test_admin_data_track_supports_since_filter_and_pagination(client):
    old_user, _ = _register(client)
    new_user, _ = _register(client)

    with appmod._users_lock:
        for entry in appmod._users:
            if entry["user_id"] == old_user:
                entry["created_at"] = "2026-06-01T17:00:00+00:00"
            elif entry["user_id"] == new_user:
                entry["created_at"] = "2026-06-01T19:00:00+00:00"
        appmod._save_users()

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
        store = appmod.get_store(user_id)
        for idx in range(regular):
            role = "user" if idx % 2 == 0 else "openclaw"
            store.append_chat(role, "chat", _env(f"{user_id}_chat_{idx}", user_id))
        for idx in range(proactive):
            store.append_chat(
                "openclaw",
                appmod.PROACTIVE_JOB_SOURCE,
                _env(f"{user_id}_proactive_{idx}", user_id),
            )

    def add_memories(user_id: str, count: int) -> None:
        store = appmod.get_store(user_id)
        appmod._save_moments(
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
