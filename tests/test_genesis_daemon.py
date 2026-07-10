import threading

import pytest

import genesis
from genesis import daemon


@pytest.mark.parametrize("enabled,secret,url,expected", [
    ("1", "s", "https://e", True),
    ("true", "s", "https://e", True),
    ("on", "s", "https://e", True),
    ("", "s", "https://e", False),
    ("0", "s", "https://e", False),
    ("false", "s", "https://e", False),
    ("1", "", "https://e", False),
    ("1", "s", "", False),
    ("1", "   ", "https://e", False),
])
def test_should_start(enabled, secret, url, expected):
    assert daemon.should_start(enabled=enabled, secret=secret, enclave_url=url) is expected


def test_run_loop_beats_before_and_after_each_tick(monkeypatch):
    beats = []
    calls = []

    class _FakeWorker:
        @staticmethod
        def reap_stale_processing_jobs():
            calls.append("reap")

        @staticmethod
        def tick(**kw):
            calls.append("tick")

    # `run_loop` does `from genesis import worker` INSIDE the function body.
    # monkeypatch.setitem(sys.modules, "genesis.worker", ...) does NOT reliably
    # intercept this: by the time this test runs, some other test module in the
    # full suite (e.g. tests/test_genesis_worker.py) has already executed
    # `from genesis import worker` at collection time, which binds a `worker`
    # attribute directly onto the `genesis` package object. CPython's
    # IMPORT_FROM opcode does getattr(package, name) FIRST and only falls back
    # to sys.modules on AttributeError — so once that attribute exists, setitem
    # on sys.modules is silently ignored and the REAL genesis.worker.tick runs
    # (verified empirically: it hangs trying to hit the DB). Patching the
    # package attribute directly is the mechanism that actually works both
    # before and after the attribute has been bound.
    monkeypatch.setattr(genesis, "worker", _FakeWorker, raising=False)

    stop = threading.Event()

    def _beat():
        beats.append(len(calls))
        if len(beats) >= 2:
            stop.set()

    daemon.run_loop(api_url="a", enclave_url="e", mint_genesis=lambda *a, **k: "t",
                    interval=0, stop_event=stop, on_beat=_beat)

    assert calls == ["reap", "tick"]
    assert beats == [0, 2]     # beat before the tick (0 calls) and after (2 calls)


def test_run_loop_survives_a_beat_failure(monkeypatch):
    """A liveness-write failure must not kill the loop it observes."""
    ticks = []

    class _FakeWorker:
        @staticmethod
        def reap_stale_processing_jobs():
            pass

        @staticmethod
        def tick(**kw):
            ticks.append(1)

    monkeypatch.setattr(genesis, "worker", _FakeWorker, raising=False)
    stop = threading.Event()

    def _boom():
        if len(ticks) >= 1:
            stop.set()
        raise RuntimeError("db down")

    daemon.run_loop(api_url="a", enclave_url="e", mint_genesis=lambda *a, **k: "t",
                    interval=0, stop_event=stop, on_beat=_boom)

    assert ticks == [1]


def test_run_loop_survives_a_tick_failure(monkeypatch):
    beats = []

    class _FakeWorker:
        @staticmethod
        def reap_stale_processing_jobs():
            raise RuntimeError("pg gone")

        @staticmethod
        def tick(**kw):
            raise AssertionError("must not be reached")

    monkeypatch.setattr(genesis, "worker", _FakeWorker, raising=False)
    stop = threading.Event()

    def _beat():
        beats.append(1)
        if len(beats) >= 2:
            stop.set()

    daemon.run_loop(api_url="a", enclave_url="e", mint_genesis=lambda *a, **k: "t",
                    interval=0, stop_event=stop, on_beat=_beat)

    assert len(beats) == 2   # beat-before + beat-after still ran despite the raise
