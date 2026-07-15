"""DB-backed concurrency coverage for the debug trace append boundary."""
from __future__ import annotations

import concurrent.futures
import time

import conftest
import db
import debug_trace


def test_concurrent_trace_batches_preserve_every_event_across_writers(backend_env):
    uid = "usr_debug_trace_atomic_batches"
    conftest.seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind=%s",
            (uid, debug_trace.DEBUG_TRACE_BLOB),
        )

    now = time.time()
    batches = [
        [
            {"ts": now + batch * 0.001 + item * 0.000001, "type": f"{batch}:{item}"}
            for item in range(20)
        ]
        for batch in range(8)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                db.append_blob_events_strict,
                uid,
                debug_trace.DEBUG_TRACE_BLOB,
                batch,
                cutoff_ts=now - 60,
                max_events=200,
            )
            for batch in batches
        ]
        for future in futures:
            future.result(timeout=5)

    persisted = db.get_blob_strict(uid, debug_trace.DEBUG_TRACE_BLOB)
    assert persisted[db._BLOB_REVISION_KEY] == len(batches)
    assert {event["type"] for event in persisted["events"]} == {
        f"{batch}:{item}" for batch in range(8) for item in range(20)
    }
