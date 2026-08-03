import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from core import store as core_store
from model_api_runtime.v2 import extraction, jobs_store, worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user", base_url="")


def _seed_v2(uid: str) -> None:
    conftest.seed_user(uid)
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_capture_batches")
        conn.execute("DELETE FROM agent_jobs")
    yield


def _job_row(job_id):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()


def _deps(**over):
    def _envelope(uid, inner, item_id=None):
        return {
            "id": item_id or "mom_test",
            "owner_user_id": uid,
            "visibility": "shared",
            "body_ct": "CT",
            "nonce": "NONCE",
            "K_user": "KU",
            "K_enclave": "KE",
            "_inner": inner,
        }

    base = dict(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after, limit: [
            {
                "id": "m1",
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "我换工作了",
            }
        ],
        read_compaction_tail_after_seq=lambda uid, after, limit, **kw: [
            {
                "id": "m1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "我换工作了",
            }
        ],
        read_memory_context=lambda uid: {
            "ai_name": "小克", "user_name": "Z", "buckets": "B",
            "threads": "T", "identity": "I", "cards": "C"},
        build_memory_envelope=_envelope,
        apply_memory_actions=lambda uid, actions: {
            "status": "ok", "applied": len(actions)},
        read_capture_state=lambda uid: {
            "last_captured_until_message_id": "",
            "last_captured_until_ts": 0.0,
            "last_captured_until_seq": 0,
            "capture_seq_initialized": True,
        },
        get_prepared_capture_batch=jobs_store.get_prepared_capture_batch,
        prepare_capture_batch=jobs_store.prepare_capture_batch,
        authorize_capture_provider_call=jobs_store.authorize_capture_provider_call,
        commit_capture_batch=jobs_store.commit_capture_batch,
        fail_capture_job=jobs_store.fail_capture_job,
        cancel_capture_job=jobs_store.cancel_capture_job,
        capture_enabled=lambda _uid: True,
        dream_enabled=lambda _uid: True,
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
    ordering = []

    def _apply(uid_, actions):
        ordering.append("memory_write")
        applied.update(n=len(actions))
        return {"status": "ok"}

    async def _profile_enqueue(uid_, *, reason, force):
        ordering.append("profile_enqueue")
        assert uid_ == uid
        assert reason == "dream_refresh"
        assert force is True
        return True

    monkeypatch.setattr(worker, "_enqueue_profile_if_due", _profile_enqueue)
    deps = _deps(apply_memory_actions=_apply)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert applied == ({"n": 1} if lane == "dream" else {})
    assert ordering == (
        ["memory_write", "profile_enqueue"] if lane == "dream" else []
    )
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
        return (None, "provider_call_failed:upstream_unavailable")

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
    assert row == ("failed", "extraction_failed:upstream_unavailable")


def test_rejected_memory_write_fails_job_instead_of_marking_completed(monkeypatch):
    uid = "u_x_write_rejected"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{"action": "add", "type": "fact", "summary": "s", "content": "c"}], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)

    def _reject(**kwargs):
        assert jobs_store.fail_capture_job(
            job_id=kwargs["job_id"],
            user_id=kwargs["user_id"],
            claimed_by=kwargs["claimed_by"],
            error="capture_semantic_rejection",
        )
        return {"rejected": True, "reason": "capture_semantic_rejection"}

    deps = _deps(prepare_capture_batch=_reject)

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
    deps = _deps(prepare_capture_batch=None)

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


def test_capture_prompt_includes_existing_card_ids(monkeypatch):
    uid = "u_x_cards_context"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    seen = {}

    async def _capture(*, prompt, **_kwargs):
        seen["prompt"] = prompt
        return [], None

    monkeypatch.setattr(extraction, "extract", _capture)
    deps = _deps(
        read_memory_context=lambda _uid: {
            "cards": "- [mom_existing] （桶：工作）之前的工作记忆"
        }
    )
    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )
    assert status == "completed"
    assert "[mom_existing]" in seen["prompt"]
    assert "target_id" in seen["prompt"]


