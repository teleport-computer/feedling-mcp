"""Deterministic tests for the worker's mixed platform/MCP batch scheduler.

These stay below ``process_job`` so barriers can prove overlap, mutation ordering,
and timeout isolation without a database or timing-dependent sleeps.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from provider_types import ToolCall, ToolExchange, ToolResult, ToolSpec
from capabilities import result_budget as cap_result_budget
from model_api_runtime.v2 import worker


def _call(call_id: str, name: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, args={})


def _path_call(call_id: str, name: str, path: str) -> ToolCall:
    args = {"path": path, "expected_revision": 0}
    if name == "workspace_write":
        args["content"] = call_id
    return ToolCall(id=call_id, name=name, args=args)


@pytest.mark.parametrize("offset", [True, -1, 1.5, "1"])
def test_web_fetch_batch_key_rejects_non_integer_and_negative_offsets(offset):
    call = ToolCall(
        id="bad-offset",
        name="web_fetch",
        args={"url": "https://example.com/page", "offset": offset},
    )

    assert worker._web_fetch_batch_key(call) is None


def test_web_fetch_continuations_wait_outside_the_read_gate_for_batch_owner():
    """Four continuations must not fill a four-permit gate ahead of offset 0."""
    async def _scenario():
        owner_complete = asyncio.Event()
        url = "https://example.com/large"
        calls = [
            ToolCall(
                id=f"continuation-{index}",
                name="web_fetch",
                args={"url": url, "offset": index * 100},
            )
            for index in range(1, 5)
        ]
        calls.append(ToolCall(
            id="owner", name="web_fetch", args={"url": url, "offset": 0}
        ))

        async def _platform_dispatch(call):
            if call.args.get("offset", 0) == 0:
                owner_complete.set()
            else:
                await owner_complete.wait()
            return ToolResult(call_id=call.id, content="ok")

        results = await asyncio.wait_for(
            worker._dispatch_mixed_tool_calls(
                calls,
                mcp_turn=_DispatchingMcpTurn([], None),
                mutating_mcp_names=frozenset(),
                dispatch_platform_one=_platform_dispatch,
                before_mcp_mutation=lambda: None,
                read_parallelism=4,
                mcp_timeout_sec=1,
            ),
            timeout=1,
        )

        assert [result.call_id for result in results] == [call.id for call in calls]
        assert all(result.content == "ok" for result in results)

    asyncio.run(_scenario())


class _DispatchingMcpTurn:
    def __init__(self, names, dispatch):
        self.tool_specs = tuple(
            ToolSpec(name=name, description=name, parameters={}) for name in names
        )
        self._names = frozenset(names)
        self._dispatch = dispatch

    def handles(self, name: str) -> bool:
        return name in self._names

    async def dispatch(self, call: ToolCall) -> ToolResult:
        return await self._dispatch(call)


def test_mcp_call_attempt_is_counted_even_when_dispatch_fails():
    async def _scenario():
        async def _fail(_call):
            raise RuntimeError("transport down")

        async def _before_mutation():
            return None

        called_names = []
        results = await worker._dispatch_mixed_tool_calls(
            [_call("mcp", "mcp__files__search")],
            mcp_turn=_DispatchingMcpTurn(["mcp__files__search"], _fail),
            mutating_mcp_names={"mcp__files__search"},
            dispatch_platform_one=lambda _call: None,
            before_mcp_mutation=_before_mutation,
            read_parallelism=1,
            mcp_timeout_sec=1,
            mcp_called_names=called_names,
        )

        assert called_names == ["mcp__files__search"]
        assert results[0].content == worker.v2_tool_loop.MCP_MUTATION_OUTCOME_UNKNOWN_ERROR
        assert worker._mcp_turn_usage_detail(
            ["mcp__files__search", "mcp__files__status"], called_names
        ) == {
            "offered_tool_count": 2,
            "offered_tool_count_lens": "turn_resolved_before_provider_budget",
            "called_tool_count": 1,
            "call_count": 1,
        }

    asyncio.run(_scenario())


def test_mcp_turn_usage_detail_keeps_zero_call_turns_visible():
    assert worker._mcp_turn_usage_detail(
        ["mcp__files__search", "mcp__files__status"], []
    ) == {
        "offered_tool_count": 2,
        "offered_tool_count_lens": "turn_resolved_before_provider_budget",
        "called_tool_count": 0,
        "call_count": 0,
    }


def test_partial_parallel_results_and_sibling_error_are_observable():
    async def _scenario():
        good_recorded = asyncio.Event()
        events = []

        async def _platform_dispatch(call):
            if call.id == "bad":
                await good_recorded.wait()
                raise worker.RecoverablePlatformReadError("read failed")
            return ToolResult(call_id=call.id, content="evidence")

        async def _tool_event(call, event_kind, payload):
            events.append((call.id, event_kind, payload))
            if call.id == "good" and event_kind == "tool_call_result":
                good_recorded.set()

        results = await worker._dispatch_mixed_tool_calls(
            [_call("good", "memory_index"), _call("bad", "memory_search")],
            mcp_turn=_DispatchingMcpTurn([], None),
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=_platform_dispatch,
            before_mcp_mutation=lambda: None,
            read_parallelism=2,
            mcp_timeout_sec=1,
            on_tool_event=_tool_event,
        )

        assert [result.content for result in results] == [
            "evidence",
            worker._PLATFORM_READ_FAILED,
        ]
        assert ("good", "tool_call_result") in [
            (call_id, kind) for call_id, kind, _payload in events
        ]
        assert ("bad", "tool_call_error") in [
            (call_id, kind) for call_id, kind, _payload in events
        ]

    asyncio.run(_scenario())


def test_platform_read_failure_reaches_model_and_turn_continues(monkeypatch):
    """The real tool loop must carry the failed observation into its next round."""

    class _Provider:
        def __init__(self):
            self.calls = []

        async def __call__(self, config, messages, *, tools=None, **kwargs):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                return {
                    "reply": "",
                    "tool_calls": [
                        {
                            "id": "read-1",
                            "name": "memory_search",
                            "args": {"query": "what do you remember?"},
                        }
                    ],
                    "usage": {},
                }
            return {
                "reply": "I can still answer with the information available.",
                "tool_calls": [],
                "usage": {},
            }

    provider = _Provider()
    monkeypatch.setattr(worker.provider_client, "chat_completion_async", provider)
    transcript_snapshots = []
    replies = []
    failure_counts = {}

    def _build_messages(transcript):
        transcript_snapshots.append(list(transcript))
        return [{"role": "user", "content": "help"}, *transcript]

    async def _dispatch(tool_calls):
        async def _failed_read(_call):
            raise worker.RecoverablePlatformReadError(
                "private operational detail"
            )

        return await worker._dispatch_mixed_tool_calls(
            tool_calls,
            mcp_turn=_DispatchingMcpTurn([], None),
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=_failed_read,
            before_mcp_mutation=lambda: None,
            read_parallelism=1,
            mcp_timeout_sec=1,
            recoverable_platform_read_failures=failure_counts,
        )

    async def _on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    async def _fold():
        return []

    outcome = asyncio.run(
        worker.v2_tool_loop.run_tool_loop(
            provider_config=worker.provider_client.ProviderConfig(
                provider="anthropic",
                model="claude-sonnet-4-test",
                api_key="test-key",
            ),
            build_messages=_build_messages,
            dispatch_tools=_dispatch,
            on_reply=_on_reply,
            fold_new_messages=_fold,
            add_usage=lambda _usage: None,
            max_calls=2,
        )
    )

    assert outcome.final_text == "I can still answer with the information available."
    assert replies == [(outcome.final_text, True)]
    assert len(provider.calls) == 2
    exchange = transcript_snapshots[1][-1]
    assert isinstance(exchange, ToolExchange)
    assert exchange.results == (
        ToolResult(call_id="read-1", content=worker._PLATFORM_READ_FAILED),
    )
    assert "private operational detail" not in exchange.results[0].content


def test_platform_read_failure_cap_is_shared_across_provider_rounds():
    async def _scenario():
        failure_counts = {}

        async def _failed_read(_call):
            raise worker.RecoverablePlatformReadError("still down")

        async def _attempt(call_id):
            return await worker._dispatch_mixed_tool_calls(
                [_call(call_id, "memory_search")],
                mcp_turn=_DispatchingMcpTurn([], None),
                mutating_mcp_names=frozenset(),
                dispatch_platform_one=_failed_read,
                before_mcp_mutation=lambda: None,
                read_parallelism=1,
                mcp_timeout_sec=1,
                recoverable_platform_read_failures=failure_counts,
            )

        assert (await _attempt("first"))[0].content == worker._PLATFORM_READ_FAILED
        assert (await _attempt("second"))[0].content == worker._PLATFORM_READ_FAILED
        with pytest.raises(RuntimeError, match="still down"):
            await _attempt("third")
        assert failure_counts == {"memory_search": 3}

    asyncio.run(_scenario())


@pytest.mark.parametrize(
    "exception_type",
    [worker.LostJobLease, worker.RuntimeModeChanged, worker.CaptureHalted],
)
def test_platform_read_control_flow_exceptions_still_raise(exception_type):
    async def _scenario():
        async def _control(_call):
            raise exception_type("control flow")

        with pytest.raises(exception_type, match="control flow"):
            await worker._dispatch_mixed_tool_calls(
                [_call("read", "memory_search")],
                mcp_turn=_DispatchingMcpTurn([], None),
                mutating_mcp_names=frozenset(),
                dispatch_platform_one=_control,
                before_mcp_mutation=lambda: None,
                read_parallelism=1,
                mcp_timeout_sec=1,
            )

    asyncio.run(_scenario())


def test_unknown_recoverable_error_subclass_fails_closed():
    class FutureControlSignal(worker.RecoverablePlatformReadError):
        pass

    with pytest.raises(FutureControlSignal, match="future signal"):
        worker._recover_platform_read_failure(
            _call("read", "memory_search"),
            FutureControlSignal("future signal"),
            {},
        )


def test_bare_runtime_error_fails_closed():
    with pytest.raises(RuntimeError, match="invariant broken"):
        worker._recover_platform_read_failure(
            _call("read", "memory_search"),
            RuntimeError("invariant broken"),
            {},
        )


def test_effect_delivery_uncertain_error_fails_closed():
    with pytest.raises(
        worker.db.EffectDeliveryUncertainError,
        match="unresolved sink claim",
    ):
        worker._recover_platform_read_failure(
            _call("read", "memory_search"),
            worker.db.EffectDeliveryUncertainError("unresolved sink claim"),
            {},
        )


def test_platform_mutation_failure_remains_terminal():
    async def _scenario():
        async def _failed_mutation(_call):
            raise RuntimeError("outcome unknown")

        with pytest.raises(RuntimeError, match="outcome unknown"):
            await worker._dispatch_mixed_tool_calls(
                [_call("write", "identity_nudge")],
                mcp_turn=_DispatchingMcpTurn([], None),
                mutating_mcp_names=frozenset(),
                dispatch_platform_one=_failed_mutation,
                before_mcp_mutation=lambda: None,
                read_parallelism=1,
                mcp_timeout_sec=1,
                recoverable_platform_read_failures={},
            )

    asyncio.run(_scenario())


def test_discarded_platform_mutation_is_a_bounded_model_visible_failure():
    failures = {}
    call = _call("write", "identity_nudge")

    first = worker._recover_discarded_platform_mutation(
        call,
        {"status": "discarded", "last_error": "private sink detail"},
        failures,
    )
    second = worker._recover_discarded_platform_mutation(
        call,
        {"status": "discarded"},
        failures,
    )

    assert first == ToolResult(
        call_id="write", content=worker._PLATFORM_READ_FAILED
    )
    assert second.content == worker._PLATFORM_READ_FAILED
    assert "private sink detail" not in first.content
    with pytest.raises(RuntimeError, match="discarded"):
        worker._recover_discarded_platform_mutation(
            call,
            {"status": "discarded"},
            failures,
        )
    assert failures == {"identity_nudge": 3}


@pytest.mark.parametrize(
    "disposition, expected_status",
    [
        pytest.param(None, "missing", id="missing-row"),
        pytest.param(
            {
                "status": "pending",
                "last_error": "dispatch_failed:EffectTerminalError",
            },
            "pending",
            id="pending-even-with-terminal-error-text",
        ),
        pytest.param(
            {"status": "reconciliation_required"},
            "reconciliation_required",
            id="reconciliation-required",
        ),
        pytest.param(
            {"status": "delivery_uncertain"},
            "delivery_uncertain",
            id="delivery-uncertain",
        ),
        pytest.param(
            {"status": "future_status"},
            "future_status",
            id="unknown-future-status",
        ),
    ],
)
def test_non_discarded_platform_mutation_dispositions_fail_closed(
    disposition, expected_status,
):
    with pytest.raises(RuntimeError, match=expected_status):
        worker._recover_discarded_platform_mutation(
            _call("write", "identity_nudge"),
            disposition,
            {},
        )


def test_subagent_lost_lease_is_not_converted_to_a_task_error():
    async def _scenario():
        async def _lost_lease(_calls):
            raise worker.LostJobLease("lease lost in task")

        with pytest.raises(worker.LostJobLease, match="lease lost in task"):
            await worker._dispatch_mixed_tool_calls(
                [ToolCall("task", "task", {"prompt": "inspect"})],
                mcp_turn=_DispatchingMcpTurn([], None),
                mutating_mcp_names=frozenset(),
                dispatch_platform_one=lambda _call: None,
                dispatch_task_batch=_lost_lease,
                before_mcp_mutation=lambda: None,
                read_parallelism=1,
                mcp_timeout_sec=1,
            )

    asyncio.run(_scenario())


def test_task_batch_overlaps_reads_and_both_settle_before_mutation():
    async def _scenario():
        read_started = asyncio.Event()
        task_started = asyncio.Event()
        release = asyncio.Event()
        mutation_started = asyncio.Event()

        async def _platform_dispatch(call):
            if call.name == "memory_index":
                read_started.set()
                await release.wait()
            else:
                mutation_started.set()
            return ToolResult(call_id=call.id, content=f"platform:{call.id}")

        async def _tasks(calls):
            task_started.set()
            await release.wait()
            return [
                ToolResult(call_id=call.id, content=f"task:{call.id}") for call in calls
            ]

        calls = [
            _call("write", "memory_write"),
            ToolCall("task", "task", {"prompt": "inspect"}),
            _call("read", "memory_index"),
        ]
        running = asyncio.create_task(
            worker._dispatch_mixed_tool_calls(
                calls,
                mcp_turn=_DispatchingMcpTurn([], None),
                mutating_mcp_names=frozenset(),
                dispatch_platform_one=_platform_dispatch,
                dispatch_task_batch=_tasks,
                before_mcp_mutation=lambda: None,
                read_parallelism=2,
                mcp_timeout_sec=1,
            )
        )

        await asyncio.wait_for(read_started.wait(), timeout=1)
        await asyncio.wait_for(task_started.wait(), timeout=1)
        assert not mutation_started.is_set()
        release.set()
        results = await running

        assert mutation_started.is_set()
        assert [result.call_id for result in results] == ["write", "task", "read"]
        assert [result.content for result in results] == [
            "platform:write",
            "task:task",
            "platform:read",
        ]

    asyncio.run(_scenario())


def test_workspace_mutations_stay_serial_and_stop_after_failure():
    async def _scenario():
        a_started = asyncio.Event()
        a_release = asyncio.Event()
        events = []

        async def _platform_dispatch(call):
            events.append(f"start:{call.id}")
            if call.id == "a":
                a_started.set()
                await a_release.wait()
                raise RuntimeError("first write failed")
            events.append(f"end:{call.id}")
            return ToolResult(call_id=call.id, content="ok")

        calls = [
            _path_call("a", "workspace_write", "/workspace/tree"),
            _path_call("b", "workspace_write", "/workspace/other"),
        ]
        running = asyncio.create_task(
            worker._dispatch_mixed_tool_calls(
                calls,
                mcp_turn=_DispatchingMcpTurn([], None),
                mutating_mcp_names=frozenset(),
                dispatch_platform_one=_platform_dispatch,
                before_mcp_mutation=lambda: None,
                read_parallelism=2,
                mcp_timeout_sec=1,
            )
        )

        await asyncio.wait_for(a_started.wait(), timeout=1)
        assert events == ["start:a"]

        a_release.set()
        with pytest.raises(RuntimeError, match="first write failed"):
            await running
        assert events == ["start:a"]

    asyncio.run(_scenario())


def test_platform_effects_are_prepared_in_provider_order_before_launch():
    async def _scenario():
        prepared = []
        observed_at_dispatch = []

        def _prepare(call):
            prepared.append(call.id)

        async def _dispatch(call):
            observed_at_dispatch.append(tuple(prepared))
            return ToolResult(call_id=call.id, content="ok")

        calls = [
            _path_call("first", "workspace_write", "/workspace/a.md"),
            _call("second", "memory_write"),
            _path_call("third", "workspace_write", "/workspace/b.md"),
        ]
        results = await worker._dispatch_mixed_tool_calls(
            calls,
            mcp_turn=_DispatchingMcpTurn([], None),
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=_dispatch,
            prepare_platform_mutation=_prepare,
            before_mcp_mutation=lambda: None,
            read_parallelism=2,
            mcp_timeout_sec=1,
        )

        assert prepared == ["first", "second", "third"]
        assert all(snapshot == tuple(prepared) for snapshot in observed_at_dispatch)
        assert [result.call_id for result in results] == prepared

    asyncio.run(_scenario())


def test_effect_reservations_pin_ordinals_and_gate_enqueue_order():
    async def _scenario():
        reservations = worker._PlatformEffectReservations(
            job_id=77,
            ordinal_counter=itertools.count(),
        )
        first = _path_call(
            "first",
            "workspace_write",
            "/workspace/a.md",
        )
        second = _path_call(
            "second",
            "workspace_write",
            "/workspace/b.md",
        )
        reservations.prepare(first)
        reservations.prepare(second)
        first_effect = reservations.get(first)
        second_effect = reservations.get(second)

        assert (first_effect.ordinal, second_effect.ordinal) == (0, 1)
        assert first_effect.effect_id == worker.v2_effect_id.derive(
            job_id=77,
            effect_type="workspace_encrypted_v1",
            ordinal=0,
        )
        assert second_effect.effect_id == worker.v2_effect_id.derive(
            job_id=77,
            effect_type="workspace_encrypted_v1",
            ordinal=1,
        )

        second_admitted = asyncio.Event()

        async def wait_second():
            await reservations.wait_for_enqueue_turn(second_effect)
            second_admitted.set()

        waiter = asyncio.create_task(wait_second())
        await asyncio.sleep(0)
        assert not second_admitted.is_set()
        reservations.mark_ready(first)
        await asyncio.wait_for(second_admitted.wait(), timeout=1)
        await waiter

    asyncio.run(_scenario())


def test_only_wake_lane_schedule_reservation_gets_self_wake_marker():
    schedule = ToolCall(
        id="schedule",
        name="schedule_wake",
        args={
            "at": "2026-07-26T10:00:00Z",
            "_self_wake": True,
        },
    )
    cancel = ToolCall(
        id="cancel",
        name="cancel_wake",
        args={"wake_id": "wake-1"},
    )
    wake_reservations = worker._PlatformEffectReservations(
        job_id=81,
        ordinal_counter=itertools.count(),
        self_wake=True,
    )
    wake_reservations.prepare(schedule)
    wake_reservations.prepare(cancel)

    chat_reservations = worker._PlatformEffectReservations(
        job_id=82,
        ordinal_counter=itertools.count(),
    )
    chat_reservations.prepare(schedule)

    assert wake_reservations.get(schedule).payload["_self_wake"] is True
    assert "_self_wake" not in wake_reservations.get(cancel).payload
    # The wake marker above was re-added by the trusted reservation. The same
    # model-authored field in a foreground/chat reservation is stripped.
    assert "_self_wake" not in chat_reservations.get(schedule).payload


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
            "first result",
            "second result",
        ]

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
        task = asyncio.create_task(
            worker._dispatch_mixed_tool_calls(
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
            )
        )
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
        task = asyncio.create_task(
            worker._dispatch_mixed_tool_calls(
                calls,
                mcp_turn=mcp,
                mutating_mcp_names=frozenset(),
                dispatch_platform_one=_read,
                before_mcp_mutation=lambda: None,
                read_parallelism=2,
                mcp_timeout_sec=1,
            )
        )
        await asyncio.wait_for(two_started.wait(), timeout=1)
        assert active == 2
        assert max_active == 2
        release.set()
        results = await task

        assert max_active == 2
        assert [result.call_id for result in results] == ["p1", "p2", "p3", "p4"]

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
            ["mcp__read", "mcp__write_1", "mcp__write_2"], _mcp_dispatch
        )
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
            mutating_mcp_names={"mcp__read", "mcp__write_1", "mcp__write_2"},
            dispatch_platform_one=_platform_dispatch,
            before_mcp_mutation=_before_mcp_mutation,
            read_parallelism=2,
            mcp_timeout_sec=1,
        )

        first_mutation = events.index("fence:mcp")
        assert set(events[:first_mutation]) == {
            "read:memory_index",
            "read:perception_snapshot",
        }
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
            "p-read-1",
            "m-read",
            "p-read-2",
            "m-write-1",
            "p-write",
            "m-write-2",
        ]

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

        mcp = _DispatchingMcpTurn(["mcp__declared_read", "mcp__write"], _mcp_dispatch)
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
            "fence",
            "start:first",
            "end:first",
            "fence",
            "start:second",
            "end:second",
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

            mcp = _DispatchingMcpTurn(["mcp__ambiguous", "mcp__later"], _mcp_dispatch)
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
            ["mcp__slow_write", "mcp__later_write"], _mcp_dispatch
        )
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

    assert worker._mcp_mutating_names_for_turn(_AdvisoryOnlyMcpTurn()) == frozenset()


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

        mcp = _DispatchingMcpTurn(["mcp__first", "mcp__blocked"], _mcp_dispatch)
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

        budget = worker._McpTurnWallBudget(3.0, clock=lambda: now[0])
        mcp = _DispatchingMcpTurn(["mcp__first", "mcp__second"], _mcp_dispatch)
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
        # 成功的那次带上结果预算标记(2026-08-12):MCP 返回不再被通用的 2000
        # 字符切碎,靠的就是这个可信 metadata 通道。这条断言同时是**生产接线**的
        # 回归锁 —— worker 是否真的挂了标记,只有走完整链路的用例能证明,
        # 手造 metadata 的单测证明不了(codex 审出)。
        # 被墙钟预算拒掉的那次不带:它返回的是我们自己造的短字符串,不需要额度。
        assert results == [
            ToolResult(
                call_id="first",
                content="ok",
                metadata={
                    cap_result_budget.RESULT_KIND_METADATA_KEY:
                        cap_result_budget.USER_MCP_RESULT_KIND,
                },
            ),
            ToolResult(
                call_id="second",
                content=worker.MCP_TURN_WALL_BUDGET_EXHAUSTED_ERROR,
            ),
        ]

    asyncio.run(_scenario())
