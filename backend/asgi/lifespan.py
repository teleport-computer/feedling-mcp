"""FastAPI lifespan — startup/shutdown reconstruction of app.py's import-time
side effects (ASGI-migration plan §8.1).

``asgi_app`` never imports ``app.py``, so every side effect that used to happen
on ``import app`` must be rebuilt explicitly here (or in gunicorn ``on_starting``
for the once-per-master items). Reconciliation table (plan §8.1 / §3.3):

| app.py side effect                          | rebuilt in                                   |
|---------------------------------------------|----------------------------------------------|
| `db.init_schema()` (alembic upgrade head)   | gunicorn_conf.on_starting (master, once)     |
| `assert_hosting_ready()`                    | gunicorn_conf.on_starting (already present)  |
| `accounts_registry.load_users()`            | this lifespan (always, per-worker registry)  |
| anyio threadpool limiter (40-token trap)    | this lifespan (always)                       |
| process-lifetime httpx.AsyncClient(s)       | this lifespan (always)                       |
| async poll-waiter wake hook (§9.3/§19.2)    | this lifespan (always) — inject registry.wake|
| `core_wake_bus.start_listener()`            | this lifespan (always — cross-worker poll wake)|
| `core_leader.run_singleton("ws", ...)`      | this lifespan (gated: FEEDLING_ASGI_BACKGROUND)|

Only the :9998 WS-leader election is gated OFF by default, so the dev-time
parallel instance (:5005, plan §8) sharing the live backend's DB does not
contend for WS leadership. The wake-bus listener IS started (it is a per-worker
LISTEN by design and is REQUIRED for cross-worker/cross-process poll wakes —
without it a native poll on the ASGI instance would miss writes made on another
process). See asgi.settings.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from asgi import threadpool
from asgi.settings import settings


def _start_wake_bus() -> None:
    """Cross-worker wake bus (per-worker LISTEN). Mirrors app.py's assembly:
    the "users" cache-evict handler is injected here (core may not import
    accounts); store channels are dispatched inside wake_bus and now also fire
    the async wake hook (core.store._fire_async_wake)."""
    from accounts import registry as accounts_registry
    from core import wake_bus as core_wake_bus

    core_wake_bus.register_handler("users", lambda _uid: accounts_registry.load_users())
    core_wake_bus.start_listener()


def _start_ws_leader() -> None:
    from core import leader as core_leader
    from screen import ws as screen_ws

    core_leader.run_singleton("ws", screen_ws.start)


def _start_tee_sync_leader() -> None:
    """In-process TEE 影子库自动同步单例（advisory-lock 选主，只一个 worker 跑）。
    仅在双写已接（mirror.enabled）时选举 —— TEE 未配置时不空占 advisory 锁。"""
    from admin import tee_sync_scheduler
    from core import leader as core_leader

    core_leader.run_singleton("tee-sync", tee_sync_scheduler.start)


@asynccontextmanager
async def lifespan(app):
    # (1) Threadpool limiter — off anyio's 40-token default (§5.2).
    threadpool.configure_thread_limiter()

    # (2) Process-lifetime httpx clients (§5.6): internal (short read) vs
    #     provider (long read) so a slow provider can't starve internal calls.
    limits = httpx.Limits(
        max_connections=settings.http_max_connections,
        max_keepalive_connections=settings.http_max_keepalive,
    )
    app.state.internal_http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        limits=limits,
    )
    app.state.provider_http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
        limits=limits,
    )

    # (3) Wire the async poll-waiter wake hook into core.store (§9.3/§19.2):
    #     every store.notify_*_waiters / _wake_store_waiters now also wakes the
    #     asyncio waiters — local writes directly (closing the self-origin gap),
    #     cross-worker writes via the wake-bus LISTEN dispatch below.
    from core import store as core_store
    from runtime.waiters import registry

    core_store.set_async_wake_hook(registry.wake)

    # (4) Initial account-registry load — mirrors app.py:190. WITHOUT this the
    #     in-memory `_users` / `_key_to_user` start empty and never populate:
    #     `_resolve_user` only reads the in-memory table (a cache miss does NOT
    #     hit the DB), and the wake-bus "users" handler below only RELOADS on a
    #     cross-process NOTIFY — neither performs the initial load. Result: every
    #     api key resolves to None → 401 on every authenticated route → clients
    #     stuck "loading". Runs per-worker (each worker owns its own registry) on
    #     the threadpool because load_users() is sync DB I/O. init_schema() has
    #     already run in gunicorn on_starting (master), so the tables exist.
    from accounts import registry as accounts_registry

    await threadpool.run_db(accounts_registry.load_users)

    # (4b) Wire core.envelope's user-public-key lookup — mirrors app.py:412.
    #      `core.envelope.get_user_public_key` ships as a RuntimeError stub so
    #      core does not import accounts; the assembly layer must inject the real
    #      lookup. Missing this, every server-side shared-envelope build in the
    #      main-backend process (e.g. /v1/model_api/chat/send, genesis
    #      persona_backfill via core.envelope._build_shared_envelope_for_store)
    #      raises "get_user_public_key not wired by assembly layer" → 500.
    from core import envelope as core_envelope

    core_envelope.get_user_public_key = accounts_registry._get_user_public_key

    # (5) Cross-worker wake bus — always on (required for poll wakes). Its
    #     "users" handler keeps the registry fresh on later cross-process writes.
    _start_wake_bus()

    # (6) :9998 WS-leader election — gated (see module docstring).
    if settings.start_background:
        _start_ws_leader()
        # (6b) TEE 影子库自动同步单例 — 仅在双写已接时选举（env 决定，接=重部署）。
        from tee_shadow import mirror as _tee_mirror

        if _tee_mirror.enabled():
            _start_tee_sync_leader()

    print(
        f"[asgi] startup ready: threadpool={settings.db_threads} "
        f"http_max={settings.http_max_connections} poller_max={settings.poller_max_active} "
        f"ws_leader={settings.start_background}",
        flush=True,
    )

    try:
        yield
    finally:
        # Shutdown: release parked waiters so in-flight polls return, drop the
        # hook, then close the async clients.
        registry.wake_all()
        core_store.set_async_wake_hook(None)
        await app.state.internal_http.aclose()
        await app.state.provider_http.aclose()
