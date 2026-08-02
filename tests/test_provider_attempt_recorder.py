"""Fail-open persistence for content-free provider-attempt events."""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import fields
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_attempt_accounting as accounting  # noqa: E402
from provider_attempt_accounting import (  # noqa: E402
    AttemptCompleteness,
    AttemptLane,
    AttemptOutcome,
    AttemptSource,
    AttemptState,
    ProviderAttemptEvent,
    ProviderAttemptRecorder,
)


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, tb):
        return False


class _Connection:
    def __init__(self, executions, *, failures=0):
        self.executions = executions
        self.failures = failures

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("database unavailable")

    def cursor(self):
        return _Context(_Cursor(self))


class _Cursor:
    def __init__(self, connection):
        self.connection = connection

    def executemany(self, sql, rows):
        self.connection.executions.append((sql, list(rows)))
        if self.connection.failures:
            self.connection.failures -= 1
            raise RuntimeError("database unavailable")


class _Pool:
    def __init__(self, connection):
        self.connection_value = connection

    def connection(self):
        return _Context(self.connection_value)


class _StoppedThread:
    def __init__(self, **_kwargs):
        self.started = False

    def start(self):
        self.started = True

    def join(self, _timeout=None):
        return None

    def is_alive(self):
        return False


def _event(*, state=AttemptState.STARTED, outcome=AttemptOutcome.UNKNOWN):
    return ProviderAttemptEvent.create(
        user_id="usr_1",
        call_id="call-1",
        outer_attempt_ordinal=0,
        inner_attempt_ordinal=0,
        source=AttemptSource.RUNTIME_RECORDER,
        lane=AttemptLane.CHAT,
        state=state,
        outcome=outcome,
        completeness=(
            AttemptCompleteness.STARTED_ONLY
            if state is AttemptState.STARTED
            else AttemptCompleteness.COMPLETE
        ),
        requested_provider="openai",
        requested_model="gpt-test",
        resolved_provider="openai",
        resolved_model="gpt-test",
        transport="responses",
    )


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("recorder did not finish before its bounded timeout")


def test_record_enqueues_without_pool_access_or_blocking():
    """A hot-path record must end at put_nowait, before any RDS work begins."""
    pool_calls = 0

    def pool_factory():
        nonlocal pool_calls
        pool_calls += 1
        raise AssertionError("hot path must not construct a database pool")

    recorder = ProviderAttemptRecorder(
        pool_factory=pool_factory,
        thread_factory=lambda **kwargs: _StoppedThread(**kwargs),
    )

    assert recorder.record(_event()) is None
    assert recorder.queue_size == 1
    assert pool_calls == 0
    assert recorder.shutdown(timeout=0) is True


def test_record_never_starts_a_worker_on_the_provider_hot_path():
    """A blocking thread start after enqueue must not delay a provider caller."""
    starts = []

    class _BlockingStartThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            starts.append(True)
            time.sleep(0.2)

        def join(self, _timeout=None):
            return None

        def is_alive(self):
            return False

    recorder = ProviderAttemptRecorder(
        thread_factory=lambda **kwargs: _BlockingStartThread(**kwargs),
    )
    began = time.monotonic()
    assert recorder.record(_event()) is None

    assert time.monotonic() - began < 0.05
    assert starts == []
    assert recorder.queue_size == 1


def test_singleton_record_never_starts_a_worker_on_the_provider_hot_path(monkeypatch):
    """The singleton wrapper has the same no-start latency guarantee as direct use."""
    starts = []

    class _BlockingStartThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            starts.append(True)
            time.sleep(0.2)

        def join(self, _timeout=None):
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr(accounting, "_recorder", None)
    monkeypatch.setattr(accounting.threading, "Thread", _BlockingStartThread)
    began = time.monotonic()
    assert accounting.record_provider_attempt(_event()) is None

    assert time.monotonic() - began < 0.05
    assert starts == []


