"""D4 load-test driver: seed synthetic users -> enqueue a chat burst -> drain
the agent_jobs queue -> collect a metrics report.

Fidelity level used by this driver: **B (simulated drain)**, not A (a real
``model_api_runtime.v2.worker``/``serve_worker`` pool). Draining with the real
V2 worker needs enclave-bound BYOK + enclave-auth wiring
(``worker.TurnDeps.resolve_provider`` / ``read_messages`` /
``mint_enclave_token`` — see ``backend/model_api_runtime/v2/worker.py``'s
``TurnDeps`` docstring) that is impractical to assemble reliably in this
driver/CI without a real enclave, so ``drain()`` instead takes an *injectable*
``processor`` callable and this module ships a SIMULATED one
(``build_simulated_processor``) that advances jobs directly via
``jobs_store`` primitives (claim -> mark_running -> record_turn_metric ->
mark_completed) with deterministic fake token counts. That still exercises
the real DB queue (agent_jobs) and the real metrics table (v2_turn_metrics)
end to end — it just never calls a real LLM or the real worker pipeline. A
future level-A run would assemble a real ``TurnDeps``/pool and pass IT as the
``processor`` to the same ``drain()`` — no other part of this driver needs to
change.

The real, large (~100-user) load test is a MANUAL, LOCAL exercise run by a
human near cutover (see docs/superpowers/plans/2026-07-09-hosted-runtime-v2-
D4-loadtest-rollout-killresident.md, Task 5). It is NOT run in CI. Any RSS
figure this script reports when run standalone is indicative of a dev box,
not CVM-authoritative — the real capacity number must come from an actual CVM
run. CI only exercises a <=5-user smoke test (see
tests/test_loadtest_harness_smoke.py).

Usage (manual, local, needs a reachable Postgres via DATABASE_URL)::

    python -m scripts.loadtest.run_loadtest --users 100 --workers 16
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

_repo_root = Path(__file__).resolve().parent.parent.parent
_backend = str(_repo_root / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import db  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

from scripts.loadtest import collect  # noqa: E402
from scripts.loadtest.mock_provider import (  # noqa: E402
    DEFAULT_COMPLETION_TOKENS,
    DEFAULT_PROMPT_TOKENS,
    MockProvider,
)

# Simulated per-turn latency stamped by the level-B processor (record_turn_metric
# wants a latency_ms; there's no real call to time, so a small fixed value keeps
# turn_latency_p95_ms non-null/sane without pretending it means anything about
# real provider latency).
_SIMULATED_LATENCY_MS = 5


def _seed_user_row(user_id: str) -> None:
    """Insert into ``users`` so per-user writes aren't rejected by the FK.

    Prefers ``conftest.seed_user`` (mirrors the in-memory registry too) when
    importable — i.e. when this runs under pytest with tests/ on sys.path, as
    in the smoke test. Standalone (manual `python -m` runs, no tests/ on
    path), falls back to the same minimal raw INSERT the DB-backed V2 tests
    use directly (see tests/test_chat_send_v2_enqueue.py's ``_seed`` /
    tests/conftest.py's ``seed_user``). The registry mirror isn't needed for
    the level-B simulated drain (it never resolves auth), only the DB row is
    load-bearing.
    """
    try:
        from conftest import seed_user as _conftest_seed_user  # type: ignore
    except ImportError:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
    else:
        _conftest_seed_user(user_id)


def seed_synthetic_users(n: int, *, mock_base_url: str) -> list[str]:
    """Create ``n`` synthetic users wired for db_action_v2 chat processing.

    Mirrors the seeding pattern in tests/test_chat_send_v2_enqueue.py and
    tests/test_v2_wake_decision_adapter.py: a ``users`` row -> a ``model_api``
    config (BYOK envelope stub, ``base_url=mock_base_url``, provider=
    "anthropic" so ``agent_runtime_cutover.resolve_driver`` would resolve a
    real "claude" driver for a future level-A run) -> ``hosted_runtime_mode``
    flipped to ``db_action_v2`` -> activated (``first_chat_ok_at`` set via
    ``store.mark_first_chat_ok()``, the same gate the proactive activation
    check and chat/send v2 enqueue path require).

    Returns the list of new user_ids.
    """
    user_ids: list[str] = []
    for _ in range(n):
        uid = f"loadtest_{uuid.uuid4().hex[:12]}"
        _seed_user_row(uid)
        # Provider config lives in model_api_routes/credentials (model-api-multi-
        # profile), not a 'model_api' blob. Seed an active, test_status='ok' route so
        # list_agent_runtime_enabled_users / _load_model_api_config see the user.
        credential_id = db.model_api_credential_create(
            uid, provider="anthropic", base_url=mock_base_url, label="Loadtest",
            api_key_envelope={"body_ct": "x", "nonce": "n", "K_user": "k"},
            api_key_hint="sk-...load", supports_responses=False)
        route_id = db.model_api_route_upsert(uid, credential_id, "loadtest-mock", None)
        db.model_api_route_mark_test(uid, route_id, status="ok")
        db.model_api_route_activate(uid, route_id)
        store = core_store.get_store(uid)
        hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
        store.mark_first_chat_ok()
        user_ids.append(uid)
    return user_ids


def enqueue_chat_burst(user_ids: list[str]) -> int:
    """Enqueue one ``chat`` lane job per user. Returns the count enqueued."""
    count = 0
    for uid in user_ids:
        jobs_store.enqueue_job(uid, "chat", reason="loadtest_burst")
        count += 1
    return count


def build_simulated_processor(
    *,
    prompt_tokens: int = DEFAULT_PROMPT_TOKENS,
    completion_tokens: int = DEFAULT_COMPLETION_TOKENS,
    latency_ms: int = _SIMULATED_LATENCY_MS,
    worker_id: str = "loadtest-sim-worker",
) -> Callable[[], bool]:
    """Return a zero-arg callable ("processor") that advances ONE pending job
    per call through the real lifecycle (claim -> mark_running ->
    record_turn_metric -> mark_completed) with deterministic fake token
    counts, so ``collect_report()``'s queue-wait/latency/tokens/stuck
    calculations run against real DB rows without a real LLM/worker. Returns
    True if it processed a job, False if the queue had nothing claimable
    (used by ``drain`` only implicitly — the return value isn't required by
    the ``drain`` contract, just handy for callers/tests)."""

    def _tick() -> bool:
        job = jobs_store.claim_next_job(worker_id)
        if job is None:
            return False
        job_id = job["id"]
        jobs_store.mark_running(job_id)
        jobs_store.record_turn_metric(
            job_id=job_id, user_id=job["user_id"], lane=job.get("lane") or "chat",
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )
        jobs_store.mark_completed(job_id)
        return True

    return _tick


def drain(*, processor: Callable[[], object], timeout_sec: float) -> dict:
    """Drive ``processor()`` (a real worker pool tick OR a simulated one)
    until ``agent_jobs`` has no pending/claimed/running rows, or
    ``timeout_sec`` elapses. Returns ``{"drained": bool, "elapsed_sec":
    float}`` — ``drained`` is False iff the timeout was hit with work still
    in flight (a load-test failure signal, not raised as an exception so the
    caller still gets a partial report)."""
    start = time.monotonic()
    while time.monotonic() - start < timeout_sec:
        if jobs_store.inflight_job_count() == 0:
            return {"drained": True, "elapsed_sec": time.monotonic() - start}
        processor()
    elapsed = time.monotonic() - start
    return {"drained": jobs_store.inflight_job_count() == 0, "elapsed_sec": elapsed}


def collect_report() -> dict:
    """Build the metrics report dict from scripts.loadtest.collect, reading
    the same agent_jobs/v2_turn_metrics rows a real (or simulated) drain just
    produced."""
    queue_wait = collect.queue_wait_samples()
    turn_latency = collect.turn_latency_ms_samples()
    tokens = collect.tokens_per_turn_samples()
    return {
        "queue_wait_p95_sec": collect.p95(queue_wait),
        "queue_wait_mean_sec": collect.mean(queue_wait),
        "turn_latency_p95_ms": collect.p95(turn_latency),
        "tokens_per_turn_mean": collect.mean(tokens),
        "stuck_jobs": collect.stuck_job_count(),
        "rss_kb": collect.peak_rss_kb(os.getpid()),
    }


def run(*, users: int, workers: int, mock: "MockProvider | None") -> dict:
    """Top-level orchestration: seed -> enqueue -> drain -> collect.

    ``workers`` is accepted for interface/report parity with a real pool run
    (and the CLI's ``--workers`` flag) but does NOT change how the level-B
    simulated drain behaves — the simulated processor here is a single
    deterministic sequence (claim/mark_running/record_turn_metric/
    mark_completed per job), not a concurrent pool, so ``workers`` is only
    echoed back in the report as ``workers_configured`` for a human reading
    the JSON. A future level-A run wiring a real worker pool would use
    ``workers`` to size that pool for real.
    """
    if mock is not None:
        mock_base_url = mock.base_url
        prompt_tokens = mock.prompt_tokens
        completion_tokens = mock.completion_tokens
    else:
        mock_base_url = "http://127.0.0.1:0"
        prompt_tokens = DEFAULT_PROMPT_TOKENS
        completion_tokens = DEFAULT_COMPLETION_TOKENS

    user_ids = seed_synthetic_users(users, mock_base_url=mock_base_url)
    enqueued = enqueue_chat_burst(user_ids)
    processor = build_simulated_processor(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
    )
    # Bound the drain timeout by scale so a large manual run doesn't get cut
    # off early, while a small smoke run still fails fast if something wedges.
    timeout_sec = max(30.0, float(users) * 2.0)
    drain_result = drain(processor=processor, timeout_sec=timeout_sec)

    report = collect_report()
    report.update({
        "users_seeded": len(user_ids),
        "jobs_enqueued": enqueued,
        "workers_configured": workers,
        **drain_result,
    })
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the D4 load-test driver (see module docstring for the "
        "level-B simulated-drain fidelity caveat)."
    )
    parser.add_argument("--users", type=int, default=100, help="synthetic users to seed (default: 100)")
    parser.add_argument("--workers", type=int, default=16, help="reported worker concurrency (default: 16)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    with MockProvider() as mock:
        report = run(users=args.users, workers=args.workers, mock=mock)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
