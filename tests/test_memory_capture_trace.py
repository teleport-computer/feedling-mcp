"""memory.capture.* flow-trace events at the two real capture seams:
enqueue_memory_capture_job (queued) and record_capture_job_status (done/error).

Modeled on tests/test_chat_route_debug_trace.py (trace enable/read) and
tests/test_proactive_jobs.py (UserStore + seed_user backed by real PG)."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DATA_DIR = tempfile.mkdtemp(prefix="feedling-capture-trace-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import debug_trace  # noqa: E402
import db  # noqa: E402
from proactive import capture_jobs  # noqa: E402
from proactive import capture_scheduler  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402

from conftest import seed_user  # noqa: E402


@pytest.fixture(autouse=True)
def _tee_primary(monkeypatch):
    original_url = os.environ["DATABASE_URL"]
    db.close_pool()
    monkeypatch.setenv("DATABASE_URL", os.environ["TEE_DATABASE_URL"])
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    yield
    db.close_pool()
    monkeypatch.setenv("DATABASE_URL", original_url)
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")


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


def test_v1_capture_banner_accumulates_across_midnight_and_ignores_noop(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_daily_v1")

    def completed(cards_added: int, until: str) -> dict:
        return {
            "job_kind": "memory_capture",
            "status": "completed",
            "cards_added": cards_added,
            "capture_window": {
                "until_message_id": until,
                "until_ts": 1.0,
            },
        }

    first_at = datetime(2026, 8, 1, 10, tzinfo=timezone.utc).timestamp()
    second_at = datetime(2026, 8, 1, 22, tzinfo=timezone.utc).timestamp()
    noop_at = datetime(2026, 8, 2, 8, tzinfo=timezone.utc).timestamp()
    next_positive_at = datetime(2026, 8, 2, 9, tzinfo=timezone.utc).timestamp()

    capture_scheduler.record_capture_job_status(
        store, completed(2, "m1"), status="completed", now=first_at
    )
    state = capture_scheduler.load_capture_state(store)
    assert state["last_capture_cards_added"] == 2
    assert state["last_capture_cards_added_at"] == first_at

    capture_scheduler.record_capture_job_status(
        store, completed(3, "m2"), status="completed", now=second_at
    )
    state = capture_scheduler.load_capture_state(store)
    assert state["last_capture_cards_added"] == 5
    assert state["last_capture_cards_added_at"] == second_at

    capture_scheduler.record_capture_job_status(
        store, completed(0, "m3"), status="completed", now=noop_at
    )
    state = capture_scheduler.load_capture_state(store)
    assert state["last_capture_cards_added"] == 5
    assert state["last_capture_cards_added_at"] == second_at
    assert state["last_capture_completed_at"] == noop_at

    capture_scheduler.record_capture_job_status(
        store, completed(1, "m4"), status="completed", now=next_positive_at
    )
    state = capture_scheduler.load_capture_state(store)
    assert state["last_capture_cards_added"] == 6
    assert state["last_capture_cards_added_at"] == next_positive_at


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
    # A declared reason, so this test keeps asserting reason passthrough rather
    # than accidentally asserting the redaction a fail-closed sanitizer applies
    # to undeclared values. `runtime_failed` is what
    # db.content_free_failure_code writes as its collapsed-failure placeholder.
    failed_job.update({"status": "failed", "status_reason": "runtime_failed"})

    capture_scheduler.record_capture_job_status(store, failed_job, status="failed")

    events = debug_trace.read_trace(store, subsystem="memory")
    errors = [e for e in events if e["type"] == "memory.capture.error"]
    assert len(errors) == 1
    assert errors[0]["job_id"] == job["job_id"]
    assert errors[0]["status"] == "error"
    assert errors[0]["detail"]["reason"] == "runtime_failed"


# `GET /v1/debug/trace` returns trace details on the user's own auth, and
# `debug_trace._safe_detail` bounds length without judging content, so a
# provider error body carried in the job's reason would reach the end user.
# Both source fields are externally supplied free text: `_job_status_patch`
# stores `payload["reason"][:500]` and `payload["noop_reason"][:500]` straight
# from the request body.
#
# Synthetic value only. `.invalid` is reserved by RFC 2606 and never resolves.
_SYNTHETIC_LEAK = "https://keys.example.invalid/settings/keys/key_synthetic1"


def test_capture_trace_error_reason_is_redacted(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_redact_error")

    job, enqueued, _ = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_redact_error",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 1},
    )
    assert enqueued is True

    failed_job = dict(job)
    failed_job.update({
        "status": "failed",
        "status_reason": f"auth_invalid: 402 {_SYNTHETIC_LEAK}",
    })

    capture_scheduler.record_capture_job_status(store, failed_job, status="failed")

    events = debug_trace.read_trace(store, subsystem="memory")
    errors = [e for e in events if e["type"] == "memory.capture.error"]
    assert len(errors) == 1
    # Redaction, not deletion: the sanctioned prefix survives so the event still
    # reads as an auth failure.
    assert errors[0]["detail"]["reason"] == "auth_invalid:<redacted>"
    assert _SYNTHETIC_LEAK not in repr(events)


def test_capture_trace_skipped_reason_is_redacted(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_redact_skipped")

    job, enqueued, _ = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_redact_skipped",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 1},
    )
    assert enqueued is True

    skipped_job = dict(job)
    skipped_job.update({
        "status": "skipped",
        "status_reason": f"heartbeat_throttled: gate said {_SYNTHETIC_LEAK}",
    })

    capture_scheduler.record_capture_job_status(store, skipped_job, status="skipped")

    events = debug_trace.read_trace(store, subsystem="memory")
    done = [e for e in events if e["type"] == "memory.capture.done"]
    assert len(done) == 1
    assert done[0]["detail"]["reason"] == "heartbeat_throttled:<redacted>"
    assert _SYNTHETIC_LEAK not in repr(events)


def test_capture_trace_noop_reason_is_redacted(tmp_path, monkeypatch):
    """The fallback field is free text too, and is the one a reader forgets.

    `status_reason` is absent here, so the writer falls through to
    `noop_reason` — a separate `_job_status_patch` field with no producer-owned
    vocabulary of its own (no backend code writes it as a literal).
    """
    store = _store(tmp_path, monkeypatch, "usr_capture_trace_redact_noop")

    job, enqueued, _ = capture_jobs.enqueue_memory_capture_job(
        store, trigger="session_break", capture_key="cap_key_redact_noop",
        window={"after_message_id": "m1", "until_message_id": "m2", "message_count": 1},
    )
    assert enqueued is True

    failed_job = dict(job)
    failed_job.update({
        "status": "failed",
        "status_reason": "",
        "noop_reason": f"402 from upstream {_SYNTHETIC_LEAK}",
    })

    capture_scheduler.record_capture_job_status(store, failed_job, status="failed")

    events = debug_trace.read_trace(store, subsystem="memory")
    errors = [e for e in events if e["type"] == "memory.capture.error"]
    assert len(errors) == 1
    # Nothing sanctioned leads this one, so it redacts whole.
    assert errors[0]["detail"]["reason"] == "<redacted>"
    assert _SYNTHETIC_LEAK not in repr(events)
