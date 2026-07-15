"""serve_worker._reconcile_loop: the D9 parent-process wiring for
db.reconcile_unenqueued_v2_messages (the A7 orphan-message sweeper that was
built but never invoked anywhere — its own docstring deferred the periodic
call to "PR D's sweeper"). Runs in the PARENT (`_serve`'s task list), never
the turn-child, so it survives a watchdog kill/respawn of the turn-child.

Two things must hold:
1. One tick actually calls db.reconcile_unenqueued_v2_messages and its
   effect (a catch-up chat job for an orphaned user) lands.
2. A per-iteration exception is swallowed — the sweeper never crashes the
   parent loop it shares with the reaper/heartbeat/scheduler/watchdog.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import serve_worker
from hosted import config_store as hosted_config_store
from core import store as core_store

from conftest import configure_model_api_route, seed_user, set_v2_runtime_owner

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 reconcile sweeper tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE chat_messages, agent_jobs, v2_effect_outbox, "
            "v2_effect_sink_applied, v2_runtime_state, user_blobs, "
            "model_api_routes, model_api_credentials CASCADE"
        )
    yield


def _mark_db_action_v2(uid: str) -> None:
    # set_hosted_runtime_mode requires an existing model_api config to persist
    # against (mirrors test_v2_reconcile.py's / test_chat_send_v2_enqueue.py's
    # _seed pattern).
    configure_model_api_route(uid, provider="anthropic", model="m", test_status="ok")
    store = core_store.get_store(uid)
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")


def _insert_user_message(uid: str, msg_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
            (uid, msg_id, time.time(), db.Jsonb({"id": msg_id, "role": "user", "ts": time.time()})),
        )


def test_one_iteration_enqueues_catchup_job_for_orphan(monkeypatch):
    """Seed an orphan exactly like test_v2_reconcile.py, then drive ONE
    _reconcile_loop iteration (stop_event set right after the first tick
    lands) -> a catch-up chat job now exists for that user."""
    uid = "u_reconcile_sweep_orphan"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_user_message(uid, "m-sweep-orphan-1")

    calls = {"n": 0}
    real_reconcile = db.reconcile_unenqueued_v2_messages

    def _counting_reconcile():
        calls["n"] += 1
        return real_reconcile()

    monkeypatch.setattr(db, "reconcile_unenqueued_v2_messages", _counting_reconcile)
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(serve_worker._reconcile_loop(stop_event, interval=0.02))
        for _ in range(200):
            if calls["n"] >= 1:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_driver())

    assert calls["n"] >= 1
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT lane, status, reason FROM agent_jobs WHERE user_id=%s", (uid,),
        ).fetchall()
    assert len(rows) == 1
    lane, status, reason = rows[0]
    assert lane == "chat"
    assert status == "pending"
    assert reason == "reconcile"


def test_per_iteration_exception_does_not_crash_the_loop(monkeypatch):
    """A transient failure inside db.reconcile_unenqueued_v2_messages must be
    logged and swallowed, never propagate out and crash the sweeper (which
    would also crash _serve's asyncio.gather of parent loops)."""
    calls = {"n": 0}

    def _flaky_reconcile():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db outage")
        return 0

    monkeypatch.setattr(db, "reconcile_unenqueued_v2_messages", _flaky_reconcile)
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(serve_worker._reconcile_loop(stop_event, interval=0.02))
        for _ in range(200):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_driver())
    assert calls["n"] >= 2  # survived the first raise and ticked again


def test_one_iteration_drains_pending_effect_without_a_future_turn(monkeypatch):
    """A completed wake/turn is not required to revisit a failed outbox row."""
    uid = "u_reconcile_sweep_effect"
    seed_user(uid)
    set_v2_runtime_owner(uid)
    generation = db.get_runtime_generation(uid)
    assert db.effect_enqueue(
        "job-reconcile:status:0",
        uid,
        991,
        "status",
        generation,
        {"kind": "processing"},
    )

    calls = {"n": 0}
    real_apply = serve_worker._apply_pending_effects_for_user

    def counting_apply(user_id):
        calls["n"] += 1
        return real_apply(user_id)

    monkeypatch.setattr(
        serve_worker,
        "_apply_pending_effects_for_user",
        counting_apply,
    )
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(
            serve_worker._reconcile_loop(stop_event, interval=0.02)
        )
        for _ in range(200):
            if calls["n"] >= 1 and not db.effect_pending(uid):
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_driver())

    assert calls["n"] >= 1
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status FROM v2_effect_outbox WHERE effect_id=%s",
            ("job-reconcile:status:0",),
        ).fetchone()
    assert row == ("applied",)
