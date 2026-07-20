from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store, reaper


def test_reap_once_surfaces_chat_timeout_but_keeps_background_silent(monkeypatch):
    monkeypatch.setattr(
        reaper.db,
        "reconcile_chat_r2_cleanup",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("job watchdog must never perform R2 I/O")
        ),
    )
    monkeypatch.setattr(
        jobs_store,
        "reap_stuck_job_rows",
        lambda: [
            {"id": 1, "user_id": "u1", "lane": "chat", "last_error": "queue_timeout"},
            {"id": 2, "user_id": "u2", "lane": "maintenance", "last_error": "lease_timeout"},
        ],
    )
    recorded = []
    reconcile_calls = []

    def _record(user_id, message):
        recorded.append((user_id, message))

    monkeypatch.setattr(
        jobs_store,
        "reconcile_terminal_failure_outbox",
        lambda **kwargs: reconcile_calls.append(kwargs) or {
            "examined": 1,
            "status_delivered": 1,
            "runtime_error_delivered": 1,
        },
    )

    count = reaper.reap_once(record_terminal_error=_record)

    assert count == 2
    assert reconcile_calls == [{"record_terminal_error": _record}]
    # The callback is consumed by the real reconciler, not the fresh row loop.
    assert recorded == []


def test_reap_once_reconciles_old_marker_even_when_no_job_expires(monkeypatch):
    """A process may die after the atomic terminal transition.  A later pass
    with zero newly expired rows must still drain the previously durable marker.
    """
    monkeypatch.setattr(jobs_store, "reap_stuck_job_rows", lambda: [])
    calls = []
    monkeypatch.setattr(
        jobs_store,
        "reconcile_terminal_failure_outbox",
        lambda **kwargs: calls.append(kwargs) or {
            "examined": 1,
            "status_delivered": 1,
            "runtime_error_delivered": 1,
        },
    )
    def sink(_uid, _code):
        return None

    assert reaper.reap_once(record_terminal_error=sink) == 0
    assert calls == [{"record_terminal_error": sink}]


def test_cleanup_once_forwards_bounded_batches(monkeypatch):
    calls = []
    monkeypatch.setattr(
        reaper.db,
        "reconcile_chat_r2_cleanup",
        lambda **kwargs: calls.append(kwargs) or 7,
    )

    assert reaper.cleanup_once(limit=9, inventory_limit=2) == 7
    assert calls == [{"limit": 9, "inventory_limit": 2}]


def test_cleanup_loop_retries_without_killing_watchdog(monkeypatch):
    import asyncio

    calls = []
    stop_event = asyncio.Event()

    def _cleanup_once(**_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("transient R2 failure")
        stop_event.set()
        return 0

    monkeypatch.setattr(reaper, "cleanup_once", _cleanup_once)

    async def _run():
        await asyncio.wait_for(
            reaper.run_cleanup_loop(
                stop_event,
                interval=0.01,
                limit=3,
                inventory_limit=1,
            ),
            timeout=1,
        )

    asyncio.run(_run())
    assert calls == [1, 2]


def test_trajectory_retention_settings_are_default_on_and_bounded(monkeypatch):
    for name in (
        "FEEDLING_V2_TRAJECTORY_RETENTION_DAYS",
        "FEEDLING_V2_TRAJECTORY_GC_INTERVAL_SEC",
        "FEEDLING_V2_TRAJECTORY_GC_BATCH",
    ):
        monkeypatch.delenv(name, raising=False)

    assert reaper.trajectory_retention_settings_from_env() == {
        "retention_days": 7,
        "interval": 3600.0,
        "limit": 100,
    }

    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_GC_BATCH", "0")
    import pytest

    with pytest.raises(RuntimeError, match="GC_BATCH"):
        reaper.trajectory_retention_settings_from_env()


def test_trajectory_cleanup_once_forwards_retention_boundary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        jobs_store,
        "purge_expired_trajectories",
        lambda **kwargs: calls.append(kwargs) or 3,
    )

    assert reaper.trajectory_cleanup_once(retention_days=7, limit=11) == 3
    assert calls == [{"retention_days": 7, "limit": 11}]


def test_trajectory_cleanup_loop_retries_transient_db_failure(monkeypatch):
    import asyncio

    calls = []
    stop_event = asyncio.Event()

    def _cleanup_once(**_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("transient DB failure")
        stop_event.set()
        return 1

    monkeypatch.setattr(reaper, "trajectory_cleanup_once", _cleanup_once)

    async def _run():
        await asyncio.wait_for(
            reaper.run_trajectory_cleanup_loop(
                stop_event,
                interval=0.01,
                retention_days=7,
                limit=5,
            ),
            timeout=1,
        )

    asyncio.run(_run())
    assert calls == [1, 2]


def test_trajectory_cleanup_loop_drains_all_full_batches_in_one_cadence(monkeypatch):
    import asyncio

    results = iter((5, 5, 2))
    calls = []
    stop_event = asyncio.Event()

    def _cleanup_once(**kwargs):
        calls.append(kwargs)
        result = next(results)
        if result < kwargs["limit"]:
            stop_event.set()
        return result

    monkeypatch.setattr(reaper, "trajectory_cleanup_once", _cleanup_once)

    async def _run():
        await asyncio.wait_for(
            reaper.run_trajectory_cleanup_loop(
                stop_event,
                interval=60,
                retention_days=7,
                limit=5,
            ),
            timeout=1,
        )

    asyncio.run(_run())
    assert calls == [
        {"retention_days": 7, "limit": 5},
        {"retention_days": 7, "limit": 5},
        {"retention_days": 7, "limit": 5},
    ]


def test_trajectory_cleanup_loop_stops_after_current_full_batch(monkeypatch):
    import asyncio

    calls = []
    stop_event = asyncio.Event()

    def _cleanup_once(**kwargs):
        calls.append(kwargs)
        stop_event.set()
        return kwargs["limit"]

    monkeypatch.setattr(reaper, "trajectory_cleanup_once", _cleanup_once)

    async def _run():
        await asyncio.wait_for(
            reaper.run_trajectory_cleanup_loop(
                stop_event,
                interval=60,
                retention_days=7,
                limit=5,
            ),
            timeout=1,
        )

    asyncio.run(_run())
    assert calls == [{"retention_days": 7, "limit": 5}]
