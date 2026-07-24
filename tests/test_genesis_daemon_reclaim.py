"""daemon.run_loop wires the death-detected reclaim (before the time reaper and
tick), threads worker_id into the claim, and feeds it the injected live workers."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from genesis import daemon  # noqa: E402
import genesis.worker as gw  # noqa: E402


def test_run_loop_reclaims_with_live_workers_before_tick(monkeypatch):
    order = []
    monkeypatch.setattr(gw, "reclaim_orphaned_processing_jobs",
                        lambda live: order.append(("reclaim", tuple(live))) or [])
    monkeypatch.setattr(gw, "reap_stale_processing_jobs",
                        lambda: order.append(("reap",)) or [])

    def _tick(*, api_url, enclave_url, mint_runtime_token, worker_id="", max_jobs=1):
        order.append(("tick", worker_id))
        return {}
    monkeypatch.setattr(gw, "tick", _tick)

    stop = threading.Event()
    beats = {"n": 0}

    def _beat():
        beats["n"] += 1
        if beats["n"] >= 2:   # beat runs before AND after one tick → stop after one loop
            stop.set()

    daemon.run_loop(api_url="", enclave_url="", mint_genesis=lambda *a, **k: "t",
                    interval=0.001, stop_event=stop, worker_id="w7:genesis",
                    list_live_workers=lambda: ["w7:genesis", "other:genesis"],
                    on_beat=_beat)

    assert ("reclaim", ("w7:genesis", "other:genesis")) in order
    assert ("tick", "w7:genesis") in order
    # reclaim runs before the time reaper, which runs before tick
    assert order.index(("reclaim", ("w7:genesis", "other:genesis"))) < order.index(("reap",))
    assert order.index(("reap",)) < order.index(("tick", "w7:genesis"))


def test_run_loop_without_live_workers_callable_passes_empty(monkeypatch):
    seen = {}
    monkeypatch.setattr(gw, "reclaim_orphaned_processing_jobs",
                        lambda live: seen.__setitem__("live", list(live)) or [])
    monkeypatch.setattr(gw, "reap_stale_processing_jobs", lambda: [])
    monkeypatch.setattr(gw, "tick", lambda **k: {})
    stop = threading.Event()
    n = {"i": 0}

    def _beat():
        n["i"] += 1
        if n["i"] >= 2:
            stop.set()

    daemon.run_loop(api_url="", enclave_url="", mint_genesis=lambda *a, **k: "t",
                    interval=0.001, stop_event=stop, on_beat=_beat)  # no list_live_workers
    assert seen["live"] == []
