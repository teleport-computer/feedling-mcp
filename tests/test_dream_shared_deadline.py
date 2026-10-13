"""Offline Dream budget contract: real retry/HTTP/session, controlled clock."""
import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

import provider_client as pc
import provider_deadline as pd
from memory import garden_component as gc
from model_api_runtime.v2 import dream_budget, extraction


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__()
        self.now = 0.0

    def time(self):
        return self.now

    def _run_once(self):
        while self._scheduled and self._scheduled[0]._cancelled:
            import heapq
            heapq.heappop(self._scheduled)
        if not self._ready and self._scheduled:
            self.now = max(self.now, self._scheduled[0]._when)
        super()._run_once()


@pytest.fixture
def clock(monkeypatch):
    loop = VirtualLoop()
    clock = SimpleNamespace(monotonic=loop.time, monotonic_ns=lambda: int(loop.time() * 1e9))
    for module in (pc, pd, dream_budget, extraction):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(pc.random, "uniform", lambda a, b: b)
    def denied(*args, **kwargs):
        raise AssertionError("external network forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    yield loop
    loop.close()


def run(loop, awaitable):
    return loop.run_until_complete(awaitable)


@pytest.mark.parametrize("terminal", [True, False])
def test_failure_before_p_and_observer_crossing_p(monkeypatch, clock, terminal):
    calls = []
    failure = pc.ProviderError("bad credential" if terminal else "unavailable", status_code=401 if terminal else 503)
    async def call(*a, **kw):
        calls.append(kw)
        raise failure
    async def observe(*a):
        await asyncio.sleep(11)
        raise RuntimeError("observer broken")
    monkeypatch.setattr(pc, "chat_completion_async", call)
    monkeypatch.setattr(pc.provider_refusal, "observe", observe)
    with pytest.raises(pc.ProviderError if terminal else pd.ProviderAbsoluteDeadlineExceeded) as raised:
        run(clock, pc.reliable_chat_completion_async(
            absolute_deadline=10, deadline_provenance=True, timeout=180,
        ))
    assert len(calls) == 1
    if terminal:
        assert raised.value is failure
    else:
        assert raised.value.deadline_phase == "retry_admission"
        assert raised.value.__cause__ is failure


@pytest.mark.parametrize("phase", ["entry", "attempt", "backoff"])
def test_owned_deadline_and_no_later_attempt(monkeypatch, clock, phase):
    calls, cancelled = [], []
    async def call(*a, **kw):
        calls.append(1)
        if phase == "backoff":
            raise pc.ProviderError("unavailable", status_code=503)
        try:
            await asyncio.sleep(50)
        finally:
            cancelled.append(True)
    monkeypatch.setattr(pc, "chat_completion_async", call)
    with pytest.raises(pd.ProviderAbsoluteDeadlineExceeded) as raised:
        run(clock, pc.reliable_chat_completion_async(
            absolute_deadline=0 if phase == "entry" else 1,
            deadline_provenance=True,
        ))
    assert raised.value.deadline_phase == phase
    assert len(calls) == (0 if phase == "entry" else 1)
    if phase == "attempt":
        assert cancelled == [True]


def test_inner_timeout_is_not_outer_deadline(monkeypatch, clock):
    failure = TimeoutError("wire failed first")
    async def call(*a, **kw):
        await asyncio.sleep(2)
        raise failure
    monkeypatch.setattr(pc, "chat_completion_async", call)
    with pytest.raises(TimeoutError) as raised:
        run(clock, pc.reliable_chat_completion_async(
            absolute_deadline=10, deadline_provenance=True, max_attempts=1,
        ))
    assert raised.value is failure
    assert not isinstance(raised.value, pd.ProviderAbsoluteDeadlineExceeded)


def test_external_cancel_is_not_budget_failure(monkeypatch, clock):
    cancelled = []
    async def call(*a, **kw):
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.append(1)
    monkeypatch.setattr(pc, "chat_completion_async", call)
    async def scenario():
        task = asyncio.create_task(pc.reliable_chat_completion_async(
            absolute_deadline=10, deadline_provenance=True,
        ))
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(clock, scenario())
    assert cancelled == [1]


def test_cancel_suppressing_transport_late_success_retains_usage(monkeypatch, clock):
    async def call(*a, **kw):
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            return {"reply": "late", "usage": {"total_tokens": 7}}
    monkeypatch.setattr(pc, "chat_completion_async", call)
    usage = []
    result = run(clock, extraction.extract(
        provider_config=None, prompt="fixture", parse=lambda x: ([], None),
        absolute_deadline=1, usage_out=usage.append,
    ))
    assert result == (None, dream_budget.EXHAUSTED)
    assert usage == [{"total_tokens": 7}]


def _session():
    garden = gc.build_garden(gc.CallableModel(lambda _: ""))
    return gc.open_dream_session(
        garden,
        cards=[{"id": f"synthetic-card-{i:04d}", "summary": f"Summary {i}", "content": "Fixture."} for i in range(12)],
        locale="en", ai_name="AI", user_name="User", recent_conversations="",
    )[0]


@pytest.mark.parametrize("replies,expected_tokens", [
    ([("{\"consolidations\": []}", "stop", 200)], [12000]),
    ([('{"consolidations": invalid}', "stop", 200), ('{"consolidations": []}', "stop", 200)], [12000, 12000]),
    ([('{"consolidations": invalid}', "stop", 200), ("partial", "length", 200), ('{"consolidations": []}', "stop", 200)], [12000, 12000, 24000]),
    ([("partial", "length", 200), ("rejected", "stop", 402), ('{"consolidations": []}', "stop", 200)], [12000, 24000, 12000]),
    ([('{"consolidations": invalid}', "stop", 200), ("partial", "length", 200), ("rejected", "stop", 402), ('{"consolidations": []}', "stop", 200)], [12000, 12000, 24000, 12000]),
])
def test_real_session_all_reasks_share_deadline(monkeypatch, clock, replies, expected_tokens):
    seen, deadlines = [], []
    original = pc.reliable_chat_completion_async
    async def observe(*a, **kw):
        deadlines.append((kw['absolute_deadline'], kw['max_attempts']))
        return await original(*a, **kw)
    monkeypatch.setattr(pc, "reliable_chat_completion_async", observe)
    async def handler(request):
        body = json.loads(request.content)
        index = len(seen)
        seen.append(body['max_tokens'])
        await asyncio.sleep(1)
        content, stop, status = replies[index]
        payload = {'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': stop}]}
        if status != 200:
            payload = {'error': {'message': 'output budget rejected'}}
        return httpx.Response(status, json=payload)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(pc, '_shared_async_client', client)
            monkeypatch.setattr(pc, '_validate_egress_url', lambda _: None)
            return await extraction.extract(
                provider_config=pc.ProviderConfig(provider='openai_compatible', model='test-model', api_key='fixture', base_url='https://fixture.invalid/v1'),
                prompt='', parse=lambda x: (None, 'unused'), session=_session(),
                max_tokens=12000, truncation_retry_max_tokens=24000,
                timeout_sec=180, wire_deadline_sec=180, absolute_deadline=10,
            )
    assert run(clock, scenario()) == ([], None)
    assert seen == expected_tokens
    assert deadlines == [(10, 3)] * len(expected_tokens)


