"""Deterministic tests for the worker's mixed platform/MCP batch scheduler.

These stay below ``process_job`` so barriers can prove actual overlap, ordering,
and timeout isolation without a database or timing-dependent sleeps.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from provider_types import ToolCall, ToolResult, ToolSpec
from model_api_runtime.v2 import worker


def _call(call_id: str, name: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, args={})


class _DispatchingMcpTurn:
    def __init__(self, names, dispatch):
        self.tool_specs = tuple(
            ToolSpec(name=name, description=name, parameters={})
            for name in names
        )
        self._names = frozenset(names)
        self._dispatch = dispatch

    def handles(self, name: str) -> bool:
        return name in self._names

    async def dispatch(self, call: ToolCall) -> ToolResult:
        return await self._dispatch(call)


def test_platform_reads_really_overlap_but_results_keep_model_order():
    async def _scenario():
        first_started = asyncio.Event()
        second_finished = asyncio.Event()
        events = []

        async def _platform_dispatch(call):
            if call.id == "first":
                events.append("first:start")
                first_started.set()
                await second_finished.wait()
                events.append("first:end")
            else:
                await first_started.wait()
                events.append("second:end")
                second_finished.set()
            return ToolResult(call_id=call.id, content=f"{call.id} result")

        mcp = _DispatchingMcpTurn([], None)
        calls = [
            _call("first", "memory_index"),
            _call("second", "perception_snapshot"),
        ]
        results = await worker._dispatch_mixed_tool_calls(
            calls,
            mcp_turn=mcp,
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=_platform_dispatch,
            before_mcp_mutation=lambda: None,
            read_parallelism=2,
            mcp_timeout_sec=1,
        )

        assert events == ["first:start", "second:end", "first:end"]
        assert [result.call_id for result in results] == ["first", "second"]
        assert [result.content for result in results] == [
            "first result", "second result"]

    asyncio.run(_scenario())


def test_approved_mcp_and_platform_reads_overlap_under_one_gate():
    async def _scenario():
        mcp_started = asyncio.Event()
        platform_started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0

        async def _enter(call):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if call.name.startswith("mcp__"):
                mcp_started.set()
            else:
                platform_started.set()
            await release.wait()
            active -= 1
            return ToolResult(call_id=call.id, content="ok")

        mcp = _DispatchingMcpTurn(["mcp__files__search"], _enter)
        task = asyncio.create_task(worker._dispatch_mixed_tool_calls(
            [
                _call("mcp", "mcp__files__search"),
                _call("platform", "memory_index"),
            ],
            mcp_turn=mcp,
            # An exact catalog approval removes this MCP tool from the mutating
            # set; without that approval the same call is serialized below.
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=_enter,
            before_mcp_mutation=lambda: None,
            read_parallelism=2,
            mcp_timeout_sec=1,
        ))
        await asyncio.wait_for(mcp_started.wait(), timeout=1)
        await asyncio.wait_for(platform_started.wait(), timeout=1)
        assert active == 2
        assert max_active == 2
        release.set()
        results = await task

        assert [result.call_id for result in results] == ["mcp", "platform"]
        assert max_active == 2

    asyncio.run(_scenario())


def test_platform_read_parallelism_gate_is_shared_across_the_batch():
    async def _scenario():
        release = asyncio.Event()
        two_started = asyncio.Event()
        active = 0
        max_active = 0

        async def _read(call):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                two_started.set()
            await release.wait()
            active -= 1
            return ToolResult(call_id=call.id, content="ok")

        mcp = _DispatchingMcpTurn([], None)
        calls = [
            _call("p1", "memory_index"),
            _call("p2", "perception_snapshot"),
            _call("p3", "web_search"),
            _call("p4", "web_fetch"),
        ]
        task = asyncio.create_task(worker._dispatch_mixed_tool_calls(
            calls,
            mcp_turn=mcp,
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=_read,
            before_mcp_mutation=lambda: None,
            read_parallelism=2,
            mcp_timeout_sec=1,
        ))
        await asyncio.wait_for(two_started.wait(), timeout=1)
        assert active == 2
        assert max_active == 2
        release.set()
        results = await task

        assert max_active == 2
        assert [result.call_id for result in results] == [
            "p1", "p2", "p3", "p4"]

    asyncio.run(_scenario())


def test_all_reads_settle_before_cross_domain_mutations_run_in_model_order():
    async def _scenario():
        events = []

        async def _mcp_dispatch(call):
            events.append(f"mutate:{call.name}")
            return ToolResult(call_id=call.id, content="ok")

        async def _platform_dispatch(call):
            kind = "mutate" if call.name == "memory_write" else "read"
            events.append(f"{kind}:{call.name}")
            return ToolResult(call_id=call.id, content="ok")

        async def _before_mcp_mutation():
            events.append("fence:mcp")

        mcp = _DispatchingMcpTurn(
            ["mcp__read", "mcp__write_1", "mcp__write_2"], _mcp_dispatch)
        calls = [
            _call("p-read-1", "memory_index"),
            _call("m-read", "mcp__read"),
            _call("p-read-2", "perception_snapshot"),
            _call("m-write-1", "mcp__write_1"),
            _call("p-write", "memory_write"),
            _call("m-write-2", "mcp__write_2"),
        ]
        results = await worker._dispatch_mixed_tool_calls(
            calls,
            mcp_turn=mcp,
            # Even the server's nominal "read" route is mutating until an
            # independent approval policy exists.
            mutating_mcp_names={
                "mcp__read", "mcp__write_1", "mcp__write_2"},
            dispatch_platform_one=_platform_dispatch,
            before_mcp_mutation=_before_mcp_mutation,
            read_parallelism=2,
            mcp_timeout_sec=1,
        )

        first_mutation = events.index("fence:mcp")
        assert set(events[:first_mutation]) == {
            "read:memory_index", "read:perception_snapshot"}
        assert events[first_mutation:] == [
            "fence:mcp",
            "mutate:mcp__read",
            "fence:mcp",
            "mutate:mcp__write_1",
            "mutate:memory_write",
            "fence:mcp",
            "mutate:mcp__write_2",
        ]
        assert [result.call_id for result in results] == [
            "p-read-1", "m-read", "p-read-2", "m-write-1",
            "p-write", "m-write-2"]

    asyncio.run(_scenario())


def test_all_mcp_tools_are_serial_and_fenced_by_default():
    async def _scenario():
        events = []
        active = 0
        max_active = 0

        async def _mcp_dispatch(call):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            events.append(f"start:{call.id}")
            await asyncio.sleep(0)
            events.append(f"end:{call.id}")
            active -= 1
            return ToolResult(call_id=call.id, content="ok")

        async def _before_mcp_mutation():
            events.append("fence")

        mcp = _DispatchingMcpTurn(
            ["mcp__declared_read", "mcp__write"], _mcp_dispatch)
        results = await worker._dispatch_mixed_tool_calls(
            [
                _call("first", "mcp__declared_read"),
                _call("second", "mcp__write"),
            ],
            mcp_turn=mcp,
            mutating_mcp_names={"mcp__declared_read", "mcp__write"},
            dispatch_platform_one=lambda call: None,
            before_mcp_mutation=_before_mcp_mutation,
            read_parallelism=8,
            mcp_timeout_sec=1,
        )

        assert max_active == 1
        assert events == [
            "fence", "start:first", "end:first",
            "fence", "start:second", "end:second",
        ]
        assert [result.call_id for result in results] == ["first", "second"]

    asyncio.run(_scenario())


def test_ambiguous_mutating_mcp_failures_lock_out_all_later_mutations():
    async def _scenario():
        for failure_kind in ("exception", "transport", "mismatch"):
            dispatches = []
            platform_mutations = []
            fences = []

            async def _mcp_dispatch(call):
                dispatches.append(call.id)
                if failure_kind == "exception":
                    raise OSError("private upstream detail")
                if failure_kind == "transport":
                    return ToolResult(
                        call_id=call.id,
                        content=worker.MCP_TRANSPORT_FAILURE_ERROR,
                    )
                return ToolResult(call_id="wrong-id", content="ok")

            async def _platform_dispatch(call):
                platform_mutations.append(call.id)
                return ToolResult(call_id=call.id, content="ok")

            async def _before_mcp_mutation():
                fences.append("fenced")

            mcp = _DispatchingMcpTurn(
                ["mcp__ambiguous", "mcp__later"], _mcp_dispatch)
            results = await worker._dispatch_mixed_tool_calls(
                [
                    _call("first", "mcp__ambiguous"),
                    _call("platform-later", "memory_write"),
                    _call("mcp-later", "mcp__later"),
                ],
                mcp_turn=mcp,
                mutating_mcp_names={"mcp__ambiguous", "mcp__later"},
                dispatch_platform_one=_platform_dispatch,
                before_mcp_mutation=_before_mcp_mutation,
                read_parallelism=2,
                mcp_timeout_sec=1,
            )

            assert dispatches == ["first"]
            assert platform_mutations == []
            assert fences == ["fenced"]
            assert [result.content for result in results] == [
                worker.v2_tool_loop.MCP_MUTATION_OUTCOME_UNKNOWN_ERROR,
                worker.v2_tool_loop.MUTATION_BLOCKED_AFTER_UNKNOWN_OUTCOME_ERROR,
                worker.v2_tool_loop.MUTATION_BLOCKED_AFTER_UNKNOWN_OUTCOME_ERROR,
            ]

    asyncio.run(_scenario())


def test_mutation_timeout_is_fenced_and_reports_unknown_outcome_without_raising():
    async def _scenario():
        never = asyncio.Event()
        fences = []
        platform_mutations = []

        async def _mcp_dispatch(call):
            await never.wait()
            raise AssertionError("unreachable")

        async def _before_mcp_mutation():
            fences.append("fenced")

        async def _platform_dispatch(call):
            platform_mutations.append(call.id)
            return ToolResult(call_id=call.id, content="ok")

        mcp = _DispatchingMcpTurn(
            ["mcp__slow_write", "mcp__later_write"], _mcp_dispatch)
        results = await worker._dispatch_mixed_tool_calls(
            [
                _call("write", "mcp__slow_write"),
                _call("platform-later", "memory_write"),
                _call("mcp-later", "mcp__later_write"),
            ],
            mcp_turn=mcp,
            mutating_mcp_names={"mcp__slow_write", "mcp__later_write"},
            dispatch_platform_one=_platform_dispatch,
            before_mcp_mutation=_before_mcp_mutation,
            read_parallelism=2,
            mcp_timeout_sec=0.01,
        )

        assert fences == ["fenced"]
        assert platform_mutations == []
        assert results == [
            ToolResult(
                call_id="write",
                content=worker.v2_tool_loop.MCP_MUTATION_OUTCOME_UNKNOWN_ERROR,
            ),
            ToolResult(
                call_id="platform-later",
                content=worker.v2_tool_loop.MUTATION_BLOCKED_AFTER_UNKNOWN_OUTCOME_ERROR,
            ),
            ToolResult(
                call_id="mcp-later",
                content=worker.v2_tool_loop.MUTATION_BLOCKED_AFTER_UNKNOWN_OUTCOME_ERROR,
            ),
        ]

    asyncio.run(_scenario())


def test_tool_level_error_is_ambiguous_and_locks_out_later_mutation():
    async def _scenario():
        events = []

        async def _mcp_dispatch(call):
            events.append("mcp")
            return ToolResult(
                call_id=call.id,
                content="error: mcp_tool_error: permission denied",
            )

        async def _platform_dispatch(call):
            events.append("platform")
            return ToolResult(call_id=call.id, content="ok")

        async def _before_mcp_mutation():
            events.append("fence")

        mcp = _DispatchingMcpTurn(["mcp__known_failure"], _mcp_dispatch)
        results = await worker._dispatch_mixed_tool_calls(
            [
                _call("mcp", "mcp__known_failure"),
                _call("platform", "memory_write"),
            ],
            mcp_turn=mcp,
            mutating_mcp_names={"mcp__known_failure"},
            dispatch_platform_one=_platform_dispatch,
            before_mcp_mutation=_before_mcp_mutation,
            read_parallelism=2,
            mcp_timeout_sec=1,
        )

        assert events == ["fence", "mcp"]
        assert [result.content for result in results] == [
            worker.v2_tool_loop.MCP_MUTATION_OUTCOME_UNKNOWN_ERROR,
            worker.v2_tool_loop.MUTATION_BLOCKED_AFTER_UNKNOWN_OUTCOME_ERROR,
        ]

    asyncio.run(_scenario())


def test_independently_approved_read_only_set_is_respected():
    class _AdvisoryOnlyMcpTurn:
        tool_specs = (
            ToolSpec(name="mcp__legacy_a", description="a", parameters={}),
            ToolSpec(name="mcp__legacy_b", description="b", parameters={}),
        )

        def handles(self, name):
            return name.startswith("mcp__legacy_")

        def is_read_only(self, _name):
            return True

        @property
        def mutating_tool_names(self):
            return frozenset()

    assert worker._mcp_mutating_names_for_turn(
        _AdvisoryOnlyMcpTurn()) == frozenset()


def test_progress_reports_read_phase_and_every_serial_or_blocked_mutation():
    async def _scenario():
        progress = []
        platform_dispatches = []

        async def _mcp_dispatch(call):
            assert call.id == "mcp-first"
            return ToolResult(
                call_id=call.id,
                content=worker.MCP_TRANSPORT_FAILURE_ERROR,
            )

        async def _platform_dispatch(call):
            platform_dispatches.append(call.id)
            return ToolResult(call_id=call.id, content="ok")

        async def _before_mcp_mutation():
            return None

        mcp = _DispatchingMcpTurn(
            ["mcp__first", "mcp__blocked"], _mcp_dispatch)
        results = await worker._dispatch_mixed_tool_calls(
            [
                _call("read", "memory_index"),
                _call("mcp-first", "mcp__first"),
                _call("platform-blocked", "memory_write"),
                _call("mcp-blocked", "mcp__blocked"),
            ],
            mcp_turn=mcp,
            mutating_mcp_names={"mcp__first", "mcp__blocked"},
            dispatch_platform_one=_platform_dispatch,
            before_mcp_mutation=_before_mcp_mutation,
            read_parallelism=2,
            mcp_timeout_sec=1,
            on_progress=progress.append,
        )

        assert platform_dispatches == ["read"]
        assert progress == [
            "tool_read_phase_complete",
            "tool_mutation_complete",
            "tool_mutation_complete",
            "tool_mutation_complete",
        ]
        assert [result.content for result in results] == [
            "ok",
            worker.v2_tool_loop.MCP_MUTATION_OUTCOME_UNKNOWN_ERROR,
            worker.v2_tool_loop.MUTATION_BLOCKED_AFTER_UNKNOWN_OUTCOME_ERROR,
            worker.v2_tool_loop.MUTATION_BLOCKED_AFTER_UNKNOWN_OUTCOME_ERROR,
        ]

    asyncio.run(_scenario())


def test_mcp_wall_budget_is_shared_and_rejects_without_starting_next_call():
    async def _scenario():
        now = [10.0]
        dispatched = []
        fenced = []

        async def _mcp_dispatch(call):
            dispatched.append(call.id)
            now[0] += 3.0
            return ToolResult(call_id=call.id, content="ok")

        async def _before_mcp_mutation():
            fenced.append("fence")

        budget = worker._McpTurnWallBudget(
            3.0, clock=lambda: now[0])
        mcp = _DispatchingMcpTurn(
            ["mcp__first", "mcp__second"], _mcp_dispatch)
        results = await worker._dispatch_mixed_tool_calls(
            [
                _call("first", "mcp__first"),
                _call("second", "mcp__second"),
            ],
            mcp_turn=mcp,
            mutating_mcp_names={"mcp__first", "mcp__second"},
            dispatch_platform_one=lambda call: None,
            before_mcp_mutation=_before_mcp_mutation,
            read_parallelism=2,
            mcp_timeout_sec=45,
            mcp_wall_budget=budget,
        )

        assert dispatched == ["first"]
        assert fenced == ["fence", "fence"]
        assert budget.used_sec == 3.0
        assert results == [
            ToolResult(call_id="first", content="ok"),
            ToolResult(
                call_id="second",
                content=worker.MCP_TURN_WALL_BUDGET_EXHAUSTED_ERROR,
            ),
        ]

    asyncio.run(_scenario())
