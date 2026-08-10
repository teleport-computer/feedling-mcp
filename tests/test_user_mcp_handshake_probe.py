"""The MCP handshake probe's verdict logic.

Why this file exists: the probe shipped twice claiming fixes that had silently
regressed, because every check lived inline in ``main()`` and nothing could
call it. Two of four claimed P1 fixes were still broken when reviewed (codex
审出). The verdict is now a pure function, and these lock the branches that a
live run only exercises by luck — a healthy test environment never produces a
timeout, a stale consumer, or a V2/V1 mix-up on demand.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.e2e.user_mcp_handshake_probe import classify  # noqa: E402


def _ev(type_, **detail):
    return {"type": type_, "detail": detail}


def _healthy_v1(verdict=None):
    return [
        _ev("mcp.materialize.applied", enabled_count=1, configured_count=1),
        _ev("agent.model.call.done", driver="claude"),
        _ev("mcp.surface.wired", wired=True),
        _ev("mcp.surface.registered", verdict=verdict or {"deepwiki": "ok"}),
    ]


def test_healthy_v1_run_passes():
    code, lines = classify(_healthy_v1(), runtime="resident", expect="ok",
                           server_count=1)
    assert code == 0
    assert any("PASS" in x for x in lines)


def test_v2_does_not_require_the_v1_materialize_and_wire_events():
    """V2 从信封同步加载工具面,压根没有 consumer 的 materialize/wired 步骤。

    照旧要求它们,会让 --runtime v2 在完全健康的运行上恒定退出 3 ——
    README 里宣传的那条命令永远不可能成功(codex 审出)。
    """
    events = [_ev("agent.model.call.done", driver="v2"),
              _ev("mcp.surface.resolved", expected=1, resolved=1, skipped=[])]
    code, _ = classify(events, runtime="v2", expect="ok", server_count=1)
    assert code == 0


def test_v2_reports_a_server_that_never_made_the_surface():
    events = [_ev("agent.model.call.done", driver="v2"),
              _ev("mcp.surface.resolved", expected=2, resolved=1,
                  skipped=[{"name": "dead", "kind": "transport_failure"}])]
    code, lines = classify(events, runtime="v2", expect="ok", server_count=2)
    assert code == 1
    assert any("dead" in x for x in lines)


def test_running_on_the_wrong_runtime_is_a_failure_not_a_pass():
    """钉了 V1 却跑在 V2 上,结果不能用 —— 那测的是没坏的那个系统。"""
    events = _healthy_v1()
    events[1] = _ev("agent.model.call.done", driver="v2")
    code, lines = classify(events, runtime="resident", expect="ok",
                           server_count=1)
    assert code == 1
    assert any("driver=v2" in x for x in lines)


def test_a_missing_registered_event_is_not_success():
    """没有观测 ≠ 没有问题。这条探针存在的理由就是这个 —— 有一整天,
    「埋点没出现」被当成了「没问题」。"""
    events = [e for e in _healthy_v1() if e["type"] != "mcp.surface.registered"]
    code, _ = classify(events, runtime="resident", expect="ok", server_count=1)
    assert code == 3


def test_an_old_consumer_is_reported_as_deploy_first_not_as_mcp_broken():
    """事件在、但没有 verdict 字段 = 部署的 consumer 早于两阶段判据。

    和「判定不符」分开报,否则有人会拿着它去 debug MCP,而真答案是「先发版」。
    """
    events = _healthy_v1()
    events[-1] = {"type": "mcp.surface.registered",
                  "detail": {"registered": ["deepwiki"], "missing": []}}
    code, lines = classify(events, runtime="resident", expect="ok",
                           server_count=1)
    assert code == 3
    assert any("先发版" in x for x in lines)


def test_enabled_count_mismatch_is_caught():
    code, _ = classify(_healthy_v1(), runtime="resident", expect="ok",
                       server_count=5)
    assert code == 1


def test_verdict_mismatch_exits_one():
    code, _ = classify(_healthy_v1({"deepwiki": "failed"}), runtime="resident",
                       expect="ok", server_count=1)
    assert code == 1


def test_expect_failed_passes_when_the_server_really_failed():
    """探针要能两头用:证明它坏了,和证明它好了,同样重要。"""
    code, _ = classify(_healthy_v1({"deepwiki": "failed"}), runtime="resident",
                       expect="failed", server_count=1)
    assert code == 0


def test_expect_any_accepts_whatever_the_verdict_says():
    code, _ = classify(_healthy_v1({"deepwiki": "inconclusive"}),
                       runtime="resident", expect="any", server_count=1)
    assert code == 0
