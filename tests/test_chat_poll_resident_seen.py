"""Resident chat-poll heartbeat -> access binding -> data-track connection."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


_CONSUMER_HEADERS = {
    "X-Feedling-Consumer": "feedling-chat-resident",
    "X-Feedling-Consumer-Id": "resident-heartbeat-test",
    "X-Feedling-Consumer-Version": "resident-v1",
}


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


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={
            "public_key": base64.b64encode(b"\x51" * 32).decode("ascii"),
            "archive_language": "en",
        },
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _poll_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key, **_CONSUMER_HEADERS}


def _binding(entry: dict, mode: str) -> dict | None:
    return next(
        (
            item
            for item in entry.get("access_bindings") or []
            if isinstance(item, dict) and item.get("access_mode") == mode
        ),
        None,
    )


def _persisted_user(user_id: str) -> dict:
    return next(entry for entry in db.load_all_users() if entry.get("user_id") == user_id)


def test_resident_poll_persists_seen_throttles_repeat_and_reports_online(client, monkeypatch):
    user_id, api_key = _register(client)
    stale = (datetime.now() - timedelta(hours=8)).isoformat()
    with registry._users_lock:
        entry = registry._find_user_entry_locked(user_id)
        resident = registry._upsert_access_binding_locked(entry, "resident")
        resident["last_seen_at"] = stale
        resident["updated_at"] = stale
        registry.persist_user(entry)

    first = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_poll_headers(api_key),
    )
    assert first.status_code == 200, first.get_data(as_text=True)
    first_seen = _binding(_persisted_user(user_id), "resident")["last_seen_at"]
    assert datetime.fromisoformat(first_seen) > datetime.fromisoformat(stale)

    detail = client.get(
        f"/v1/admin/data-track/users/{user_id}",
        headers={"X-Admin-Token": "admin-test-token"},
    )
    assert detail.status_code == 200, detail.get_data(as_text=True)
    connection = detail.get_json()["user"]["connection"]
    assert connection["status"] == "ok"
    assert connection["label"] == "在线"

    real_upsert = db.upsert_user
    upserts: list[str] = []

    def spy_upsert(entry):
        upserts.append(str(entry.get("user_id") or ""))
        return real_upsert(entry)

    monkeypatch.setattr(db, "upsert_user", spy_upsert)
    second = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_poll_headers(api_key),
    )
    assert second.status_code == 200, second.get_data(as_text=True)
    assert upserts == [], "a repeat poll inside 60s must not persist the user row"
    assert _binding(_persisted_user(user_id), "resident")["last_seen_at"] == first_seen


@pytest.mark.parametrize("route", ["model_api", "official_import"])
def test_non_resident_route_poll_does_not_touch_resident_or_current_binding(client, monkeypatch, route):
    user_id, api_key = _register(client)
    switched = client.post(
        "/v1/onboarding/route",
        headers={"X-API-Key": api_key},
        json={"route": route},
    )
    assert switched.status_code == 200, switched.get_data(as_text=True)
    before = _persisted_user(user_id)
    resident_before = _binding(before, "resident")
    current_seen_before = (_binding(before, route) or {}).get("last_seen_at", "")

    upserts: list[str] = []
    monkeypatch.setattr(db, "upsert_user", lambda entry: upserts.append(entry["user_id"]))
    poll = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_poll_headers(api_key),
    )
    assert poll.status_code == 200, poll.get_data(as_text=True)
    assert upserts == []
    with registry._users_lock:
        after = registry._find_user_entry_locked(user_id)
        assert _binding(after, "resident") == resident_before
        assert (_binding(after, route) or {}).get("last_seen_at", "") == current_seen_before


def test_plain_chat_poll_does_not_claim_resident_liveness(client, monkeypatch):
    user_id, api_key = _register(client)
    upserts: list[str] = []
    monkeypatch.setattr(db, "upsert_user", lambda entry: upserts.append(entry["user_id"]))

    poll = client.get(
        "/v1/chat/poll?timeout=0",
        headers={"X-API-Key": api_key},
    )
    assert poll.status_code == 200, poll.get_data(as_text=True)
    assert upserts == []
    with registry._users_lock:
        assert _binding(registry._find_user_entry_locked(user_id), "resident") is None


def test_failed_resident_seen_persist_rolls_back_for_next_poll(client, monkeypatch):
    user_id, api_key = _register(client)
    real_upsert = db.upsert_user
    attempts = {"count": 0}

    def flaky_upsert(entry):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient user-row write failure")
        return real_upsert(entry)

    monkeypatch.setattr(db, "upsert_user", flaky_upsert)
    first = client.get("/v1/chat/poll?timeout=0", headers=_poll_headers(api_key))
    assert first.status_code == 200
    with registry._users_lock:
        assert _binding(registry._find_user_entry_locked(user_id), "resident") is None

    second = client.get("/v1/chat/poll?timeout=0", headers=_poll_headers(api_key))
    assert second.status_code == 200
    assert attempts["count"] == 2
    assert _binding(_persisted_user(user_id), "resident")["last_seen_at"]
