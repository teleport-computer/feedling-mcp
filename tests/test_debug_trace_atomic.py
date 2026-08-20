"""DB-backed concurrency coverage for append-only trace batches."""
from __future__ import annotations

import concurrent.futures
import os
import time

import db


def test_concurrent_trace_batches_preserve_every_event(backend_env, monkeypatch):
    uid = "usr_debug_trace_atomic_batches"
    original_url = os.environ["DATABASE_URL"]
    db.close_pool()
    monkeypatch.setenv("DATABASE_URL", os.environ["TEE_DATABASE_URL"])
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    now = time.time()
    batches = [
        [
            {"ts": now + batch * 0.001 + item * 0.000001, "type": f"{batch}:{item}"}
            for item in range(20)
        ]
        for batch in range(8)
    ]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(db.insert_trace_events_strict, uid, batch)
                for batch in batches
            ]
            for future in futures:
                assert future.result(timeout=5) == 20

        persisted = db.query_trace_events(user_id=uid, limit=200)
        assert {event["type"] for event in persisted} == {
            f"{batch}:{item}" for batch in range(8) for item in range(20)
        }
    finally:
        db.delete_trace_events_for_user(uid)
        db.close_pool()
        monkeypatch.setenv("DATABASE_URL", original_url)
        monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
