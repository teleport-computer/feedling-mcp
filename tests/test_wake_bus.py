"""Unit tests for the cross-worker wake bus dispatch (core/wake_bus.py).

Covers the routing logic only — no Postgres: notify()'s SQL is monkeypatched and
the store-channel path is exercised against an uncached user (so _evict_store
returns without a DB read). The real two-worker LISTEN/NOTIFY round trip is a
Step-5 integration concern.

Run:  python -m pytest tests/test_wake_bus.py -q
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import wake_bus


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


def test_dispatch_store_channel_evicts(monkeypatch):
    # Cross-origin store-channel notify must call _evict_store for the user.
    from core import store as core_store

    seen = []
    monkeypatch.setattr(core_store, "_evict_store", lambda uid: seen.append(uid))
    wake_bus._dispatch(json.dumps({"u": "u7", "c": "proactive", "o": "OTHER"}))
    assert seen == ["u7"]


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
