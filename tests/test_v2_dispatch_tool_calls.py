"""C3 — `executor.dispatch_tool_calls`（spec 2026-07-13 PR-C Task 4）。

用假 capabilities.registry.run_capability（monkeypatch，返回罐装 CapabilityResult-shaped
dict）+ 一个记录调用的 enqueue_write_effect 驱动，纯 asyncio，无 DB。断言：
(a) 两个 READ tool_calls 都跑并按各自 call_id 拿到 ToolResult；
(b) 有 turn_authorization 的 WRITE tool_call 只调 enqueue_write_effect 一次、拿到
    "queued" ToolResult，且绝不经 run_capability 内联跑；
(c) 无 turn_authorization 的 WRITE tool_call 拿到拒绝 ToolResult，且不 enqueue；
(d) 未知工具名 → error ToolResult（不抛异常）；
(e) args_ok=False 的 ToolCall → error ToolResult（不抛异常）；
(f) 混合列表整体按 tool_calls 原序、每个 call_id 都在返回里出现一次。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import registry as cap_registry  # noqa: E402
from model_api_runtime.v2 import executor as v2_executor  # noqa: E402
from provider_types import ToolCall, ToolResult  # noqa: E402


class _FakeResult:
    """Stand-in for capabilities.types.CapabilityResult — dispatch_tool_calls only
    ever touches .to_dict()."""

    def __init__(self, ok, data=None, error=None):
        self._ok, self._data, self._error = ok, data, error

    def to_dict(self):
        if self._ok:
            return {"ok": True, "data": self._data or {}, "trace": {}, "warnings": []}
        return {"ok": False, "error": self._error or {"code": "boom"}}


class _Store:
    user_id = "u1"


def _run(tool_calls, *, turn_authorization, run_capability, enqueue_write_effect=None, monkeypatch):
    monkeypatch.setattr(cap_registry, "run_capability", run_capability)
    calls = []
    if enqueue_write_effect is None:
        def enqueue_write_effect(tc):
            calls.append(tc)
    return asyncio.run(v2_executor.dispatch_tool_calls(
        tool_calls, store=_Store(), api_key="k", runtime_token="rt",
        enclave_sem=asyncio.Semaphore(8), turn_authorization=turn_authorization,
        enqueue_write_effect=enqueue_write_effect,
    )), calls


def test_two_reads_dispatch_and_return_results_by_call_id(monkeypatch):
    ran = []

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        ran.append(action_type)
        return _FakeResult(True, {"body": f"result-for-{action_type}"})

    tool_calls = [
        ToolCall(id="c1", name="memory_index", args={}),
        ToolCall(id="c2", name="web_search", args={"query": "x"}),
    ]
    results, enqueued = _run(
        tool_calls, turn_authorization=False, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert sorted(ran) == ["memory_index", "web_search"]
    assert [r.call_id for r in results] == ["c1", "c2"]
    by_id = {r.call_id: r for r in results}
    assert "result-for-memory_index" in by_id["c1"].content
    assert "result-for-web_search" in by_id["c2"].content
    assert enqueued == []


def test_write_with_authorization_is_enqueued_not_run_inline(monkeypatch):
    ran = []

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        ran.append(action_type)   # must never be called for a write
        return _FakeResult(True, {})

    tool_calls = [ToolCall(id="w1", name="memory_write", args={"actions": []})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert ran == []   # NOT run inline
    assert len(enqueued) == 1 and enqueued[0].id == "w1"
    assert results[0].call_id == "w1"
    assert "queued" in results[0].content
    assert "memory_write" in results[0].content


def test_write_without_authorization_is_refused_not_enqueued(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        raise AssertionError("run_capability must not be called for a refused write")

    tool_calls = [ToolCall(id="w2", name="identity_patch", args={"patch": {}})]
    results, enqueued = _run(
        tool_calls, turn_authorization=False, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].call_id == "w2"
    assert "authorization" in results[0].content


def test_unknown_tool_name_returns_error_result_no_raise(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        raise AssertionError("run_capability must not be called for an unknown tool")

    tool_calls = [ToolCall(id="u1", name="not_a_real_tool", args={})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].call_id == "u1"
    assert "unknown tool" in results[0].content


def test_args_not_ok_returns_error_result_no_raise(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        raise AssertionError("run_capability must not be called for unparseable args")

    tool_calls = [ToolCall(id="b1", name="memory_write", args={}, args_raw="{not json", args_ok=False)]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].call_id == "b1"
    assert "error" in results[0].content


def test_mixed_batch_preserves_original_order_and_every_call_id(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        return _FakeResult(True, {"body": "ok"})

    tool_calls = [
        ToolCall(id="a", name="memory_write", args={"actions": []}),          # write, authorized
        ToolCall(id="b", name="bogus_tool", args={}),                          # unknown
        ToolCall(id="c", name="memory_index", args={}),                        # read
        ToolCall(id="d", name="schedule_wake", args={"when": "x"}, args_ok=False, args_raw="oops"),  # bad args
    ]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert [r.call_id for r in results] == ["a", "b", "c", "d"]
    assert all(isinstance(r, ToolResult) for r in results)
    assert [tc.id for tc in enqueued] == ["a"]
