import asyncio
import multiprocessing

from model_api_runtime.v2 import enclave_broker


def _request(number, pool, generation="g7"):
    return enclave_broker.EnclaveRequest(
        request_id=f"r{number}",
        pool=pool,
        slot_id=f"{pool}-{number}",
        slot_generation=generation,
    )


def _broker():
    granted = []
    broker = enclave_broker.EnclaveBroker(
        limit=4,
        reservations={"foreground": 2, "wake": 1, "heavy": 1},
        on_grant=lambda request: granted.append(request.request_id),
    )
    return broker, granted


def test_never_grants_more_than_instance_limit_and_reserves_each_pool():
    broker, granted = _broker()

    for request in (
        _request(1, "foreground"),
        _request(2, "foreground"),
        _request(3, "wake"),
        _request(4, "heavy"),
        _request(5, "heavy"),
    ):
        broker.request(request)

    assert granted == ["r1", "r2", "r3", "r4"]
    assert broker.snapshot()["granted"] == {
        "foreground": 2,
        "wake": 1,
        "heavy": 1,
    }
    assert broker.snapshot()["waiting"]["heavy"] == 1


def test_idle_reservations_are_borrowed_but_return_to_waiting_pool():
    broker, granted = _broker()
    heavy = [_request(number, "heavy") for number in range(1, 6)]
    for request in heavy[:4]:
        broker.request(request)
    foreground = _request(10, "foreground")
    broker.request(foreground)

    assert granted == ["r1", "r2", "r3", "r4"]
    broker.release("r1", "g7")

    assert granted[-1] == "r10"
    assert broker.snapshot()["granted"] == {
        "foreground": 1,
        "wake": 0,
        "heavy": 3,
    }


def test_borrowing_priority_is_foreground_then_wake_then_heavy():
    broker, granted = _broker()
    for number in range(1, 5):
        broker.request(_request(number, "heavy"))
    broker.request(_request(10, "heavy"))
    broker.request(_request(11, "wake"))
    broker.request(_request(12, "foreground"))

    broker.release("r1", "g7")
    broker.release("r2", "g7")
    broker.release("r3", "g7")

    assert granted[-3:] == ["r12", "r11", "r10"]


def test_waiting_pools_recover_reserved_minima_before_spare_borrowing():
    broker, granted = _broker()
    for number in range(1, 5):
        broker.request(_request(number, "heavy"))
    broker.request(_request(10, "foreground"))
    broker.request(_request(11, "foreground"))
    broker.request(_request(12, "foreground"))
    broker.request(_request(13, "wake"))
    broker.request(_request(14, "heavy"))

    for number in range(1, 5):
        broker.release(f"r{number}", "g7")

    assert granted[-4:] == ["r10", "r11", "r13", "r14"]
    assert broker.snapshot()["granted"] == {
        "foreground": 2,
        "wake": 1,
        "heavy": 1,
    }


def test_release_wakes_oldest_eligible_waiter_within_pool():
    broker, granted = _broker()
    for number in range(1, 5):
        broker.request(_request(number, "foreground"))
    broker.request(_request(5, "foreground"))
    broker.request(_request(6, "foreground"))

    broker.release("r1", "g7")
    broker.release("r2", "g7")

    assert granted[-2:] == ["r5", "r6"]


def test_drop_generation_releases_grants_removes_waiters_and_ignores_late_release():
    broker, granted = _broker()
    broker.request(_request(1, "foreground", "g7"))
    broker.request(_request(2, "wake", "g7"))
    broker.request(_request(3, "heavy", "g8"))
    broker.request(_request(4, "foreground", "g8"))
    broker.request(_request(5, "heavy", "g7"))
    broker.request(_request(6, "heavy", "g8"))

    broker.drop_generation("g7")
    after_drop = broker.snapshot()
    broker.release("r1", "g7")

    assert granted[-1] == "r6"
    assert broker.snapshot() == after_drop
    assert broker.snapshot()["total_granted"] == 3
    assert broker.snapshot()["waiting"] == {
        "foreground": 0,
        "wake": 0,
        "heavy": 0,
    }


def test_cancel_waiter_removes_it_without_leaking_capacity():
    broker, granted = _broker()
    for number in range(1, 5):
        broker.request(_request(number, "heavy"))
    broker.request(_request(5, "foreground"))

    assert broker.cancel("r5", "g7") is True
    broker.release("r1", "g7")

    assert "r5" not in granted
    assert broker.snapshot()["total_granted"] == 3
    assert broker.snapshot()["waiting"]["foreground"] == 0


def test_snapshot_reports_bounded_wait_p95_by_pool():
    now = [100.0]
    broker = enclave_broker.EnclaveBroker(
        limit=1,
        reservations={"foreground": 1, "wake": 0, "heavy": 0},
        on_grant=lambda _request: None,
        clock=lambda: now[0],
    )
    broker.request(_request(1, "foreground"))
    broker.request(_request(2, "heavy"))

    now[0] += 0.3
    broker.release("r1", "g7")

    assert broker.snapshot()["wait_p95_ms"] == {
        "foreground": 0.0,
        "wake": 0.0,
        "heavy": 300.0,
    }


def test_child_semaphore_waits_for_matching_parent_grant_and_releases():
    parent, child = multiprocessing.Pipe(duplex=True)

    async def drive():
        semaphore = enclave_broker.BrokerSemaphore(
            child,
            pool="foreground",
            slot_id="foreground-0",
            slot_generation="g7",
        )
        observed = {}

        async def grant_from_parent():
            message = await asyncio.to_thread(parent.recv)
            action, request = enclave_broker.decode_child_message(message)
            assert action == "acquire"
            observed["request"] = request
            parent.send(
                enclave_broker.grant_message(
                    request.request_id, request.slot_generation
                )
            )

        grant_task = asyncio.create_task(grant_from_parent())
        assert await semaphore.acquire() is True
        await grant_task
        semaphore.release()
        request = observed["request"]
        action, identity = enclave_broker.decode_child_message(
            await asyncio.to_thread(parent.recv)
        )
        assert action == "release"
        assert identity == (request.request_id, "g7")
        await semaphore.close()

    try:
        asyncio.run(drive())
    finally:
        parent.close()
        child.close()


def test_cancelling_child_wait_sends_exact_cancel_message():
    parent, child = multiprocessing.Pipe(duplex=True)

    async def drive():
        semaphore = enclave_broker.BrokerSemaphore(
            child,
            pool="heavy",
            slot_id="heavy-0",
            slot_generation="g8",
        )
        acquire_task = asyncio.create_task(semaphore.acquire())
        action, request = enclave_broker.decode_child_message(
            await asyncio.to_thread(parent.recv)
        )
        assert action == "acquire"
        acquire_task.cancel()
        await asyncio.gather(acquire_task, return_exceptions=True)
        action, identity = enclave_broker.decode_child_message(
            await asyncio.to_thread(parent.recv)
        )
        assert action == "cancel"
        assert identity == (request.request_id, "g8")
        await semaphore.close()

    try:
        asyncio.run(drive())
    finally:
        parent.close()
        child.close()