def test_logger_and_counter_failures_cannot_escape_direct_record(monkeypatch):
    """A broken diagnostic path must not turn a telemetry drop into a call failure."""
    recorder = ProviderAttemptRecorder(
        queue_capacity=1,
        thread_factory=lambda **kwargs: _StoppedThread(**kwargs),
    )
    assert recorder.record(_event()) is None
    monkeypatch.setattr(
        accounting.log,
        "warning",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("logger failed")),
    )

    assert recorder.record(_event()) is None

    counter_failure = ProviderAttemptRecorder(
        queue_capacity=1,
        thread_factory=lambda **kwargs: _StoppedThread(**kwargs),
    )
    assert counter_failure.record(_event()) is None

    class _ExplodingCounter:
        def __iadd__(self, _value):
            raise RuntimeError("counter failed")

    counter_failure._dropped_count = _ExplodingCounter()
    assert counter_failure.record(_event()) is None


def test_direct_dataclass_construction_rejects_unsafe_metadata():
    """Bypassing create() cannot bypass the content-free identifier contract."""
    event = _event()
    direct_values = {field.name: getattr(event, field.name) for field in fields(event)}
    direct_values["requested_model"] = "Bearer secret"

    try:
        ProviderAttemptEvent(**direct_values)
    except ValueError as exc:
        assert str(exc) == "invalid_requested_model"
    else:
        raise AssertionError("direct dataclass construction accepted unsafe metadata")


def test_forged_event_is_dropped_before_cursor_execution():
    """Persistence revalidates a mutated frozen object before its data reaches SQL."""
    executions = []
    recorder = ProviderAttemptRecorder(
        pool_factory=lambda: _Pool(_Connection(executions)),
        batch_size=1,
        flush_interval=0.01,
        reconcile_interval=3600,
    )
    event = _event()
    object.__setattr__(event, "requested_model", "Bearer secret")

    assert recorder.record(event) is None
    recorder._ensure_worker()
    _wait_until(lambda: recorder.dropped_count == 1)

    assert executions == []
    assert recorder.shutdown(timeout=0.5) is True


def test_shutdown_never_succeeds_while_thread_start_is_still_gated():
    """Shutdown must await startup publication or report its bounded timeout."""
    entered_start = threading.Event()
    release_start = threading.Event()

    class _GatedThread:
        def __init__(self, **_kwargs):
            self.alive = False

        def start(self):
            entered_start.set()
            release_start.wait()
            self.alive = True

        def join(self, _timeout=None):
            self.alive = False

        def is_alive(self):
            return self.alive

    recorder = ProviderAttemptRecorder(
        thread_factory=lambda **kwargs: _GatedThread(**kwargs),
    )
    result = []
    starter = threading.Thread(target=lambda: result.append(recorder.start()))
    starter.start()
    assert entered_start.wait(0.5)

    try:
        assert recorder.shutdown(timeout=0.01) is False
    finally:
        release_start.set()
        starter.join(0.5)
    assert result == [False]
    assert recorder.shutdown(timeout=0.1) is True


def test_repeated_start_and_shutdown_publish_at_most_one_live_worker():
    """Lifecycle calls are idempotent and shutdown fences every later start."""
    factories = []

    class _LiveThread:
        def __init__(self, **_kwargs):
            self.alive = False

        def start(self):
            self.alive = True

        def join(self, _timeout=None):
            self.alive = False

        def is_alive(self):
            return self.alive

    def thread_factory(**kwargs):
        factories.append(kwargs)
        return _LiveThread(**kwargs)

    recorder = ProviderAttemptRecorder(thread_factory=thread_factory)
    assert recorder.start() is True
    assert recorder.start() is True
    assert len(factories) == 1
    assert recorder.shutdown(timeout=0.1) is True
    assert recorder.shutdown(timeout=0.1) is True
    assert recorder.start() is False
    assert len(factories) == 1


