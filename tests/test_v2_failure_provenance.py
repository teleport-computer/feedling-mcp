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


@pytest.mark.parametrize("lane", ["capture", "dream"])
@pytest.mark.parametrize("fault", ["none", "health_same_code", "usage_same_code", "cancel", "lease", "projector"])
def test_extraction_snapshot_binds_only_returned_failure(monkeypatch, lane, fault):
    """Real worker/extract fence; same-code secondary errors are not the cause."""
    from types import SimpleNamespace
    import test_v2_extraction_lanes as L
    from model_api_runtime.v2 import extraction

    uid = f"u_cause_{lane}_{fault}"
    L._seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("cause-worker")
    events, calls = [], []
    reason = "provider_call_failed:upstream_unavailable"
    original = provider_client.ProviderError(SECRET)
    original.__cause__ = httpx.PoolTimeout(SECRET)

    async def provider(*a, **kw):
        calls.append("provider")
        raise original

    async def health(*a, **kw):
        calls.append("health")
        if fault == "health_same_code":
            raise RuntimeError(reason)
        if fault == "cancel":
            raise asyncio.CancelledError()
        if fault == "lease":
            raise worker.LostJobLease("synthetic lost lease")

    def usage(*a):
        calls.append("usage")
        if fault == "usage_same_code":
            raise RuntimeError(reason)

    async def record(_recorder, kind, payload, **kw):
        events.append((kind, dict(payload)))
        return True

    def forbidden(*a, **kw):
        pytest.fail("projection must not add store/emit/thread I/O")

    real_build = fp.build_detail

    def project(*a, **kw):
        # These operations are prohibited specifically during the projection,
        # not during the worker's pre-existing persistence operations.
        with monkeypatch.context() as m:
            m.setattr(fp, "observe", forbidden)
            m.setattr(worker.asyncio, "to_thread", forbidden)
            m.setattr(L.core_store, "get_store_per_load_mode", forbidden)
            assert asyncio.get_running_loop().is_running()
            calls.append("projection")
            if fault == "projector":
                raise RuntimeError(SECRET)
            return real_build(*a, **kw)

    monkeypatch.setattr(fp, "build_detail", project)
    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", provider)
    monkeypatch.setattr(worker, "_record_provider_failure_class", health)
    monkeypatch.setattr(worker, "_record_trajectory", record)
    tm = SimpleNamespace(add_call=usage, flush=lambda **kw: None)

    async def run():
        return await worker._run_extraction(
            job_id, uid, lane, L._deps(), L._BYOK, asyncio.Semaphore(1),
            claimed_by=job["claimed_by"], tm=tm,
        )

    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
        assert not [p for k, p in events if k == "turn_exception"]
        return
    if fault == "lease":
        with pytest.raises(worker.LostJobLease):
            asyncio.run(run())
        assert not [p for k, p in events if k == "turn_exception"]
        return
    result = asyncio.run(run())
    assert result == "failed"
    failures = [p for k, p in events if k == "turn_exception"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["error_code"] == "extraction_failed:upstream_unavailable"
    assert L._job_row(job_id) == ("failed", "extraction_failed:upstream_unavailable")
    assert ("provider_failure" in failure) == (fault == "none")
    if fault == "none":
        detail = failure["provider_failure"]
        assert detail["exception_family"] == "provider_error"
        assert detail["cause_family"] == "http_pool_timeout"
        assert detail["stage"] == "extraction_provider"
        assert detail["lane"] == lane and detail["observation_only"] is True
        assert SECRET not in json.dumps(failure)
        assert calls == ["provider", "projection", "usage", "health"]
    assert calls.count("provider") == calls.count("projection") == 1


@pytest.mark.parametrize("failure_kind", ["runtime", "read", "status400", "status503"])
@pytest.mark.parametrize("broken_observer", [False, True])
def test_extract_observer_preserves_reliable_attempts_and_return(monkeypatch, failure_kind, broken_observer):
    from model_api_runtime.v2 import extraction
    calls, seen = [], []

    def error():
        if failure_kind == "runtime":
            return RuntimeError(SECRET)
        if failure_kind == "read":
            e = provider_client.ProviderError(SECRET)
            e.__cause__ = httpx.ReadTimeout(SECRET)
            return e
        return provider_client.ProviderError(SECRET, status_code=int(failure_kind[6:]))

    async def provider(*a, **kw):
        calls.append((kw["max_tokens"], kw["timeout"]))
        raise error()

    async def no_sleep(*a):
        pass

    def observe(exc, code):
        seen.append((fp._family(exc), fp._family(exc.__cause__), code))
        if broken_observer:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    monkeypatch.setattr(provider_client.asyncio, "sleep", no_sleep)
    args = dict(provider_config=object(), prompt="synthetic", parse=lambda _: ([], None),
                max_tokens=1500, timeout_sec=90, wire_deadline_sec=90)
    baseline = asyncio.run(extraction.extract(**args))
    baseline_calls = list(calls)
    calls.clear()
    result = asyncio.run(extraction.extract(**args, provider_failure_observer=observe))
    assert result == baseline and calls == baseline_calls
    assert len(calls) == (1 if failure_kind == "status400" else 3)
    assert len(seen) == 1
    assert seen[0][2] == ("provider_config" if failure_kind == "status400" else "upstream_unavailable")
    if failure_kind == "read":
        assert seen[0][:2] == ("provider_error", "http_timeout")


@pytest.mark.parametrize("special", ["refusal", "truncation", "deadline", "budget_fallback"])
def test_extract_does_not_observe_existing_special_paths(monkeypatch, special):
    import time
    import provider_deadline
    from model_api_runtime.v2 import extraction
    calls = []
    deadline = time.monotonic() + 60

    async def provider(*a, **kw):
        calls.append(kw["max_tokens"])
        if special == "deadline":
            raise provider_deadline.ProviderAbsoluteDeadlineExceeded(deadline, "entry")
        if special == "refusal":
            # Use the actual parser's supported refusal projection.
            return provider_client._parse_anthropic_body({
                "stop_reason": "refusal",
                "stop_details": {"category": "reasoning_extraction", "explanation": SECRET},
                "content": [], "usage": {"input_tokens": 5, "output_tokens": 0},
            }, model="test", require_reply=True)
        if len(calls) == 1 or special == "truncation":
            return {"reply": "partial", "stop_reason": "length", "usage": {}}
        if len(calls) == 2:
            raise provider_client.ProviderError(SECRET, status_code=402)
        return {"reply": "{}", "stop_reason": "stop", "usage": {}}

    def forbidden(*a):
        pytest.fail("special path must not become generic provenance")

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", provider)
    result = asyncio.run(extraction.extract(
        provider_config=object(), prompt="synthetic", parse=lambda _: ([], None),
        max_tokens=1500, truncation_retry_max_tokens=3000,
        absolute_deadline=deadline if special == "deadline" else None,
        parse_retry=extraction.ParseRetry(lambda _: True, lambda p, e: p, lambda _: ([], None),
                                         build_truncation_prompt=lambda p: p),
        provider_failure_observer=forbidden,
    ))
    expected = {"refusal": (None, "provider_call_failed:content_filtered"),
                "truncation": (None, "output_truncated"),
                "deadline": (None, "dream_turn_budget_exhausted"),
                "budget_fallback": ([], None)}
    assert result == expected[special]
    assert calls == ({"refusal": [1500], "truncation": [1500, 3000],
                      "deadline": [1500], "budget_fallback": [1500, 3000, 1500]}[special])


@pytest.mark.parametrize("recording", ["enabled", "disabled", "drop"])
def test_worker_provenance_adds_no_io_or_events(monkeypatch, recording):
    import test_v2_extraction_lanes as L
    from model_api_runtime.v2 import extraction
    real_extract, real_thread = extraction.extract, asyncio.to_thread
    counts, events = [], []

    async def thread(fn, *a, **kw):
        counts.append(fn.__qualname__)
        return await real_thread(fn, *a, **kw)

    class Recorder:
        async def record(self, kind, payload):
            events.append((kind, dict(payload)))

        async def record_best_effort(self, kind, payload):
            if recording == "drop":
                return False
            await self.record(kind, payload)
            return True

    async def provider(*a, **kw):
        counts.append("provider")
        raise provider_client.ProviderError(SECRET, status_code=503)

    async def health(*a, **kw):
        counts.append("health")

    monkeypatch.setattr(asyncio, "to_thread", thread)
    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", provider)
    monkeypatch.setattr(worker, "_record_provider_failure_class", health)
    evidence = []
    for observed in [False, True]:
        uid = f"u_cause_io_{recording}_{observed}"
        L._seed_v2(uid)
        job_id, _ = jobs_store.enqueue_job(uid, "dream")
        job = jobs_store.claim_next_job("cause-io")

        async def extract(**kw):
            if not observed:
                kw.pop("provider_failure_observer", None)
            return await real_extract(**kw)

        monkeypatch.setattr(extraction, "extract", extract)
        counts.clear()
        events.clear()

        async def run():
            return await worker._run_extraction(
                job_id, uid, "dream", L._deps(), L._BYOK, asyncio.Semaphore(1),
                claimed_by=job["claimed_by"],
                trajectory_recorder=None if recording == "disabled" else Recorder(),
            )

        assert asyncio.run(run()) == "failed"
        assert L._job_row(job_id) == ("failed", "extraction_failed:upstream_unavailable")
        failures = [p for k, p in events if k == "turn_exception"]
        if recording == "enabled":
            assert len(failures) == 1
            assert ("provider_failure" in failures[0]) == observed
        else:
            assert failures == []
        evidence.append((list(counts), [k for k, p in events]))
    assert evidence[0] == evidence[1]


def test_concurrent_extractions_keep_provenance_job_local(monkeypatch):
    import test_v2_extraction_lanes as L
    from types import SimpleNamespace
    ready = set()
    events = {}
    jobs = []
    for label in ["first", "second"]:
        uid = "u_cause_concurrent_" + label
        L._seed_v2(uid)
        job_id, _ = jobs_store.enqueue_job(uid, "dream")
        job = jobs_store.claim_next_job("cause-concurrent")
        jobs.append((label, uid, job_id, job["claimed_by"]))

    async def scenario():
        both = asyncio.Event()

        async def provider(*a, **kw):
            label = asyncio.current_task().get_name()
            if label == "first":
                e = provider_client.ProviderError(SECRET)
                e.__cause__ = httpx.ReadTimeout(SECRET)
                raise e
            raise provider_client.ProviderError(SECRET, status_code=503)

        async def health(uid, *a, **kw):
            ready.add(uid)
            if len(ready) == 2:
                both.set()
            await both.wait()

        async def record(recorder, kind, payload, **kw):
            if kind == "turn_exception":
                events[recorder.label] = payload["provider_failure"]
            return True

        monkeypatch.setattr(provider_client, "reliable_chat_completion_async", provider)
        monkeypatch.setattr(worker, "_record_provider_failure_class", health)
        monkeypatch.setattr(worker, "_record_trajectory", record)
        tasks = [asyncio.create_task(worker._run_extraction(
            jid, uid, "dream", L._deps(), L._BYOK, asyncio.Semaphore(1),
            claimed_by=owner, trajectory_recorder=SimpleNamespace(label=label),
        ), name=label) for label, uid, jid, owner in jobs]
        assert await asyncio.wait_for(asyncio.gather(*tasks), 5) == ["failed", "failed"]

    asyncio.run(scenario())
    assert events["first"]["cause_family"] == "http_timeout"
    assert events["first"]["http_status"] is None
    assert events["second"]["cause_family"] == "none"
    assert events["second"]["http_status"] == 503
