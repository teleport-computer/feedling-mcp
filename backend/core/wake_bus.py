"""Cross-worker wake bus over Postgres LISTEN/NOTIFY.

Why this exists: the long-poll endpoints park on in-process ``threading.Event``s
and the per-user ``UserStore`` is an in-process write-through cache. Both only
work when one gunicorn worker serves the whole backend (``-w 1``). To lift that
ceiling we keep the in-process fast path but add a cross-process broadcast: a
genuine write issues a ``NOTIFY``, and every *other* worker's listener wakes the
local long-poll waiters and refreshes that user's cached store in place.

Layering (see CONTRIBUTING §2): ``db.py`` owns the SQL primitives
(``pg_notify`` / ``listen_connection``) and stays business-free; this module
(core) owns the payload + dispatch. Targets core may not import upward (e.g. the
accounts registry reload) are wired in via ``register_handler`` from
asgi/lifespan.py.

No storm: the listener only acts on notifies whose origin worker is *not* us, so
the ``_evict_store`` it triggers (which itself wakes local waiters) never feeds
back into another NOTIFY. The genuine-write NOTIFY is emitted from the write
chokepoints (``append_chat`` / ``append_proactive_job`` / …), never from the
wake/reload path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Callable

import db

log = logging.getLogger("feedling.wake_bus")

# Single Postgres NOTIFY channel; the JSON payload carries the logical channel.
PG_CHANNEL = "feedling_wake"

# This worker's identity. A genuine write tags its NOTIFY with this so the same
# worker's listener can skip it (the local fast path already handled it).
WORKER_ID = uuid.uuid4().hex

# Logical channels whose target is a per-user cached store: a cross-worker
# notify refreshes that store in place (which also wakes its long-poll waiters).
_STORE_CHANNELS = frozenset({"chat", "proactive", "frames", "blob"})

# Extra per-channel handlers injected by the assembly layer for targets core may
# not import upward (channel -> [fn(user_id)]). E.g. asgi/lifespan.py wires the
# accounts registry reload onto the "users" channel.
_extra_handlers: dict[str, list[Callable[[str], None]]] = {}

_RECONNECT_DELAY_SEC = 5.0
_listener_started = False
_listener_lock = threading.Lock()


def _enabled() -> bool:
    return os.environ.get("FEEDLING_WAKE_BUS_ENABLED", "1") == "1"


def register_handler(channel: str, fn: Callable[[str], None]) -> None:
    """Wire an extra handler for ``channel`` (called with the notify's user_id).
    Used by asgi/lifespan.py to attach upward targets the core layer can't import."""
    _extra_handlers.setdefault(channel, []).append(fn)


def notify(channel: str, user_id: str = "") -> None:
    """Broadcast a genuine write so other workers wake/refresh. Best-effort: a
    dropped notify degrades to the long-poll timeout / store TTL, never an error.
    Call this only from write chokepoints, never from the wake/reload path."""
    if not _enabled():
        return
    payload = json.dumps(
        {"u": user_id, "c": channel, "o": WORKER_ID}, separators=(",", ":")
    )
    db.pg_notify(PG_CHANNEL, payload)


def _dispatch(payload: str) -> None:
    try:
        data = json.loads(payload)
    except Exception:
        return
    if data.get("o") == WORKER_ID:
        return  # our own write — the local fast path already handled it
    channel = data.get("c") or ""
    user_id = data.get("u") or ""
    if channel in _STORE_CHANNELS and user_id:
        # Lazy import breaks the core.store <-> core.wake_bus cycle (store
        # imports wake_bus at module load to emit notifies). _evict_store
        # reloads the cached store in place and wakes its local waiters, so a
        # poller parked here returns and re-reads fresh state.
        from core import store as core_store

        try:
            core_store._evict_store(user_id)
        except Exception:
            log.exception("[wake_bus] evict failed for user=%s", user_id)
    for fn in _extra_handlers.get(channel, ()):  # injected upward targets
        try:
            fn(user_id)
        except Exception:
            log.exception("[wake_bus] handler failed for channel=%s", channel)


def _reconnect_catch_up() -> None:
    """Catch up after (re)establishing LISTEN: notifies sent while this worker
    had no live listener are gone forever (LISTEN/NOTIFY has no replay), so any
    store cached before/through that window may be stale for up to the 15-min
    TTL — the "push arrived, chat page didn't" failure (2026-07-15 延迟诊断).
    Refresh every cached store in place (which also wakes its parked long-poll
    waiters) and replay the injected channel handlers (e.g. the accounts
    registry reload on "users") once. Runs AFTER LISTEN is active, so notifies
    arriving during the refresh buffer on the connection instead of being lost.
    First connect at worker boot: the store cache is empty → pure no-op (except
    the cheap handler replay, which covers registry writes that landed between
    lifespan's load_users and this LISTEN going live)."""
    from core import store as core_store  # lazy — breaks the store<->wake_bus cycle

    refreshed = 0
    for user_id in list(core_store._stores.keys()):
        try:
            core_store._evict_store(user_id)
            refreshed += 1
        except Exception:  # noqa: BLE001 — one bad store must not stop the sweep
            log.exception("[wake_bus] reconnect refresh failed for user=%s", user_id)
    for channel, fns in _extra_handlers.items():
        for fn in fns:
            try:
                fn("")
            except Exception:  # noqa: BLE001
                log.exception("[wake_bus] reconnect handler replay failed for channel=%s", channel)
    if refreshed:
        log.info("[wake_bus] reconnect catch-up: refreshed %d cached store(s)", refreshed)


def _listen_loop() -> None:
    while True:
        conn = None
        try:
            conn = db.listen_connection()
            conn.execute(f"LISTEN {PG_CHANNEL}")
            log.info("[wake_bus] listening on %s (worker=%s)", PG_CHANNEL, WORKER_ID)
            _reconnect_catch_up()  # notifies missed while unlistened are gone — resync once
            for note in conn.notifies():  # blocks; raises if the conn drops
                _dispatch(note.payload)
        except Exception as e:
            log.warning("[wake_bus] listener error: %s; reconnecting in %ss", e, _RECONNECT_DELAY_SEC)
            time.sleep(_RECONNECT_DELAY_SEC)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def start_listener() -> None:
    """Start this worker's wake-bus listener (one daemon thread per worker).
    Idempotent. Called from asgi/lifespan.py at startup (which also wires
    screen_ws.start via the WS leader election)."""
    global _listener_started
    if not _enabled():
        return
    with _listener_lock:
        if _listener_started:
            return
        _listener_started = True
    threading.Thread(target=_listen_loop, daemon=True, name="wake-bus-listener").start()