def test_shutdown_waits_for_a_gated_factory_failure_before_succeeding():
    """An unreturned factory is an in-flight start, even if it later fails."""
    entered_factory = threading.Event()
    release_factory = threading.Event()

    def thread_factory(**_kwargs):
        entered_factory.set()
        release_factory.wait()
        raise RuntimeError("factory failed")

    recorder = ProviderAttemptRecorder(thread_factory=thread_factory)
    result = []
    starter = threading.Thread(target=lambda: result.append(recorder.start()))
    starter.start()
    assert entered_factory.wait(0.5)

    try:
        assert recorder.shutdown(timeout=0.01) is False
    finally:
        release_factory.set()
        starter.join(0.5)
    assert result == [False]
    assert recorder.shutdown(timeout=0.1) is True


def test_full_queue_drops_event_without_raising_or_growing_memory():
    """A full bounded queue drops telemetry instead of delaying a provider call."""
    recorder = ProviderAttemptRecorder(
        queue_capacity=1,
        thread_factory=lambda **kwargs: _StoppedThread(**kwargs),
    )

    assert recorder.record(_event()) is None
    assert recorder.record(_event()) is None
    assert recorder.queue_size == 1
    assert recorder.dropped_count == 1
    assert recorder.shutdown(timeout=0) is True


def test_explicit_bootstrap_starts_one_worker_for_many_records():
    """The off-hot-path bootstrap starts one daemon for queued provider facts."""
    executions = []
    starts = []

    def thread_factory(**kwargs):
        starts.append(kwargs)
        return threading.Thread(**kwargs)

    recorder = ProviderAttemptRecorder(
        pool_factory=lambda: _Pool(_Connection(executions)),
        thread_factory=thread_factory,
        batch_size=8,
        flush_interval=0.01,
        reconcile_interval=3600,
    )

    assert recorder.start() is True
    assert recorder.record(_event()) is None
    assert recorder.record(_event()) is None
    _wait_until(lambda: len(executions) >= 1)

    assert len(starts) == 1
    assert recorder.shutdown(timeout=0.5) is True


def test_worker_batches_queued_events_into_one_full_row_upsert():
    """Several queued facts become one batch write, never one pool use per event."""
    executions = []
    connection = _Connection(executions)
    recorder = ProviderAttemptRecorder(
        pool_factory=lambda: _Pool(connection),
        batch_size=8,
        flush_interval=0.01,
        reconcile_interval=3600,
    )

    recorder._queue.put_nowait(_event())
    recorder._queue.put_nowait(
        _event(state=AttemptState.COMPLETED, outcome=AttemptOutcome.SUCCEEDED)
    )
    recorder._ensure_worker()
    _wait_until(lambda: len(executions) == 1)

    sql, rows = executions[0]
    assert "INSERT INTO llm_provider_attempts" in sql
    assert "ON CONFLICT (attempt_id) DO UPDATE" in sql
    assert "CASE WHEN llm_provider_attempts.state = 'completed'" in sql
    assert len(rows) == 2
    assert rows[1]["state"] == "completed"
    assert rows[1]["outcome"] == "succeeded"
    assert recorder.shutdown(timeout=0.5) is True


def test_completed_event_recovers_missing_start_and_replay_stays_idempotent():
    """A terminal fact is independently durable and cannot be undone by a replay."""
    executions = []
    recorder = ProviderAttemptRecorder(
        pool_factory=lambda: _Pool(_Connection(executions)),
        batch_size=1,
        flush_interval=0.01,
        reconcile_interval=3600,
    )

    completed = _event(state=AttemptState.COMPLETED, outcome=AttemptOutcome.FAILED)
    assert recorder.start() is True
    assert recorder.record(completed) is None
    _wait_until(lambda: len(executions) == 1)
    assert recorder.record(completed) is None
    _wait_until(lambda: len(executions) == 2)

    first_rows = executions[0][1]
    replay_rows = executions[1][1]
    assert first_rows == replay_rows == [completed.as_row()]
    assert "finished_at = CASE" in executions[0][0]
    assert recorder.shutdown(timeout=0.5) is True


