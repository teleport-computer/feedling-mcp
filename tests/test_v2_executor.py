"""V2 executor（spec §7.4）：读并行、写串行、每 action 出脱敏 status、结果折叠。

用假 capabilities.registry + 假 jobs_store（monkeypatch 模块函数）驱动，纯 asyncio，无 DB。
断言：(1) 读在写之前；(2) 并行读受 read_parallelism 闸；(3) 每 action 落 status 事件；
(4) 敏感 data 只进 action_results，action_digest 只有粗计数。Pure-unit。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import registry as cap_registry  # noqa: E402
from model_api_runtime.v2 import executor as v2_executor  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


class _FakeResult:
    def __init__(self, ok, data):
        self._ok, self._data = ok, data

    def to_dict(self):
        return {"ok": self._ok, "data": self._data, "error": None, "trace": {}, "warnings": []}


def test_partition_excludes_final_response():
    plan = [
        {"type": "memory_fetch", "payload": {}},
        {"type": "memory_write", "payload": {}},
        {"type": "final_response", "payload": {}},
    ]
    reads, writes = v2_executor.partition_plan(plan)
    assert [r["type"] for r in reads] == ["memory_fetch"]
    assert [w["type"] for w in writes] == ["memory_write"]


def test_execute_plan_reads_parallel_writes_serial_and_status(monkeypatch):
    order = []
    status = []

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        order.append(action_type)
        return _FakeResult(True, {"secret_body": "PLAINTEXT-" + action_type})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)
    monkeypatch.setattr(jobs_store, "mark_action_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_done", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_failed", lambda *a, **k: None)
    monkeypatch.setattr(
        jobs_store, "append_status_event",
        lambda user_id, kind, **k: status.append(kind) or 1)

    class _Store:
        user_id = "u1"

    plan = [
        {"type": "memory_fetch", "payload": {}, "_action_id": 1},
        {"type": "perception_snapshot", "payload": {}, "_action_id": 2},
        {"type": "memory_write", "payload": {}, "_action_id": 3},
        {"type": "final_response", "payload": {}},
    ]
    out = asyncio.run(v2_executor.execute_plan(
        _Store(), job_id=7, api_key="k", runtime_token="rt",
        plan=plan, read_parallelism=4, enclave_sem=asyncio.Semaphore(8)))

    # writes strictly after reads
    assert order.index("memory_write") > order.index("memory_fetch")
    assert order.index("memory_write") > order.index("perception_snapshot")
    # status carried the merged read line + the write line
    assert "reading_memory" in status and "capturing_memory" in status
    # sensitive body only in action_results, NEVER in action_digest
    assert out["action_results"]["memory_fetch"][0]["data"]["secret_body"] == "PLAINTEXT-memory_fetch"
    assert out["action_digest"]["memory_fetch"] == {"ok": 1, "count": 1}
    assert "secret_body" not in str(out["action_digest"])


def test_read_parallelism_is_bounded(monkeypatch):
    live = {"n": 0, "peak": 0}

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        live["n"] += 1
        live["peak"] = max(live["peak"], live["n"])
        # busy a moment so overlap is observable
        for _ in range(10000):
            pass
        live["n"] -= 1
        return _FakeResult(True, {})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)
    monkeypatch.setattr(jobs_store, "mark_action_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_done", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_failed", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "append_status_event", lambda *a, **k: 1)

    class _Store:
        user_id = "u1"

    plan = [{"type": "memory_fetch", "payload": {}, "_action_id": i} for i in range(6)]
    asyncio.run(v2_executor.execute_plan(
        _Store(), job_id=1, api_key="k", runtime_token="rt",
        plan=plan, read_parallelism=2, enclave_sem=asyncio.Semaphore(8)))
    assert live["peak"] <= 2


def test_failed_action_marks_failed_and_digest_reflects_it(monkeypatch):
    """A failing capability call should route through mark_action_failed (not
    mark_action_done) and the digest's ok-count should reflect the failure —
    without leaking the error/data payload into the digest."""
    marked = {"done": [], "failed": []}

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        return _FakeResult(False, None)

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)
    monkeypatch.setattr(jobs_store, "mark_action_running", lambda *a, **k: None)
    monkeypatch.setattr(
        jobs_store, "mark_action_done",
        lambda action_id, result: marked["done"].append(action_id))
    monkeypatch.setattr(
        jobs_store, "mark_action_failed",
        lambda action_id, error: marked["failed"].append((action_id, error)))
    monkeypatch.setattr(jobs_store, "append_status_event", lambda *a, **k: 1)

    class _FailResult:
        def to_dict(self):
            return {"ok": False, "error": {"code": "boom", "message": "nope", "retryable": False}}

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FailResult())

    class _Store:
        user_id = "u1"

    plan = [{"type": "memory_fetch", "payload": {}, "_action_id": 42}]
    out = asyncio.run(v2_executor.execute_plan(
        _Store(), job_id=3, api_key="k", runtime_token="rt",
        plan=plan, read_parallelism=4, enclave_sem=asyncio.Semaphore(8)))

    assert marked["done"] == []
    assert len(marked["failed"]) == 1
    failed_action_id, failed_error = marked["failed"][0]
    assert failed_action_id == 42
    assert failed_error == "boom"
    assert out["action_digest"]["memory_fetch"] == {"ok": 0, "count": 1}


def test_two_credentials_passed_no_byok(monkeypatch):
    """execute_plan must forward the enclave-auth api_key/runtime_token to
    run_capability and never a BYOK/provider key — the executor itself never
    talks to an LLM."""
    seen = {}

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        seen["api_key"] = api_key
        seen["runtime_token"] = runtime_token
        return _FakeResult(True, {})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)
    monkeypatch.setattr(jobs_store, "mark_action_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_done", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_failed", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "append_status_event", lambda *a, **k: 1)

    class _Store:
        user_id = "u1"

    plan = [{"type": "memory_fetch", "payload": {}, "_action_id": 1}]
    asyncio.run(v2_executor.execute_plan(
        _Store(), job_id=1, api_key="enclave-key", runtime_token="enclave-rt",
        plan=plan, read_parallelism=4, enclave_sem=asyncio.Semaphore(8)))

    assert seen == {"api_key": "enclave-key", "runtime_token": "enclave-rt"}


def test_control_actions_are_skipped_not_run_or_failed(monkeypatch):
    """sleep/capture_memory/final_response (and any non-capability type) must never reach
    run_capability, never be mark_action_failed'd, and must not appear in action_results
    or action_digest — they are control/deferred actions the worker/responder interprets,
    not executor failures. Actions carrying an _action_id get mark_action_skipped instead."""
    ran = []
    marked = {"failed": [], "done": [], "skipped": []}

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        ran.append(action_type)
        return _FakeResult(True, {"body": "ok-" + action_type})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)
    monkeypatch.setattr(jobs_store, "mark_action_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_done",
                         lambda action_id, result: marked["done"].append(action_id))
    monkeypatch.setattr(jobs_store, "mark_action_failed",
                         lambda action_id, error: marked["failed"].append((action_id, error)))
    monkeypatch.setattr(jobs_store, "mark_action_skipped",
                         lambda action_id: marked["skipped"].append(action_id))
    monkeypatch.setattr(jobs_store, "append_status_event", lambda *a, **k: 1)

    class _Store:
        user_id = "u1"

    plan = [
        {"type": "memory_index", "payload": {}, "_action_id": 100},
        {"type": "sleep", "payload": {}, "_action_id": 101},
        {"type": "capture_memory", "payload": {}, "_action_id": 102},
        {"type": "final_response", "payload": {}},  # no _action_id — just dropped
    ]
    out = asyncio.run(v2_executor.execute_plan(
        _Store(), job_id=9, api_key="k", runtime_token="rt",
        plan=plan, read_parallelism=4, enclave_sem=asyncio.Semaphore(8)))

    # only the real capability ran
    assert ran == ["memory_index"]
    assert set(out["action_results"].keys()) == {"memory_index"}
    assert set(out["action_digest"].keys()) == {"memory_index"}
    # control actions were never marked as failures
    assert marked["failed"] == []
    # the two control actions carrying an _action_id were cleanly resolved as skipped
    assert sorted(marked["skipped"]) == [101, 102]
