"""v1 flow trace: beta default-on recording with deploy/per-user safety valves."""
import os
import queue
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from core import enclave as core_enclave  # noqa: E402
import db  # noqa: E402
import debug_trace  # noqa: E402


class _Store:
    def __init__(self, uid="usr_dbg"):
        self.user_id = uid


def _reset(monkeypatch, store):
    debug_trace._flag_cache.clear()
    # isolate DB blobs to an in-memory dict so the test never needs Postgres
    blobs: dict = {}
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))
    monkeypatch.setattr(db, "set_blob", lambda uid, kind, doc: blobs.__setitem__((uid, kind), doc))

    def append_events(uid, kind, new_events, *, cutoff_ts, max_events):
        current = blobs.get((uid, kind)) or {}
        events = list(current.get("events") or []) + list(new_events)
        events = [event for event in events if event.get("ts", 0) >= cutoff_ts]
        persisted = {"v": 1, "events": events[-max_events:]}
        blobs[(uid, kind)] = persisted
        return persisted

    monkeypatch.setattr(db, "append_blob_events_strict", append_events)
    return blobs


def test_default_on_records_no_env_needed(monkeypatch):
    store = _Store()
    blobs = _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)  # no env set (default)
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.is_enabled(store) is True
    assert debug_trace.read_trace(store)[0]["type"] == "route.decided"
    assert (store.user_id, debug_trace.DEBUG_TRACE_BLOB) in blobs


def test_default_can_be_restored_to_opt_in_with_env(monkeypatch):
    store = _Store()
    blobs = _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE_DEFAULT", "0")
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.is_enabled(store) is False
    assert debug_trace.read_trace(store) == []
    assert (store.user_id, debug_trace.DEBUG_TRACE_BLOB) not in blobs
    debug_trace.set_enabled(store, True)
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.read_trace(store)[0]["type"] == "route.decided"


def test_env_zero_hard_disables_even_with_flag_on(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "0")  # prod kill switch
    debug_trace.set_enabled(store, True)  # user toggled on, but...
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    assert debug_trace.is_enabled(store) is False
    assert debug_trace.read_trace(store) == []


def test_records_when_flag_on_no_env_needed(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)
    debug_trace.trace_event(store, subsystem="route", type="route.decided",
                            summary="host", detail={"mode": "agent_runtime", "reason": "text"})
    debug_trace.trace_event(store, subsystem="memory", type="memory.index.called",
                            detail={"counts": {"items": 50, "fetched": 2}})
    events = debug_trace.read_trace(store)
    assert [e["type"] for e in events] == ["memory.index.called", "route.decided"]  # newest first
    assert debug_trace.read_trace(store, subsystem="route")[0]["detail"]["mode"] == "agent_runtime"
    # Per-user opt-out still works.
    debug_trace.set_enabled(store, False)
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    assert len(debug_trace.read_trace(store)) == 2


def test_detail_is_size_bounded_metadata(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "1")
    debug_trace.set_enabled(store, True)
    debug_trace.trace_event(store, subsystem="memory", type="t", detail={"big": "x" * 9999})
    ev = debug_trace.read_trace(store)[0]
    assert len(ev["detail"]["big"]) <= 200  # caller content can't bloat the buffer


def test_trace_event_does_not_wait_for_slow_blob_storage(monkeypatch):
    store = _Store()
    monkeypatch.setattr(debug_trace, "is_enabled", lambda _store: True)
    monkeypatch.setattr(debug_trace, "verbose_enabled", lambda _store: False)

    def slow_get_blob(_uid, _kind):
        time.sleep(0.2)
        return {}

    def slow_set_blob(_uid, _kind, _doc):
        time.sleep(0.2)

    monkeypatch.setattr(debug_trace.db, "get_blob", slow_get_blob)
    monkeypatch.setattr(debug_trace.db, "set_blob", slow_set_blob)

    started = time.monotonic()
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    elapsed = time.monotonic() - started

    assert elapsed < 0.05


