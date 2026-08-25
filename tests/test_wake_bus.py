"""Unit tests for the cross-worker wake bus dispatch (core/wake_bus.py).

Covers the routing logic only — no Postgres: notify()'s SQL is monkeypatched and
the store-channel path is exercised against an uncached user (so _evict_store
returns without a DB read). The real two-worker LISTEN/NOTIFY round trip is a
Step-5 integration concern.

Run:  python -m pytest tests/test_wake_bus.py -q
"""
import io
import json
import logging
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import wake_bus
from core.telemetry_logging import stderr_info_logger


@contextmanager
def _capture_logger(logger):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)


def test_chat_sync_telemetry_is_info_enabled_in_backend_runtime():
    assert wake_bus.log.isEnabledFor(logging.INFO)


def test_stderr_info_logger_is_idempotent_and_does_not_propagate():
    logger = stderr_info_logger("feedling.test.telemetry")
    original_handlers = list(logger.handlers)

    assert stderr_info_logger("feedling.test.telemetry") is logger
    assert logger.handlers == original_handlers
    assert logger.propagate is False


def test_chat_sync_telemetry_reaches_stderr_without_root_logging_config():
    backend_dir = Path(__file__).parent.parent / "backend"
    code = """
from core import store, wake_bus
wake_bus._chat_sync_telemetry(
    user_id="private-user",
    mode="incremental",
    result="applied",
    reason="event_sync",
    hot_rows=3,
)
store._chat_snapshot_fallback_telemetry(
    user_id="private-user",
    reason="gap",
    hot_rows=3,
)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=True,
    )

    assert (
        "chat_sync mode=incremental result=applied reason=event_sync"
        in result.stderr
    )
    assert "chat_sync_snapshot_fallback reason=gap" in result.stderr
    assert "private-user" not in result.stderr


def _reset_handlers():
    wake_bus._extra_handlers.clear()
    wake_bus._job_cancel_handlers.clear()


def test_job_cancellation_codec_round_trip():
    event = wake_bus.JobCancellation(
        job_id=3694,
        claimed_by="worker:heavy:0:g7",
        reason="preempted_by_chat",
    )

    assert wake_bus.JobCancellation.from_payload(event.to_payload()) == event


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"j": 0, "b": "worker", "r": "preempted_by_chat"},
        {"j": "3694", "b": "worker", "r": "preempted_by_chat"},
        {"j": 3694, "b": "", "r": "preempted_by_chat"},
        {"j": 3694, "b": "worker", "r": ""},
        {"j": 3694, "b": "worker", "r": "preempted_by_chat", "x": 1},
        {"j": 3694, "b": "w" * 201, "r": "preempted_by_chat"},
        {"j": 3694, "b": "worker", "r": "r" * 121},
    ],
)
def test_job_cancellation_rejects_invalid_or_oversized_payload(payload):
    with pytest.raises(ValueError):
        wake_bus.JobCancellation.from_payload(payload)


def test_notify_job_cancel_uses_existing_wake_channel(monkeypatch):
    captured = {}
    event = wake_bus.JobCancellation(3694, "worker:heavy:0:g7", "preempted_by_chat")
    monkeypatch.setenv("FEEDLING_WAKE_BUS_ENABLED", "1")
    monkeypatch.setattr(
        wake_bus.db,
        "pg_notify",
        lambda channel, payload: captured.update(
            channel=channel, payload=json.loads(payload)
        ),
    )

    wake_bus.notify_job_cancel(event)

    assert captured == {
        "channel": wake_bus.PG_CHANNEL,
        "payload": {
            "c": "job_cancel",
            "o": wake_bus.WORKER_ID,
            "j": 3694,
            "b": "worker:heavy:0:g7",
            "r": "preempted_by_chat",
        },
    }


def test_dispatch_job_cancel_is_typed_and_rejects_extra_keys():
    _reset_handlers()
    fired = []
    wake_bus.register_job_cancel_handler(fired.append)
    payload = {
        "c": "job_cancel",
        "o": "OTHER",
        "j": 3694,
        "b": "worker:heavy:0:g7",
        "r": "preempted_by_chat",
    }

    wake_bus._dispatch(json.dumps(payload))
    wake_bus._dispatch(json.dumps({**payload, "unexpected": True}))
    wake_bus._dispatch(json.dumps({**payload, "o": 123}))

    assert fired == [
        wake_bus.JobCancellation(3694, "worker:heavy:0:g7", "preempted_by_chat")
    ]
    _reset_handlers()


def test_notify_payload_shape(monkeypatch):
    captured = {}

    def fake_pg_notify(channel, payload):
        captured["channel"] = channel
        captured["payload"] = json.loads(payload)

    monkeypatch.setattr(wake_bus.db, "pg_notify", fake_pg_notify)
    monkeypatch.setenv("FEEDLING_WAKE_BUS_ENABLED", "1")
    wake_bus.notify("chat", "user-42")

    assert captured["channel"] == wake_bus.PG_CHANNEL
    assert captured["payload"] == {"u": "user-42", "c": "chat", "o": wake_bus.WORKER_ID}


def test_notify_chat_wake_only_emits_exact_typed_payload(monkeypatch):
    sent = []
    monkeypatch.setenv("FEEDLING_WAKE_BUS_ENABLED", "1")
    monkeypatch.setattr(wake_bus, "WORKER_ID", "worker-a")
    monkeypatch.setattr(
        wake_bus.db,
        "pg_notify",
        lambda channel, payload: sent.append((channel, payload)),
    )

    wake_bus.notify_chat_wake_only("u7")

    assert sent == [(
        wake_bus.PG_CHANNEL,
        '{"c":"chat","u":"u7","o":"worker-a","w":1}',
    )]


def test_notify_chat_wake_only_disabled_is_noop(monkeypatch):
    sent = []
    monkeypatch.setenv("FEEDLING_WAKE_BUS_ENABLED", "0")
    monkeypatch.setattr(
        wake_bus.db,
        "pg_notify",
        lambda *args: sent.append(args),
    )

    wake_bus.notify_chat_wake_only("u7")

    assert sent == []


def test_forked_workers_get_distinct_identities():
    if not hasattr(os, "fork"):
        return

    backend_path = Path(__file__).parent.parent / "backend"
    script = f"""
