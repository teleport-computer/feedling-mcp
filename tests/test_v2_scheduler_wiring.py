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