def test_exact_p_after_trajectory_prevents_wire(monkeypatch, clock):
    async def call(*a, **kw):
        pytest.fail('wire started after P')
    async def trajectory(*a):
        await asyncio.sleep(5)
    monkeypatch.setattr(pc, 'chat_completion_async', call)
    assert run(clock, extraction.extract(
        provider_config=None, prompt='fixture', parse=lambda _: ([], None),
        absolute_deadline=5, trajectory_out=trajectory,
    )) == (None, dream_budget.EXHAUSTED)


def test_budget_preprocessing_and_task_isolation(clock):
    budget = dream_budget.from_start(0, 1260)
    assert budget.provider_deadline == 1140
    async def scenario():
        token = dream_budget.current.set(budget)
        try:
            await asyncio.sleep(1140)
            assert dream_budget.exhausted()
        finally:
            dream_budget.current.reset(token)
        assert dream_budget.current.get() is None
    run(clock, scenario())


@pytest.mark.parametrize('bad', [None, float('inf'), float('nan'), 120, 0])
def test_production_budget_validation(bad):
    with pytest.raises((ValueError, TypeError)):
        dream_budget.validate_slot_budget(bad)


@pytest.mark.parametrize('owned', [True, False])
def test_budget_terminal_requires_owner_confirmation(monkeypatch, owned):
    from model_api_runtime.v2 import worker
    events, metrics, writes = [], [], []
    def mark(job_id, code, **kw):
        writes.append((job_id, code, kw))
        return owned
    async def emit(*args, **kwargs):
        events.append(kwargs['outcome'])
    monkeypatch.setattr(worker.jobs_store, 'mark_failed', mark)
    monkeypatch.setattr(worker, '_emit_v2_dream_lifecycle', emit)
    tm = SimpleNamespace(flush=lambda **kw: metrics.append(kw))
    outcome = asyncio.run(worker._settle_dream_budget(12, 'fixture', 'owner', SimpleNamespace(), tm))
    assert outcome == ('failed' if owned else 'lease_lost')
    assert len(writes) == 1
    assert events == (['budget_exhausted'] if owned else [])
    assert len(metrics) == int(owned)


