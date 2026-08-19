"""Regression: the ASGI lifespan MUST perform the initial account-registry load.

Flask's ``app.py`` did ``accounts_registry.load_users()`` at import (app.py:190),
populating the in-memory ``_users`` / ``_key_to_user`` table that
``registry._resolve_user`` reads on every request. ``asgi_app`` never imports
``app.py``, so that load has to be rebuilt in ``asgi.lifespan`` (plan §8.1).

If it is missing, the registry starts empty and NEVER fills (a ``_resolve_user``
cache miss does not hit the DB, and the wake-bus "users" handler only RELOADS on
a cross-process NOTIFY) — so every api key resolves to ``None`` and every
authenticated route returns 401. That shipped once and wedged the whole app
behind an endless "loading" spinner; this test guards against a repeat.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi.lifespan as lifespan_mod  # noqa: E402
import debug_trace  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from asgi import threadpool  # noqa: E402
from core import store as core_store  # noqa: E402


@pytest.mark.asyncio
async def test_lifespan_loads_registry_before_wake_bus(monkeypatch):
    calls: list[str] = []

    # Spy the initial load and stub every heavy side effect so no DB / socket
    # is touched — we only assert the startup ORDER of operations.
    monkeypatch.setattr(
        accounts_registry, "load_users", lambda: calls.append("load_users")
    )
    monkeypatch.setattr(lifespan_mod, "_start_wake_bus", lambda: calls.append("wake_bus"))
    monkeypatch.setattr(lifespan_mod, "_start_ws_leader", lambda: calls.append("ws"))
    monkeypatch.setattr(threadpool, "configure_thread_limiter", lambda: None)
    monkeypatch.setattr(core_store, "set_async_wake_hook", lambda _fn: None)
    monkeypatch.setattr(
        debug_trace,
        "start_trace_stats_writer",
        lambda: calls.append("trace_stats"),
    )
    monkeypatch.setattr(
        debug_trace,
        "stop_trace_stats_writer",
        lambda: calls.append("trace_stats_stop"),
    )

    # run_db normally hops to the threadpool; here just invoke the callable so the
    # spy fires synchronously.
    async def _run_db(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(threadpool, "run_db", _run_db)

    app = SimpleNamespace(state=SimpleNamespace())
    async with lifespan_mod.lifespan(app):
        pass

    assert "load_users" in calls, "lifespan never performed the initial load_users()"
    assert calls.index("load_users") < calls.index("wake_bus"), (
        "load_users() must run before the wake bus starts listening"
    )
    assert calls.count("trace_stats") == 1
    assert calls.count("trace_stats_stop") == 1
