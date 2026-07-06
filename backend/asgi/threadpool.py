"""Bounded sync->thread bridge (ASGI-migration plan §5.2 / §7.2).

`async def` routes must never call blocking sync code (sync `db.py`, boto3,
sync provider SDKs) directly — that pins the event loop. They hand it to
a bounded threadpool instead. anyio's default global thread limiter is only
**40** tokens, well under the legacy 128-thread gunicorn worker, so a heavy
route would be silently throttled; `configure_thread_limiter()` raises it in the
lifespan to `FEEDLING_ASGI_DB_THREADS`.
"""

from __future__ import annotations

import anyio.to_thread

from asgi.settings import settings


def configure_thread_limiter() -> int:
    """Raise anyio's default thread limiter off its 40-token default. Call once
    at startup. Returns the token count set."""
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = settings.db_threads
    return settings.db_threads


async def run_db(fn, *args, **kwargs):
    """Run a blocking sync callable (e.g. sync `db.py`) on the bounded threadpool.

    The single sanctioned way for an async route to touch sync DB / blocking I/O
    until the async DB gateway lands (plan §10). Direct sync calls in a route are
    a red-line review failure (plan §5.0).
    """
    def _call():
        return fn(*args, **kwargs)

    return await anyio.to_thread.run_sync(_call)