def test_debug_read_waits_for_worker_instead_of_becoming_a_second_writer(monkeypatch):
    """A read barrier must never steal queue items and race the worker's RMW."""
    uid = "usr_debug_read_barrier"
    isolated_queue: queue.Queue = queue.Queue()
    isolated_queue.put((uid, {"ts": time.time(), "type": "queued"}))
    monkeypatch.setattr(debug_trace, "_event_queue", isolated_queue)
    monkeypatch.setattr(debug_trace, "_pending_by_uid", {uid: 1})

    writes: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        debug_trace,
        "_append_events",
        lambda event_uid, events: writes.append((event_uid, events)),
    )

    debug_trace._flush_pending_for_user(uid, timeout=0.01)

    assert writes == []
    assert isolated_queue.qsize() == 1


# --- M1: explain / content_excerpt / dur_ms + verbose gate + caps -----------


class FakeStore:
    def __init__(self, uid="u1"):
        self.user_id = uid


def _reset_verbose(monkeypatch):
    """In-memory blob store + force gate ON."""
    blobs = {}
    monkeypatch.setattr(debug_trace.db, "get_blob", lambda uid, k: blobs.get((uid, k)))
    monkeypatch.setattr(debug_trace.db, "set_blob", lambda uid, k, v: blobs.__setitem__((uid, k), v))

    def append_events(uid, kind, new_events, *, cutoff_ts, max_events):
        current = blobs.get((uid, kind)) or {}
        events = list(current.get("events") or []) + list(new_events)
        events = [event for event in events if event.get("ts", 0) >= cutoff_ts]
        persisted = {"v": 1, "events": events[-max_events:]}
        blobs[(uid, kind)] = persisted
        return persisted

    monkeypatch.setattr(debug_trace.db, "append_blob_events_strict", append_events)
    monkeypatch.setattr(debug_trace, "_hard_disabled", lambda: False)
    debug_trace._flag_cache.clear()
    return blobs


def test_verbose_off_strips_content_excerpt(monkeypatch):
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.setenv("FEEDLING_DEBUG_VERBOSE", "0")  # force strip
    debug_trace.trace_event(store, subsystem="agent", type="agent.model.call.done",
                            explain="模型返回", content_excerpt={"reply": "hello"}, dur_ms=12.0)
    ev = debug_trace.read_trace(store, limit=10)[0]
    assert ev["explain"] == "模型返回"
    assert ev["dur_ms"] == 12.0
    assert ev.get("content_excerpt") in (None, {}, )  # stripped when verbose off


def test_content_excerpt_field_truncation(monkeypatch):
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.delenv("FEEDLING_DEBUG_VERBOSE", raising=False)  # verbose defaults ON with gate
    big = "x" * 5000
    debug_trace.trace_event(store, subsystem="agent", type="t",
                            content_excerpt={"prompt": big})
    ev = debug_trace.read_trace(store, limit=10)[0]
    assert len(ev["content_excerpt"]["prompt"]) <= 2048 + len("…(truncated)")
    assert ev["content_excerpt"]["prompt"].endswith("…(truncated)")


def test_verbose_ring_cap(monkeypatch):
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.delenv("FEEDLING_DEBUG_VERBOSE", raising=False)
    for i in range(260):
        debug_trace.trace_event(store, subsystem="route", type=f"t{i}")
    assert len(debug_trace.read_trace(store, limit=1000)) == 200  # verbose cap


class _FakeEnclaveResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


def _install_enclave_client(monkeypatch, outcome):
    class _FakeEnclaveClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(core_enclave.httpx, "Client", lambda **_kwargs: _FakeEnclaveClient())


def _capture_enclave_events(monkeypatch):
    events = []
    monkeypatch.setattr(
        core_enclave.debug_trace,
        "trace_event",
        lambda _store, **event: events.append(event),
    )
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    return events


