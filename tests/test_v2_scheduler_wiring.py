"""D3 Task 5：serve_worker._build_scheduler_deps —— 把纯模块
`model_api_runtime.v2.scheduler.run_scheduler_tick` 接到真实实现的装配层适配器。

纯接线断言，不跑真的 scheduler 循环：monkeypatch jobs_store 上的
enqueue_job/upsert_wake_schedule/due_heartbeat_users，断言 deps 的四个 callable
把参数原样转发到正确的 lane/kwarg 上；wake_decision 断言就是 Task 3 的
`_wake_decision_for_user` 适配器本体（不是重新实现一份）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store  # noqa: E402
from model_api_runtime.v2 import serve_worker  # noqa: E402


def test_enqueue_heartbeat_enqueues_on_the_heartbeat_lane(monkeypatch):
    calls = []
    monkeypatch.setattr(
        jobs_store, "enqueue_job",
        lambda uid, lane, **kw: calls.append((uid, lane, kw)))

    deps = serve_worker._build_scheduler_deps()
    deps.enqueue_heartbeat("u1")

    assert calls == [("u1", "heartbeat", {})]


def test_advance_heartbeat_upserts_next_heartbeat_at(monkeypatch):
    calls = []
    monkeypatch.setattr(
        jobs_store, "upsert_wake_schedule",
        lambda uid, **kw: calls.append((uid, kw)))

    deps = serve_worker._build_scheduler_deps()
    deps.advance_heartbeat("u1", 123.0)

    assert calls == [("u1", {"next_heartbeat_at": 123.0})]


def test_due_users_delegates_to_due_heartbeat_users(monkeypatch):
    monkeypatch.setattr(jobs_store, "due_heartbeat_users", lambda: ["a", "b"])

    deps = serve_worker._build_scheduler_deps()

    assert deps.due_users() == ["a", "b"]


def test_wake_decision_is_the_task3_adapter_not_a_reimplementation():
    deps = serve_worker._build_scheduler_deps()

    assert deps.wake_decision is serve_worker._wake_decision_for_user


def test_runtime_mode_fence_uses_strict_control_plane_read(monkeypatch):
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda uid: object())
    monkeypatch.setattr(
        serve_worker.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda store: True,
    )

    assert serve_worker._build_scheduler_deps().runtime_mode_enabled("u1") is True


def test_extraction_producer_is_quarantined_by_default(monkeypatch):
    monkeypatch.setattr(serve_worker, "_CAPTURE_ENABLED", False)
    monkeypatch.setattr(serve_worker, "_DREAM_ENABLED", False)
    monkeypatch.setattr(
        serve_worker.admin_core,
        "list_runtime_modes",
        lambda: {"db_action_v2": ["u1"]},
    )
    deps = serve_worker._build_scheduler_deps()
    assert deps.extraction_users() == []


def test_capture_and_dream_are_independently_gated(monkeypatch):
    calls = []
    monkeypatch.setattr(serve_worker, "_CAPTURE_ENABLED", True)
    monkeypatch.setattr(serve_worker, "_DREAM_ENABLED", False)
    monkeypatch.setattr(
        serve_worker, "_tick_capture_for_user", lambda uid: calls.append(("capture", uid)) or 1
    )
    monkeypatch.setattr(
        serve_worker, "_tick_dream_for_user", lambda uid: calls.append(("dream", uid)) or 1
    )

    assert serve_worker._tick_extraction_for_user("u1") == 1
    assert calls == [("capture", "u1")]

    calls.clear()
    monkeypatch.setattr(serve_worker, "_CAPTURE_ENABLED", False)
    monkeypatch.setattr(serve_worker, "_DREAM_ENABLED", True)
    assert serve_worker._tick_extraction_for_user("u1") == 1
    assert calls == [("dream", "u1")]


def test_existing_v2_schedule_backfill_is_idempotent(monkeypatch):
    monkeypatch.setattr(
        serve_worker.admin_core,
        "list_runtime_modes",
        lambda: {"db_action_v2": ["new", "null-heartbeat", "existing"]},
    )
    monkeypatch.setattr(
        jobs_store,
        "get_wake_schedule",
        lambda uid: (
            {"user_id": uid, "next_heartbeat_at": 456.0}
            if uid == "existing"
            else {"user_id": uid, "next_heartbeat_at": None}
            if uid == "null-heartbeat"
            else None
        ),
    )
    writes = []
    monkeypatch.setattr(
        jobs_store,
        "upsert_wake_schedule",
        lambda uid, **kwargs: writes.append((uid, kwargs)),
    )

    assert serve_worker._seed_existing_v2_wake_schedules(now=123.0) == 2
    assert writes == [
        ("new", {"next_heartbeat_at": 123.0}),
        ("null-heartbeat", {"next_heartbeat_at": 123.0}),
    ]


# --- backlog scan loop wiring ----------------------------------------------


def test_backlog_scan_loop_enqueues_maintenance_for_every_due_user(monkeypatch):
    import asyncio

    enqueued: list[tuple] = []
    monkeypatch.setattr(
        jobs_store,
        "due_compaction_users",
        lambda **kw: [("usr_a", 900), ("usr_b", 300)],
    )
    monkeypatch.setattr(
        jobs_store,
        "enqueue_job",
        lambda uid, lane, **kw: enqueued.append((uid, lane, kw.get("reason"))) or (1, False),
    )
    monkeypatch.setattr(serve_worker.core_wake_bus, "notify", lambda *a, **k: None)

    async def _one_pass():
        stop = asyncio.Event()
        task = asyncio.create_task(serve_worker._backlog_scan_loop(stop, interval=30.0))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(_one_pass())

    assert ("usr_a", "maintenance", "backlog_scan") in enqueued
    assert ("usr_b", "maintenance", "backlog_scan") in enqueued


def test_one_failing_user_does_not_stop_the_backlog_scan(monkeypatch):
    import asyncio

    enqueued: list[str] = []

    def _flaky(uid, lane, **kw):
        if uid == "usr_broken":
            raise RuntimeError("enqueue exploded")
        enqueued.append(uid)
        return (1, False)

    monkeypatch.setattr(
        jobs_store,
        "due_compaction_users",
        lambda **kw: [("usr_broken", 900), ("usr_ok", 300)],
    )
    monkeypatch.setattr(jobs_store, "enqueue_job", _flaky)
    monkeypatch.setattr(serve_worker.core_wake_bus, "notify", lambda *a, **k: None)

    async def _one_pass():
        stop = asyncio.Event()
        task = asyncio.create_task(serve_worker._backlog_scan_loop(stop, interval=30.0))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(_one_pass())

    assert enqueued == ["usr_ok"]


def test_backlog_scan_survives_a_failing_query(monkeypatch):
    """A transient DB failure must never kill the loop."""
    import asyncio

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(jobs_store, "due_compaction_users", _boom)

    async def _one_pass():
        stop = asyncio.Event()
        task = asyncio.create_task(serve_worker._backlog_scan_loop(stop, interval=30.0))
        await asyncio.sleep(0.05)
        stop.set()
        await task  # must return cleanly, not raise

    asyncio.run(_one_pass())