def test_extraction_reads_go_through_the_enclave_semaphore(monkeypatch):
    """spec §4: read_memory_context (3 post_enclave round-trips) and read_tail (per-message
    decrypt) are BOTH enclave-bound. The enclave is a shared, capacity-bounded decrypt proxy
    (prod: 4 workers x 32 threads, GIL-bound crypto) — protecting it is the whole point of
    this subproject — so both must sit inside the turn's enclave_sem. A background lane that
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

    def _tail(uid_, after, limit, **_kwargs):
        inside["tail"] = sem.held
        return [
            {
                "id": "m1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "content": "hi",
            }
        ]

    async def _empty(*, provider_config, prompt, parse, **kw):
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _empty)
    deps = _deps(
        read_memory_context=_ctx,
        read_compaction_tail_after_seq=_tail,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None,
        runtime_token="rt", enclave_sem=sem))

    assert status == "completed"
    assert inside["ctx"] == 1, "read_memory_context ran OUTSIDE enclave_sem"
    assert inside["tail"] == 1, "read_tail ran OUTSIDE enclave_sem"
    assert sem.acquire_count >= 1


def test_capture_all_non_live_batch_advances_without_provider(monkeypatch):
    uid = "u_x_non_live_only"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    provider_calls = []

    async def _provider_forbidden(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider_forbidden)
    deps = _deps(
        read_compaction_tail_after_seq=lambda *_args, **_kwargs: [
            {
                "id": "verify-1",
                "seq": 1,
                "ts": 20.0,
                "role": "user",
                "raw_role": "user",
                "source": "verify_ping",
                "capture_eligible": False,
                "content": "synthetic secret",
            },
            {
                "id": "import-2",
                "seq": 2,
                "ts": 10.0,
                "role": "user",
                "raw_role": "user",
                "source": "history_import",
                "capture_eligible": False,
                "content": "old imported content",
            },
        ]
    )
    assert asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"
    assert provider_calls == []
    assert _job_row(job_id)[0] == "completed"
    state = db.get_blob_strict(uid, "capture_state")
    assert state["last_captured_until_seq"] == 2
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_moments WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0


def test_capture_mixed_batch_discloses_only_live_rows(monkeypatch):
    uid = "u_x_mixed_sources"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    prompts = []

    async def _capture(*, prompt, **_kwargs):
        prompts.append(prompt)
        return [], None

    monkeypatch.setattr(extraction, "extract", _capture)
    deps = _deps(
        read_compaction_tail_after_seq=lambda *_args, **_kwargs: [
            {
                "id": "import-1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "history_import",
                "capture_eligible": False,
                "content": "MUST_NOT_DISCLOSE",
            },
            {
                "id": "live-2",
                "seq": 2,
                "ts": 2.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "eligible live turn",
            },
        ]
    )
    assert asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"
    assert len(prompts) == 1
    assert "eligible live turn" in prompts[0]
    assert "MUST_NOT_DISCLOSE" not in prompts[0]
    assert db.get_blob_strict(uid, "capture_state")["last_captured_until_seq"] == 2


def test_empty_capture_successor_completes_without_backoff_or_provider(monkeypatch):
    uid = "u_x_empty_successor"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    provider_calls = []

    async def _provider_forbidden(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider_forbidden)
    assert asyncio.run(
        worker.process_job(
            job,
            _deps(read_compaction_tail_after_seq=lambda *_a, **_k: []),
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"
    assert provider_calls == []
    assert _job_row(job_id)[0] == "completed"
    state = db.get_blob_strict(uid, "capture_state")
    assert state is None or int(state.get("capture_fail_streak") or 0) == 0


@pytest.mark.parametrize(
    "case,gate",
    [
        ("off", lambda _uid: False),
        (
            "error",
            lambda _uid: (_ for _ in ()).throw(RuntimeError("db down")),
        ),
    ],
)
def test_run_turn_capture_preflight_skips_provider_setup_and_chat_error(case, gate):
    uid = f"u_x_preflight_{case}"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    calls = []

    def _forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("provider/enclave setup must not run")

    deps = _deps(
        capture_enabled=gate,
        resolve_provider=_forbidden,
        mint_enclave_token=_forbidden,
        record_terminal_error=lambda *_args: calls.append("error-chip"),
    )
    assert asyncio.run(worker._run_turn(job, deps)) == "failed"
    assert calls == []


def test_capture_opt_out_after_initial_gate_prevents_provider_call(monkeypatch):
    uid = "u_x_disable_before_provider"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    entered_context = threading.Event()
    release_context = threading.Event()
    provider_calls = []

    def _blocked_context(_uid):
        entered_context.set()
        assert release_context.wait(timeout=2.0)
        return {}

    async def _provider(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider)
    deps = _deps(read_memory_context=_blocked_context)

    async def _scenario():
        task = asyncio.create_task(
            worker.process_job(
                job,
                deps,
                provider_config=_BYOK,
                api_key=None,
                runtime_token="rt",
            )
        )
        assert await asyncio.to_thread(entered_context.wait, 2.0)
        await asyncio.to_thread(
            core_store.UserStore(uid).save_proactive_settings,
            {"capture_enabled": False},
        )
        release_context.set()
        return await task

    assert asyncio.run(_scenario()) == "failed"
    assert provider_calls == []
    assert db.get_blob_strict(uid, "capture_state")["capture_fail_streak"] == 0


def test_capture_live_halt_after_context_read_prevents_provider_call(monkeypatch):
    uid = "u_x_halt_before_provider"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    entered_context = threading.Event()
    release_context = threading.Event()
    halted = threading.Event()
    provider_calls = []

    def _blocked_context(_uid):
        entered_context.set()
        assert release_context.wait(timeout=2.0)
        return {}

    async def _provider(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider)
    monkeypatch.setattr(
        worker.kill_switch,
        "turns_halted_uncached",
        lambda **_kwargs: halted.is_set(),
    )
    deps = _deps(read_memory_context=_blocked_context)

    async def _scenario():
        task = asyncio.create_task(
            worker.process_job(
                job,
                deps,
                provider_config=_BYOK,
                api_key=None,
                runtime_token="rt",
            )
        )
        assert await asyncio.to_thread(entered_context.wait, 2.0)
        halted.set()
        release_context.set()
        return await task

    assert asyncio.run(_scenario()) == "failed"
    assert provider_calls == []
    status, last_error = _job_row(job["id"])
    assert (status, last_error) == ("failed", "turns_halted")
    state = db.get_blob_strict(uid, "capture_state") or {}
    assert int(state.get("capture_fail_streak") or 0) == 0
