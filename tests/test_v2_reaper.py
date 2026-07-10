from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store, reaper, status_stream


def test_reap_once_surfaces_chat_timeout_but_keeps_background_silent(monkeypatch):
    monkeypatch.setattr(
        jobs_store,
        "reap_stuck_job_rows",
        lambda: [
            {"id": 1, "user_id": "u1", "lane": "chat", "last_error": "queue_timeout"},
            {"id": 2, "user_id": "u2", "lane": "maintenance", "last_error": "lease_timeout"},
        ],
    )
    events = []
    recorded = []
    wakes = []
    monkeypatch.setattr(status_stream, "redact_status", lambda kind: {
        "kind": "error", "label": "failed", "detail": {},
    })
    monkeypatch.setattr(
        jobs_store,
        "append_status_event",
        lambda user_id, kind, **kwargs: events.append((user_id, kind, kwargs["job_id"])),
    )
    monkeypatch.setattr(reaper.wake_bus, "notify", lambda channel, uid: wakes.append((channel, uid)))

    count = reaper.reap_once(
        record_terminal_error=lambda uid, message: recorded.append((uid, message)))

    assert count == 2
    assert events == [("u1", "error", 1)]
    assert recorded == [("u1", "queue_timeout")]
    assert wakes == [("chat", "u1")]
