"""T807: observe causes without changing durable/public failure semantics."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from psycopg_pool import PoolTimeout as DatabasePoolTimeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import provider_client
from model_api_runtime.v2 import failure_provenance as fp, worker, jobs_store

SECRET = "sk-private-token https://private.example/user body private-content"


@pytest.mark.parametrize(
    "exc,family,status",
    [
        (provider_client.ProviderError(SECRET, status_code=503), "provider_error", 503),
        (DatabasePoolTimeout(SECRET), "database_pool_timeout", None),
        (httpx.PoolTimeout(SECRET), "http_pool_timeout", None),
        (httpx.ReadTimeout(SECRET), "http_timeout", None),
        (RuntimeError(SECRET), "runtime_error", None),
        (Exception(SECRET), "unknown", None),
    ],
)
def test_closed_projection_distinguishes_types_without_text(exc, family, status):
    d = fp.build_detail(
        exc,
        stage="wake",
        lane="heartbeat",
        code="wake_failed:error",
        error_class="unknown",
        public_codes=worker.PUBLIC_FAILURE_CODES,
    )
    assert d["exception_family"] == family and d["http_status"] == status
    assert SECRET not in json.dumps(d) and "private" not in json.dumps(d)
    assert len(d) <= 20 and d["observation_only"] is True


def test_only_explicit_cause_and_valid_provider_status_are_evidence():
    e = RuntimeError(SECRET)
    e.__cause__ = provider_client.ProviderError(SECRET, status_code=429)
    d = fp.build_detail(
        e,
        stage="wake",
        lane="heartbeat",
        code="bad-private",
        error_class="bad-private",
        public_codes=worker.PUBLIC_FAILURE_CODES,
    )
    assert d["cause_family"] == "provider_error" and d["cause_http_status"] == 429
    assert d["error_code"] == d["error_class"] == "unknown"
    e.__cause__ = None
    e.__context__ = provider_client.ProviderError(SECRET, status_code=503)
    e.status_code = 503  # An arbitrary RuntimeError cannot assert HTTP provenance.
    d = fp.build_detail(
        e,
        stage="private",
        lane="private",
        code="wake_failed:error",
        error_class="unknown",
        public_codes=worker.PUBLIC_FAILURE_CODES,
    )
    assert d["cause_family"] == "none" and d["http_status"] is None
    assert d["stage"] == "unknown" and d["lane"] == "other"


@pytest.mark.parametrize("status", [True, "503", -1, 600, None])
def test_malformed_http_status_is_unknown(status):
    e = provider_client.ProviderError(SECRET, status_code=status)
    assert fp._status(e) is None


def test_unknown_class_names_and_unprintable_messages_are_never_serialized():
    class PrivateClass(Exception):
        def __str__(self):
            raise AssertionError("must not stringify")

    d = fp.build_detail(
        PrivateClass(),
        stage="wake",
        lane="heartbeat",
        code="wake_failed:error",
        error_class="unknown",
        public_codes=worker.PUBLIC_FAILURE_CODES,
    )
    assert d["exception_family"] == "unknown" and "PrivateClass" not in json.dumps(d)


@pytest.mark.parametrize("lane", ["heartbeat", "scheduled"])
@pytest.mark.parametrize("emitter_fails", [False, True])
def test_actual_wake_failure_retains_queue_code_and_safe_observation(
    monkeypatch, emitter_fails, lane
):
    import test_v2_wake_worker as W

    uid = "u_provenance_" + lane + str(emitter_fails).lower()
    W.conftest.seed_user(uid)
    W._reset(uid)
    job, _ = jobs_store.enqueue_job(uid, lane, trace_id="trace-safe")
    claimed = W._claim(job)
    events = []
    deps = W._wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])

    def emit(user, event, **fields):
        if event == "v2.failure.observed":
            events.append((user, fields))
            if emitter_fails:
                raise RuntimeError(SECRET)

    deps.emit_debug_trace = emit

    async def fail_loop(**kwargs):
        try:
            raise DatabasePoolTimeout(SECRET)
        except DatabasePoolTimeout as cause:
            raise RuntimeError(SECRET) from cause

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", fail_loop)
    result = asyncio.run(
        worker._run_wake(
            job,
            uid,
            lane,
            deps,
            W._BYOK,
            asyncio.Semaphore(4),
            claimed,
            trace_id="trace-safe",
        )
    )
    assert result == ("rescheduled" if lane == "scheduled" else "failed")
    with jobs_store._pool().connection() as conn:
        row = conn.execute(
            "SELECT status,last_error,attempt_count FROM agent_jobs WHERE id=%s", (job,)
        ).fetchone()
    assert row == (
        ("pending", "scheduled_retry:wake_failed:runtimeerror", 1)
        if lane == "scheduled"
        else ("failed", "wake_failed:runtimeerror", 1)
    )
    assert len(events) == 1  # Parent worker drops the exception provenance here.
    user, fields = events[0]
    assert (
        user == uid
        and fields["job_id"] == str(job)
        and fields["trace_id"] == "trace-safe"
    )
    assert fields["detail"]["cause_family"] == "database_pool_timeout"
    assert fields["detail"]["exception_family"] == "runtime_error"
    assert "private" not in json.dumps(fields)


def test_disabled_emitter_does_not_classify_exception():
    def classify():
        raise AssertionError("disabled trace must not classify")

    asyncio.run(
        fp.observe(
            None,
            user_id="u",
            job_id=1,
            trace_id="",
            exc=RuntimeError(),
            stage="wake",
            lane="heartbeat",
            code="wake_failed:runtimeerror",
            classify=classify,
            public_codes=worker.PUBLIC_FAILURE_CODES,
        )
    )


@pytest.mark.parametrize("stage", ["chat", "setup"])
def test_chat_observes_original_provider_status(monkeypatch, stage):
    import test_v2_worker_mcp as M
    import test_v2_wake_worker as W

    uid = "u_provenance_" + stage
    M.conftest.seed_user(uid)
    M._reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat", trace_id="trace-chat")
    W._claim(job_id)
    with jobs_store._pool().connection() as conn:
        from psycopg.rows import dict_row

        with conn.cursor(row_factory=dict_row) as cur:
            job = cur.execute(
                "SELECT * FROM agent_jobs WHERE id=%s", (job_id,)
            ).fetchone()
    deps = M._deps([{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    events = []

    def emit(user, event, **fields):
        if event == "v2.failure.observed":
            events.append(fields)

    deps.emit_debug_trace = emit

    async def fail(**kwargs):
        raise provider_client.ProviderError(SECRET, status_code=503)

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", fail)
    if stage == "setup":

        async def fail_resolve(*args):
            raise provider_client.ProviderError(SECRET, status_code=503)

        monkeypatch.setattr(worker, "_resolve_provider_for_current_job", fail_resolve)
        result = asyncio.run(worker._run_turn_body(job, deps))
    else:
        result = asyncio.run(
            worker.process_job(
                job, deps, provider_config=M._BYOK, api_key=None, runtime_token="test"
            )
        )
    assert result == "failed"
    assert len(events) == 1
    detail = events[0]["detail"]
    assert detail["stage"] == stage and detail["http_status"] == 503
    assert detail["exception_family"] == "provider_error"
    assert "private" not in json.dumps(events)