import json, os, sys
sys.path.insert(0, {str(backend_path)!r})
from core import wake_bus

worker_ids = [wake_bus.WORKER_ID]
children = []
for _ in range(2):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.write(write_fd, wake_bus.WORKER_ID.encode())
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    children.append((pid, read_fd))
for pid, read_fd in children:
    worker_ids.append(os.read(read_fd, 128).decode())
    os.close(read_fd)
    os.waitpid(pid, 0)
print(json.dumps(worker_ids))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    worker_ids = json.loads(result.stdout)

    assert len(set(worker_ids)) == 3


def test_notify_disabled_is_noop(monkeypatch):
    called = []
    monkeypatch.setattr(wake_bus.db, "pg_notify", lambda *a, **k: called.append(a))
    monkeypatch.setenv("FEEDLING_WAKE_BUS_ENABLED", "0")
    wake_bus.notify("chat", "user-42")
    assert called == []


def test_dispatch_skips_self_origin(monkeypatch):
    _reset_handlers()
    fired = []
    wake_bus.register_handler("chat", lambda uid: fired.append(uid))
    wake_bus._dispatch(json.dumps({"u": "u1", "c": "chat", "o": wake_bus.WORKER_ID}))
    assert fired == []  # our own write — handlers must not run
    _reset_handlers()


def test_dispatch_runs_injected_handler_for_other_worker(monkeypatch):
    _reset_handlers()
    fired = []
    wake_bus.register_handler("users", lambda uid: fired.append(uid))
    wake_bus._dispatch(json.dumps({"u": "u9", "c": "users", "o": "OTHER_WORKER"}))
    assert fired == ["u9"]
    _reset_handlers()


def test_dispatch_wake_only_only_wakes_chat_waiters(monkeypatch):
    from core import store as core_store

    calls = []

    class Store:
        chat_messages = []

        def ensure_chat_fresh(self, **kwargs):
            calls.append(("ensure", kwargs))
            return True

        def reload_chat_hot_strict(self):
            calls.append("snapshot")

        def notify_chat_waiters(self):
            calls.append("chat_waiters")

    monkeypatch.setattr(core_store, "_stores", {"u7": Store()})
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "incremental")

    wake_bus._dispatch(json.dumps(
        {"c": "chat", "u": "u7", "o": "OTHER", "w": 1}
    ))

    assert calls == ["chat_waiters"]


@pytest.mark.parametrize(
    "payload",
    [
        {"c": "chat", "u": "u7", "o": "OTHER", "w": True},
        {"c": "chat", "u": "u7", "o": "OTHER", "w": 2},
        {"c": "chat", "u": "u7", "o": "OTHER", "w": 1, "x": 1},
        {"c": "chat", "u": "u7", "w": 1},
    ],
)
def test_dispatch_rejects_malformed_wake_only_payload(monkeypatch, payload):
    from core import store as core_store

    calls = []

    class Store:
        chat_messages = []

        def ensure_chat_fresh(self, **kwargs):
            calls.append(("ensure", kwargs))
            return True

        def reload_chat_hot_strict(self):
            calls.append("snapshot")

        def notify_chat_waiters(self):
            calls.append("chat_waiters")

    monkeypatch.setattr(core_store, "_stores", {"u7": Store()})
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "incremental")

    wake_bus._dispatch(json.dumps(payload))

    assert calls == []


