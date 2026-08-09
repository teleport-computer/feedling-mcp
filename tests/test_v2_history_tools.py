"""Batch 3 tests: history tools runtime wiring + double gate (spec §8.4/§8.5/§8.6).

覆盖：registry/schema/facade 注册链路、三态语义工具描述、双层 gate 矩阵
（wake/subagent 目录移除、executor dispatch 默认拒绝、kill switch 关闭后
facade 拒绝）、`_PRIVATE_READ_TOOLS` 出站隔离（真实 tool_loop 协议测试）、
回合预算（跨 round 累计 + 同 round 第 2 个调用被拒）、结果结构化缩减 +
executor/tool_loop 双层截断后 JSON 完整（8 调用混合批）。

纯单元：readside/enclave/provider 全部 monkeypatch，不碰 DB —— 本文件加入
tests/conftest.py 的 `_PURE_UNIT` 白名单。Style 照 tests/test_v2_tool_loop.py
（sync 测试函数 + asyncio.run，不依赖 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_BACKEND = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

import provider_client  # noqa: E402
from provider_types import ToolCall, ToolResult, ToolSpec  # noqa: E402
from capabilities import errors as cap_errors  # noqa: E402
from capabilities import history as cap_history  # noqa: E402
from capabilities import registry as cap_registry  # noqa: E402
from capabilities import result_budget  # noqa: E402
from capabilities import tool_schema  # noqa: E402
from capabilities import activity_metadata  # noqa: E402
from model_api_runtime.v2 import executor as v2_executor  # noqa: E402
from model_api_runtime.v2 import history_readside  # noqa: E402
from model_api_runtime.v2 import history_search  # noqa: E402
from model_api_runtime.v2 import tool_loop as v2_tool_loop  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402


_STORE = SimpleNamespace(user_id="usr_hist")


def _search_result(
    *, matches=None, complete=False, next_cursor="c" * 900, scanned=42
) -> dict:
    result = {
        "matches": list(matches or []),
        "complete": complete,
        "scanned_count": scanned,
        "unavailable_count": 0,
        "coverage_gap": False,
    }
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


def _match(i: int, snippet_chars: int = 240) -> dict:
    return {
        "message_id": f"msg_{i}",
        "ts": 1720000000.0 + i,
        "role": "user",
        "snippet": "记" * snippet_chars,
        "content_truncated": False,
    }


# ---------------------------------------------------------------------------
# A. 注册链路 + 目录语义
# ---------------------------------------------------------------------------


def test_history_tools_registered_and_read_only():
    assert "history_search" in cap_registry.CAPABILITIES
    assert "history_fetch" in cap_registry.CAPABILITIES
    assert "history_search" in cap_registry.READ_ACTIONS
    assert "history_fetch" in cap_registry.READ_ACTIONS
    assert "history_search" not in cap_registry.WRITE_ACTIONS
    assert "history_fetch" not in cap_registry.WRITE_ACTIONS
    specs = {spec.name for spec in tool_schema.build_tool_specs()}
    assert "history_search" in specs and "history_fetch" in specs


def test_history_tool_args_validate():
    assert tool_schema.validate_tool_args("history_search", {"query": "老王"}) is None
    assert tool_schema.validate_tool_args(
        "history_search",
        {"start": "2026-06-01T00:00:00Z", "end": "2026-07-01T00:00:00Z", "limit": 5},
    ) is None
    assert tool_schema.validate_tool_args("history_search", {"cursor": "abc"}) is None
    assert tool_schema.validate_tool_args("history_search", {"bogus": 1}) is not None
    assert tool_schema.validate_tool_args("history_fetch", {}) is not None
    assert tool_schema.validate_tool_args(
        "history_fetch", {"message_id": "m1", "before": 1, "after": 0}
    ) is None


def test_history_search_description_carries_three_state_semantics():
    """Spec §3.1 三态语义原文 + 「续页只传 cursor」必须进工具描述。"""
    desc = tool_schema.DESCRIPTIONS["history_search"]
    assert "complete=true 扫完了" in desc
    assert "带 cursor 原样再调" in desc
    assert "coverage_gap=true" in desc
    assert "历史区间原文已不存在" in desc
    assert "续页只传 cursor" in desc
    assert "NOT time order" in desc  # 结果顺序 = 扫描优先级（spec §3.1）
    fetch_desc = tool_schema.DESCRIPTIONS["history_fetch"]
    assert "not_found_or_not_visible" in fetch_desc


def test_history_tools_private_read_and_subagent_exclusion():
    assert "history_search" in worker._PRIVATE_READ_TOOLS
    assert "history_fetch" in worker._PRIVATE_READ_TOOLS
    assert "history_search" not in worker._SUBAGENT_ALLOWED_TOOLS
    assert "history_fetch" not in worker._SUBAGENT_ALLOWED_TOOLS
    assert "history_search" in worker._SUBAGENT_DISABLED_TOOLS
    assert "history_fetch" in worker._SUBAGENT_DISABLED_TOOLS


def test_history_tools_not_in_external_reads():
    # Private read (fences later outbound), not an external-content read.
    from model_api_runtime.v2 import provenance

    assert "history_search" not in provenance.EXTERNAL_READS
    assert "history_fetch" not in provenance.EXTERNAL_READS


# ---------------------------------------------------------------------------
# B. facade：错误映射 / kill switch / env 预算
# ---------------------------------------------------------------------------


def _patch_secret(monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "test-secret-0123456789")


def test_kill_switch_defaults_on(monkeypatch):
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)
    assert cap_history.enabled() is True
    for off in ("0", "false", "off", "no", " FALSE "):
        monkeypatch.setenv(cap_history.ENABLED_ENV, off)
        assert cap_history.enabled() is False
    monkeypatch.setenv(cap_history.ENABLED_ENV, "1")
    assert cap_history.enabled() is True


def test_search_facade_rejects_when_switch_off(monkeypatch):
    _patch_secret(monkeypatch)
    monkeypatch.setenv(cap_history.ENABLED_ENV, "0")
    called = []
    monkeypatch.setattr(
        history_readside, "run_history_search",
        lambda *a, **k: called.append(1) or {})
    result = cap_history.search(_STORE, params={"query": "x"})
    assert not result.ok
    assert result.error["code"] == "tool_not_allowed"
    assert not called  # 开关关闭时绝不触达 readside/enclave
    fetch_result = cap_history.fetch(_STORE, params={"message_id": "m"})
    assert not fetch_result.ok and fetch_result.error["code"] == "tool_not_allowed"


def test_search_facade_maps_input_and_cursor_errors(monkeypatch):
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)

    def _raise(exc):
        def _fn(*a, **k):
            raise exc
        return _fn

    cases = [
        (history_search.HistorySearchInputError("missing_query_or_time_range"),
         "missing_query_or_time_range"),
        (history_search.HistorySearchInputError("invalid_time"), "invalid_time"),
        (history_search.CursorInvalid("expired"), "cursor_invalid"),
        (history_search.CursorMismatch("query"), "cursor_mismatch"),
    ]
    for exc, expected_code in cases:
        monkeypatch.setattr(history_readside, "run_history_search", _raise(exc))
        result = cap_history.search(_STORE, params={"query": "x"})
        assert not result.ok
        # 稳定 slug 必须放 code 位：executor 喂回模型的失败结果只暴露 code。
        assert result.error["code"] == expected_code, expected_code


def test_search_facade_enclave_route_missing_is_explicit(monkeypatch):
    """版本错位（enclave 无 history route）→ capability_unavailable，绝不静默空结果。"""
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)

    def _fn(*a, **k):
        raise RuntimeError("enclave_history_capability_unavailable")

    monkeypatch.setattr(history_readside, "run_history_search", _fn)
    result = cap_history.search(_STORE, params={"query": "x"})
    assert not result.ok
    assert result.error["code"] == cap_errors.UNAVAILABLE
    assert result.error["retryable"] is False

    monkeypatch.setattr(history_readside, "run_history_fetch", _fn)
    fetch_result = cap_history.fetch(_STORE, params={"message_id": "m"})
    assert not fetch_result.ok
    assert fetch_result.error["code"] == cap_errors.UNAVAILABLE


def test_search_facade_upstream_errors_retryable(monkeypatch):
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)

    def _fn(*a, **k):
        raise RuntimeError("enclave_http_503:busy")

    monkeypatch.setattr(history_readside, "run_history_search", _fn)
    result = cap_history.search(_STORE, params={"query": "x"})
    assert not result.ok
    assert result.error["code"] == cap_errors.UPSTREAM
    assert result.error["retryable"] is True


def test_search_facade_success_and_env_budget(monkeypatch):
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)
    monkeypatch.setenv("FEEDLING_V2_HISTORY_CALL_MAX_ROWS", "99")
    monkeypatch.setenv("FEEDLING_V2_HISTORY_CALL_DEADLINE_MS", "1234")
    captured = {}

    def _fn(user_id, **kwargs):
        captured["user_id"] = user_id
        captured.update(kwargs)
        return _search_result(matches=[_match(1)], complete=True, next_cursor=None)

    monkeypatch.setattr(history_readside, "run_history_search", _fn)
    result = cap_history.search(
        _STORE, runtime_token="tok", params={"query": "老王", "limit": 2})
    assert result.ok
    assert captured["user_id"] == "usr_hist"
    assert captured["query"] == "老王" and captured["limit"] == 2
    assert captured["runtime_token"] == "tok"
    assert captured["budget"].call_max_rows == 99
    assert captured["budget"].call_deadline_ms == 1234
    assert isinstance(captured["cursor_hmac_key"], bytes)
    assert result.data["complete"] is True
    assert result.data["matches"][0]["message_id"] == "msg_1"


def test_fetch_facade_not_found_and_defaults(monkeypatch):
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)
    captured = {}

    def _fn(user_id, **kwargs):
        captured.update(kwargs)
        return {"error": "not_found_or_not_visible"}

    monkeypatch.setattr(history_readside, "run_history_fetch", _fn)
    result = cap_history.fetch(_STORE, params={"message_id": "m1"})
    assert not result.ok
    assert result.error["code"] == "not_found_or_not_visible"
    assert captured["before"] == 15 and captured["after"] == 4

    missing = cap_history.fetch(_STORE, params={})
    assert not missing.ok and missing.error["code"] == cap_errors.INVALID


def test_fetch_window_defaults_are_asymmetric(monkeypatch):
    """spec §3.2：before 默认 15 / after 默认 4（线索几乎总在前文里）。"""
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)
    captured = {}

    def _fn(user_id, **kwargs):
        captured.update(kwargs)
        return {"anchor": None, "before": [], "after": [], "unavailable_count": 0}

    monkeypatch.setattr(history_readside, "run_history_fetch", _fn)
    cap_history.fetch(_STORE, params={"message_id": "m1"})
    assert captured["before"] == 15 and captured["after"] == 4


def test_fetch_neighbor_counts_clamped_to_15(monkeypatch):
    """readside 侧钳 [0,15]（请求上限 31 条），越界不报错、按上限走。"""
    posted = {}

    def _post(op, payload):
        posted["before"] = len(payload["before"])
        posted["after"] = len(payload["after"])
        return {"anchor": {"message_id": "a", "content": "x"},
                "before": [], "after": [], "unavailable_count": 0}

    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_anchor_row",
        lambda uid, mid: {"id": mid, "seq": 50, "ts": 1.0, "role": "user",
                          "content_type": "text", "v": 1,
                          "owner_user_id": uid, "body_ct": "c",
                          "nonce": "n", "K_enclave": "k"})
    asked = {}

    def _neighbors(uid, seq, *, before, after):
        asked["before"], asked["after"] = before, after
        return [], []

    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_neighbor_rows", _neighbors)
    history_readside.run_history_fetch(
        "usr_a", message_id="m1", before=99, after=99, post_enclave=_post)
    assert asked == {"before": 15, "after": 15}
    history_readside.run_history_fetch(
        "usr_a", message_id="m1", before=-3, after=-1, post_enclave=_post)
    assert asked == {"before": 0, "after": 0}


def test_fetch_tool_schema_documents_the_wider_window():
    desc = tool_schema.DESCRIPTIONS["history_fetch"]
    assert "0-15" in desc
    assert "15" in desc and "4" in desc


# ---------------------------------------------------------------------------
# C. 结构化缩减（序列化前）
# ---------------------------------------------------------------------------


def test_search_result_shrinks_to_cap_json_intact():
    result = _search_result(
        matches=[_match(i) for i in range(5)],
        complete=False,
        next_cursor="c" * 1024,
    )
    assert cap_history._rendered_chars(result) > 1800
    shrunk = cap_history._shrink_search_result(result, max_chars=1800)
    rendered = json.dumps(shrunk, ensure_ascii=False)
    assert len(rendered) <= 1800
    parsed = json.loads(rendered)
    # 三态字段与 cursor 绝不动：截断不能伪装成"扫完了"。
    assert parsed["next_cursor"] == "c" * 1024
    assert parsed["complete"] is False
    assert parsed["scanned_count"] == 42
    for match in parsed["matches"]:
        assert match["content_truncated"] is True
        assert match["message_id"].startswith("msg_")


def test_search_result_shrink_prefers_snippets_over_dropping_matches():
    # 略超预算：全部 match 保留，只缩 snippet。
    result = _search_result(
        matches=[_match(i) for i in range(3)], next_cursor="c" * 400)
    shrunk = cap_history._shrink_search_result(result, max_chars=1200)
    assert len(shrunk["matches"]) == 3
    assert all(len(m["snippet"]) <= 160 for m in shrunk["matches"])


def test_search_result_small_passes_through_untouched():
    result = _search_result(
        matches=[_match(1, snippet_chars=20)], complete=True, next_cursor=None)
    shrunk = cap_history._shrink_search_result(result, max_chars=1800)
    assert shrunk == result


def _fetch_item(i: int, chars: int) -> dict:
    return {
        "message_id": f"n_{i}",
        "ts": 1720000000.0 + i,
        "role": "user",
        "content": "话" * chars,
        "content_truncated": False,
    }


def _wide_fetch_result(*, before=15, after=4, chars=300, anchor_chars=900) -> dict:
    """spec §3.2 的请求上限形态：before 15 / after 4，anchor 只出现一次。"""
    return {
        "anchor": _fetch_item(0, anchor_chars),
        "before": [_fetch_item(-i, chars) for i in range(before, 0, -1)],
        "after": [_fetch_item(i, chars) for i in range(1, after + 1)],
        "unavailable_count": 0,
    }


def test_fetch_shrink_keeps_every_message_before_dropping_any():
    """spec §3.2 缩减顺序：①全员结构+短摘录 → ②近邻补全正文 → ③才丢最远的。

    反过来（先砍最远邻居）恰好砍掉这次把窗口放宽到 15 想找回的前文线索。
    """
    result = _wide_fetch_result()
    assert cap_history._rendered_chars(result) > 4500
    shrunk = cap_history._shrink_fetch_result(result, max_chars=4500)
    rendered = json.dumps(shrunk, ensure_ascii=False)
    assert len(rendered) <= 4500
    parsed = json.loads(rendered)
    # ① 一条都没丢：19 条邻居的结构和摘录都在
    assert len(parsed["before"]) == 15 and len(parsed["after"]) == 4
    assert parsed["omitted_before"] == 0 and parsed["omitted_after"] == 0
    assert all(item["content"] for item in parsed["before"] + parsed["after"])
    # ② 锚点与贴身近邻拿到完整正文，最远的只剩摘录
    assert parsed["anchor"]["content"] == "话" * 900
    assert len(parsed["before"][-1]["content"]) > len(parsed["before"][0]["content"])


def test_fetch_shrink_drops_farthest_only_when_skeleton_does_not_fit():
    """骨架本身都装不下时才丢消息，并如实计入 omitted_*（锚点永不丢）。"""
    result = _wide_fetch_result(chars=200, anchor_chars=200)
    shrunk = cap_history._shrink_fetch_result(result, max_chars=1200)
    rendered = json.dumps(shrunk, ensure_ascii=False)
    assert len(rendered) <= 1200
    parsed = json.loads(rendered)
    assert parsed["anchor"]["message_id"] == "n_0"
    assert parsed["omitted_before"] + parsed["omitted_after"] > 0
    # 丢了多少如实报：两侧各自守恒
    assert len(parsed["before"]) + parsed["omitted_before"] == 15
    assert len(parsed["after"]) + parsed["omitted_after"] == 4
    # 丢的是最远的：留下来的 before 尾部仍贴着锚点
    if parsed["before"]:
        assert parsed["before"][-1]["message_id"] == "n_-1"


def test_fetch_shrink_reports_zero_omissions_when_nothing_is_cut():
    small = {"anchor": _fetch_item(0, 10), "before": [], "after": [],
             "unavailable_count": 0}
    shrunk = cap_history._shrink_fetch_result(small, max_chars=4500)
    assert shrunk["omitted_before"] == 0 and shrunk["omitted_after"] == 0
    assert shrunk["anchor"] == small["anchor"]


def test_fetch_result_shrinks_to_cap_anchor_last():
    result = {
        "anchor": _fetch_item(0, 900),
        "before": [_fetch_item(i, 400) for i in range(1, 5)],
        "after": [_fetch_item(i, 400) for i in range(5, 9)],
        "unavailable_count": 0,
    }
    assert cap_history._rendered_chars(result) > 1600
    shrunk = cap_history._shrink_fetch_result(result, max_chars=1600)
    rendered = json.dumps(shrunk, ensure_ascii=False)
    assert len(rendered) <= 1600
    parsed = json.loads(rendered)
    # 锚点是这次调用的目的：永远保留（正文最后才截）。
    assert parsed["anchor"]["message_id"] == "n_0"
    # 骨架装得下 → 8 条邻居一条不丢，只是正文缩成摘录（spec §3.2 缩减顺序）。
    assert len(parsed["before"]) == 4 and len(parsed["after"]) == 4
    assert parsed["omitted_before"] == 0 and parsed["omitted_after"] == 0
    assert all(item["content_truncated"] for item in parsed["before"][:1])


def test_fetch_result_neighbor_drop_farthest_first():
    # 只需砍邻居就能达标时：before 从头部（最旧）、after 从尾部（最新）砍。
    result = {
        "anchor": _fetch_item(0, 50),
        "before": [_fetch_item(i, 300) for i in range(1, 4)],
        "after": [_fetch_item(i, 300) for i in range(4, 7)],
        "unavailable_count": 0,
    }
    shrunk = cap_history._shrink_fetch_result(result, max_chars=1200)
    kept_before = [item["message_id"] for item in shrunk["before"]]
    kept_after = [item["message_id"] for item in shrunk["after"]]
    # 贴锚点的行存活：before 的尾部、after 的头部。
    if kept_before:
        assert kept_before[-1] == "n_3"
    if kept_after:
        assert kept_after[0] == "n_4"


# ---------------------------------------------------------------------------
# D. executor dispatch gate + 双层截断协议（§8.4/§8.6）
# ---------------------------------------------------------------------------


class _FakeCapResult:
    def __init__(self, data=None, ok=True, code="err"):
        self._data = data or {}
        self._ok = ok
        self._code = code

    def to_dict(self):
        if self._ok:
            return {"ok": True, "data": self._data, "trace": {}, "warnings": []}
        return {"ok": False, "error": {"code": self._code, "retryable": False}}


def _dispatch(tool_calls, *, history_tools_allowed=None, run_capability=None,
              monkeypatch=None):
    if run_capability is not None:
        monkeypatch.setattr(cap_registry, "run_capability", run_capability)
    kwargs = {}
    if history_tools_allowed is not None:
        kwargs["history_tools_allowed"] = history_tools_allowed
    return asyncio.run(v2_executor.dispatch_tool_calls(
        tool_calls,
        store=_STORE,
        api_key=None,
        runtime_token="tok",
        enclave_sem=asyncio.Semaphore(1),
        turn_authorization=True,
        enqueue_write_effect=lambda tc: None,
        **kwargs,
    ))


def test_executor_rejects_history_by_default(monkeypatch):
    """未显式 opt-in 的 caller（wake/subagent/直连）dispatch 一律拒——
    即使参数合法、工具名在 READ_ACTIONS 里。"""
    called = []
    monkeypatch.setattr(
        cap_registry, "run_capability",
        lambda *a, **k: called.append(a) or _FakeCapResult())
    results = _dispatch([
        ToolCall(id="h1", name="history_search", args={"query": "x"}),
        ToolCall(id="h2", name="history_fetch", args={"message_id": "m"}),
        ToolCall(id="m1", name="memory_search", args={"query": "x"}),
    ])
    by_id = {r.call_id: r for r in results}
    assert by_id["h1"].content == "error: tool_not_allowed"
    assert by_id["h2"].content == "error: tool_not_allowed"
    # 兄弟调用不受影响；capability 只为 memory_search 跑了一次。
    assert not by_id["m1"].content.startswith("error")
    assert len(called) == 1 and called[0][0] == "memory_search"


def test_executor_dispatches_history_when_allowed(monkeypatch):
    def _run(action_type, store, **kw):
        assert action_type == "history_search"
        return _FakeCapResult(data=_search_result(
            matches=[_match(1, 30)], complete=True, next_cursor=None))

    results = _dispatch(
        [ToolCall(id="h1", name="history_search", args={"query": "x"})],
        history_tools_allowed=True, run_capability=_run, monkeypatch=monkeypatch)
    parsed = json.loads(results[0].content)
    assert parsed["complete"] is True
    assert results[0].metadata == {
        "history_scanned_rows": 42, "result_budget_kind": "history_search"}


def test_history_result_survives_double_truncation_in_mixed_batch(monkeypatch):
    """§8.4：executor 单结果闸 + tool_loop 同批 8000 水位后，history JSON 逐字节
    完整可解析。批形态 = 每 round 唯一 1 个 history 结果最大化（~1800）+ 7 个
    混合兄弟（两个 2000 字符顶格结果 + 5 个短结果）。"""
    big = _search_result(
        matches=[_match(i) for i in range(5)], next_cursor="c" * 1024)
    shrunk = cap_history._shrink_search_result(big, max_chars=1800)

    def _run(action_type, store, **kw):
        return _FakeCapResult(data=shrunk)

    (history_result,) = _dispatch(
        [ToolCall(id="h1", name="history_search", args={"query": "x"})],
        history_tools_allowed=True, run_capability=_run, monkeypatch=monkeypatch)
    # 第一层：executor 2000 闸之下，无 [truncated] 切串。
    assert len(history_result.content) <= 1800
    assert "[truncated]" not in history_result.content

    batch = [history_result]
    batch.append(ToolResult(call_id="w1", content="W" * 2000))
    batch.append(ToolResult(call_id="w2", content="M" * 2000))
    for i in range(5):
        batch.append(ToolResult(call_id=f"s{i}", content="ok"))
    assert len(batch) == 8
    normalized = v2_tool_loop._normalize_tool_results(
        batch,
        per_result_cap=v2_tool_loop.DEFAULT_TOOL_RESULT_CHAR_CAP,
        batch_cap=v2_tool_loop.DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP,
    )
    total = sum(len(r.content) for r in normalized)
    assert total <= v2_tool_loop.DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP
    # 第二层之后 history 结果必须仍是完整 JSON（含 cursor 原样）。
    history_after = next(r for r in normalized if r.call_id == "h1")
    parsed = json.loads(history_after.content)
    assert parsed["next_cursor"] == "c" * 1024
    assert [m["message_id"] for m in parsed["matches"]]


def test_history_fetch_full_window_survives_worst_case_sibling_batch(monkeypatch):
    """§3.3 协议测试，真实最坏批形：history_fetch 顶满 4500 + 7 个各 ≥2000
    的兄弟。atomic_json 结果整额预留、逐字节不切；同批总量 ≤ 8000+2500。

    不许用短 "ok" 结果凑数释放水位——那正好绕开了要测的东西。
    """
    shrunk = cap_history._shrink_fetch_result(
        _wide_fetch_result(), max_chars=4500)

    def _run(action_type, store, **kw):
        assert action_type == "history_fetch"
        return _FakeCapResult(data=shrunk)

    (fetch_result,) = _dispatch(
        [ToolCall(id="h1", name="history_fetch", args={"message_id": "m1"})],
        history_tools_allowed=True, run_capability=_run, monkeypatch=monkeypatch)
    # 第一层：executor 的单结果闸按共享策略抬到 4500，没有切串。
    assert 2000 < len(fetch_result.content) <= 4500
    assert "[truncated]" not in fetch_result.content
    assert json.loads(fetch_result.content)["anchor"]["message_id"] == "n_0"

    batch = [fetch_result] + [
        ToolResult(call_id=f"w{i}", content=f"W{i}" * 1000) for i in range(7)
    ]
    assert len(batch) == 8
    assert all(len(r.content) >= 2000 for r in batch[1:])
    normalized = v2_tool_loop._normalize_tool_results(
        batch,
        per_result_cap=v2_tool_loop.DEFAULT_TOOL_RESULT_CHAR_CAP,
        batch_cap=v2_tool_loop.DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP,
    )
    after = {r.call_id: r.content for r in normalized}
    # atomic_json：逐字节不动，仍是完整 JSON
    assert after["h1"] == fetch_result.content
    json.loads(after["h1"])
    # 同批总预算抬到 8000+2500，兄弟们分摊剩下的（不是被压到 ~500）
    total = sum(len(c) for c in after.values())
    assert total <= 10500
    for i in range(7):
        assert len(after[f"w{i}"]) >= 700


def test_history_search_result_survives_seven_maxed_siblings(monkeypatch):
    """Codex 复现的最坏批形：~1700 字符 history_search + 7 个 2000 顶格兄弟。
    旧水位分摊会把它砍到 ~1000（Unterminated string）。"""
    shrunk = cap_history._shrink_search_result(
        _search_result(matches=[_match(i) for i in range(5)],
                       next_cursor="c" * 1024),
        max_chars=1800)

    def _run(action_type, store, **kw):
        return _FakeCapResult(data=shrunk)

    (history_result,) = _dispatch(
        [ToolCall(id="h1", name="history_search", args={"query": "x"})],
        history_tools_allowed=True, run_capability=_run, monkeypatch=monkeypatch)
    assert len(history_result.content) > 1600  # 顶格 history 结果
    batch = [history_result] + [
        ToolResult(call_id=f"w{i}", content=f"W{i}" * 1000) for i in range(7)
    ]
    normalized = v2_tool_loop._normalize_tool_results(
        batch,
        per_result_cap=v2_tool_loop.DEFAULT_TOOL_RESULT_CHAR_CAP,
        batch_cap=v2_tool_loop.DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP,
    )
    history_after = next(r for r in normalized if r.call_id == "h1")
    assert history_after.content == history_result.content
    assert json.loads(history_after.content)["next_cursor"] == "c" * 1024


def test_batch_without_history_results_is_byte_identical(monkeypatch):
    """最小侵入证明：批里没有 history 结果时，分配结果逐字节不变。

    直接对着水位算法本身核（``_result_quotas`` + 单结果闸），而不是抄一份
    期望值——任何"顺手"改动共享路径的行为都会在这里现形。
    """
    contents = ["A" * 3000, "B" * 2500, "C" * 10, "D" * 1800, "E" * 900,
                "F" * 5, "G" * 2000, "H" * 2000]
    batch = [ToolResult(call_id=f"c{i}", content=c) for i, c in enumerate(contents)]
    normalized = v2_tool_loop._normalize_tool_results(
        batch,
        per_result_cap=v2_tool_loop.DEFAULT_TOOL_RESULT_CHAR_CAP,
        batch_cap=v2_tool_loop.DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP,
    )
    capped = [
        v2_tool_loop._truncate_result_content(
            c, v2_tool_loop.DEFAULT_TOOL_RESULT_CHAR_CAP)
        for c in contents
    ]
    quotas = v2_tool_loop._result_quotas(
        [len(c) for c in capped], v2_tool_loop.DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP)
    expected = [
        v2_tool_loop._truncate_result_content(c, q) for c, q in zip(capped, quotas)
    ]
    assert [r.content for r in normalized] == expected
    assert sum(len(c) for c in expected) <= (
        v2_tool_loop.DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP)


def test_error_results_are_json_free_and_stable(monkeypatch):
    """失败路径喂回模型的只有稳定 code，绝不含解密体/参数。"""
    def _run(action_type, store, **kw):
        return _FakeCapResult(ok=False, code="cursor_invalid")

    (result,) = _dispatch(
        [ToolCall(id="h1", name="history_search", args={"cursor": "junk"})],
        history_tools_allowed=True, run_capability=_run, monkeypatch=monkeypatch)
    assert result.content == "error: cursor_invalid"


# ---------------------------------------------------------------------------
# D2. 共享预算策略的配置校验 + atomic 违约兜底（§3.3）
# ---------------------------------------------------------------------------


def test_shipped_defaults_pass_the_cross_check():
    """默认配置（1800 / 4500+2500 / 8000）自洽——这条绿了才说明校验没写反。"""
    result_budget.validate_result_caps(batch_cap=worker.TOOL_BATCH_RESULT_CHAR_CAP)


def test_minimum_skeletons_track_the_real_cursor_ceiling():
    """骨架下界必须容得下 next_cursor（缩减唯一不能丢的可变长字段）。"""
    assert result_budget._CURSOR_MAX_CHARS == history_search.CURSOR_MAX_CHARS
    assert (result_budget.MIN_RESULT_CAPS["history_search"]
            > history_search.CURSOR_MAX_CHARS)


def test_result_cap_below_the_minimum_skeleton_is_rejected(monkeypatch):
    """cap=1：结构化缩减压不到 1 字符，facade 只能交出超限对象 → 下游切串
    → ``"{...[truncated]"`` 非法 JSON。这种配置必须在启动期就死。"""
    monkeypatch.setenv(result_budget.HISTORY_FETCH_RESULT_MAX_CHARS_ENV, "1")
    with pytest.raises(RuntimeError, match="HISTORY_FETCH_RESULT_MAX_CHARS"):
        result_budget.validate_result_caps(batch_cap=8000)


def test_result_cap_that_overflows_the_raised_batch_budget_is_rejected(monkeypatch):
    """cap=12000：atomic 整额预留 > 8000+2500，同批总量突破 10500 总闸。"""
    monkeypatch.setenv(result_budget.HISTORY_FETCH_RESULT_MAX_CHARS_ENV, "12000")
    with pytest.raises(RuntimeError, match="HISTORY_FETCH_RESULT_MAX_CHARS"):
        result_budget.validate_result_caps(batch_cap=8000)


def test_lowering_the_generic_batch_cap_is_rejected_too(monkeypatch):
    """反向同理：通用 batch cap 调小到装不下 atomic 结果，也是同一个洞。"""
    with pytest.raises(RuntimeError, match="TOOL_BATCH_RESULT_CHAR_CAP"):
        result_budget.validate_result_caps(batch_cap=1000)


def test_two_atomic_results_cannot_jointly_overflow_the_batch(monkeypatch):
    """每个都合规、加起来不合规也要拦（round gate 之外的纵深防御）。"""
    monkeypatch.setenv(result_budget.HISTORY_SEARCH_RESULT_MAX_CHARS_ENV, "6500")
    with pytest.raises(RuntimeError, match="TOOL_BATCH_RESULT_CHAR_CAP"):
        result_budget.validate_result_caps(batch_cap=8000)


def test_worker_fails_fast_at_import_on_a_broken_result_cap():
    """真·启动期：坏配置下 import worker 必须炸，而不是安静地上线。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_BACKEND)
    env["FEEDLING_V2_HISTORY_FETCH_RESULT_MAX_CHARS"] = "12000"
    proc = subprocess.run(
        [sys.executable, "-c", "from model_api_runtime.v2 import worker"],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode != 0
    assert "FEEDLING_V2_HISTORY_FETCH_RESULT_MAX_CHARS" in proc.stderr


def test_executor_replaces_an_over_cap_atomic_result_instead_of_slicing_it():
    """atomic 路径绝不走通用切串闸：策略违约整体换成合法的错误结果。"""
    oversized = {"ok": True, "data": {"anchor": {"content": "x" * 9000}}}
    rendered = v2_executor._summarize_capability_result(
        oversized, tool_name="history_fetch")
    assert rendered == f"error: {result_budget.OVERFLOW_ERROR_CODE}"
    assert "[truncated]" not in rendered
    # 非 atomic 工具照旧切串，行为逐字节不变
    plain = v2_executor._summarize_capability_result(
        {"ok": True, "data": {"text": "x" * 9000}}, tool_name="web_search")
    assert plain.endswith("...[truncated]")


def test_tool_loop_replaces_an_over_cap_atomic_result_instead_of_slicing_it():
    huge = ToolResult(
        call_id="h1", content="{" + "x" * 9000 + "}",
        metadata={result_budget.RESULT_KIND_METADATA_KEY: "history_fetch"})
    (out,) = v2_tool_loop._normalize_tool_results(
        [huge],
        per_result_cap=v2_tool_loop.DEFAULT_TOOL_RESULT_CHAR_CAP,
        batch_cap=v2_tool_loop.DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP)
    assert out.content == f"error: {result_budget.OVERFLOW_ERROR_CODE}"
    assert out.metadata == huge.metadata


def test_fetch_facade_returns_a_stable_error_when_the_shrink_cannot_fit(monkeypatch):
    """缩减后仍超限 → 完整、稳定、合法 JSON 的错误结果，绝不进字符串截断。"""
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)
    monkeypatch.setenv(result_budget.HISTORY_FETCH_RESULT_MAX_CHARS_ENV, "1")
    monkeypatch.setattr(
        history_readside, "run_history_fetch",
        lambda uid, **kw: _wide_fetch_result())
    result = cap_history.fetch(_STORE, params={"message_id": "m1"})
    assert not result.ok
    assert result.error["code"] == result_budget.OVERFLOW_ERROR_CODE
    rendered = v2_executor._summarize_capability_result(
        result.to_dict(), tool_name="history_fetch")
    assert rendered == f"error: {result_budget.OVERFLOW_ERROR_CODE}"


