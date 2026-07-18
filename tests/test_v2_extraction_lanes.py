import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from model_api_runtime.v2 import extraction, jobs_store, worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user", base_url="")


def _seed_v2(uid: str) -> None:
    conftest.seed_user(uid)
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _job_row(job_id):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()


def _deps(**over):
    base = dict(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after, limit: [
            {"id": "m1", "ts": 1.0, "role": "user", "content": "我换工作了"}],
        read_memory_context=lambda uid: {
            "ai_name": "小克", "user_name": "Z", "buckets": "B",
            "threads": "T", "identity": "I", "cards": "C"},
        build_memory_envelope=lambda uid, inner: {"body_ct": "CT", "_inner": inner},
        apply_memory_actions=lambda uid, actions: {
            "status": "ok", "applied": len(actions)},
    )
    base.update(over)
    return worker.TurnDeps(**base)


def test_dream_is_a_lane_with_background_priority():
    assert "dream" in jobs_store.LANES
    assert jobs_store.LANE_PRIORITY["dream"] == jobs_store.LANE_PRIORITY["capture"]


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_lane_applies_actions_and_completes(monkeypatch, lane):
    uid = f"u_x_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        assert provider_config is _BYOK          # BYOK-only
        if lane == "capture":
            return ([{"action": "add", "summary": "s", "content": "c"}], None)
        return ([{
            "op": "merge",
            "card_ids": ["old-a", "old-b"],
            "result": {"summary": "s", "content": "c"},
        }], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    applied = {}
    deps = _deps(apply_memory_actions=lambda uid_, actions: (
        applied.update(n=len(actions)) or {"status": "ok"}))

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert applied == {"n": 1}
    assert _job_row(job_id)[0] == "completed"


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_lane_records_whole_turn_metric_on_success(monkeypatch, lane):
    """PR B review finding: `_run_extraction` makes a real `v2_extraction.extract`
    BYOK call but never flushed a `v2_turn_metrics` row on success.
    Extraction now surfaces the same normalized usage/cache telemetry as the
    native chat loop."""
    uid = f"u_x_metric_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        kw["usage_out"]({
            "prompt_tokens": 90,
            "completion_tokens": 9,
            "cache_read_tokens": 70,
            "cache_write_tokens": None,
            "cache_miss_tokens": 20,
        })
        if lane == "capture":
            return ([{"action": "add", "summary": "s", "content": "c"}], None)
        return ([{
            "op": "merge",
            "card_ids": ["old-a", "old-b"],
            "result": {"summary": "s", "content": "c"},
        }], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    deps = _deps()

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT lane, prompt_tokens, completion_tokens, model_calls, failed, status, "
            "cache_read_tokens, cache_miss_tokens, usage_reported_calls, "
            "cache_reported_calls, provider, model "
            "FROM v2_turn_metrics WHERE job_id=%s", (job_id,)).fetchone()
    assert row is not None
    assert row[0] == lane
    assert row[1] == 90 and row[2] == 9
    assert row[3] == 1
    assert row[4] is False
    assert row[5] == "ok"
    assert row[6:] == (
        70, 20, 1, 1, "anthropic", "claude-sonnet-4-test",
    )


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_zero_results_completes_without_applying_anything(monkeypatch, lane):
    """`nothing_worth_keeping` is SUCCESS — mirrors the wake lane's weak-wake-sleeps."""
    uid = f"u_x_empty_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _empty(*, provider_config, prompt, parse, **kw):
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _empty)
    applied = {"n": 0}
    deps = _deps(apply_memory_actions=lambda uid_, a: applied.update(n=applied["n"] + 1) or {})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert applied["n"] == 0
    assert _job_row(job_id)[0] == "completed"


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_failure_is_silent_no_bubble_no_error_chip(monkeypatch, lane):
    uid = f"u_x_fail_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _err(*, provider_config, prompt, parse, **kw):
        return (None, "provider_call_failed:RuntimeError")

    monkeypatch.setattr(extraction, "extract", _err)
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(t=text) or {"id": "r"})
    emitted = []
    monkeypatch.setattr(worker, "_emit_status", lambda *a, **k: emitted.append(a))

    status = asyncio.run(worker.process_job(
        job, _deps(), provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert written == {}                       # no chat bubble
    assert emitted == []                       # no user-visible status/error chip
    row = _job_row(job_id)
    assert row == ("failed", "extraction_failed:runtimeerror")


def test_rejected_memory_write_fails_job_instead_of_marking_completed(monkeypatch):
    uid = "u_x_write_rejected"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{"action": "add", "type": "fact", "summary": "s", "content": "c"}], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    deps = _deps(apply_memory_actions=lambda _uid, _actions: {
        "status": "error", "error": "occurred_at_required",
    })

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert _job_row(job_id)[0] == "failed"


def test_nonempty_extraction_without_writer_fails_closed(monkeypatch):
    uid = "u_x_writer_missing"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{"action": "add", "summary": "s", "content": "c"}], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    deps = _deps(apply_memory_actions=None)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert _job_row(job_id)[0] == "failed"