def test_dispatch_legacy_chat_in_incremental_mode_syncs_then_wakes(monkeypatch):
    from core import store as core_store

    calls = []

    class Store:
        chat_messages = []

        def ensure_chat_fresh(self, **kwargs):
            calls.append(("ensure", kwargs))
            return True

        def reload_chat_hot_strict(self):
            calls.append("snapshot")

        def notify_chat_waiters(self):
            calls.append("chat_waiters")

    monkeypatch.setattr(core_store, "_stores", {"u7": Store()})
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "incremental")

    wake_bus._dispatch(json.dumps({"u": "u7", "c": "chat", "o": "OTHER"}))

    assert calls == [("ensure", {"force": True}), "chat_waiters"]


def test_dispatch_legacy_chat_in_incremental_mode_wakes_when_sync_fails(
    monkeypatch,
):
    from core import store as core_store

    calls = []

    class Store:
        chat_messages = []

        def ensure_chat_fresh(self, **kwargs):
            calls.append(("ensure", kwargs))
            return False

        def reload_chat_hot_strict(self):
            calls.append("snapshot")

        def notify_chat_waiters(self):
            calls.append("chat_waiters")

    monkeypatch.setattr(core_store, "_stores", {"u7": Store()})
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "incremental")

    wake_bus._dispatch(json.dumps({"u": "u7", "c": "chat", "o": "OTHER"}))

    assert calls == [("ensure", {"force": True}), "chat_waiters"]


def test_dispatch_legacy_chat_in_legacy_mode_keeps_snapshot_behavior(monkeypatch):
    from core import store as core_store

    calls = []

    class Store:
        chat_messages = []

        def reload_chat_hot_strict(self):
            calls.append("snapshot")

        def notify_chat_waiters(self):
            calls.append("chat_waiters")

    monkeypatch.setattr(core_store, "_stores", {"u7": Store()})
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "legacy")

    wake_bus._dispatch(json.dumps({"u": "u7", "c": "chat", "o": "OTHER"}))

    assert calls == ["snapshot", "chat_waiters"]


def test_dispatch_v2_chat_uses_target_version_without_origin(monkeypatch):
    from core import store as core_store

    calls = []

    class Store:
        chat_version = 6

        def ensure_chat_fresh(self, **kwargs):
            calls.append(kwargs)
            self.chat_version = max(self.chat_version, kwargs["target_version"])
            return True

    target = Store()
    monkeypatch.setattr(core_store, "_stores", {"u7": target})
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "incremental")

    payload = {"v": 2, "c": "chat", "u": "u7", "r": 7}
    wake_bus._dispatch(json.dumps(payload))
    wake_bus._dispatch(json.dumps(payload))

    assert calls == [{"force": True, "target_version": 7}]


@pytest.mark.parametrize("version", [None, 0, -1, True, 1.5, "7"])
def test_dispatch_v2_chat_rejects_malformed_versions(monkeypatch, version):
    from core import store as core_store

    calls = []
    monkeypatch.setattr(
        core_store,
        "_stores",
        {"u7": type("Store", (), {"ensure_chat_fresh": lambda *_a, **_k: calls.append(True)})()},
    )
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "incremental")

    wake_bus._dispatch(json.dumps({"v": 2, "c": "chat", "u": "u7", "r": version}))

    assert calls == []


def test_store_channels_refresh_only_their_component(monkeypatch):
    from core import store as core_store

    calls = []

    class Store:
        user_id = "u7"
        frames_lock = threading.Lock()
        world_books_lock = threading.Lock()
        proactive_job_waiters_lock = threading.Lock()
        proactive_job_waiters = []

        def _load_frames_meta(self):
            calls.append("frames")

        def _load_world_books(self):
            calls.append("world_books")

        def _load_tokens(self):
            calls.append("tokens")

        def _load_live_activity_state(self):
            calls.append("live")

        def _load_push_state(self):
            calls.append("push")

        def notify_proactive_job_waiters(self):
            calls.append("proactive")

    monkeypatch.setattr(core_store, "_stores", {"u7": Store()})
    monkeypatch.setattr(core_store, "_evict_store", lambda _uid: calls.append("all"))

    wake_bus._dispatch(json.dumps({"u": "u7", "c": "frames", "o": "OTHER"}))
    assert calls == ["frames"]
    calls.clear()
    wake_bus._dispatch(json.dumps({"u": "u7", "c": "blob", "o": "OTHER"}))
    assert calls == ["world_books", "tokens", "live", "push"]
    calls.clear()
    wake_bus._dispatch(json.dumps({"u": "u7", "c": "proactive", "o": "OTHER"}))
    assert calls == ["proactive"]