def test_claim_budget_context_is_reset_and_not_inherited_by_capture(monkeypatch):
    from model_api_runtime.v2 import worker
    progress, seen = [], []
    jobs = iter([{'id': 1, 'lane': 'dream'}, {'id': 2, 'lane': 'capture'}])
    monkeypatch.setattr(worker.kill_switch, 'turns_halted', lambda: False)
    monkeypatch.setattr(worker.jobs_store, 'claim_next_job', lambda *a, **kw: next(jobs))
    async def scenario():
        stop = asyncio.Event()
        async def turn(job, deps):
            seen.append(dream_budget.current.get())
            if job['id'] == 2:
                stop.set()
        monkeypatch.setattr(worker, '_run_turn', turn)
        await worker._slot_loop('fixture-owner', poll_interval=1, stop_event=stop,
                                deps=SimpleNamespace(), progress_cb=progress.append,
                                turn_absolute_budget_sec=1260)
        assert dream_budget.current.get() is None
    asyncio.run(scenario())
    assert seen[0].start == progress[0].turn_start
    assert seen[0].provider_deadline == seen[0].start + 1140
    assert seen[1] is None


def test_lease_lost_skips_uniform_terminal_and_notifications(monkeypatch):
    from model_api_runtime.v2 import worker
    async def lost(*a, **kw):
        return dream_budget.LEASE_LOST
    async def forbidden(*a, **kw):
        pytest.fail('lost owner emitted terminal')
    monkeypatch.setattr(worker, '_run_turn_body', lost)
    monkeypatch.setattr(worker, '_emit_job_terminal_trace', forbidden)
    monkeypatch.setattr(worker, '_notify_capture_backoff', forbidden)
    assert asyncio.run(worker._run_turn({'id': 1}, SimpleNamespace())) == dream_budget.LEASE_LOST


def test_preprocessing_expired_gate_uses_owner_neutral_return(monkeypatch):
    from model_api_runtime.v2 import worker
    events = []
    monkeypatch.setattr(worker.jobs_store, 'mark_failed', lambda *a, **kw: False)
    monkeypatch.setattr(worker, 'TurnMetrics', lambda **kw: SimpleNamespace(flush=lambda **kw: events.append(kw)))
    async def scenario():
        token = dream_budget.current.set(dream_budget.from_start(0, 1260))
        try:
            return await worker._run_turn_body(
                {'id': 1, 'lane': 'dream', 'user_id': 'fixture', 'claimed_by': 'old-owner'},
                SimpleNamespace(), enclave_sem=asyncio.Semaphore(1),
            )
        finally:
            dream_budget.current.reset(token)
    assert asyncio.run(scenario()) == dream_budget.LEASE_LOST
    assert events == []


def test_budget_result_is_health_neutral(monkeypatch):
    from model_api_runtime.v2 import worker
    async def extract(**kw):
        return None, dream_budget.EXHAUSTED
    async def forbidden(*a, **kw):
        pytest.fail('budget updated route health')
    monkeypatch.setattr(worker.v2_extraction, 'extract', extract)
    for name in ('_record_provider_success', '_record_provider_failure', '_record_provider_failure_class'):
        monkeypatch.setattr(worker, name, forbidden)
    assert asyncio.run(worker._extract_with_provider_health('fixture')) == (None, dream_budget.EXHAUSTED)


def test_production_spawn_wrapper_preserves_generation_and_budget(monkeypatch):
    from model_api_runtime.v2 import turn_child
    seen = []
    monkeypatch.setattr(turn_child, 'main', lambda *a, **kw: seen.append((a, kw)))
    turn_child.main_with_turn_budget(None, 'worker', 1, 'heavy', 'heavy-0', ('dream',), 2, 1260, 'generation')
    assert seen == [((None, 'worker', 1, 'heavy', 'heavy-0', ('dream',), 2, 'generation'), {'turn_absolute_budget_sec': 1260})]


