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


# --- V2 判据:这条路以前几乎什么都不查,四种假绿都能过 -----------------------

def _v2(**detail):
    base = {"expected": 1, "resolved": 1, "skipped": []}
    base.update(detail)
    return [{"type": "agent.model.call.done", "detail": {"driver": "v2"}},
            {"type": "mcp.surface.resolved", "detail": base}]


def test_v2_zero_expected_is_not_a_pass():
    """运行时说它一台都没看到,而我们配了一台 —— 这是失配,不是通过。"""
    code, _ = classify(_v2(expected=0, resolved=0), runtime="v2", expect="ok",
                       server_count=1)
    assert code == 1


def test_v2_config_list_failure_is_no_observation_not_a_pass():
    """配置列表本轮读不出来 = 对服务器零观测。

    拿它当「每台都好」是凭空造证据 —— 而且是这条探针最该防的那种造。
    """
    code, _ = classify(_v2(surface_failure_kind="envelopes_unavailable"),
                       runtime="v2", expect="ok", server_count=1)
    assert code == 3


def test_v2_inconsistent_counts_are_rejected():
    """resolved + skipped != expected:这份 summary 自相矛盾,不能从中读判定。"""
    code, _ = classify(_v2(expected=2, resolved=1, skipped=[]),
                       runtime="v2", expect="ok", server_count=2)
    assert code == 3


def test_v2_expect_failed_rejects_a_healthy_surface():
    """反向用法也要成立:期望失败却每台都好,那就是没复现出来。"""
    code, _ = classify(_v2(), runtime="v2", expect="failed", server_count=1)
    assert code == 1


def test_v2_expect_failed_passes_on_a_real_skip():
    code, _ = classify(
        _v2(expected=2, resolved=1,
            skipped=[{"name": "dead", "kind": "transport_failure"}]),
        runtime="v2", expect="failed", server_count=2)
    assert code == 0


def test_v2_refuses_expectations_it_cannot_represent():
    """recovered / inconclusive 是 V1 才有的概念(工具面会中途变)。

    V2 同步加载,一台要么进了要么没进。让它「通过」一个它根本表达不了的期望,
    等于把探针变成许愿池。
    """
    for expect in ("recovered", "inconclusive"):
        code, _ = classify(_v2(), runtime="v2", expect=expect, server_count=1)
        assert code == 2, expect


def test_v2_pre_deploy_event_shape_says_deploy_first():
    events = [{"type": "agent.model.call.done", "detail": {"driver": "v2"}},
              {"type": "mcp.surface.resolved", "detail": {"kept": 3, "servers": 1}}]
    code, lines = classify(events, runtime="v2", expect="ok", server_count=1)
    assert code == 3
    assert any("先发版" in x for x in lines)


# --- 完整观测 / 授权 ---------------------------------------------------------

def test_a_missing_driver_receipt_is_no_observation():
    """埋点是各自独立的守护线程发的,MCP 事件先到、driver 回执还在路上是真会发生的。

    此时分类等于在给一个没确认过运行时的轮次下判决。
    """
    events = [e for e in _healthy_v1() if e["type"] != "agent.model.call.done"]
    code, _ = classify(events, runtime="resident", expect="ok", server_count=1)
    assert code == 3


def test_explicitly_missing_grant_rule_fails_even_when_init_looked_fine():
    """wired 但 has_grant_rule=false —— 这正是这条探针最初要抓的形态之一。

    init 连上了、且本轮没被调用,证明不了模型有权调用它;而
    has_grant_rule=false 是「授权缺失」的**正证据**(反过来 true 证明不了有效)。
    """
    events = _healthy_v1()
    events[2] = {"type": "mcp.surface.wired",
                 "detail": {"wired": True, "has_grant_rule": False,
                            "ungranted": ["deepwiki"]}}
    code, lines = classify(events, runtime="resident", expect="ok",
                           server_count=1)
    assert code == 1
    assert any("授权" in x for x in lines)


def test_verdict_must_cover_every_configured_server():
    code, _ = classify(_healthy_v1({"a": "ok"}), runtime="resident",
                       expect="ok", server_count=2)
    assert code == 1


# --- 轮询终止条件 ------------------------------------------------------------

def test_polling_waits_for_the_runtime_specific_terminal_event():
    """V2 永远不发 registered。等它 = 每次健康的 V2 运行都白等满 --trace-timeout。"""
    from tools.e2e.user_mcp_handshake_probe import (
        observation_complete, terminal_event_type)
    assert terminal_event_type("resident") == "mcp.surface.registered"
    assert terminal_event_type("v2") == "mcp.surface.resolved"

    v2_done = [{"type": "agent.model.call.done"}, {"type": "mcp.surface.resolved"}]
    assert observation_complete(v2_done, "v2")
    assert not observation_complete(v2_done, "resident")
    # MCP 事件到了但 driver 回执没到 —— 还不能收
    assert not observation_complete([{"type": "mcp.surface.resolved"}], "v2")