def test_chat_sync_mode_is_validated(monkeypatch):
    for mode in ("legacy", "observe", "incremental"):
        monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", mode)
        assert wake_bus._chat_sync_mode() == mode
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "typo")
    with pytest.raises(RuntimeError, match="FEEDLING_CHAT_SYNC_MODE"):
        wake_bus._chat_sync_mode()


def test_chat_sync_telemetry_is_fixed_enum_and_content_free():
    user_id = "usr_private_telemetry"
    with _capture_logger(wake_bus.log) as stream:
        wake_bus._chat_sync_telemetry(
            user_id=user_id,
            mode="incremental",
            result="applied",
            reason="event_sync",
            hot_rows=17,
        )
    text = stream.getvalue()
    assert "mode=incremental result=applied reason=event_sync" in text
    assert "hot_rows=17" in text
    assert user_id not in text
    for secret in ("body_ct", "K_user", "postgresql://", "message_ids"):
        assert secret not in text

    with pytest.raises(ValueError):
        wake_bus._chat_sync_telemetry(
            user_id=user_id, mode="bad", result="applied",
            reason="event_sync", hot_rows=0,
        )


def test_observe_mode_compares_identity_only_and_keeps_legacy_result(
    monkeypatch,
):
    from core import store as core_store

    class Store:
        chat_version = 1
        chat_lock = threading.Lock()
        chat_messages = [{"id": "old", "seq": 1, "body_ct": "secret"}]

        def ensure_chat_fresh(self, **_kwargs):
            self.chat_version = 2
            self.chat_messages = [{"id": "incremental", "seq": 2}]
            return True

        def reload_chat_hot_strict(self):
            self.chat_messages = [{"id": "legacy", "seq": 2}]

        def notify_chat_waiters(self):
            pass

    target = Store()
    monkeypatch.setattr(core_store, "_stores", {"u-private": target})
    monkeypatch.setenv("FEEDLING_CHAT_SYNC_MODE", "observe")
    monkeypatch.setattr(wake_bus, "_observe_chat_user", lambda _uid: True)

    with _capture_logger(wake_bus.log) as stream:
        wake_bus._dispatch(
            json.dumps({"v": 2, "c": "chat", "u": "u-private", "r": 2})
        )

    assert target.chat_messages == [{"id": "legacy", "seq": 2}]
    text = stream.getvalue()
    assert "chat_sync_observe_mismatch" in text
    assert "u-private" not in text
    assert "secret" not in text


def test_dispatch_ignores_malformed_payload():
    wake_bus._dispatch("not json")  # must not raise
    wake_bus._dispatch("[]")


def test_reconnect_catch_up_refreshes_stores_and_handlers(monkeypatch):
    # 重连补课：LISTEN 断线窗口内的 NOTIFY 永久丢失（PG 不补发），重新建立 LISTEN
    # 后必须把本 worker 已缓存的 store 全部就地刷新（并唤醒 waiter），同时重放
    # 注册过的 extra handlers（如 users → 注册表 reload），把漏广播的损失从
    # 「最长 15min TTL」压到「重连即恢复」。
    from core import store as core_store

    evicted = []
    monkeypatch.setattr(core_store, "_evict_store",
                        lambda uid: evicted.append(uid) or True)
    monkeypatch.setattr(core_store, "_stores",
                        {"u1": object(), "u2": object()})
    fired = []
    monkeypatch.setattr(wake_bus, "_extra_handlers",
                        {"users": [lambda uid: fired.append(("users", uid))]})

    wake_bus._reconnect_catch_up()
    assert sorted(evicted) == ["u1", "u2"]
    assert fired == [("users", "")]


def test_reconnect_catch_up_empty_cache_is_noop(monkeypatch):
    # 首次连接（worker 刚启动）时缓存为空 → 天然 no-op，但 handlers 照样重放
    # （注册表可能在监听建立前就被别的 worker 改过）。
    from core import store as core_store

    monkeypatch.setattr(core_store, "_stores", {})
    fired = []
    monkeypatch.setattr(wake_bus, "_extra_handlers",
                        {"users": [lambda uid: fired.append(uid)]})
    wake_bus._reconnect_catch_up()   # must not raise
    assert fired == [""]


def test_reconnect_catch_up_survives_evict_errors(monkeypatch):
    # 单个 store 刷新失败不得中断补课（其余 store 照常刷）。
    from core import store as core_store

    evicted = []

    def evict(uid):
        if uid == "bad":
            raise RuntimeError("boom")
        evicted.append(uid)
    monkeypatch.setattr(core_store, "_evict_store", evict)
    monkeypatch.setattr(core_store, "_stores",
                        {"bad": object(), "good": object()})
    monkeypatch.setattr(wake_bus, "_extra_handlers", {})
    wake_bus._reconnect_catch_up()
    assert evicted == ["good"]
