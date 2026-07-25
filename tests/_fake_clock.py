"""Deterministic ``time.monotonic()`` for tests that fabricate past timestamps.

``time.monotonic()`` counts seconds since boot, so a test that builds "N seconds
ago" as ``time.monotonic() - N`` produces a NEGATIVE timestamp on a host that
booted minutes ago, and a sentinel like ``_last_self_update_mono = 0.0`` (meant
as "the throttle window has long since elapsed") still sits INSIDE a 300s
window there. Dev laptops have days of uptime and hide both mistakes.

That is not hypothetical: on 2026-07-25 fifteen resident-consumer tests were
green on every laptop and red in CI, because a GitHub Actions runner is only
~2.5 minutes old by the time the suite runs (``_run_self_update`` returned
early on the throttle, and the over-age whoami fallback saw a negative
``_whoami_cache_loaded_at`` and skipped its ``> 0`` guard).

Freeze the clock instead of trusting host uptime.
"""

import time

# ~11.5 days of uptime — comfortably larger than any window these tests
# fabricate, so "N seconds ago" stays positive and outside every threshold.
FROZEN_MONOTONIC = 1_000_000.0


def freeze_monotonic(monkeypatch, now: float = FROZEN_MONOTONIC) -> float:
    """Pin ``time.monotonic()`` to ``now`` for the duration of one test.

    Patches the stdlib attribute so the test body and the module under test
    read the same clock; ``monkeypatch`` restores it at teardown. Returns the
    frozen value for tests that want to do their own arithmetic on it.
    """
    monkeypatch.setattr(time, "monotonic", lambda: now)
    return now
