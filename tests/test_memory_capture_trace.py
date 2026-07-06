"""memory.capture.* flow-trace events at the two real capture seams:
enqueue_memory_capture_job (queued) and record_capture_job_status (done/error).

Modeled on tests/test_chat_route_debug_trace.py (trace enable/read) and
tests/test_proactive_jobs.py (UserStore + seed_user backed by real PG)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="feedling-capture-trace-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import debug_trace  # noqa: E402
from proactive import capture_jobs  # noqa: E402
from proactive import capture_scheduler  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402

from conftest import seed_user  # noqa: E402


def _store(tmp_path, monkeypatch, user_id: str):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    debug_trace._flag_cache.clear()
    store = core_store.UserStore(user_id)
    seed_user(store.user_id)
    debug_trace.set_enabled(store, True)
    return store


def test_enqueue_memory_capture_job_emits_queued_event(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_queued")

    job, enqueued, reason = capture_jobs.enqueue_memory_capture_job(
        store,
        trigger="session_break",
        capture_key="cap_key_1",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 3},
    )

    assert enqueued is True
    assert reason == "enqueued"
    assert job is not None

    events = debug_trace.read_trace(store, subsystem="memory")
    queued = [e for e in events if e["type"] == "memory.capture.queued"]
    assert len(queued) == 1
    assert queued[0]["job_id"] == job["job_id"]
    assert queued[0]["actor"] == "backend"
    assert queued[0]["explain"]


def test_enqueue_duplicate_capture_key_does_not_emit_queued_event(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_dedupe")

    job1, enqueued1, _ = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_dupe",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 1},
    )
    assert enqueued1 is True

    job2, enqueued2, reason2 = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_dupe",
        window={"after_message_id": "m1", "until_message_id": "m3", "message_count": 2},
    )
    assert enqueued2 is False
    assert reason2 == "duplicate_capture_key"
    assert job2["job_id"] == job1["job_id"]

    events = debug_trace.read_trace(store, subsystem="memory")
    queued = [e for e in events if e["type"] == "memory.capture.queued"]
    # Only the first, real enqueue should have emitted a queued event.
    assert len(queued) == 1


def test_record_capture_job_status_completed_emits_done_event(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_done")

    job, enqueued, _ = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_done",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 2},
    )
    assert enqueued is True

    completed_job = dict(job)
    completed_job.update({
        "status": "completed",
        "cards_added": 2,
        "capture_result": {"titles": ["蛋子是狗", "喜欢咖啡"]},
    })

    capture_scheduler.record_capture_job_status(store, completed_job, status="completed")

    events = debug_trace.read_trace(store, subsystem="memory")
    done = [e for e in events if e["type"] == "memory.capture.done"]
    assert len(done) == 1
    assert done[0]["job_id"] == job["job_id"]
    assert done[0]["status"] == "ok"
    assert done[0]["detail"]["cards_added"] == 2
    assert "蛋子是狗" in done[0]["content_excerpt"]["titles"]


def test_record_capture_job_status_completed_zero_cards_is_legal_noop_wording(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_zero")

    job, enqueued, _ = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_zero",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 1},
    )
    assert enqueued is True

    completed_job = dict(job)
    completed_job.update({"status": "completed", "cards_added": 0})

    capture_scheduler.record_capture_job_status(store, completed_job, status="completed")

    events = debug_trace.read_trace(store, subsystem="memory")
    done = [e for e in events if e["type"] == "memory.capture.done"]
    assert len(done) == 1
    assert "没有可抓取的新记忆" in done[0]["explain"]


def test_record_capture_job_status_skipped_emits_done_event_not_error(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_skipped")

    job, enqueued, _ = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_skipped",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 1},
    )
    assert enqueued is True

    skipped_job = dict(job)
    skipped_job.update({"status": "skipped", "status_reason": "throttled"})

    capture_scheduler.record_capture_job_status(store, skipped_job, status="skipped")

    events = debug_trace.read_trace(store, subsystem="memory")
    errors = [e for e in events if e["type"] == "memory.capture.error"]
    assert len(errors) == 0

    done = [e for e in events if e["type"] == "memory.capture.done"]
    assert len(done) == 1
    assert done[0]["job_id"] == job["job_id"]
    assert done[0]["status"] != "error"
    assert done[0]["status"] == "ok"
    assert done[0]["detail"]["status"] == "skipped"


def test_record_capture_job_status_failed_emits_error_event(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_error")

    job, enqueued, _ = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_error",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 1},
    )
    assert enqueued is True

    failed_job = dict(job)
    failed_job.update({"status": "failed", "status_reason": "handler_exception"})

    capture_scheduler.record_capture_job_status(store, failed_job, status="failed")

    events = debug_trace.read_trace(store, subsystem="memory")
    errors = [e for e in events if e["type"] == "memory.capture.error"]
    assert len(errors) == 1
    assert errors[0]["job_id"] == job["job_id"]
    assert errors[0]["status"] == "error"
    assert errors[0]["detail"]["reason"] == "handler_exception"
