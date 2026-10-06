"""T779 step 2c PR-1: the readings the shadow window will be judged on.

Memory comes from the processes themselves through the foreground fleet
heartbeat (no shell on test); the parent encoder counts every query outcome, so
its worst wait is not taken from successes only; shadow events carry the same
turn coordinates as memory.recall.completed plus clearly scoped timings.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from memory.embedding import query_service  # noqa: E402
from model_api_runtime.v2 import process_memory, worker  # noqa: E402


# --------------------------------------------------------------------------- #
# process_memory: parsing, units, and "unreadable is None, never 0"
# --------------------------------------------------------------------------- #

def test_proc_fields_are_parsed_and_missing_files_are_none(tmp_path, monkeypatch):
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmRSS:\t  123456 kB\nThreads:\t9\n")
    rollup = tmp_path / "smaps_rollup"
    rollup.write_text("Rss:  123456 kB\nPss:   98765 kB\n")
    assert process_memory._field_kb(str(status), "VmRSS") == 123456
    assert process_memory._field_kb(str(rollup), "Pss") == 98765
    assert process_memory._field_kb(str(tmp_path / "absent"), "VmRSS") is None
    bad = tmp_path / "bad"
    bad.write_text("VmRSS:\tlots kB\n")
    assert process_memory._field_kb(str(bad), "VmRSS") is None
    assert process_memory.process(None) == {"rss_kb": None, "pss_kb": None}


def test_cgroup_bytes_become_kilobytes_with_v1_fallback(tmp_path, monkeypatch):
    v2 = tmp_path / "memory.current"
    v1 = tmp_path / "usage_in_bytes"
    monkeypatch.setattr(process_memory, "_CGROUP_FILES", (str(v2), str(v1)))
    assert process_memory.cgroup_kb() is None
    v1.write_text("2097152\n")
    assert process_memory.cgroup_kb() == 2048                 # v1 used when v2 is absent
    v2.write_text("1048576\n")
    assert process_memory.cgroup_kb() == 1024                 # bytes -> kB, v2 preferred


def test_this_process_reads_real_values_where_proc_exists():
    reading = process_memory.process(os.getpid())
    if Path("/proc/self/status").exists():
        assert isinstance(reading["rss_kb"], int) and reading["rss_kb"] > 0
    else:
        assert reading == {"rss_kb": None, "pss_kb": None}   # macOS: no /proc, no invented 0


# --------------------------------------------------------------------------- #
# the foreground fleet heartbeat actually written
# --------------------------------------------------------------------------- #

class _Key:
    def __init__(self, pool, index):
        self.pool, self.index = pool, index


class _Fleet:
    def __init__(self, pids):
        self._keys = [_Key("foreground", i) for i in range(len(pids))]
        self._sup = {k: types.SimpleNamespace(child_pid=(lambda p=p: p)) for k, p in zip(self._keys, pids)}

    def keys(self):
        return tuple(self._keys)

    def supervisor(self, key):
        return self._sup[key]

    def healthy_capacity(self, pool, stale_sec):
        return len(self._keys)

    def snapshots(self):
        return {k: None for k in self._keys}

    def broker_snapshot(self):
        return {"granted": {}}


@pytest.fixture(autouse=True)
def _no_pending_memory_read(monkeypatch):
    from model_api_runtime.v2 import serve_worker
    monkeypatch.setattr(serve_worker, "_memory_read_pending", None)


def _one_heartbeat(monkeypatch, fleet, pool="foreground"):
    from model_api_runtime.v2 import serve_worker
    written = []
    stop = asyncio.Event()

    def record(worker_id, **kwargs):
        written.append((worker_id, kwargs))
        stop.set()
    monkeypatch.setattr(serve_worker.jobs_store, "record_worker_heartbeat", record)
    monkeypatch.setattr(serve_worker.jobs_store, "trajectory_review_enabled", lambda: False)
    monkeypatch.setattr(serve_worker.db, "get_pool", lambda: types.SimpleNamespace(
        get_stats=lambda: {"pool_size": 2, "pool_available": 1}))
    loop = serve_worker._fleet_heartbeat_loop("w1", pool, fleet, stop, interval=0.01)
    try:
        # A heartbeat that is never written must fail here, not hang the suite.
        asyncio.run(asyncio.wait_for(loop, timeout=5))
    except asyncio.TimeoutError:
        pytest.fail("no heartbeat row was written")
    return written[0][1]["runtime_state"]


def test_the_foreground_row_carries_one_memory_sample_and_encoder_status(monkeypatch):
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "abcdef1234567890")
    state = _one_heartbeat(monkeypatch, _Fleet([None, os.getpid()]))
    memory = state["memory"]
    assert set(memory) == {"sampled_at", "release", "parent", "slots", "cgroup_kb"}
    assert memory["release"] == "abcdef123456"
    assert memory["parent"]["pid"] == os.getpid()
    assert [s["slot"] for s in memory["slots"]] == ["foreground:0", "foreground:1"]
    assert memory["slots"][0] == {"slot": "foreground:0", "pid": None, "rss_kb": None, "pss_kb": None}
    assert abs(memory["sampled_at"] - time.time()) < 60
    assert state["query_encoder"]["serving"] is False and state["query_encoder"]["scheduler"] is None
    assert state["embedding_model"]["state"] in {"not_loaded", "ready", "unavailable", "failed"}


def test_a_failed_reading_never_costs_the_heartbeat_row(monkeypatch):
    class _NoSupervisors(_Fleet):
        def supervisor(self, key):
            raise AttributeError("no supervisor")
    def broken_status():
        raise RuntimeError("status unavailable")
    monkeypatch.setattr(query_service, "status", broken_status)
    state = _one_heartbeat(monkeypatch, _NoSupervisors([os.getpid()]))
    assert state["memory"] == {"unavailable": "AttributeError"}
    assert state["query_encoder"] == {"unavailable": "RuntimeError"}
    assert "state" in state["embedding_model"]
    assert state["slots"]["configured"] == 1                  # the rest of the row is intact


def test_other_pool_rows_do_not_repeat_the_sample(monkeypatch):
    state = _one_heartbeat(monkeypatch, _Fleet([os.getpid()]), pool="wake")
    assert "memory" not in state and "query_encoder" not in state and "embedding_model" not in state


def test_a_slow_procfs_read_never_stalls_the_loop_or_piles_up_threads(monkeypatch):
    """A blocked /proc read: other coroutines keep running, the foreground row is
    still written (memory marked timeout, then busy), only one reader thread
    exists, and the late result is never written as a fresh sample."""
    from model_api_runtime.v2 import serve_worker
    gate, reads = threading.Event(), []
    real = process_memory.process

    def slow(pid):
        reads.append(pid)
        if len(reads) == 1:
            gate.wait(10)
        return real(pid)
    monkeypatch.setattr(serve_worker.v2_process_memory, "process", slow)
    monkeypatch.setattr(serve_worker, "_MEMORY_READ_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(serve_worker.jobs_store, "trajectory_review_enabled", lambda: False)
    monkeypatch.setattr(serve_worker.db, "get_pool", lambda: types.SimpleNamespace(
        get_stats=lambda: {"pool_size": 2, "pool_available": 1}))
    rows, ticks = [], []
    monkeypatch.setattr(serve_worker.jobs_store, "record_worker_heartbeat",
                        lambda worker_id, **kw: rows.append((time.time(), kw["runtime_state"])))

    async def other_work(stop):
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    async def go():
        stop = asyncio.Event()
        fleet = _Fleet([os.getpid()])
        tasks = [asyncio.create_task(serve_worker._fleet_heartbeat_loop(
            "w1", "foreground", fleet, stop, interval=0.02)), asyncio.create_task(other_work(stop))]
        await asyncio.sleep(0.3)                      # the first read stays blocked
        blocked_rows = len(rows)
        threads = [t for t in threading.enumerate() if t.name == "v2-heartbeat-memory"]
        released_at = time.time()
        gate.set()
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.gather(*tasks)
        return blocked_rows, threads, released_at

    blocked_rows, threads, released_at = asyncio.run(go())
    assert blocked_rows >= 3 and len(ticks) > 20        # heartbeat and other work kept going
    assert len(threads) == 1                            # repeated ticks did not stack readers
    early = [state["memory"] for at, state in rows[:blocked_rows]]
    assert early[0] == {"unavailable": "reading_timeout"}
    assert set(map(str, early[1:])) == {str({"unavailable": "reading_busy"})}
    live = [state for at, state in rows[blocked_rows:] if "memory" in state]   # not the exit row
    fresh = [state["memory"] for state in live if "sampled_at" in state["memory"]]
    assert fresh, "a new sample follows once the reader is free"
    assert all(m["sampled_at"] >= round(released_at, 1) - 0.1 for m in fresh)   # late one dropped


# --------------------------------------------------------------------------- #
# the model's state, read without loading it
# --------------------------------------------------------------------------- #

def test_model_state_never_builds_the_model_or_waits_on_the_load_lock(monkeypatch):
    from memory.embedding import e5_onnx, sweep
    monkeypatch.setattr(e5_onnx, "E5SmallOnnxEmbedder",
                        lambda *a, **k: pytest.fail("model_state built the model"))
    monkeypatch.setattr(sweep, "_embedder", None)
    monkeypatch.setattr(sweep, "_loading", False)
    monkeypatch.setattr(sweep, "_load_error", None)
    assert sweep.model_state() == {"state": "not_loaded", "owner_pid": None, "model_id": None,
                                   "load_seconds": None, "reason": None}
    with sweep._embedder_lock:                           # a load holds the lock
        monkeypatch.setattr(sweep, "_loading", True)
        done = []
        reader = threading.Thread(target=lambda: done.append(sweep.model_state()))
        reader.start()
        reader.join(1)
        assert done and done[0]["state"] == "loading" and done[0]["owner_pid"] == os.getpid()


def test_model_state_reports_ready_unavailable_and_failed(monkeypatch):
    from memory.embedding import e5_onnx, query_service as qs, sweep
    full_id = "intfloat/multilingual-e5-small:int8:" + "a" * 64 + ":p1:mean-l2-512-v1"
    monkeypatch.setattr(sweep, "_loading", False)
    monkeypatch.setattr(sweep, "_embedder", types.SimpleNamespace(
        available=True, model_id=full_id, load_seconds=3.14159, unavailable_reason=""))
    ready = sweep.model_state()                           # e.g. mode off, loaded by the sweep
    assert ready == {"state": "ready", "owner_pid": os.getpid(), "model_id": full_id,
                     "load_seconds": 3.14, "reason": None}
    monkeypatch.setattr(sweep, "_embedder", types.SimpleNamespace(
        available=False, model_id="x:unloaded", load_seconds=0.0,
        unavailable_reason="model_files_missing"))
    assert sweep.model_state()["state"] == "unavailable"
    assert sweep.model_state()["reason"] == "model_files_missing"

    def broken(*a, **k):
        raise MemoryError("oom")
    monkeypatch.setattr(sweep, "_embedder", None)
    monkeypatch.setattr(e5_onnx, "E5SmallOnnxEmbedder", broken)
    monkeypatch.setattr(qs, "assert_embedder_owner", lambda: None)
    with pytest.raises(MemoryError):
        sweep.get_embedder()
    assert sweep.model_state() == {"state": "failed", "owner_pid": None, "model_id": None,
                                   "load_seconds": None, "reason": "MemoryError"}
    assert sweep._loading is False


# --------------------------------------------------------------------------- #
# encoder counters: every outcome, waits including expired queries
# --------------------------------------------------------------------------- #

class _Gated:
    model_id, dim = "gated", 4

    def __init__(self):
        self.release = {}

    def _gate(self, key):
        self.release.setdefault(key, threading.Event()).wait(5)

    def encode_query(self, text):
        self._gate(("q", text))
        return [1.0, 0.0, 0.0, 0.0]

    def encode_passages(self, texts):
        self._gate(("s", tuple(texts)))
        return [[0.0, 1.0, 0.0, 0.0] for _ in texts]


def _wait(cond, n=500):
    for _ in range(n):
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_scheduler_counts_every_outcome_and_waits_include_expired():
    emb = _Gated()
    sched = query_service.Scheduler(emb)
    try:
        seg = threading.Thread(target=lambda: sched.encode_segment(["a"]))
        seg.start()
        assert _wait(lambda: sched._running_kind == "segment")
        late = sched.submit_query(["late"], time.monotonic() + 0.05)        # behind the segment
        kept = sched.submit_query(["kept"], time.monotonic() + 5)
        fillers = [sched.submit_query([f"f{i}"], time.monotonic() + 5) for i in range(2)]
        with pytest.raises(query_service.Refused):
            sched.submit_query(["over"], time.monotonic() + 5)
        time.sleep(0.1)                                                    # "late" expires while queued
        emb.release.setdefault(("s", ("a",)), threading.Event()).set()
        for text in ["kept", "f0", "f1"]:
            emb.release.setdefault(("q", text), threading.Event()).set()
        assert late.done.wait(2) and kept.done.wait(2) and all(f.done.wait(2) for f in fillers)
        seg.join(2)
        stats = sched.stats()
        counts = stats["counts"]
        assert counts["query_submitted"] == 4
        assert counts["query_refused_busy"] == 1
        assert counts["query_behind_segment"] == 4
        assert counts["query_expired_dropped"] == 1
        assert counts["query_served"] == 3
        assert counts["segments"] == 1
        assert stats["query_wait_ms"]["n"] == 4                            # the expired one included
        assert stats["query_wait_ms"]["max"] >= 100.0
        assert stats["segment_ms"]["n"] == 1
    finally:
        for event in emb.release.values():
            event.set()
        sched.close()


def test_status_reports_readiness_without_content(monkeypatch):
    monkeypatch.setenv(query_service.OWNER_ENV, "4242")
    try:
        assert query_service.status() == {"owner_pid": "4242", "reserved": False,
                                          "loader_started": False, "serving": False,
                                          "model_id": None, "scheduler": None}
    finally:
        query_service.stop()


# --------------------------------------------------------------------------- #
# turn coordinates and timing scopes on the shadow event
# --------------------------------------------------------------------------- #

def test_turn_coordinates_match_memory_recall_completed():
    captured = []
    deps = types.SimpleNamespace(emit_debug_trace=lambda uid, event, **kw: captured.append(kw))
    job = {"id": 77, "trace_id": "tr-1", "attempt_count": 2}
    emit = worker._memory_recall_callback(deps, "usr", job, "chat")
    asyncio.run(emit({"counts": {}}))
    completed = captured[-1]["detail"]
    ours = worker._turn_coordinates("chat", job["id"], job["trace_id"], job["attempt_count"])
    assert {k: completed[k] for k in ours} == ours
    wake = worker._turn_coordinates("heartbeat", 9, "", None)
    assert wake == {"lane": "wake", "turn_id": "heartbeat:9", "job_id": "9", "attempt": 0}


def test_memory_context_read_forwards_coordinates_only_when_given():
    calls = []
    deps = types.SimpleNamespace(read_context_memories=lambda uid, **kw: calls.append(kw) or {})

    async def go(**kw):
        return await worker._load_turn_memory_context(deps, "usr", 5, asyncio.Semaphore(1), **kw)
    asyncio.run(go())
    asyncio.run(go(coordinates={"job_id": "1"}))
    assert calls == [{"through_seq": 5}, {"through_seq": 5, "coordinates": {"job_id": "1"}}]


def test_shadow_event_carries_coordinates_and_scoped_timings(monkeypatch):
    import debug_trace
    from memory import plaintext_recall
    from model_api_runtime.v2 import serve_worker
    events = []
    monkeypatch.setattr(serve_worker, "_emit_v2_debug_trace_for_user",
                        lambda uid, event, **kw: events.append(kw))
    local = plaintext_recall.LocalResult(
        payload={"context_memories": [], "context_memory_trace": {},
                 "context_memory_log": {"dur_ms": 3.0, "hybrid": {"encode_queue_ms": 1.5,
                                                                  "encode_compute_ms": 40.0}}},
        input_fingerprint={"hybrid": "active:"}, summary={"ids": "x"}, sealed_cards=2, elapsed_ms=90.0)
    monkeypatch.setattr(serve_worker, "_plaintext_recall_deps", lambda: None)
    monkeypatch.setattr(plaintext_recall, "select", lambda *_a: local)
    diag = {"input_fingerprint": {"other": 1}, "summary": {"ids": "y"},
            "normalized": {"input_fingerprint": {"hybrid": "active:"}, "summary": {"ids": "x"}}}
    coords = {"lane": "chat", "turn_id": "tr-1", "job_id": "77", "attempt": 2}
    serve_worker._submit_recall_shadow("usr", 5, diag, coordinates=coords,
                                       enclave_rpc_ms=120.0, enclave_select_ms=4.0)
    assert _wait(lambda: events)
    event = events[0]
    assert event["turn_id"] == "tr-1" and event["job_id"] == "77"
    detail = event["detail"]
    assert detail["verdict"] == "normalized"
    for key, value in {"local_ms": 90.0, "local_select_ms": 3.0, "enclave_select_ms": 4.0,
                       "enclave_rpc_ms": 120.0, "encode_queue_ms": 1.5, "encode_compute_ms": 40.0,
                       **coords}.items():
        assert detail[key] == value, key
    assert debug_trace._safe_detail(detail) == detail          # widest event fits the durable cap


def test_child_supervisor_reports_only_a_live_child_pid():
    from model_api_runtime.v2.child_supervisor import ChildSupervisor
    alive = types.SimpleNamespace(pid=321, is_alive=lambda: True)
    dead = types.SimpleNamespace(pid=321, is_alive=lambda: False)
    assert ChildSupervisor.child_pid(types.SimpleNamespace(_proc=alive)) == 321
    assert ChildSupervisor.child_pid(types.SimpleNamespace(_proc=dead)) is None
    assert ChildSupervisor.child_pid(types.SimpleNamespace(_proc=None)) is None