def test_worker_retries_database_failure_with_bounded_backoff():
    """A transient write failure retries off-path, without unbounded spinning."""
    executions = []
    connection = _Connection(executions, failures=1)
    delays = []
    recorder = ProviderAttemptRecorder(
        pool_factory=lambda: _Pool(connection),
        batch_size=1,
        flush_interval=0.01,
        max_retries=2,
        retry_backoff=0.01,
        sleeper=delays.append,
        reconcile_interval=3600,
    )

    assert recorder.start() is True
    assert recorder.record(_event()) is None
    _wait_until(lambda: len(executions) == 2)

    assert delays == [0.01]
    assert recorder.dropped_count == 0
    assert recorder.shutdown(timeout=0.5) is True


def test_background_reconciliation_marks_only_stale_started_rows():
    """Possibly-billed recovery is a recorder-worker query, not a call-path query."""
    executions = []
    recorder = ProviderAttemptRecorder(
        pool_factory=lambda: _Pool(_Connection(executions)),
        batch_size=1,
        flush_interval=0.01,
        reconcile_interval=0,
    )

    assert recorder.start() is True
    assert recorder.record(_event()) is None
    _wait_until(lambda: len(executions) >= 2)

    reconcile_sql = executions[1][0]
    assert "SET possibly_billed = TRUE" in reconcile_sql
    assert "state = 'started'" in reconcile_sql
    assert "finished_at IS NULL" in reconcile_sql
    assert recorder.shutdown(timeout=0.5) is True


def test_all_hot_path_failures_are_fail_open(monkeypatch):
    """Telemetry failures never escape or change the caller-visible return value."""
    recorder = ProviderAttemptRecorder(
        thread_factory=lambda **kwargs: _StoppedThread(**kwargs),
    )
    monkeypatch.setattr(
        recorder._queue,
        "put_nowait",
        lambda _event: (_ for _ in ()).throw(RuntimeError("queue failed")),
    )

    assert recorder.record(_event()) is None
    assert recorder.dropped_count == 1
    assert recorder.shutdown(timeout=0) is True


def test_startup_serialization_pool_and_sql_failures_are_all_contained():
    """Each recorder-side failure is dropped/logged; provider work never observes it."""
    class _BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

        def join(self, _timeout=None):
            return None

        def is_alive(self):
            return False

    startup_failure = ProviderAttemptRecorder(thread_factory=lambda **kwargs: _BrokenThread(**kwargs))
    assert startup_failure.record(_event()) is None
    assert startup_failure.start() is False
    assert startup_failure.dropped_count == 1

    pool_failure = ProviderAttemptRecorder(
        pool_factory=lambda: (_ for _ in ()).throw(RuntimeError("pool failed")),
        flush_interval=0.01,
        max_retries=0,
    )
    assert pool_failure.record(_event()) is None
    assert pool_failure.start() is True
    _wait_until(lambda: pool_failure.dropped_count == 1)
    assert pool_failure.shutdown(timeout=0.5) is True

    class _BadEvent:
        def as_row(self):
            raise RuntimeError("serialization failed")

    serialization_failure = ProviderAttemptRecorder(
        pool_factory=lambda: (_ for _ in ()).throw(AssertionError("must not serialize to RDS")),
        flush_interval=0.01,
        max_retries=0,
    )
    assert serialization_failure.record(_BadEvent()) is None
    assert serialization_failure.start() is True
    _wait_until(lambda: serialization_failure.dropped_count == 1)
    assert serialization_failure.shutdown(timeout=0.5) is True

    sql_failure = ProviderAttemptRecorder(
        pool_factory=lambda: _Pool(_Connection([], failures=1)),
        flush_interval=0.01,
        max_retries=0,
    )
    assert sql_failure.record(_event()) is None
    assert sql_failure.start() is True
    _wait_until(lambda: sql_failure.dropped_count == 1)
    assert sql_failure.shutdown(timeout=0.5) is True
