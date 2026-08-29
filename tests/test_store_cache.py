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
import io
import logging
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402
from core import config as core_config  # noqa: E402
from core.store_sections import SectionStatus, StoreSection, StoreSectionUnavailable  # noqa: E402
from store_load_helpers import install_counting_loaders  # noqa: E402


@contextmanager
def _capture_logger(logger):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)


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


def test_user_store_constructor_and_shell_get_are_sql_free(monkeypatch):
    calls = install_counting_loaders(monkeypatch, core_store)
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    core_store._stores.clear()

    store = core_store.get_store("u-shell")

    assert store.user_id == "u-shell"
    assert calls == []
    assert store.loaded_sections() == frozenset()


def test_require_chat_loads_only_chat(monkeypatch):
    calls = install_counting_loaders(monkeypatch, core_store)
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    core_store._stores.clear()

    store = core_store.get_store(
        "u-chat", require={StoreSection.CHAT}
    )

    assert calls == ["chat"]
    assert store.loaded_sections() == frozenset({StoreSection.CHAT})


@pytest.mark.parametrize(
    ("mode", "ordinary_count", "legacy_count"),
    [
        ("legacy", 6, 6),
        ("selective", 0, 6),
        ("lazy", 0, 6),
    ],
)
def test_store_load_mode_matrix(
    monkeypatch, mode, ordinary_count, legacy_count
):
    calls = install_counting_loaders(monkeypatch, core_store)
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", mode)
    core_store._stores.clear()

    core_store.get_store(f"u-{mode}-ordinary")
    assert len(calls) == ordinary_count

    calls.clear()
    core_store.get_store_legacy(f"u-{mode}-compat")
    assert len(calls) == legacy_count


def test_multi_section_failure_keeps_successful_section(monkeypatch):
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    core_store._stores.clear()
    monkeypatch.setattr(
        core_store.UserStore, "_load_frames_meta", lambda _self: None
    )
    monkeypatch.setattr(
        core_store.UserStore,
        "_load_world_books",
        lambda _self: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    with pytest.raises(StoreSectionUnavailable) as exc_info:
        core_store.get_store(
            "u-partial",
            require={StoreSection.FRAMES, StoreSection.WORLD_BOOKS},
        )

    store = core_store._stores["u-partial"]
    assert exc_info.value.section is StoreSection.WORLD_BOOKS
    assert store._section_slots[StoreSection.FRAMES].status is SectionStatus.FRESH
    assert (
        store._section_slots[StoreSection.WORLD_BOOKS].status
        is SectionStatus.UNLOADED
    )


def test_ttl_marks_only_loaded_section_stale_without_loading(monkeypatch):
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    core_store._stores.clear()
    calls = install_counting_loaders(monkeypatch, core_store)
    store = core_store.get_store(
        "u-ttl", require={StoreSection.CHAT}
    )
    calls.clear()
    store._section_slots[StoreSection.CHAT].loaded_at_mono = 1.0
    store.loaded_at = 1.0
    monkeypatch.setattr(core_store.time, "monotonic", lambda: 1000.0)

    same = core_store.get_store("u-ttl")

    assert same is store
    assert calls == []
    assert (
        store._section_slots[StoreSection.CHAT].status
        is SectionStatus.STALE
    )
    assert (
        store._section_slots[StoreSection.TOKENS].status
        is SectionStatus.UNLOADED
    )


def test_reload_refreshes_only_sections_used_before(monkeypatch):
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    core_store._stores.clear()
    calls = install_counting_loaders(monkeypatch, core_store)
    store = core_store.get_store(
        "u-reload",
        require={StoreSection.CHAT, StoreSection.TOKENS},
    )
    calls.clear()

    assert store.reload() is True

    assert calls == ["chat", "tokens"]


def test_evicting_shell_loads_no_sections(monkeypatch):
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    core_store._stores.clear()
    calls = install_counting_loaders(monkeypatch, core_store)
    store = core_store.get_store("u-shell-evict")

    assert core_store._evict_store(store.user_id) is True

    assert calls == []
    assert store.loaded_sections() == frozenset()


def test_targeted_refresh_rejects_cache_entries_without_section_api(monkeypatch):
    """Targeted invalidation must not bypass UserStore section state."""

    class LegacyStoreAdapter:
        frames_lock = threading.Lock()

        def __init__(self):
            self.loaded = False

        def _load_frames_meta(self):
            self.loaded = True

    adapter = LegacyStoreAdapter()
    monkeypatch.setitem(core_store._stores, "u-legacy-adapter", adapter)

    with pytest.raises(AttributeError, match="note_section_change"):
        core_store._refresh_store_channel("u-legacy-adapter", "frames")

    assert adapter.loaded is False


def test_refresh_failure_retains_last_good_chat(monkeypatch):
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    core_store._stores.clear()
    store = core_store.get_store("u-last-good")

    def initial_load():
        store.chat_messages = [{"id": "kept", "seq": 1}]
        return list(store.chat_messages)

    monkeypatch.setattr(store, "reload_chat_hot_strict", initial_load)
    assert store.ensure_sections({StoreSection.CHAT}) is True
    store._section_slots[StoreSection.CHAT].mark_stale()
    monkeypatch.setattr(
        store,
        "reload_chat_hot_strict",
        lambda: (_ for _ in ()).throw(RuntimeError("postgresql://private")),
    )

    assert store.ensure_sections(
        {StoreSection.CHAT}, reason="ttl", strict=False
    ) is False
    assert store.chat_messages == [{"id": "kept", "seq": 1}]
    assert (
        store._section_slots[StoreSection.CHAT].status
        is SectionStatus.STALE
    )


def test_store_load_telemetry_is_fixed_enum_and_content_free():
    with _capture_logger(core_store.log) as stream:
        core_store._store_load_telemetry(
            section=StoreSection.CHAT,
            reason="first_use",
            cache_state="cold",
            row_count=256,
            duration_ms=12.5,
            outcome="applied",
        )

    text = stream.getvalue()
    assert "section=chat reason=first_use cache_state=cold" in text
    assert "rows=256" in text
    assert "outcome=applied" in text
    for forbidden in (
        "user_id",
        "body_ct",
        "K_user",
        "postgresql://",
    ):
        assert forbidden not in text


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


def test_strict_chat_append_does_not_leave_in_memory_phantom(monkeypatch):
    store = object.__new__(core_store.UserStore)
    store.user_id = "strict-user"
    store.chat_messages = []
    store.chat_lock = threading.Lock()

    def _fail(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "chat_append_strict", _fail)
    envelope = {
        "id": "strict-reply",
        "v": 1,
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped-key",
        "owner_user_id": store.user_id,
    }

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.append_chat("openclaw", "model_api", envelope, strict=True)

    assert store.chat_messages == []


def test_db_strict_append_raises_while_legacy_wrapper_is_best_effort(monkeypatch):
    class _BrokenPool:
        def connection(self):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "get_pool", lambda: _BrokenPool())

    with pytest.raises(RuntimeError, match="database unavailable"):
        db.chat_append_strict("u", "m", 1.0, {"id": "m"}, max_messages=0)

    # Existing callers retain the historical non-raising contract.
    db.chat_append("u", "m", 1.0, {"id": "m"}, max_messages=0)