def test_search_facade_returns_a_stable_error_when_the_shrink_cannot_fit(monkeypatch):
    """search 同理：next_cursor 不能丢，所以极小 cap 下必然压不进去。"""
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)
    monkeypatch.setenv(result_budget.HISTORY_SEARCH_RESULT_MAX_CHARS_ENV, "20")
    monkeypatch.setattr(
        history_readside, "run_history_search",
        lambda uid, **kw: _search_result(matches=[_match(0)]))
    result = cap_history.search(_STORE, params={"query": "x"})
    assert not result.ok
    assert result.error["code"] == result_budget.OVERFLOW_ERROR_CODE


# ---------------------------------------------------------------------------
# E. 出站隔离（§8.5，真实 tool_loop）
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, config, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, **kwargs})
        if not self.responses:
            raise AssertionError("provider called more times than scripted")
        return self.responses.pop(0)


def test_history_read_fences_later_outbound_tools(monkeypatch):
    """history 读之后的下一轮：web/MCP/task 出站工具从目录移除（含注入的
    MCP extra spec），本地工具保留——由 `_PRIVATE_READ_TOOLS` 成员资格自动
    生效，不另造闸。"""
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "h1", "name": "history_search", "args": {"query": "餐厅"}}
            ],
            "usage": {},
        },
        {"reply": "找到了", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    async def _dispatch_tools(tool_calls):
        return [
            ToolResult(
                call_id=tc.id,
                content=json.dumps(
                    _search_result(matches=[_match(1, 20)], complete=True,
                                   next_cursor=None),
                    ensure_ascii=False,
                ),
            )
            for tc in tool_calls
        ]

    async def _reply(text, *, final, reasoning=""):
        return None

    async def _fold():
        return []

    mcp_spec = ToolSpec(name="mcp_notion_read", description="x",
                        parameters={"type": "object", "properties": {}})
    cfg = provider_client.ProviderConfig(
        provider="anthropic", model="claude-sonnet-4-test", api_key="k")
    outcome = asyncio.run(v2_tool_loop.run_tool_loop(
        provider_config=cfg,
        build_messages=lambda transcript: [{"role": "user", "content": "hi"}],
        dispatch_tools=_dispatch_tools,
        on_reply=_reply,
        fold_new_messages=_fold,
        add_usage=lambda usage: None,
        max_calls=4,
        extra_tool_specs=[mcp_spec],
        outbound_blocking_read_tool_names=worker._PRIVATE_READ_TOOLS,
    ))
    assert outcome.final_text == "找到了"
    assert len(provider.calls) == 2
    round1_names = {spec.name for spec in provider.calls[0]["tools"]}
    assert {"history_search", "history_fetch", "web_search", "mcp_notion_read"} <= (
        round1_names
    )
    round2_names = {spec.name for spec in provider.calls[1]["tools"]}
    assert "web_search" not in round2_names
    assert "web_fetch" not in round2_names
    assert "mcp_notion_read" not in round2_names
    assert tool_schema.TASK_TOOL not in round2_names
    # 本地读写不受出站 fence 影响（read-then-edit 仍是核心工作流）。
    assert "memory_search" in round2_names
    assert "workspace_read" in round2_names


# ---------------------------------------------------------------------------
# F. gate 矩阵（§8.6）+ 回合预算（§5）
# ---------------------------------------------------------------------------


def test_wake_lane_source_withholds_history_tools():
    """wake/heartbeat/screen_watch/manual_wake（_run_wake）目录必须不含
    history 工具。源码级断言，形态照 test_provider_usage_withheld_from_wake_
    lane_source（_run_wake 需要全套 DB TurnDeps 才能跑通）。"""
    source = inspect.getsource(worker._run_wake)
    assert "HISTORY_SEARCH_TOOL" in source
    assert "HISTORY_FETCH_TOOL" in source


def test_chat_lane_source_wires_offer_gate_and_budget():
    source = inspect.getsource(worker.process_job)
    assert "history_tools_offered" in source
    assert "_HistoryTurnBudget" in source
    assert "history_tools_allowed=history_tools_offered" in source
    assert "_history_round_blocked_results" in source


def test_history_round_gate_one_call_per_round():
    budget = worker._HistoryTurnBudget(max_rows=1500, max_wall_sec=8)
    calls = [
        ToolCall(id="m1", name="memory_search", args={"query": "x"}),
        ToolCall(id="h1", name="history_search", args={"query": "x"}),
        ToolCall(id="h2", name="history_fetch", args={"message_id": "m"}),
        ToolCall(id="h3", name="history_search", args={"cursor": "c"}),
    ]
    blocked = worker._history_round_blocked_results(calls, budget=budget)
    assert set(blocked) == {"h2", "h3"}  # 第一个放行，其余短错误；非 history 不动
    assert blocked["h2"].content == worker._HISTORY_ROUND_LIMIT_ERROR
    assert blocked["h3"].content == worker._HISTORY_ROUND_LIMIT_ERROR


def test_history_turn_budget_accumulates_rows_across_rounds():
    budget = worker._HistoryTurnBudget(max_rows=1500, max_wall_sec=8)
    budget.settle_call(budget.reserve_call(), scanned_rows=800)
    assert not budget.exhausted()
    budget.settle_call(budget.reserve_call(), scanned_rows=800)
    assert budget.exhausted()  # 800+800 >= 1500，跨 round 累计
    assert budget.reserve_call() is None
    calls = [ToolCall(id="h1", name="history_search", args={"query": "x"})]
    blocked = worker._history_round_blocked_results(calls, budget=budget)
    assert blocked["h1"].content == worker._HISTORY_TURN_BUDGET_ERROR


def test_history_turn_budget_wall_clock():
    now = [100.0]
    budget = worker._HistoryTurnBudget(
        max_rows=1500, max_wall_sec=8, clock=lambda: now[0])
    lease = budget.reserve_call()
    now[0] += 5.0
    budget.settle_call(lease, scanned_rows=10)
    assert not budget.exhausted()
    lease = budget.reserve_call()
    now[0] += 3.5
    budget.settle_call(lease, scanned_rows=10)
    assert budget.exhausted()  # 5s + 3.5s >= 8s


def test_history_turn_budget_reserves_rows_before_the_call():
    """1499/1500 行已用时，下一次调用只许扫 1 行——不是照样放行 512 行。

    先调用后结算的旧写法把回合闸变成了"超限后才拦下一次"：单次上限 512、
    回合上限 1500，实际最坏能扫到 2011 行。
    """
    budget = worker._HistoryTurnBudget(max_rows=1500, max_wall_sec=8)
    budget.settle_call(budget.reserve_call(), scanned_rows=1499)
    assert not budget.exhausted()
    lease = budget.reserve_call()
    assert lease is not None and lease.max_rows == 1
    call_budget = history_readside.clamp_to_remaining(
        history_readside.HistorySearchBudget(),
        max_rows=lease.max_rows, deadline_ms=lease.deadline_ms)
    assert call_budget.call_max_rows == 1          # 不是 512
    assert call_budget.raw_batch_rows == 128       # 其余闸不动


def test_history_turn_budget_reserves_wall_time_before_the_call():
    """7.9s/8s 已用时，下一次调用的 deadline 只剩 100ms——不是照样给 2.5s。"""
    now = [100.0]
    budget = worker._HistoryTurnBudget(
        max_rows=1500, max_wall_sec=8, clock=lambda: now[0])
    lease = budget.reserve_call()
    now[0] += 7.9
    budget.settle_call(lease, scanned_rows=1)
    assert not budget.exhausted()
    lease = budget.reserve_call()
    assert lease.deadline_ms == 100
    call_budget = history_readside.clamp_to_remaining(
        history_readside.HistorySearchBudget(),
        max_rows=lease.max_rows, deadline_ms=lease.deadline_ms)
    assert call_budget.call_deadline_ms == 100     # 不是 2500


def test_clamp_to_remaining_never_raises_the_configured_budget():
    base = history_readside.HistorySearchBudget(
        call_max_rows=100, call_deadline_ms=500)
    same = history_readside.clamp_to_remaining(
        base, max_rows=10_000, deadline_ms=99_999)
    assert same.call_max_rows == 100 and same.call_deadline_ms == 500


def test_facade_applies_the_turn_call_clamp(monkeypatch):
    """worker 把回合剩余额度通过可信通道钉进本次调用的 budget。"""
    _patch_secret(monkeypatch)
    monkeypatch.delenv(cap_history.ENABLED_ENV, raising=False)
    captured = {}

    def _fn(user_id, **kwargs):
        captured.update(kwargs)
        return _search_result(complete=True, next_cursor=None)

    monkeypatch.setattr(history_readside, "run_history_search", _fn)
    with cap_history.call_limits(max_rows=7, deadline_ms=120):
        cap_history.search(_STORE, params={"query": "x"})
    assert captured["budget"].call_max_rows == 7
    assert captured["budget"].call_deadline_ms == 120
    # 出了作用域立刻恢复（绝不泄漏给下一次调用）
    cap_history.search(_STORE, params={"query": "x"})
    assert captured["budget"].call_max_rows == 512
    assert captured["budget"].call_deadline_ms == 2500


def test_chat_lane_source_reserves_history_budget_before_dispatch():
    source = inspect.getsource(worker.process_job)
    assert "reserve_call()" in source
    assert "settle_call(" in source
    assert "cap_history.call_limits(" in source
    # 结算走同一个判定函数（保守兜底在里面），不是就地读 metadata
    assert "_history_rows_charged(" in source


def test_history_call_with_unknown_usage_is_charged_its_whole_lease():
    """已经开跑却拿不到可信计数 → 按 lease 上限保守结算（spec §5）。

    旧写法 ``scanned_rows = 0``：capability 抛异常被 executor 吞成
    ``error: capability_failed``（或 metadata 通道丢了）时，这一次扫描按 0 行
    入账，下一次仍然租出满额 1500——多轮快速失败就能突破回合累计闸。
    宁可多扣，不许白嫖。
    """
    budget = worker._HistoryTurnBudget(max_rows=1500, max_wall_sec=8)
    lease = budget.reserve_call()
    assert lease.max_rows == 1500
    swallowed = ToolResult(call_id="h1", content="error: capability_failed")
    budget.settle_call(
        lease, scanned_rows=worker._history_rows_charged(swallowed, lease))
    assert budget.exhausted()
    assert budget.reserve_call() is None


def test_history_partial_lease_charge_shrinks_the_next_lease():
    """租出 400 → 用量未知 → 下一次 lease 必须相应缩小，而不是照样 1500。"""
    budget = worker._HistoryTurnBudget(max_rows=1500, max_wall_sec=8)
    budget.settle_call(budget.reserve_call(), scanned_rows=1100)
    lease = budget.reserve_call()
    assert lease.max_rows == 400
    lost = ToolResult(call_id="h2", content="error: capability_failed")
    budget.settle_call(lease, scanned_rows=worker._history_rows_charged(lost, lease))
    assert budget.used_rows == 1500
    assert budget.reserve_call() is None


def test_history_errors_that_never_reached_the_scan_settle_at_zero():
    """本地校验 / kill switch / dispatch gate / 锚点未命中 = 0 行，不许乱扣。"""
    budget = worker._HistoryTurnBudget(max_rows=1500, max_wall_sec=8)
    lease = budget.reserve_call()
    for content in (
        "error: tool_not_allowed",                       # kill switch / dispatch gate
        f"error: {cap_errors.INVALID}",                  # facade 本地参数校验
        "error: cursor_invalid",
        "error: cursor_mismatch",
        "error: missing_query_or_time_range",
        "error: invalid_limit",
        "error: invalid_neighbor_count",
        "error: not_found_or_not_visible",               # 锚点查空，未发 enclave
        "error: invalid args for history_search: bad",   # tool_schema 校验
        "error: unparseable args for history_search",
        "error: unknown tool history_search",
    ):
        result = ToolResult(call_id="h", content=content)
        assert worker._history_rows_charged(result, lease) == 0, content


def test_history_trusted_metadata_still_settles_the_actual_rows():
    budget = worker._HistoryTurnBudget(max_rows=1500, max_wall_sec=8)
    lease = budget.reserve_call()
    ok_result = ToolResult(
        call_id="h1", content='{"matches": []}',
        metadata={"history_scanned_rows": 12,
                  "result_budget_kind": "history_search"})
    assert worker._history_rows_charged(ok_result, lease) == 12
    # 可信的实际 0（扫完了但一行都没检查）仍然记 0
    zero_result = ToolResult(
        call_id="h2", content='{"matches": []}',
        metadata={"history_scanned_rows": 0,
                  "result_budget_kind": "history_search"})
    assert worker._history_rows_charged(zero_result, lease) == 0


def test_history_round_gate_untouched_without_history_calls():
    budget = worker._HistoryTurnBudget(max_rows=1, max_wall_sec=0.001)
    budget.settle_call(budget.reserve_call(), scanned_rows=5)
    calls = [
        ToolCall(id="m1", name="memory_search", args={"query": "x"}),
        ToolCall(id="w1", name="web_search", args={"query": "x"}),
    ]
    assert worker._history_round_blocked_results(calls, budget=budget) == {}


def test_facade_switch_off_is_dispatch_denial_end_to_end(monkeypatch):
    """开关关闭 + 目录隐藏被绕过（直接 dispatch）→ facade 仍拒。
    走真实 executor→registry→facade 链路，不打桩 run_capability。"""
    _patch_secret(monkeypatch)
    monkeypatch.setenv(cap_history.ENABLED_ENV, "0")
    (result,) = _dispatch(
        [ToolCall(id="h1", name="history_search", args={"query": "x"})],
        history_tools_allowed=True)  # 即使 executor 层被错误放行
    assert result.content == "error: tool_not_allowed"


# ---------------------------------------------------------------------------
# G. metadata 通道
# ---------------------------------------------------------------------------


def test_history_result_metadata_projection():
    search_ok = {"ok": True, "data": _search_result(scanned=7)}
    assert activity_metadata.history_result_metadata(
        "history_search", search_ok) == {
            "history_scanned_rows": 7, "result_budget_kind": "history_search"}
    fetch_ok = {"ok": True, "data": {
        "anchor": {}, "before": [{}, {}], "after": [{}], "unavailable_count": 0,
        "omitted_before": 0, "omitted_after": 0}}
    assert activity_metadata.history_result_metadata(
        "history_fetch", fetch_ok) == {
            "history_scanned_rows": 4, "result_budget_kind": "history_fetch"}
    assert activity_metadata.history_result_metadata(
        "memory_search", search_ok) == {}
    assert activity_metadata.history_result_metadata(
        "history_search", {"ok": False}) == {}


def test_fetch_row_accounting_counts_messages_dropped_by_the_shrink():
    """回合预算按**结构化缩减前**的真实扫描行数记账。

    缩减丢掉的邻居已经在 enclave 里解密过了，按缩减后的列表记账 = 每次 fetch
    都少算一截，窗口放宽到 31 条之后失真最大。
    """
    shrunk = {"ok": True, "data": {
        "anchor": {}, "before": [{}, {}], "after": [{}],
        "unavailable_count": 0, "omitted_before": 13, "omitted_after": 3}}
    assert activity_metadata.history_result_metadata(
        "history_fetch", shrunk)["history_scanned_rows"] == 1 + 3 + 16


def test_turn_budget_rejects_invalid_config():
    with pytest.raises(ValueError):
        worker._HistoryTurnBudget(max_rows=0, max_wall_sec=8)
    with pytest.raises(ValueError):
        worker._HistoryTurnBudget(max_rows=10, max_wall_sec=0)