def test_extraction_rollback_during_llm_blocks_memory_write(monkeypatch):
    uid = "u_x_rollback"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        return ([{"action": "add", "summary": "s", "content": "c"}], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    mode_checks = iter([True, False])
    applied = {"n": 0}
    deps = _deps(
        runtime_mode_enabled=lambda uid_: next(mode_checks),
        apply_memory_actions=lambda uid_, actions: (
            applied.update(n=len(actions)) or {"status": "ok"}),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK,
        api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert applied["n"] == 0
    assert _job_row(job_id)[0] == "failed"


def test_capture_prompt_degrades_when_memory_context_is_missing(monkeypatch):
    """Context fetch failure must degrade, not fail the job (spec §3.5)."""
    uid = "u_x_nocontext"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    seen = {}

    async def _cap(*, provider_config, prompt, parse, **kw):
        seen["prompt"] = prompt
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _cap)
    status = asyncio.run(worker.process_job(
        job, _deps(read_memory_context=None), provider_config=_BYOK,
        api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert "（暂无）" in seen["prompt"]          # prompt builder's own fallback kicked in


def test_extraction_reads_go_through_the_enclave_semaphore(monkeypatch):
    """spec §4: read_memory_context (3 post_enclave round-trips) and read_tail (per-message
    decrypt) are BOTH enclave-bound. The enclave is single-threaded — the whole point of this
    subproject — so both must sit inside the turn's enclave_sem. A background lane that
    bypasses the gate can starve the interactive chat path."""
    import asyncio as _asyncio

    class _CountingSemaphore(_asyncio.Semaphore):
        def __init__(self, value=2):
            super().__init__(value)
            self.held = 0
            self.acquire_count = 0

        async def acquire(self):
            self.acquire_count += 1
            got = await super().acquire()
            self.held += 1
            return got

        def release(self):
            self.held -= 1
            super().release()

    uid = "u_x_sem"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    sem = _CountingSemaphore(2)
    inside = {"ctx": None, "tail": None}

    def _ctx(uid_):
        inside["ctx"] = sem.held          # must be >0 -> we are inside the gate
        return {"buckets": "B"}

    def _tail(uid_, after, limit):
        inside["tail"] = sem.held
        return [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]

    async def _empty(*, provider_config, prompt, parse, **kw):
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _empty)
    deps = _deps(read_memory_context=_ctx, read_tail=_tail)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None,
        runtime_token="rt", enclave_sem=sem))

    assert status == "completed"
    assert inside["ctx"] == 1, "read_memory_context ran OUTSIDE enclave_sem"
    assert inside["tail"] == 1, "read_tail ran OUTSIDE enclave_sem"
    assert sem.acquire_count >= 1