@pytest.mark.parametrize('stage,owned,expected', [
    ('context', True, 'failed'), ('context', False, 'lease_lost'),
    ('model', True, 'failed'), ('model', False, 'lease_lost'), ('mapping', True, 'failed'),
    ('mapping', False, 'lease_lost'), ('write', True, 'completed'),
])
def test_real_worker_budget_gate_before_write_and_no_new_cancel_after_write(monkeypatch, stage, owned, expected):
    from model_api_runtime.v2 import worker
    now, calls, events = [0.0], [], []
    monkeypatch.setattr(dream_budget, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    def context(_uid):
        if stage == 'context':
            now[0] = 10
        return {'card_items': [{'id': f'card-{i}', 'summary': 'fixture', 'content': 'fixture'} for i in range(12)]}
    def mapper(*a, **kw):
        if stage == 'mapping':
            now[0] = 10
        return ([{'type': 'add', 'dream_op': 'merge'}], 1, 0)
    def write(*a):
        calls.append('write')
        now[0] = 11
        return {'status': 'ok'}
    async def model(*a, **kw):
        calls.append('model')
        assert kw['absolute_deadline'] == 10
        if stage == 'model':
            now[0] = 10
            return None, dream_budget.EXHAUSTED
        return ([{'fixture': True}], None)
    async def emit(*a, **kw):
        events.append((a[2], kw.get('outcome')))
    async def noop(*a, **kw):
        pass
    monkeypatch.setattr(worker, '_extract_with_provider_health', model)
    monkeypatch.setattr(worker.v2_extraction, 'consolidations_to_actions', mapper)
    monkeypatch.setattr(worker, '_emit_v2_dream_lifecycle', emit)
    monkeypatch.setattr(worker, '_enqueue_profile_if_due', noop)
    monkeypatch.setattr(worker.jobs_store, 'renew_job_lease', lambda *a, **kw: True)
    monkeypatch.setattr(worker.jobs_store, 'mark_failed', lambda *a, **kw: calls.append('failed') or owned)
    monkeypatch.setattr(worker.jobs_store, 'mark_completed', lambda *a, **kw: calls.append('completed') or True)
    deps = worker.TurnDeps(read_messages=lambda _: [], resolve_provider=lambda _: (None, {}),
                          mint_enclave_token=lambda _: 'fixture', read_memory_context=context,
                          build_memory_envelope=lambda *a: {}, apply_memory_actions=write)
    async def scenario():
        token = dream_budget.current.set(dream_budget.TurnBudget(0, 130, 10))
        try:
            return await worker._run_extraction(1, 'fixture', 'dream', deps, None,
                                                asyncio.Semaphore(1), claimed_by='owner')
        finally:
            dream_budget.current.reset(token)
    assert asyncio.run(scenario()) == expected
    assert ('write' in calls) == (stage == 'write')
    assert ('model' in calls) == (stage != 'context')
    assert ('completed' in calls) == (stage == 'write')
    assert (('memory.dream.error', 'budget_exhausted') in events) == (stage != 'write' and owned)
    assert (('memory.dream.model.error', 'budget_exhausted') in events) == (stage == 'model' and owned)
    assert sum(name == 'memory.dream.error' for name, _ in events) == int(stage != 'write' and owned)


def test_slotfleet_passes_same_spec_budget_to_spawn(monkeypatch):
    from model_api_runtime.v2 import pool_config, pool_supervisor
    seen = []
    class Supervisor:
        def __init__(self, target, **kwargs):
            seen.append(kwargs)
    config = pool_config.RuntimePoolConfig.from_env()
    pool_supervisor.SlotFleet(config, spawn_target=lambda: None, worker_id='fixture',
                              poll_interval=1, supervisor_factory=Supervisor)
    for spec, args in zip(config.slots, seen):
        assert args['spawn_args'][-1] == spec.absolute_budget_sec
        assert args['spawn_args'][-2] == 2  # existing DB pool argument


def test_dream_success_health_can_wait_for_write_admission(monkeypatch):
    from model_api_runtime.v2 import worker
    deferred, health = [], []
    async def extract(**kw):
        return [{'fixture': 1}], None
    async def record(*a, **kw):
        health.append(kw)
    monkeypatch.setattr(worker.v2_extraction, 'extract', extract)
    monkeypatch.setattr(worker, '_record_provider_success', record)
    assert asyncio.run(worker._extract_with_provider_health('fixture', success_out=deferred.append))[1] is None
    assert len(deferred) == 1
    assert health == []


def test_compatibility_wire_cannot_reset_attempt_deadline(monkeypatch, clock):
    wires = []
    async def handler(request):
        body = json.loads(request.content)
        wires.append(body)
        await asyncio.sleep(1)
        if len(wires) == 1:
            return httpx.Response(400, json={'error': {'message': 'Unsupported parameter: temperature'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(pc, '_shared_async_client', client)
            monkeypatch.setattr(pc, '_validate_egress_url', lambda _: None)
            with pytest.raises(pd.ProviderAbsoluteDeadlineExceeded) as raised:
                await pc.reliable_chat_completion_async(
                    pc.ProviderConfig(provider='openai_compatible', model='test-model', api_key='fixture', base_url='https://fixture.invalid/v1', capture_attempt_trace=True),
                    [{'role': 'user', 'content': 'fixture'}], temperature=0.3,
                    absolute_deadline=1.5, deadline_provenance=True, wire_deadline_sec=180,
                )
            assert raised.value.deadline_phase == 'attempt'
            trace = pc.runtime_provider_attempt_trace(raised.value)['attempts']
            http = [item for item in trace if item['kind'] == 'http_attempt']
            assert len(http) == 2
            assert http[0]['status'] == 400 and http[0]['compatibility_fallback']
            assert http[1]['status'] is None and http[1]['outcome'] == 'cancelled'
            assert http[1]['duration_ms'] == 500
            assert trace[-1]['wire']['ordinals'] == [1, 2]
    run(clock, scenario())
    assert len(wires) == 2
    assert 'temperature' in wires[0] and 'temperature' not in wires[1]
    assert clock.time() == 1.5


def test_semantic_reask_cannot_receive_fresh_budget(monkeypatch, clock):
    requests = []
    leak = json.dumps({'consolidations': [{'op': 'merge', 'card_ids': ['synthetic-card-0000', 'synthetic-card-0001'],
        'rationale': 'same topic', 'result': {'summary': 'synthetic-card-0000', 'content': 'fixture', 'bucket': 'life', 'threads': []}}]})
    async def handler(request):
        requests.append(json.loads(request.content))
        await asyncio.sleep(1)
        return httpx.Response(200, json={'choices': [{'message': {'content': leak if len(requests) == 1 else '{"consolidations": []}'}, 'finish_reason': 'stop'}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(pc, '_shared_async_client', client)
            monkeypatch.setattr(pc, '_validate_egress_url', lambda _: None)
            return await extraction.extract(
                provider_config=pc.ProviderConfig(provider='openai_compatible', model='test-model', api_key='fixture', base_url='https://fixture.invalid/v1'),
                prompt='', parse=lambda _: (None, 'unused'), session=_session(),
                max_tokens=12000, absolute_deadline=1.5,
            )
    assert run(clock, scenario()) == (None, dream_budget.EXHAUSTED)
    assert len(requests) == 2
    assert clock.time() == 1.5


def test_invalid_dream_slot_budget_rejected_by_parent_before_spawn():
    from model_api_runtime.v2 import pool_config, pool_supervisor
    config = pool_config.RuntimePoolConfig(
        slots=(pool_config.SlotSpec('heavy', 0, frozenset({'dream'}), 210, 120),),
        profile_instance_concurrency=1, enclave_instance_concurrency=4,
    )
    def forbidden(*a, **kw):
        pytest.fail('invalid production configuration reached spawn assembly')
    with pytest.raises(ValueError, match='exceed reserve'):
        pool_supervisor.SlotFleet(config, spawn_target=forbidden, worker_id='fixture',
                                 poll_interval=1, supervisor_factory=forbidden)


@pytest.mark.parametrize('status', [200, 503])
def test_late_wire_result_or_cause_keeps_private_attempt_facts(monkeypatch, clock, status):
    async def handler(request):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass  # emulate an adapter that suppresses cancellation
        return httpx.Response(status, json={
            'choices': [{'message': {'content': 'late'}, 'finish_reason': 'stop'}],
            'usage': {'total_tokens': 7}, 'error': {'message': 'unavailable'},
        })
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(pc, '_shared_async_client', client)
            monkeypatch.setattr(pc, '_validate_egress_url', lambda _: None)
            with pytest.raises(pd.ProviderAbsoluteDeadlineExceeded) as raised:
                await pc.reliable_chat_completion_async(
                    pc.ProviderConfig(provider='openai_compatible', model='test-model', api_key='fixture',
                                      base_url='https://fixture.invalid/v1', capture_attempt_trace=True),
                    [{'role': 'user', 'content': 'fixture'}],
                    absolute_deadline=1, deadline_provenance=True,
                )
            trace = pc.runtime_provider_attempt_trace(raised.value)['attempts']
            assert len(trace) == 2
            assert trace[0]['kind'] == 'http_attempt' and trace[0]['status'] == status
            assert trace[1]['wire']['ordinals'] == [1]
            if status == 200:
                assert raised.value.late_result['usage']['total_tokens'] == 7
            else:
                assert isinstance(raised.value.__cause__, pc.ProviderError)
    run(clock, scenario())
