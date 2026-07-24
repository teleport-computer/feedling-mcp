"""D4 load-test driver smoke test — scripts/loadtest/run_loadtest.py.

Fidelity level: **B (simulated drain)**, not A (real worker pool). Draining
the queue with the real V2 worker (``model_api_runtime.v2.worker.process_job``)
needs enclave-bound BYOK/enclave-auth decryption (``TurnDeps.resolve_provider``/
``read_messages``/``mint_enclave_token``) that's impractical to wire in a CI
unit test (see ``worker.TurnDeps`` docstring). So this smoke test exercises the
driver's orchestration (seed -> enqueue -> drain -> collect) with a SIMULATED
processor that advances jobs directly via ``jobs_store`` primitives (claim ->
mark_running -> record_turn_metric -> mark_completed) with deterministic fake
token counts — proving out the COLLECT + REPORT path end-to-end on real DB
rows, same scaffolding as tests/test_v2_jobs_store.py (seed_user + autouse
table-clean fixture).

The real 100-user run (against a real worker pool / real MockProvider traffic)
is a manual, ops-run exercise near cutover — NOT executed here. This test only
ever seeds 5 users.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user  # noqa: F401 (re-exported for parity with sibling test modules)

from scripts.loadtest import run_loadtest
from scripts.loadtest.mock_provider import MockProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed load-test harness tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def _clean_tables():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
        conn.execute("DELETE FROM v2_turn_metrics")
    yield


def test_smoke_5_users_simulated_drain_produces_full_report():
    """End-to-end orchestration at n=5 (never the 100-user manual run): seed ->
    enqueue -> drain (simulated processor) -> collect. Asserts every report
    key is present and the simulated token counts flow through to
    tokens_per_turn_mean."""
    with MockProvider(prompt_tokens=111, completion_tokens=33) as mock:
        user_ids = run_loadtest.seed_synthetic_users(5, mock_base_url=mock.base_url)
        assert len(user_ids) == 5

        enqueued = run_loadtest.enqueue_chat_burst(user_ids)
        assert enqueued == 5
        assert jobs_store.inflight_job_count() == 5

        processor = run_loadtest.build_simulated_processor(
            prompt_tokens=mock.prompt_tokens, completion_tokens=mock.completion_tokens,
        )
        drain_result = run_loadtest.drain(processor=processor, timeout_sec=10.0)

        assert drain_result["drained"] is True
        assert drain_result["elapsed_sec"] >= 0
        assert jobs_store.inflight_job_count() == 0

        report = run_loadtest.collect_report()

    expected_keys = {
        "queue_wait_p95_sec", "queue_wait_mean_sec", "turn_latency_p95_ms",
        "tokens_per_turn_mean", "stuck_jobs", "failed_turns", "rss_kb",
    }
    assert expected_keys <= set(report.keys())
    assert report["tokens_per_turn_mean"] == pytest.approx(111 + 33)
    assert report["stuck_jobs"] == 0
    assert report["failed_turns"] == 0
    assert report["queue_wait_p95_sec"] is not None
    assert report["turn_latency_p95_ms"] is not None

    # The simulated processor now writes whole-turn rows via
    # record_whole_turn_metric (spec B5), not the older per-call
    # record_turn_metric shape — every row should carry model_calls>=1.
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT model_calls, failed, status FROM v2_turn_metrics")
            rows = cur.fetchall()
    assert len(rows) == 5
    assert all(model_calls >= 1 for model_calls, _failed, _status in rows)
    assert all(failed is False for _model_calls, failed, _status in rows)
    assert all(status == "ok" for _model_calls, _failed, status in rows)


def test_seed_synthetic_users_wires_db_action_v2_and_activation():
    """The seeded users must be exactly the shape the level-B drain (and any
    future level-A wiring) needs: model_api config pointed at the mock
    provider, hosted_runtime_mode=db_action_v2, and activated
    (first_chat_ok_at set) — mirrors tests/test_chat_send_v2_enqueue.py /
    tests/test_v2_wake_decision_adapter.py."""
    from core import store as core_store
    from hosted import config_store as hosted_config_store

    with MockProvider() as mock:
        expected_base_url = mock.base_url
        [uid] = run_loadtest.seed_synthetic_users(1, mock_base_url=expected_base_url)

    store = core_store.get_store(uid)
    assert hosted_config_store.get_hosted_runtime_mode(store) == "db_action_v2"
    settings = store.load_proactive_settings()
    assert str(settings.get("first_chat_ok_at") or "").strip() != ""
    # Provider config now lives in model_api_routes/credentials (model-api-multi-
    # profile), not a 'model_api' blob. Verify the active route points at the mock.
    config = hosted_config_store._load_model_api_config(store)
    assert config is not None
    assert config.get("base_url") == expected_base_url


def test_run_orchestrates_seed_enqueue_drain_collect_at_n5():
    """Top-level `run()` ties the steps together and never runs the 100-user
    real load — this test always passes users=5."""
    with MockProvider(prompt_tokens=50, completion_tokens=10) as mock:
        report = run_loadtest.run(users=5, workers=2, mock=mock)

    assert report["users_seeded"] == 5
    assert report["jobs_enqueued"] == 5
    assert report["drained"] is True
    assert jobs_store.inflight_job_count() == 0
    assert report["tokens_per_turn_mean"] == pytest.approx(60)


def test_drain_times_out_when_processor_never_finishes():
    """No-op processor + a job stuck 'pending' forever -> drain gives up at the
    timeout and reports drained=False (never hangs the caller)."""
    seed_user("u_loadtest_stuck")
    jobs_store.enqueue_job("u_loadtest_stuck", "chat")
    result = run_loadtest.drain(processor=lambda: None, timeout_sec=0.3)
    assert result["drained"] is False
    assert result["elapsed_sec"] >= 0.3


def test_mock_provider_estimates_tokens_from_request_and_accumulates():
    import json
    import urllib.request
    from scripts.loadtest.mock_provider import MockProvider, estimate_tokens_from_text

    long_prompt = "x" * 4000
    with MockProvider(reply="ok", estimate_tokens=True) as p:
        def _post(content: str) -> dict:
            req = urllib.request.Request(
                f"{p.base_url}/chat/completions",
                data=json.dumps({"messages": [{"role": "user", "content": content}]}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())

        small = _post("hi")
        large = _post(long_prompt)

        # A bigger prompt MUST cost more prompt_tokens — the whole point of the gate.
        assert large["usage"]["prompt_tokens"] > small["usage"]["prompt_tokens"]
        assert large["usage"]["prompt_tokens"] == estimate_tokens_from_text(long_prompt)
        assert small["usage"]["completion_tokens"] == estimate_tokens_from_text("ok")

        # Server-side accumulators see EVERY call, whoever made it.
        assert p.request_count == 2
        assert p.total_prompt_tokens == (
            small["usage"]["prompt_tokens"] + large["usage"]["prompt_tokens"])


def test_mock_provider_fixed_tokens_remains_the_default():
    import json
    import urllib.request
    from scripts.loadtest.mock_provider import MockProvider

    with MockProvider(reply="ok", prompt_tokens=100, completion_tokens=20) as p:
        req = urllib.request.Request(
            f"{p.base_url}/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "x" * 4000}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
    assert body["usage"]["prompt_tokens"] == 100
    assert body["usage"]["completion_tokens"] == 20


def test_mock_provider_serves_the_openai_responses_wire_with_sse():
    """codex speaks Responses (`POST /v1/responses`), not `/chat/completions`. Without this
    route the resident-baseline harness receives zero requests."""
    import json
    import urllib.request
    from scripts.loadtest.mock_provider import MockProvider

    with MockProvider(reply="ok", estimate_tokens=True) as p:
        req = urllib.request.Request(
            f"{p.base_url}/v1/responses",
            data=json.dumps({
                "instructions": "S" * 4000,           # codex's own system prompt
                "input": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "name": "shell"}],
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            assert r.headers.get("Content-Type") == "text/event-stream"
            body = r.read().decode()

    assert "event: response.completed" in body
    assert "output_text" in body
    # instructions + input + tools all counted: 4000 chars of instructions alone is 1000 tokens.
    assert p.total_prompt_tokens > 1000
    assert p.request_count == 1


def test_responses_token_count_includes_instructions_and_tools():
    """Counting only the user's message understates the resident by >10x — the whole point."""
    from scripts.loadtest.mock_provider import _estimate_responses_prompt_tokens

    user_only = _estimate_responses_prompt_tokens({"input": [{"role": "user", "content": "hi"}]})
    with_overhead = _estimate_responses_prompt_tokens({
        "instructions": "S" * 20000,
        "input": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "name": f"t{i}"} for i in range(9)],
    })
    assert with_overhead > user_only * 50


def test_chat_completions_route_is_unaffected_by_the_responses_branch():
    import json
    import urllib.request
    from scripts.loadtest.mock_provider import MockProvider

    with MockProvider(reply="ok", prompt_tokens=100, completion_tokens=20) as p:
        req = urllib.request.Request(
            f"{p.base_url}/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
    assert body["usage"]["prompt_tokens"] == 100
    assert body["object"] == "chat.completion"
