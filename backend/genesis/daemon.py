"""Genesis import worker daemon: the gate + the polling loop.

Extracted from ``agent_runtime.supervisor`` (2026-07-10). It lived there for one
reason — supervisor was the only long-running non-request process inside a CVM,
so it was the only place with a ``while True`` to hang a poller on. It never had
anything to do with the resident CLI runtime: it needs an enclave URL (to decrypt
E2E chunk envelopes), a runtime-token secret (to mint genesis-scoped tokens), and
a loop. Nothing else.

Framework-neutral on purpose: this module must import NEITHER ``agent_runtime``
nor ``model_api_runtime``, so the V2 assembly layer (``serve_worker.py``) and the
resident supervisor can both host it without violating the dependency-direction
guard in ``tests/test_v2_dependency_direction.py``.

Concurrency contract: the claim is ``FOR UPDATE SKIP LOCKED``
(``genesis.worker.tick`` -> ``db.genesis_claim_uploaded_jobs``), so running this
loop in several processes at once is safe and de-dupes. That is what makes a
coexistence rollout possible.
"""
from __future__ import annotations

from typing import Callable

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def should_start(*, enabled: str, secret: str, enclave_url: str) -> bool:
    """Activate the genesis worker only when explicitly enabled AND its
    prerequisites are present (runtime-token secret to mint scoped tokens, enclave
    URL to decrypt chunks). Default OFF — landing the hook must not run genesis
    until the env opts in; missing prereqs stay dormant rather than fail jobs."""
    if not _truthy(enabled):
        return False
    return bool(str(secret or "").strip()) and bool(str(enclave_url or "").strip())


def _beat(on_beat: Callable[[], None] | None) -> None:
    """A liveness-write failure must never kill the loop it is observing."""
    if on_beat is None:
        return
    try:
        on_beat()
    except Exception as e:  # noqa: BLE001
        print(f"[genesis:daemon] heartbeat write failed: {type(e).__name__}:{str(e)[:120]}")


def run_loop(*, api_url, enclave_url, mint_genesis, interval, stop_event, on_beat=None) -> None:
    """Poll ``genesis.worker.tick``. Blocking — the caller owns the thread.

    Beats before AND after each tick: a tick can block for minutes on the user's
    LLM, and a beat only at the top of the loop would look dead for that whole
    window. Reaps jobs wedged in 'processing' before claiming new work, so a
    crashed job cannot block the user's onboarding forever.
    """
    from genesis import worker as genesis_worker
    while not stop_event.is_set():
        _beat(on_beat)
        try:
            genesis_worker.reap_stale_processing_jobs()
            genesis_worker.tick(api_url=api_url, enclave_url=enclave_url,
                                mint_runtime_token=mint_genesis, max_jobs=1)
        except Exception as e:  # noqa: BLE001
            print(f"[genesis:daemon] tick failed: {type(e).__name__}:{str(e)[:200]}")
        _beat(on_beat)
        stop_event.wait(interval)
