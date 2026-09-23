"""Request-local, content-free timings for the admin users read.

Only users_payload activates collection. Pool/connection/cursor facades retain
no SQL, arguments or results beyond the underlying driver's normal lifetime.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
import time


_current: ContextVar[Timings | None] = ContextVar("admin_read_timings", default=None)


class Timings:
    def __init__(self):
        self.seconds = {"pool_wait_ms": 0.0, "sql_ms": 0.0, "python_assembly_ms": 0.0}
        self.active = None
        self.since = time.monotonic()

    def _switch(self, name):
        now = time.monotonic()
        if self.active is not None:
            self.seconds[self.active] += now - self.since
        self.since = now
        self.active = name

    @contextmanager
    def stage(self, name):
        previous = self.active
        self._switch(name)
        try:
            yield
        finally:
            self._switch(previous)

    def stages_ms(self, *, total_ms: int, queue_ms: int) -> dict[str, int]:
        stages = {"queue_ms": queue_ms}
        stages.update({key: int(value * 1000) for key, value in self.seconds.items()})
        # Retain the residual (including integer truncation), never distribute it.
        stages["unaccounted_ms"] = total_ms - sum(stages.values())
        return stages


@contextmanager
def collect():
    timings = Timings()
    token = _current.set(timings)
    try:
        yield timings
    finally:
        _current.reset(token)


def python_assembly(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        timings = _current.get()
        if timings is None:
            return fn(*args, **kwargs)
        # Payload wall time excluding pool acquisition, driver execute/fetch and lease cleanup; not CPU time.
        with timings.stage("python_assembly_ms"):
            return fn(*args, **kwargs)
    return wrapped


def wrap_pool(pool):
    timings = _current.get()
    return pool if timings is None else _Pool(pool, timings)


class _Pool:
    def __init__(self, pool, timings):
        self._pool, self._timings = pool, timings

    def __getattr__(self, name):
        return getattr(self._pool, name)

    @contextmanager
    def connection(self, *args, **kwargs):
        parent = self._timings.active
        # Only lease entry is pool wait; lease exit/commit/return remains unaccounted.
        with self._timings.stage(None):
            with ExitStack() as stack:
                with self._timings.stage("pool_wait_ms"):
                    conn = stack.enter_context(self._pool.connection(*args, **kwargs))
                with self._timings.stage(parent):
                    yield _Connection(conn, self._timings)


class _Connection:
    def __init__(self, conn, timings):
        self._conn, self._timings = conn, timings

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, *args, **kwargs):
        with self._timings.stage("sql_ms"):
            cursor = self._conn.execute(*args, **kwargs)
        return _Cursor(cursor, self._timings)

    def cursor(self, *args, **kwargs):
        return _Cursor(self._conn.cursor(*args, **kwargs), self._timings)


class _Cursor:
    def __init__(self, cursor, timings):
        self._cursor, self._timings = cursor, timings

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)

    def execute(self, *args, **kwargs):
        with self._timings.stage("sql_ms"):
            self._cursor.execute(*args, **kwargs)
        return self

    def __iter__(self):
        return self

    def __next__(self):
        # Time each driver step, including exhaustion, never the consumer body.
        with self._timings.stage("sql_ms"):
            return next(self._cursor)

    def fetchmany(self, *args, **kwargs):
        with self._timings.stage("sql_ms"):
            return self._cursor.fetchmany(*args, **kwargs)

    def fetchone(self):
        with self._timings.stage("sql_ms"):
            return self._cursor.fetchone()

    def fetchall(self):
        with self._timings.stage("sql_ms"):
            return self._cursor.fetchall()
