"""Request-local, content-free timing of the authenticated index read.

queue_ms ends when run_db's callable starts; core_ms covers that callable,
including upstream_ms (the sum of its synchronous enclave calls). dur_ms ends
at route completion, before telemetry: return scheduling/response construction
can make it greater than queue_ms + core_ms. Missing/incomplete measurements
are null, not zero. No shared threadpool policy or cancellation is changed.
"""
from __future__ import annotations

import math
import threading
import time

import debug_trace


def _now():
    try:
        value = time.monotonic()
        return value if math.isfinite(value) else None
    except Exception:
        return None


def _milliseconds(start, end):
    if start is None or end is None or end < start:
        return None
    return round((end - start) * 1000, 1)


class IndexTiming:
    def __init__(self, *, lane: str, request_id: str):
        self.lane = lane
        self.request_id = request_id
        self.started = _now()
        self.lock = threading.Lock()
        self.core_start = None
        self.core_end = None
        self.core_state = "not_started"
        self.upstream_calls = 0
        self.upstream_active = False
        self.upstream_ms = 0.0
        self.upstream_state = "not_called"

    def run_core(self, fn, *args, **kwargs):
        with self.lock:
            self.core_start = _now()
            self.core_state = "running"
        state = "failed"
        try:
            result = fn(*args, **kwargs)
            state = "completed"
            return result
        finally:
            with self.lock:
                self.core_end = _now()
                self.core_state = state

    def upstream(self, fn):
        def call(*args, **kwargs):
            with self.lock:
                self.upstream_calls += 1
                self.upstream_active = True
                start = _now()
            state = "failed"
            try:
                result = fn(*args, **kwargs)
                state = "completed"
                return result
            finally:
                with self.lock:
                    duration = _milliseconds(start, _now())
                    self.upstream_ms = (
                        self.upstream_ms + duration
                        if duration is not None and self.upstream_ms is not None
                        else None
                    )
                    self.upstream_active = False
                    # Preserve an earlier failed call if a later one succeeds.
                    if self.upstream_state != "failed":
                        self.upstream_state = state
        return call

    def emit(self, store, *, status: str, http_status: int | None):
        try:
            ended = _now()
            with self.lock:
                segments = {
                    "queue_ms": _milliseconds(self.started, self.core_start),
                    "core_ms": _milliseconds(self.core_start, self.core_end),
                    "core_state": self.core_state,
                    "upstream_ms": (
                        self.upstream_ms
                        if self.upstream_calls and not self.upstream_active
                        else None
                    ),
                    "upstream_calls": self.upstream_calls,
                    "upstream_state": (
                        "running" if self.upstream_active else self.upstream_state
                    ),
                }
            debug_trace.trace_event(
                store, subsystem="memory", type="memory.index.timing",
                status=status, trace_id=self.request_id,
                dur_ms=_milliseconds(self.started, ended),
                detail={"lane": self.lane, "http_status": http_status,
                        "segments": segments},
            )
        except Exception:
            # Instrumentation must neither replace an error nor change a reply.
            pass
