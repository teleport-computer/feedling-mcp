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
