"""Native async poll — the migration payoff, end to end (plan §9.5 / §14.2 / §19.2).

Drives the FastAPI ``/v1/chat/poll`` and ``/v1/proactive/jobs/poll`` over
``httpx.ASGITransport`` and asserts the behaviors that make the async rewrite
worth doing and safe:

- an idle poll parks an **asyncio waiter** (future), not a thread;
- a **same-worker write** wakes the parked poll promptly — the §19.2 self-origin
  gap is closed because the write path calls ``store.notify_*_waiters`` directly
  (not via the self-origin-filtered wake bus);
- a cancelled poll (client disconnect) unregisters its waiter (no leak);
- per-user caps shed to an immediate timed-out response;
- two concurrent claiming polls never both get the same message.

The lifespan (which injects the wake hook) is not run under ASGITransport, so the
fixture injects ``registry.wake`` into ``core.store`` exactly as the lifespan
would, then removes it.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from proactive import service as proactive_service  # noqa: E402
from runtime.waiters import registry  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    # Inject the async wake hook the way the lifespan would (ASGITransport does
    # not run the lifespan).
    core_store.set_async_wake_hook(registry.wake)
    yield body["user_id"], body["api_key"]
    core_store.set_async_wake_hook(None)


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app.app), base_url="http://t"
    )


async def _wait_until_parked(expected: int, timeout: float = 2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if registry.active_count() >= expected:
            return True
        await asyncio.sleep(0.005)
    return False


def _append_user_message(store, msg_id: str):
    store.append_chat(
        role="user",
        source="chat",
        envelope={"id": msg_id, "v": 1, "body_ct": "x", "nonce": "x", "K_user": "x"},
    )


# --------------------------------------------------------------------------- #
# chat poll: park → same-worker write → wake
# --------------------------------------------------------------------------- #

def test_chat_poll_parks_then_same_worker_write_wakes(user):
    uid, api_key = user

    async def go():
        store = core_store.get_store(uid)
        async with _client() as c:
            poll = asyncio.create_task(
                c.get("/v1/chat/poll", params={"timeout": 30}, headers={"X-API-Key": api_key})
            )
            assert await _wait_until_parked(1), "poll did not park an asyncio waiter"
            assert registry.active_count() == 1  # a future, not a thread

            # Same-worker write: append + notify (exactly what the chat write path
            # does). This must wake the parked poll WITHOUT the wake bus.
            _append_user_message(store, "m_wake")
            store.notify_chat_waiters()

            resp = await asyncio.wait_for(poll, timeout=2.0)
            assert resp.status_code == 200
            body = resp.json()
            assert body["timed_out"] is False
            assert "m_wake" in [m["id"] for m in body["messages"]]
        assert registry.active_count() == 0  # unregistered on return

    asyncio.run(go())


def test_chat_poll_times_out_empty_when_no_write(user):
    _uid, api_key = user

    async def go():
        async with _client() as c:
            resp = await c.get(
                "/v1/chat/poll", params={"timeout": 0.2}, headers={"X-API-Key": api_key}
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["timed_out"] is True
            assert body["messages"] == []
        assert registry.active_count() == 0

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# proactive poll: park → same-worker write → wake
# --------------------------------------------------------------------------- #

def test_proactive_poll_parks_then_write_wakes(user):
    uid, api_key = user

    async def go():
        store = core_store.get_store(uid)
        async with _client() as c:
            poll = asyncio.create_task(
                c.get("/v1/proactive/jobs/poll", params={"timeout": 30}, headers={"X-API-Key": api_key})
            )
            assert await _wait_until_parked(1), "proactive poll did not park"

            store.append_proactive_job({
                "job_id": "pj_wake",
                "source": proactive_service.PROACTIVE_JOB_SOURCE,
                "job_kind": "introduction",
                "ts": 1.0,
                "status": "pending",
            })
            store.notify_proactive_job_waiters()

            resp = await asyncio.wait_for(poll, timeout=2.0)
            assert resp.status_code == 200
            body = resp.json()
            assert body["timed_out"] is False
            assert "pj_wake" in [j["job_id"] for j in body["jobs"]]
        assert registry.active_count() == 0

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# cancellation: waiter must not leak
# --------------------------------------------------------------------------- #

def test_cancelled_poll_unregisters_waiter(user):
    _uid, api_key = user

    async def go():
        async with _client() as c:
            poll = asyncio.create_task(
                c.get("/v1/chat/poll", params={"timeout": 30}, headers={"X-API-Key": api_key})
            )
            assert await _wait_until_parked(1)
            poll.cancel()
            with pytest.raises((asyncio.CancelledError, httpx.HTTPError, Exception)):
                await poll
            # give the route's finally: registry.unregister a tick to run
            for _ in range(100):
                if registry.active_count() == 0:
                    break
                await asyncio.sleep(0.005)
            assert registry.active_count() == 0, "cancelled poll leaked a waiter"

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# per-user cap: shed to immediate timed_out
# --------------------------------------------------------------------------- #

def test_per_user_cap_sheds_extra_poll(user):
    """Default per-user chat cap is 2 (FEEDLING_POLLER_MAX_PER_USER_CHAT): two
    polls park, a third sheds to an immediate timed-out response."""
    uid, api_key = user

    async def go():
        async with _client() as c:
            parked = [
                asyncio.create_task(
                    c.get("/v1/chat/poll", params={"timeout": 30}, headers={"X-API-Key": api_key})
                )
                for _ in range(2)
            ]
            assert await _wait_until_parked(2)
            assert registry.active_count() == 2

            # 3rd poll: per-user cap hit → immediate timed_out (does not park).
            resp = await asyncio.wait_for(
                c.get("/v1/chat/poll", params={"timeout": 30}, headers={"X-API-Key": api_key}),
                timeout=2.0,
            )
            assert resp.status_code == 200
            assert resp.json()["timed_out"] is True
            assert registry.active_count() == 2  # still only the two parked

            # release the parked ones
            store = core_store.get_store(uid)
            _append_user_message(store, "m_release")
            store.notify_chat_waiters()
            await asyncio.wait_for(asyncio.gather(*parked), timeout=3.0)
        assert registry.active_count() == 0

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# no duplicate claim across two concurrent polls
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# wake wiring: both the direct notify path and the cross-worker LISTEN path
# (_wake_store_waiters) must fire the async hook for both channels (§9.3/§19.2).
# This closes the cross-worker link without spinning a second process: the
# LISTEN thread's dispatch calls _wake_store_waiters, which we exercise here.
# --------------------------------------------------------------------------- #

def test_notify_methods_fire_async_hook(user):
    uid, _api_key = user
    store = core_store.get_store(uid)
    calls: list = []
    core_store.set_async_wake_hook(lambda ch, u: calls.append((ch, u)))
    try:
        store.notify_chat_waiters()
        store.notify_proactive_job_waiters()
        assert ("chat", uid) in calls
        assert ("proactive", uid) in calls
    finally:
        core_store.set_async_wake_hook(registry.wake)


def test_wake_store_waiters_fires_async_hook_both_channels(user):
    uid, _api_key = user
    store = core_store.get_store(uid)
    calls: list = []
    core_store.set_async_wake_hook(lambda ch, u: calls.append((ch, u)))
    try:
        core_store._wake_store_waiters(store)  # what the cross-worker LISTEN path calls
        assert ("chat", uid) in calls
        assert ("proactive", uid) in calls
    finally:
        core_store.set_async_wake_hook(registry.wake)


def test_two_polls_one_message_no_duplicate_claim(user):
    uid, api_key = user

    async def go():
        store = core_store.get_store(uid)
        async with _client() as c:
            polls = [
                asyncio.create_task(
                    c.get(
                        "/v1/chat/poll",
                        params={"timeout": 30, "consumer_id": f"consumer-{i}"},
                        headers={"X-API-Key": api_key},
                    )
                )
                for i in range(2)
            ]
            assert await _wait_until_parked(2)

            _append_user_message(store, "m_solo")
            store.notify_chat_waiters()

            results = await asyncio.wait_for(asyncio.gather(*polls), timeout=3.0)
            got = [
                m["id"]
                for r in results
                for m in r.json()["messages"]
            ]
            assert got.count("m_solo") == 1, f"duplicate claim: {got}"
        assert registry.active_count() == 0

    asyncio.run(go())
