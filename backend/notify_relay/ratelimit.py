"""Per-process sliding-window rate limiter for the notify relay.

The backend has no shared rate-limit substrate (no Redis; PG would put a hot
write on every relay call), so this is plain per-worker memory. Under gunicorn
``-w N`` the effective ceiling is therefore ≈ N × the configured limit — that
is fine for what this is: abuse damping for an open endpoint, not precise
quota accounting. Documented in deploy/SELF_HOSTING.md.

Rate specs are env-configurable as ``"<count>/<window_seconds>"`` strings,
e.g. ``120/60`` = 120 requests per rolling 60s window.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

# Hard cap on tracked keys. The relay's key space is tiny in practice (one key
# per relay token / caller IP); this only guards against a deliberate flood of
# unique keys (e.g. spoofed XFF) growing the dict without bound.
_MAX_KEYS = 10_000


def parse_rate(spec: str, default: tuple[int, float]) -> tuple[int, float]:
    """``"120/60"`` -> ``(120, 60.0)``; malformed/empty specs fall back."""
    try:
        count_s, _, window_s = (spec or "").partition("/")
        count, window = int(count_s), float(window_s)
        if count > 0 and window > 0:
            return count, window
    except (TypeError, ValueError):
        pass
    return default


def rate_from_env(env_name: str, default: tuple[int, float]) -> tuple[int, float]:
    return parse_rate(os.environ.get(env_name, ""), default)


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> tuple[bool, float]:
        """Record one hit for ``key`` iff it is under ``limit`` hits in the last
        ``window`` seconds. Returns ``(allowed, retry_after_seconds)`` —
        ``retry_after`` is 0 when allowed, else the wait until the oldest hit
        leaves the window (for the 429 ``Retry-After`` header)."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= _MAX_KEYS:
                    self._sweep(now, window)
                    if len(self._hits) >= _MAX_KEYS:
                        # 10k concurrently-hot keys — refuse to grow. A flood of
                        # unique keys defeats per-key limiting either way; letting
                        # the untracked request through beats evicting the
                        # trackers of legitimate hot keys.
                        return True, 0.0
                hits = self._hits[key] = deque()
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(0.0, window - (now - hits[0]))
            hits.append(now)
            return True, 0.0

    def peek(self, key: str, limit: int, window: float) -> tuple[bool, float]:
        """Like ``allow`` but WITHOUT recording a hit: is ``key`` currently
        under the limit? Used to short-circuit an over-limit caller before any
        expensive work (e.g. the push route's DB lookup), while the actual
        charge happens only when the request turns out to be a failure."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return True, 0.0
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(0.0, window - (now - hits[0]))
            return True, 0.0

    def _sweep(self, now: float, window: float) -> None:
        # Key-cap pressure: drop every key whose newest hit is already outside
        # the window (dead weight), oldest-inactive first is unnecessary — any
        # still-hot key survives because its newest hit is recent.
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= window]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
