"""Notify-relay rate limiting — thin alias over the shared kernel limiter.

The implementation moved to ``core.ratelimit`` so the V1 web execution gate can
reuse it without a lateral ``web -> notify_relay`` import. This module keeps its
historical import path (``from notify_relay import ratelimit``) and the
notify-relay-specific note below; behaviour is byte-for-byte the same limiter.

Per-worker semantics for the relay: under gunicorn ``-w N`` the effective
register/push ceiling is ≈ N × the configured limit. That is fine for what the
relay uses it for — abuse damping for an open endpoint, not precise quota
accounting. Documented in deploy/SELF_HOSTING.md.
"""

from __future__ import annotations

from core.ratelimit import (  # noqa: F401 — re-exported on the notify_relay path
    SlidingWindowLimiter,
    parse_rate,
    rate_from_env,
)

__all__ = ["SlidingWindowLimiter", "parse_rate", "rate_from_env"]
