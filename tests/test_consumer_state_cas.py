from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from chat import consumer  # noqa: E402
from conftest import seed_user  # noqa: E402


def test_cross_worker_mutations_retry_and_merge_disjoint_fields(backend_env):
    """Two process-local locks must still converge through the PostgreSQL CAS."""
    user_id = f"usr_{uuid.uuid4().hex[:16]}"
    seed_user(user_id)
    db.set_blob(
        user_id,
        "consumer_state",
        {"resident_maintenance": {}},
    )
    stores = [
        SimpleNamespace(user_id=user_id, consumer_state_lock=threading.Lock()),
        SimpleNamespace(user_id=user_id, consumer_state_lock=threading.Lock()),
    ]
    barrier = threading.Barrier(2)
    calls = [0, 0]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            def mutate(state: dict) -> None:
                calls[index] += 1
                if calls[index] == 1:
                    barrier.wait(timeout=5)
                if index == 0:
                    state["last_poll_epoch"] = 456.0
                else:
                    maintenance = dict(state.get("resident_maintenance") or {})
                    maintenance["last_reminder_epoch"] = 123.0
                    maintenance["last_prompt_delivered"] = True
                    state["resident_maintenance"] = maintenance

            assert consumer._mutate_consumer_state(stores[index], mutate) is not None
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(calls) == [1, 2], "one stale snapshot must lose CAS and retry"
    state = db.get_blob(user_id, "consumer_state")
    assert state["last_poll_epoch"] == 456.0
    assert state["resident_maintenance"] == {
        "last_reminder_epoch": 123.0,
        "last_prompt_delivered": True,
    }


def test_consumer_state_first_writers_do_not_clobber_each_other(backend_env):
    user_id = f"usr_{uuid.uuid4().hex[:16]}"
    seed_user(user_id)
    stores = [
        SimpleNamespace(user_id=user_id, consumer_state_lock=threading.Lock()),
        SimpleNamespace(user_id=user_id, consumer_state_lock=threading.Lock()),
    ]
    barrier = threading.Barrier(2)
    calls = [0, 0]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            def mutate(state: dict) -> None:
                calls[index] += 1
                if calls[index] == 1:
                    barrier.wait(timeout=5)
                state[f"writer_{index}"] = True

            assert consumer._mutate_consumer_state(stores[index], mutate) is not None
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(calls) == [1, 2]
    assert db.get_blob(user_id, "consumer_state") == {
        "writer_0": True,
        "writer_1": True,
    }


def test_poll_history_keeps_hosted_and_resident_identities(backend_env, monkeypatch):
    user_id = f"usr_{uuid.uuid4().hex[:16]}"
    seed_user(user_id)
    store = SimpleNamespace(user_id=user_id, consumer_state_lock=threading.Lock())
    monkeypatch.setattr(consumer, "_touch_resident_binding_seen", lambda *a, **k: None)
    times = iter([100.0, 101.0])
    monkeypatch.setattr(consumer.time, "time", lambda: next(times))

    common = {
        "consumer_name": "feedling-chat-resident",
        "consumer_version": "resident-v1",
        "official": True,
    }
    consumer._record_consumer_event(
        store,
        "poll",
        info={**common, "consumer_id": f"agent-runner:{user_id}"},
    )
    consumer._record_consumer_event(
        store,
        "poll",
        info={**common, "consumer_id": "resident-vps:123"},
    )

    state = db.get_blob(user_id, "consumer_state")
    assert state["consumer_id"] == "resident-vps:123"
    assert state["poll_consumers"][f"agent-runner:{user_id}"]["responder"] == "hosted_v1"
    assert state["poll_consumers"]["resident-vps:123"]["responder"] == "resident"
    assert state["poll_consumers"][f"agent-runner:{user_id}"]["last_poll_epoch"] == 100.0
    assert state["poll_consumers"]["resident-vps:123"]["last_poll_epoch"] == 101.0
