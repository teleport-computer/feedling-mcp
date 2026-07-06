"""Tests for the in-process UserStore cache (TTL reload + admin eviction).

Background: `_stores` is a single shared in-process cache (gunicorn runs one
worker). A UserStore is a write-through cache over PostgreSQL — every mutation
persists immediately, so reloading from the DB is always safe. Out-of-band DB
writes (e.g. the orphan-account recovery tool) leave the cached store stale;
these tests pin the two mechanisms that resolve that staleness without a
backend redeploy: a TTL on the cache, and a targeted admin eviction endpoint.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402
from core import config as core_config  # noqa: E402


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


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _append_chat_row_directly(user_id: str, msg_id: str) -> None:
    """Write a chat row straight to the DB, bypassing the cached store —
    simulates an out-of-band change (like the recovery tool's re-own)."""
    msg = {
        "id": msg_id, "role": "user", "ts": 1234.0, "source": "chat",
        "v": 1, "body_ct": "x", "nonce": "x", "K_user": "x",
        "content_type": "text", "owner_user_id": user_id, "visibility": "shared",
    }
    db.chat_append(user_id, msg_id, msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)


def test_get_store_returns_cached_instance_within_ttl(client):
    user_id, _ = _register(client)
    store1 = core_store.get_store(user_id)
    store2 = core_store.get_store(user_id)
    assert store2 is store1  # same instance: served from cache within TTL


def test_get_store_reloads_in_place_after_ttl_expiry(client, monkeypatch):
    user_id, _ = _register(client)
    store1 = core_store.get_store(user_id)
    assert all(m["id"] != "ooband" for m in store1.chat_messages)

    # Out-of-band DB write the cached store can't see.
    _append_chat_row_directly(user_id, "ooband")
    assert all(m["id"] != "ooband" for m in core_store.get_store(user_id).chat_messages)

    # Expire the cache → next get_store refreshes IN PLACE (same instance, so a
    # concurrent holder that writes through the same object can't be lost), and
    # the refreshed state now includes the out-of-band row.
    monkeypatch.setattr(core_store, "STORE_CACHE_TTL_SECONDS", 0)
    store2 = core_store.get_store(user_id)
    assert store2 is store1  # stable identity — no swap race
    assert any(m["id"] == "ooband" for m in store2.chat_messages)


def test_admin_store_evict_refreshes_in_place(client):
    user_id, _ = _register(client)
    store1 = core_store.get_store(user_id)

    res = client.post(
        "/v1/admin/store/evict",
        json={"user_id": user_id},
        headers={"X-Admin-Token": "admin-test-token"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json().get("evicted") is True

    # Same instance retained (refresh-in-place, not object swap).
    store2 = core_store.get_store(user_id)
    assert store2 is store1


def test_admin_store_evict_surfaces_out_of_band_write(client):
    user_id, _ = _register(client)
    core_store.get_store(user_id)
    _append_chat_row_directly(user_id, "ooband")

    client.post(
        "/v1/admin/store/evict",
        json={"user_id": user_id},
        headers={"X-Admin-Token": "admin-test-token"},
    )
    reloaded = core_store.get_store(user_id)
    assert any(m["id"] == "ooband" for m in reloaded.chat_messages)


def test_admin_store_evict_requires_admin(client):
    user_id, _ = _register(client)
    res = client.post("/v1/admin/store/evict", json={"user_id": user_id})
    assert res.status_code in (401, 503)
    # store must NOT be evicted by an unauthorized call
    core_store.get_store(user_id)
    assert user_id in core_store._stores
