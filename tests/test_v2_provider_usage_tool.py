"""Task 5: provider_usage tool-schema catalog entry + lane scoping.

Pure unit — only imports ``capabilities.tool_schema`` and
``model_api_runtime.v2.worker``, no DB. See
``.superpowers/sdd/task-5-brief.md`` for the binding design decisions:
static catalog (not extra_tool_specs), subagent auto-exclusion via
absence from ``_SUBAGENT_ALLOWED_TOOLS``, not in
``provenance.EXTERNAL_READS``, yes in ``_PRIVATE_READ_TOOLS``, and
unconditionally withheld from the wake/screen_watch/manual_wake lane.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from capabilities import tool_schema
from provider_types import ToolCall


def test_provider_usage_in_catalog_no_params():
    specs = {s.name: s for s in tool_schema.build_tool_specs()}
    assert tool_schema.PROVIDER_USAGE_TOOL in specs
    spec = specs[tool_schema.PROVIDER_USAGE_TOOL]
    assert spec.parameters.get("properties") == {}
    assert "余额" in spec.description or "usage" in spec.description.lower()


def test_provider_usage_args_validate_empty_only():
    assert tool_schema.validate_tool_args(tool_schema.PROVIDER_USAGE_TOOL, {}) is None
    err = tool_schema.validate_tool_args(tool_schema.PROVIDER_USAGE_TOOL, {"x": 1})
    assert err  # non-empty args rejected


def test_provider_usage_excluded_from_subagent_and_private_read():
    from model_api_runtime.v2 import worker

    assert tool_schema.PROVIDER_USAGE_TOOL not in worker._SUBAGENT_ALLOWED_TOOLS
    assert tool_schema.PROVIDER_USAGE_TOOL in worker._SUBAGENT_DISABLED_TOOLS
    assert tool_schema.PROVIDER_USAGE_TOOL in worker._PRIVATE_READ_TOOLS


def test_provider_usage_not_in_external_reads():
    # Design decision: results are our own normalized JSON (numbers/enums/
    # truncated slugs), not third-party free text — must not trip the
    # external-content fence.
    from model_api_runtime.v2 import provenance

    assert tool_schema.PROVIDER_USAGE_TOOL not in provenance.EXTERNAL_READS


def test_provider_usage_withheld_from_wake_lane_source():
    """The wake/screen_watch/manual_wake lane (``_run_wake``) must never be
    able to offer this tool — read the source of ``_run_wake`` and assert
    the disabled-tool-names expression it builds for its
    ``run_tool_loop`` call includes ``PROVIDER_USAGE_TOOL``.

    This is a source-level assertion (not a call-through unit test)
    because ``_run_wake`` requires full DB-backed TurnDeps to execute.
    """
    import inspect

    from model_api_runtime.v2 import worker

    source = inspect.getsource(worker._run_wake)
    assert "PROVIDER_USAGE_TOOL" in source, (
        "expected _run_wake's disabled_tool_names construction to "
        "reference cap_tool_schema.PROVIDER_USAGE_TOOL"
    )


# ---------------------------------------------------------------------------
# Task 6: V2 dispatch branch — closure over turn provider_config, live halt
# re-check, classification routing in `_dispatch_mixed_tool_calls`.
# ---------------------------------------------------------------------------
import asyncio
import json


def test_dispatcher_returns_normalized_payload(monkeypatch):
    import provider_client as pc
    from core import provider_usage as pu
    from model_api_runtime.v2 import worker

    cfg = pc.ProviderConfig(provider="deepseek", model="m", api_key="sk-x")

    async def fake_query(config):
        assert config is cfg  # same object — no re-decrypt
        m = pu._empty_metrics()
        m["balance"] = pu._metric(
            "ok", amounts=[{"amount": "25.06", "unit": "CNY"}], scope="account"
        )
        return pu.build_payload("deepseek", "deepseek_balance", m)

    monkeypatch.setattr(pu, "query_usage_async", fake_query)
    monkeypatch.setattr(worker.kill_switch, "provider_usage_halted", lambda: False)
    dispatch = worker._make_provider_usage_dispatcher(provider_config=cfg)
    calls = [ToolCall(id="c1", name=tool_schema.PROVIDER_USAGE_TOOL, args={})]
    results = asyncio.run(dispatch(calls))
    body = json.loads(results[0].content)
    assert body["metrics"]["balance"]["status"] == "ok"
    assert "sk-x" not in results[0].content


def test_dispatcher_same_object_identity_across_multiple_calls(monkeypatch):
    """Two calls in one batch must both receive the exact same provider_config
    object — the dispatcher never re-resolves/re-decrypts per call."""
    import provider_client as pc
    from core import provider_usage as pu
    from model_api_runtime.v2 import worker

    cfg = pc.ProviderConfig(provider="deepseek", model="m", api_key="sk-x")
    seen_configs = []

    async def fake_query(config):
        seen_configs.append(config)
        return pu.build_payload("deepseek", "deepseek_balance", pu._empty_metrics())

    monkeypatch.setattr(pu, "query_usage_async", fake_query)
    monkeypatch.setattr(worker.kill_switch, "provider_usage_halted", lambda: False)
    dispatch = worker._make_provider_usage_dispatcher(provider_config=cfg)
    calls = [
        ToolCall(id="c1", name=tool_schema.PROVIDER_USAGE_TOOL, args={}),
        ToolCall(id="c2", name=tool_schema.PROVIDER_USAGE_TOOL, args={}),
    ]
    results = asyncio.run(dispatch(calls))
    assert len(results) == 2
    assert all(config is cfg for config in seen_configs)
    assert [r.call_id for r in results] == ["c1", "c2"]


def test_dispatcher_live_halt(monkeypatch):
    import provider_client as pc
    from model_api_runtime.v2 import worker

    cfg = pc.ProviderConfig(provider="deepseek", model="m", api_key="sk-x")
    monkeypatch.setattr(worker.kill_switch, "provider_usage_halted", lambda: True)
    dispatch = worker._make_provider_usage_dispatcher(provider_config=cfg)
    calls = [ToolCall(id="c1", name=tool_schema.PROVIDER_USAGE_TOOL, args={})]
    results = asyncio.run(dispatch(calls))
    assert results[0].content == "error: provider_usage_halted"


def test_dispatcher_halt_check_raises_fails_closed(monkeypatch):
    """A broken/raising kill-switch read must be treated as halted, not
    propagate — this is the dispatcher's own belt-and-suspenders try/except
    around `kill_switch.provider_usage_halted()`."""
    import provider_client as pc
    from model_api_runtime.v2 import worker

    cfg = pc.ProviderConfig(provider="deepseek", model="m", api_key="sk-x")

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(worker.kill_switch, "provider_usage_halted", _boom)
    dispatch = worker._make_provider_usage_dispatcher(provider_config=cfg)
    calls = [ToolCall(id="c1", name=tool_schema.PROVIDER_USAGE_TOOL, args={})]
    results = asyncio.run(dispatch(calls))
    assert results[0].content == "error: provider_usage_halted"


def test_dispatcher_unexpected_exception_is_static_error(monkeypatch):
    import provider_client as pc
    from core import provider_usage as pu
    from model_api_runtime.v2 import worker

    cfg = pc.ProviderConfig(provider="deepseek", model="m", api_key="sk-x")

    async def fake_query(config):
        raise RuntimeError("boom: sk-x leaked in exception text")

    monkeypatch.setattr(pu, "query_usage_async", fake_query)
    monkeypatch.setattr(worker.kill_switch, "provider_usage_halted", lambda: False)
    dispatch = worker._make_provider_usage_dispatcher(provider_config=cfg)
    calls = [ToolCall(id="c1", name=tool_schema.PROVIDER_USAGE_TOOL, args={})]
    results = asyncio.run(dispatch(calls))
    assert results[0].content == "error: provider_usage_failed"
    assert "sk-x" not in results[0].content


def test_dispatch_mixed_routes_provider_usage_to_injected_callable():
    """`_dispatch_mixed_tool_calls` classification loop routes the tool by
    name to the injected `dispatch_provider_usage` callable."""
    from model_api_runtime.v2 import worker

    async def _fake_dispatch(tool_calls):
        return [
            worker.ToolResult(call_id=tc.id, content="ok:" + tc.id)
            for tc in tool_calls
        ]

    async def _unreachable_platform(tc):
        raise AssertionError("provider_usage must not reach platform dispatch")

    async def _unreachable_before_mutation():
        raise AssertionError("no mutation expected for provider_usage")

    calls = [ToolCall(id="c1", name=tool_schema.PROVIDER_USAGE_TOOL, args={})]
    results = asyncio.run(
        worker._dispatch_mixed_tool_calls(
            calls,
            mcp_turn=worker._EMPTY_MCP_TURN,
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=_unreachable_platform,
            before_mcp_mutation=_unreachable_before_mutation,
            read_parallelism=1,
            mcp_timeout_sec=5.0,
            dispatch_provider_usage=_fake_dispatch,
        )
    )
    assert results[0].call_id == "c1"
    assert results[0].content == "ok:c1"


def test_dispatch_mixed_provider_usage_tool_not_allowed_when_callable_none():
    """wake/subagent lanes bind no `dispatch_provider_usage` — belt-and-
    suspenders: even if the tool name slipped through offer-time exclusion,
    dispatch must still refuse it rather than silently drop/route it."""
    from model_api_runtime.v2 import worker

    async def _unreachable_platform(tc):
        raise AssertionError("provider_usage must not reach platform dispatch")

    async def _unreachable_before_mutation():
        raise AssertionError("no mutation expected for provider_usage")

    calls = [ToolCall(id="c1", name=tool_schema.PROVIDER_USAGE_TOOL, args={})]
    results = asyncio.run(
        worker._dispatch_mixed_tool_calls(
            calls,
            mcp_turn=worker._EMPTY_MCP_TURN,
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=_unreachable_platform,
            before_mcp_mutation=_unreachable_before_mutation,
            read_parallelism=1,
            mcp_timeout_sec=5.0,
            # dispatch_provider_usage omitted -> defaults to None
        )
    )
    assert results[0].call_id == "c1"
    assert results[0].content == "error: tool_not_allowed"


def test_dispatcher_never_touches_enclave_semaphore():
    """Source-level assertion (no runtime fixture short of a real semaphore
    deadlock could prove this): `_make_provider_usage_dispatcher` must not
    reference `enclave_sem`/`ENCLAVE_SEMAPHORE` — the third-party usage HTTP
    call is a plain `await`, never wrapped in the enclave gate. It is called
    from `_dispatch_mixed_tool_calls`'s read/task phase, the same layer
    `_make_task_batch_dispatcher`'s `_dispatch` runs at, which is also outside
    that semaphore."""
    import inspect

    from model_api_runtime.v2 import worker

    source = inspect.getsource(worker._make_provider_usage_dispatcher)
    assert "enclave_sem" not in source
    assert "ENCLAVE_SEMAPHORE" not in source
