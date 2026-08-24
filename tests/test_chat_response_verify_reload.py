from __future__ import annotations

import sys
import threading
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from chat import routes_asgi  # noqa: E402


_VERIFY_REPLY = {"source": "verify_ping", "reply_to_message_id": "ping-1"}


def _verify_ping_msg(ts: float = 1.0) -> dict:
    return {
        "id": "ping-1",
        "role": "user",
        "source": "verify_ping",
        "ts": ts,
    }


class _FakeStore:
    user_id = "usr_test"

    def __init__(self, cached_messages: list[dict]):
        self.chat_lock = threading.RLock()
        self.chat_messages = list(cached_messages)
        self.reload_calls = 0

    def reload(self):
        self.reload_calls += 1
        raise AssertionError("verify admission must not reload the whole store")


def test_verify_reply_pending_check_point_reads_stale_negative_cache(monkeypatch):
    store = _FakeStore(cached_messages=[])
    calls = []
    monkeypatch.setattr(
        routes_asgi.db,
        "chat_get_strict",
        lambda user_id, msg_id: calls.append((user_id, msg_id)) or _verify_ping_msg(),
    )
    monkeypatch.setattr(
        routes_asgi.boot_gates,
        "_chat_loop_verified_by_server",
        lambda checked_store: False,
    )

    assert routes_asgi._allow_verify_reply_with_fresh_pending_check(store, _VERIFY_REPLY) is True
    assert calls == [("usr_test", "ping-1")]
    assert store.reload_calls == 0


def test_verify_reply_pending_check_still_rejects_when_point_read_has_no_ping(monkeypatch):
    store = _FakeStore(cached_messages=[])
    monkeypatch.setattr(routes_asgi.db, "chat_get_strict", lambda *_args: None)
    monkeypatch.setattr(
        routes_asgi.boot_gates,
        "_chat_loop_verified_by_server",
        lambda checked_store: False,
    )

    assert routes_asgi._allow_verify_reply_with_fresh_pending_check(store, _VERIFY_REPLY) is False
    assert store.reload_calls == 0


def test_verify_reply_pending_check_avoids_db_when_cache_is_already_true(monkeypatch):
    store = _FakeStore(cached_messages=[_verify_ping_msg()])
    monkeypatch.setattr(
        routes_asgi.db,
        "chat_get_strict",
        lambda *_args: (_ for _ in ()).throw(AssertionError("point read should not run")),
    )
    monkeypatch.setattr(
        routes_asgi.boot_gates,
        "_chat_loop_verified_by_server",
        lambda checked_store: (_ for _ in ()).throw(
            AssertionError("verified check should not run")
        ),
    )

    assert routes_asgi._allow_verify_reply_with_fresh_pending_check(store, _VERIFY_REPLY) is True
    assert store.reload_calls == 0


def test_verify_reply_pending_check_point_reads_even_after_previous_loop_verified(monkeypatch):
    store = _FakeStore(cached_messages=[])
    monkeypatch.setattr(
        routes_asgi.db, "chat_get_strict", lambda *_args: _verify_ping_msg()
    )
    monkeypatch.setattr(
        routes_asgi.boot_gates,
        "_chat_loop_verified_by_server",
        lambda checked_store: (_ for _ in ()).throw(
            AssertionError("sticky history must not suppress exact point read")
        ),
    )

    assert routes_asgi._allow_verify_reply_with_fresh_pending_check(store, _VERIFY_REPLY) is True
    assert store.reload_calls == 0


def test_verify_reply_pending_check_rejects_wrong_source_and_wrong_parent(monkeypatch):
    store = _FakeStore(cached_messages=[_verify_ping_msg()])
    monkeypatch.setattr(routes_asgi.db, "chat_get_strict", lambda *_args: None)
    monkeypatch.setattr(
        routes_asgi.boot_gates,
        "_chat_loop_verified_by_server",
        lambda checked_store: False,
    )

    assert routes_asgi._allow_verify_reply_with_fresh_pending_check(
        store, {"source": "chat", "reply_to_message_id": "ping-1"}
    ) is False
    assert routes_asgi._allow_verify_reply_with_fresh_pending_check(
        store, {"source": "verify_ping", "reply_to_message_id": "other"}
    ) is False
    assert store.reload_calls == 0


def test_verify_reply_point_read_rejects_non_ping_target(monkeypatch):
    store = _FakeStore(cached_messages=[])
    monkeypatch.setattr(
        routes_asgi.db,
        "chat_get_strict",
        lambda *_args: {**_verify_ping_msg(), "source": "chat"},
    )

    assert routes_asgi._allow_verify_reply_with_fresh_pending_check(
        store, _VERIFY_REPLY
    ) is False
    assert store.reload_calls == 0