def test_tee_replicate_success_does_not_write_enclave_trace(monkeypatch):
    events = _capture_enclave_events(monkeypatch)
    response = _FakeEnclaveResponse(body={"plaintext_b64": "cGxhaW50ZXh0"})
    _install_enclave_client(monkeypatch, response)

    plaintext = core_enclave._decrypt_envelope_via_enclave(
        {"owner_user_id": "usr_replicate", "body_ct": "ciphertext"},
        "api-key",
        purpose="tee_replicate:chat:msg_1",
    )

    assert plaintext == b"plaintext"
    assert events == []


@pytest.mark.parametrize(
    ("outcome", "expected_type"),
    [
        (_FakeEnclaveResponse(status_code=503, text="unavailable"), "enclave.call.error"),
        (core_enclave.httpx.ReadTimeout("timed out"), "enclave.call.timeout"),
    ],
)
def test_tee_replicate_failure_keeps_enclave_error_trace(monkeypatch, outcome, expected_type):
    events = _capture_enclave_events(monkeypatch)
    _install_enclave_client(monkeypatch, outcome)

    with pytest.raises(RuntimeError):
        core_enclave._decrypt_envelope_via_enclave(
            {"owner_user_id": "usr_replicate", "body_ct": "ciphertext"},
            "api-key",
            purpose="tee_replicate:memory",
        )

    assert [event["type"] for event in events] == [expected_type]
    assert events[0]["status"] == "error"
    assert events[0]["detail"]["purpose"] == "tee_replicate:memory"


def test_normal_decrypt_keeps_enclave_start_and_done_traces(monkeypatch):
    events = _capture_enclave_events(monkeypatch)
    response = _FakeEnclaveResponse(body={"plaintext_b64": "cGxhaW50ZXh0"})
    _install_enclave_client(monkeypatch, response)

    plaintext = core_enclave._decrypt_envelope_via_enclave(
        {"owner_user_id": "usr_chat", "body_ct": "ciphertext"},
        "api-key",
        purpose="chat:history",
    )

    assert plaintext == b"plaintext"
    assert [event["type"] for event in events] == ["enclave.call.start", "enclave.call.done"]
    assert all(event["detail"]["purpose"] == "chat:history" for event in events)


def test_flush_pending_waits_for_worker_in_flight_batch(monkeypatch):
    """Read-after-write consistency: unfinished_tasks must not hit zero while
    the worker still holds an un-appended batch (a concurrent reader would
    early-return stale DB), and a read must see the event once the worker's
    append + task_done complete. Deterministic: the append is gated."""
    import threading

    gate = threading.Event()
    entered = threading.Event()
    written: list = []
    real_wait = debug_trace._FLUSH_WAIT_SEC

    def blocking_append(uid, events):
        entered.set()
        assert gate.wait(5)
        written.extend(events)

    monkeypatch.setattr(debug_trace, "_append_events", blocking_append)
    monkeypatch.setattr(debug_trace, "_FLUSH_WAIT_SEC", 0.01)  # tight batch window
    try:
        debug_trace._enqueue("usr_dbg_race", {"ts": time.time(), "type": "unit.race"})
        assert entered.wait(5)  # worker popped the item and is mid-append (pre-task_done)

        t0 = time.monotonic()
        debug_trace._flush_pending_for_user("usr_dbg_race", timeout=0.2)
        blocked_wait = time.monotonic() - t0
        # Must NOT early-return while the append is in flight.
        assert blocked_wait >= 0.15
        assert not written

        gate.set()
        t1 = time.monotonic()
        debug_trace._flush_pending_for_user("usr_dbg_race", timeout=2.0)
        # Returns promptly once the worker acked, and the event is visible.
        assert time.monotonic() - t1 < 1.0
        assert written and written[0]["type"] == "unit.race"
        assert debug_trace._event_queue.unfinished_tasks == 0
    finally:
        gate.set()  # never leave the daemon worker blocked past this test
        monkeypatch.setattr(debug_trace, "_FLUSH_WAIT_SEC", real_wait)
