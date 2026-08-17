"""The `/v1/chat/poll` response advertises the user's MCP-server-config
fingerprint, so a resident consumer knows when to re-materialize its MCP
tool wiring (Task 6 depends on this signal).

Run:  python -m pytest tests/test_user_mcp_poll.py -v
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
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import mcp_core  # noqa: E402
from runtime.waiters import registry as waiter_registry  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = iter(range(1, 10_000))


def _register(client) -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _fake_envelope(monkeypatch):
    from core import envelope as core_envelope
    from hosted import mcp_probe as _probe

    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda store, raw, item_id=None: ({"v": 1, "id": item_id, "body_ct": raw.hex()}, ""),
    )
    # SSRF DNS resolve is environment-dependent; stub the upsert-time guard so
    # this test only exercises the poll-response wiring.
    monkeypatch.setattr(_probe, "blocked_url_kind", lambda url: None)


def test_poll_advertises_user_mcp_fingerprint(client, monkeypatch):
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    h = {"X-API-Key": key}

    r = client.get("/v1/chat/poll?timeout=0", headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["user_mcp"] == {"fingerprint": ""}

    r2 = client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira", "url": "https://mcp.example.com/mcp", "headers": {}})
    assert r2.status_code in (200, 201), r2.get_data(as_text=True)

    r3 = client.get("/v1/chat/poll?timeout=0", headers=h)
    assert r3.status_code == 200, r3.get_data(as_text=True)
    assert r3.get_json()["user_mcp"]["fingerprint"].startswith("sha256:")


async def _wait_until_parked(expected: int, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if waiter_registry.active_count() >= expected:
            return True
        await asyncio.sleep(0.005)
    return False


def test_parked_poll_reflects_mid_park_fingerprint_change(client, monkeypatch):
    """Fix 3 (with fix 2): a poll that is already PARKED with the pre-park
    empty-fingerprint snapshot must, when a config write lands mid-park, wake
    (via _save's notify) and return the FRESH fingerprint — not the stale
    pre-park context. Without fix 3 the woken poll would echo ``fingerprint: ""``;
    without fix 2 it would never wake and the wait_for below would time out."""
    _fake_envelope(monkeypatch)
    uid, key = _register(client)
    store = core_store.get_store(uid)
    # ASGITransport does not run the lifespan, so wire the same-worker wake hook
    # the lifespan would inject (mirrors tests/test_asgi_poll_native.py).
    core_store.set_async_wake_hook(waiter_registry.wake)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app.app), base_url="http://t"
        ) as c:
            poll = asyncio.create_task(c.get(
                "/v1/chat/poll", params={"timeout": 30}, headers={"X-API-Key": key}))
            assert await _wait_until_parked(1), "poll did not park"

            # Mid-park config write, run OFF the loop exactly like the real
            # run_db threadpool hop: _save fires notify_chat_waiters +
            # wake_bus.notify, waking the parked poll cross-thread.
            loop = asyncio.get_running_loop()
            _, status = await loop.run_in_executor(None, lambda: mcp_core.upsert_server(
                store, {"name": "jira", "url": "https://a.example.com", "headers": {}}))
            assert status == 200

            resp = await asyncio.wait_for(poll, timeout=3.0)
            assert resp.status_code == 200
            body = resp.json()
            # Woken (not timed out) AND carrying the post-write fingerprint.
            assert body["timed_out"] is False
            assert body["user_mcp"]["fingerprint"].startswith("sha256:")
        assert waiter_registry.active_count() == 0

    try:
        asyncio.run(go())
    finally:
        core_store.set_async_wake_hook(None)
