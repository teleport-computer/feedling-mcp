"""worker：Task 8 全流程集成 —— process_job（coalesce→planner→executor→(replan)→responder）
+ _run_turn（单次 BYOK 解密 + is_official/mint_enclave_token 装配）+ run_worker_loop（claim
循环、优雅退出、故障隔离）。

风格：真实 jobs_store（真 DB：claim/mark_*/status events/runtime_state 都落真表，断言真读
回）+ 真实 core_store（真 DB chat/reload）+ 真实 v2.coalesce/v2.planner（is_official=False
走确定性零 LLM 的 rule_plan）/v2.executor（真实读写分派、真实 status 事件）+ 真实
v2.invalidation（决策路径未被打桩的测试里）；只在两个必须的边界打桩：
cap_registry.run_capability（capability 自己的正确性有专门测试文件覆盖，这里只喂假 data）
和 v2_responder.respond（LLM 出口，也有专门测试文件覆盖）。TurnDeps.read_messages 同样打桩
——enclave 解密在单测环境不可用——喂给真实的 `worker._coalesce_inputs`/`coalesce_pending`。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import compaction as v2_compaction
from model_api_runtime.v2 import invalidation as v2_inval
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import planner as v2_planner
from model_api_runtime.v2 import responder as v2_responder
from model_api_runtime.v2 import worker


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """claim_next_job() is a GLOBAL work-queue claim (by design it doesn't filter
    by user_id — see jobs_store.claim_next_job docstring). A pending job left
    behind by another test module (e.g. test_v2_jobs_store.py, which runs
    alphabetically before this file in a full suite) would otherwise get
    claimed here instead of the row a given test just enqueued. Truncate
    before each test so claim_next_job only ever sees this test's own row —
    mirrors the identical fixture in test_v2_jobs_store.py."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))


_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")


class _FakeCapResult:
    def __init__(self, data=None, ok=True):
        self._data = data or {}
        self._ok = ok

    def to_dict(self):
        return {"ok": self._ok, "data": self._data, "error": None, "trace": {}, "warnings": []}


def _patch_cheap_boundaries(monkeypatch, *, reply="model reply", memory_index=None):
    """Stub the two things a unit test genuinely can't run for real: capability
    dispatch (no enclave/DB-backed memory in this test process) and the LLM
    responder call. `reply` may be an Exception instance to make v2_responder.respond
    raise it instead of returning text (drives the ResponderError/no-filler test).

    v2_responder.respond is now natively async (worker awaits it directly, no
    asyncio.to_thread bridge — see provider_client.reliable_chat_completion_async's
    module docstring), so both stub branches must themselves be awaitable."""
    monkeypatch.setattr(
        cap_registry, "run_capability",
        lambda action_type, store, **k: _FakeCapResult(
            memory_index if action_type == "memory_index" else {}))
    if isinstance(reply, BaseException):
        def _raise(*a, **k):
            raise reply
        monkeypatch.setattr(v2_responder, "respond", _raise)
    else:
        async def _return(*a, **k):
            return reply
        monkeypatch.setattr(v2_responder, "respond", _return)


def _deps(*, messages, provider=None, is_official=False, token="rt-enclave"):
    provider = provider if provider is not None else (_BYOK, {})
    return worker.TurnDeps(
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: provider,
        is_official=lambda cfg: is_official,
        mint_enclave_token=lambda uid: token,
    )


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


class _CountingSemaphore(asyncio.Semaphore):
    """A real asyncio.Semaphore that also counts acquisitions, so a test can
    assert *which* code paths went through the shared enclave gate (spec §11
    R3) without having to mock away asyncio's own synchronization primitive."""

    def __init__(self, value=2):
        super().__init__(value)
        self.acquire_count = 0

    async def acquire(self):
        self.acquire_count += 1
        return await super().acquire()


# ------------------------------------------------------------------
# process_job: the full turn body
# ------------------------------------------------------------------

def test_process_job_end_to_end_writes_reply_and_completes(monkeypatch):
    """Happy path (spec §13 steps 5-8): a pending user message -> real coalesce ->
    real rule_plan (is_official=False) -> real executor (control-only plan, no
    capability calls) -> stubbed responder -> encrypted reply written -> job
    completed -> action_digest folded into runtime_state."""
    uid = "u_w_happy"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="MODEL REPLY")
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}])
    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text, user_id=store.user_id) or {"id": "r1"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert written == {"text": "MODEL REPLY", "user_id": uid}
    row = _job_status(job_id)
    assert row[0] == "completed"
    state = jobs_store.get_runtime_state(uid)
    assert state.get("last_replied_ts") == 10.0
    assert "action_digest" in state  # non-sensitive digest only; no capability data leaked here


def test_process_job_acquires_enclave_semaphore_for_read_messages_and_prefetch(monkeypatch):
    """FIX 2 (spec §11 R3): the per-turn enclave_sem must bound EVERY enclave-bound
    call in a turn, not just provider-key decrypt (_run_turn) and executor
    capability calls (_run_one). Before this fix, _coalesce_inputs's call to
    deps.read_messages (per-message chat decrypt) and the two _cap_data prefetch
    calls (memory_index/perception_snapshot) ran unbounded -> N concurrent
    workers could hit the single-threaded enclave without ever passing through
    the shared gate. Uses a real (counting) Semaphore, not a mock, so the
    assertion exercises actual async acquire/release semantics."""
    uid = "u_w_semaphore"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="R")
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    read_messages_calls = {"n": 0}

    def _read_messages(uid_):
        read_messages_calls["n"] += 1
        return [{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}]

    deps = worker.TurnDeps(
        read_messages=_read_messages,
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
    )
    sem = _CountingSemaphore(2)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt",
        enclave_sem=sem,
    ))

    assert status == "completed"
    assert read_messages_calls["n"] == 1
    # 1 acquisition for read_messages (_coalesce_inputs) + 1 for memory_index
    # prefetch + 1 for perception_snapshot prefetch, at minimum (a replan would
    # add more, but the happy path here takes none).
    assert sem.acquire_count >= 3


def test_coalesce_inputs_and_cap_data_tolerate_enclave_sem_none(monkeypatch):
    """Direct unit coverage of the `enclave_sem is None` guard added to the two
    newly-wrapped helpers (mirrors executor._run_one's tolerance): calling them
    with no semaphore at all — not even process_job's ENCLAVE_SEMAPHORE default
    substitution — must not raise."""
    uid = "u_w_semaphore_none"
    conftest.seed_user(uid)
    store = core_store.get_store(uid)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
    )
    coalesced, cursor = asyncio.run(worker._coalesce_inputs(deps, uid, 0.0, enclave_sem=None))
    assert cursor == 1.0
    assert coalesced and coalesced[0]["content"] == "hi"

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({"x": 1}))
    data = asyncio.run(worker._cap_data(
        store, "memory_index", api_key=None, runtime_token="rt", enclave_sem=None))
    assert data == {"x": 1}


def test_process_job_sleep_plan_no_responder_no_bubble(monkeypatch):
    """A plan without final_response (sleep — e.g. a wake with nothing visible to
    react to) must never call the responder and must never write a reply bubble,
    yet still cleanly completes the job.

    Lane: "capture" (not "heartbeat") — D3 Task 6 gave heartbeat/scheduled/
    manual_wake their own dedicated `_run_wake` dispatch in `process_job`
    (before this generic chat-turn/rule_plan path is ever reached); capture is
    explicitly deferred/out of scope for that task (different semantics —
    memory extraction, not a reply) and still falls through to this generic
    path, so it's the lane that continues to exercise rule_plan's sleep-plan
    degradation exactly as this test intends."""
    uid = "u_w_sleep"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    responder_called = {"n": 0}
    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    monkeypatch.setattr(
        v2_responder, "respond",
        lambda *a, **k: responder_called.update(n=responder_called["n"] + 1) or "SHOULD NOT BE CALLED")
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    # capture lane, no coalesced messages -> has_user_text=False and lane != "chat"
    # -> real rule_plan degrades to [{"type": "sleep", ...}] (no final_response).
    deps = _deps(messages=[])
    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert responder_called["n"] == 0
    assert write_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"


def test_process_job_responder_error_marks_failed_no_filler(monkeypatch):
    """ResponderError (empty model reply / provider failure) must mark the job
    failed and must NEVER write a placeholder bubble — the no-filler invariant."""
    uid = "u_w_resperr"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply=v2_responder.ResponderError("empty_reply"))
    deps = _deps(messages=[{"id": "m1", "ts": 5.0, "role": "user", "content": "hi"}])
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert write_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert "ResponderError" in (row[1] or "") and "empty_reply" in (row[1] or "")


def test_process_job_no_pending_messages_chat_lane_completes_without_planning(monkeypatch):
    """A chat-lane job that finds no coalesced pending messages (already answered
    by a racing job) must complete cleanly without ever invoking the planner —
    no wasted LLM call, no filler."""
    uid = "u_w_nopending"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    plan_calls = {"n": 0}

    async def _counting_empty_plan(*a, **k):
        plan_calls["n"] += 1
        return []

    monkeypatch.setattr(v2_planner, "plan", _counting_empty_plan)
    deps = _deps(messages=[])  # nothing pending -> coalesce_pending returns ([], 0.0)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert plan_calls["n"] == 0
    assert _job_status(job_id)[0] == "completed"


def test_process_job_replans_on_concurrent_new_message_within_budget(monkeypatch):
    """The safe-point invalidation state machine (spec §8): a concurrent new
    message arriving mid-turn triggers exactly one replan (within the default
    budget of 2), then a second safe-point check with no further new messages
    finishes normally. invalidation.evaluate's OWN new-message detection logic
    is unit-tested exhaustively in test_v2_invalidation.py — here we stub the
    safe-point DECISION to isolate what Task 8 actually wires: does process_job's
    loop correctly react to REPLAN/CONTINUE (re-coalesce, call invalidate(),
    bounded re-planning) end to end, with the real coalesce/planner/executor
    otherwise running."""
    uid = "u_w_replan"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="R")
    plan_calls = {"n": 0}
    orig_plan = v2_planner.plan

    def _counting_plan(*a, **k):
        plan_calls["n"] += 1
        return orig_plan(*a, **k)

    monkeypatch.setattr(v2_planner, "plan", _counting_plan)

    # First coalesce sees only m1; after the (stubbed) executor "runs", a safe-point
    # check discovers m2 arrived concurrently -> replan -> re-coalesce sees both.
    feed = iter([
        [{"id": "m1", "ts": 10.0, "role": "user", "content": "first"}],
        [{"id": "m1", "ts": 10.0, "role": "user", "content": "first"},
         {"id": "m2", "ts": 20.0, "role": "user", "content": "second"}],
    ])
    deps = worker.TurnDeps(
        read_messages=lambda uid: next(feed),
        resolve_provider=lambda uid: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid: "rt",
    )

    decisions = iter([v2_inval.REPLAN, v2_inval.CONTINUE])
    monkeypatch.setattr(v2_inval, "evaluate", lambda *a, **k: next(decisions))
    invalidate_calls = []
    monkeypatch.setattr(
        v2_inval, "invalidate",
        lambda job_id, *, replan_job_id: invalidate_calls.append((job_id, replan_job_id)) or 0)

    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text) or {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert plan_calls["n"] == 2          # planned once, replanned exactly once
    assert invalidate_calls == [(job_id, job_id)]  # within-job replan: replan_job_id == job_id
    assert written["text"] == "R"
    assert _job_status(job_id)[0] == "completed"
    # last_replied_ts should reflect the SECOND (post-replan) coalesce cursor (20.0),
    # not the first (10.0) — the replanned turn answered both messages.
    assert jobs_store.get_runtime_state(uid).get("last_replied_ts") == 20.0


def _status_events(uid):
    return jobs_store.list_status_events(uid, after_id=0, limit=100)


def test_process_job_terminal_failure_emits_error_status_and_calls_callback(monkeypatch):
    """Task 3: a terminally-failed turn must surface, not just write invisible
    agent_jobs.last_error — an "error"-kind status event goes on the stream
    (iOS's poll surface) AND the injected TurnDeps.record_terminal_error
    callback fires with (user_id, message), so serve_worker can also patch
    hosted's last_runtime_error. No chat reply must ever be written on failure
    (no-filler)."""
    uid = "u_w_terminalerr"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="should not matter")
    monkeypatch.setattr(v2_planner, "plan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("planner blew up")))
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    recorded = []
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 5.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert write_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"

    events = _status_events(uid)
    error_events = [e for e in events if e["kind"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["job_id"] == job_id

    assert len(recorded) == 1
    rec_uid, rec_msg = recorded[0]
    assert rec_uid == uid
    assert "RuntimeError" in rec_msg and "planner blew up" in rec_msg


def test_process_job_terminal_failure_tolerates_missing_callback(monkeypatch):
    """record_terminal_error defaults to None (dependency boundary preserved for
    callers that don't supply it) — the failure path must not crash when it's
    absent, and must still emit the error status event."""
    uid = "u_w_terminalerr_nocb"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="x")
    monkeypatch.setattr(v2_planner, "plan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    deps = _deps(messages=[{"id": "m1", "ts": 5.0, "role": "user", "content": "hi"}])
    assert deps.record_terminal_error is None

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "failed"
    events = _status_events(uid)
    assert any(e["kind"] == "error" for e in events)


def test_run_turn_provider_resolve_failure_emits_error_status_and_callback(monkeypatch):
    """The early (pre-process_job) provider-resolve failure path in _run_turn is
    the SECOND terminal-failure site — it must surface the same way, using
    user_id only (no `store` binding is available there)."""
    uid = "u_w_terminalerr_early"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    def _boom(*a, **k):
        raise AssertionError("must not run past provider resolution failure")

    recorded = []
    deps = worker.TurnDeps(
        read_messages=_boom,
        resolve_provider=lambda uid_: (None, {"error": "model_api_key_decrypt_failed"}),
        is_official=_boom,
        mint_enclave_token=_boom,
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
    )
    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "failed"
    events = _status_events(uid)
    error_events = [e for e in events if e["kind"] == "error"]
    assert len(error_events) == 1
    assert len(recorded) == 1
    rec_uid, rec_msg = recorded[0]
    assert rec_uid == uid
    assert "model_api_key_decrypt_failed" in rec_msg


def test_run_turn_maintenance_resolve_failure_is_silent_no_user_error(monkeypatch):
    """A maintenance-lane (compaction) job whose provider can't be resolved must
    fail SILENTLY — no user-facing error chip. The provider-resolve failure lives
    in _run_turn, one level above the lane fork in process_job; without a lane
    gate it would inherit the chat lane's _surface_terminal_error and pop a
    spurious error chip for a background job the user never triggered."""
    uid = "u_w_maint_resolve_fail"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance", reason="compaction")
    job = jobs_store.claim_next_job("w")

    def _boom(*a, **k):
        raise AssertionError("must not run past provider resolution failure")

    recorded = []
    deps = worker.TurnDeps(
        read_messages=_boom,
        resolve_provider=lambda uid_: (None, {"error": "model_api_key_decrypt_failed"}),
        is_official=_boom,
        mint_enclave_token=_boom,
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
    )
    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "failed"
    # SILENT: no "error"-kind status event, no record_terminal_error callback.
    assert [e for e in _status_events(uid) if e["kind"] == "error"] == []
    assert recorded == []


def test_run_turn_heartbeat_resolve_failure_is_silent_no_user_error(monkeypatch):
    """D3 Task 6: the same silence extends to wake lanes (heartbeat/scheduled/
    manual_wake) — a heartbeat job whose provider can't be resolved must fail
    SILENTLY, mirroring test_run_turn_maintenance_resolve_failure_is_silent_no_user_error
    above. The _run_turn gate change (`lane == "chat"`, was `lane != "maintenance"`)
    means only the chat lane still surfaces a user-visible error chip."""
    uid = "u_w_heartbeat_resolve_fail"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    def _boom(*a, **k):
        raise AssertionError("must not run past provider resolution failure")

    recorded = []
    deps = worker.TurnDeps(
        read_messages=_boom,
        resolve_provider=lambda uid_: (None, {"error": "model_api_key_decrypt_failed"}),
        is_official=_boom,
        mint_enclave_token=_boom,
        record_terminal_error=lambda user_id, message: recorded.append((user_id, message)),
    )
    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "failed"
    # SILENT: no "error"-kind status event, no record_terminal_error callback.
    assert [e for e in _status_events(uid) if e["kind"] == "error"] == []
    assert recorded == []


# ------------------------------------------------------------------
# _run_turn: single BYOK decrypt/turn + is_official/mint wiring
# ------------------------------------------------------------------

def test_run_turn_resolves_provider_exactly_once_even_across_a_replan(monkeypatch):
    """Single-decrypt-per-turn invariant: resolve_provider (the BYOK decrypt) must
    be called exactly once per turn, even when the turn internally replans."""
    uid = "u_w_singledecrypt"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="R")
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    async def _final_response_plan(*a, **k):
        return [{"type": "final_response", "payload": {}}]

    monkeypatch.setattr(v2_planner, "plan", _final_response_plan)

    feed = iter([
        [{"id": "m1", "ts": 1.0, "role": "user", "content": "a"}],
        [{"id": "m1", "ts": 1.0, "role": "user", "content": "a"},
         {"id": "m2", "ts": 2.0, "role": "user", "content": "b"}],
    ])
    resolve_calls = {"n": 0}

    def _resolve(uid_):
        resolve_calls["n"] += 1
        return _BYOK, {}

    deps = worker.TurnDeps(
        read_messages=lambda uid: next(feed),
        resolve_provider=_resolve,
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid: "rt",
    )
    decisions = iter([v2_inval.REPLAN, v2_inval.CONTINUE])
    monkeypatch.setattr(v2_inval, "evaluate", lambda *a, **k: next(decisions))
    monkeypatch.setattr(v2_inval, "invalidate", lambda job_id, *, replan_job_id: 0)

    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "completed"
    assert resolve_calls["n"] == 1


def test_run_turn_fails_when_provider_unresolved_and_never_enters_process_job(monkeypatch):
    """resolve_provider returning (None, {"error": ...}) must mark the job failed
    and never touch read_messages/planner/responder (no wasted work, no filler)."""
    uid = "u_w_noprovider"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    def _boom(*a, **k):
        raise AssertionError("process_job must not run when provider resolution fails")

    deps = worker.TurnDeps(
        read_messages=_boom,
        resolve_provider=lambda uid: (None, {"error": "model_api_key_decrypt_failed"}),
        is_official=_boom,
        mint_enclave_token=_boom,
    )
    status = asyncio.run(worker._run_turn(job, deps))

    assert status == "failed"
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert "model_api_key_decrypt_failed" in (row[1] or "")


# ------------------------------------------------------------------
# run_worker_loop: claim loop, graceful drain, per-slot fault isolation
# ------------------------------------------------------------------

def _ok_deps(rec, *, messages=None):
    if messages is None:
        messages = [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    return worker.TurnDeps(
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid: "rt",
    )


def _patch_loop_boundaries(monkeypatch, rec, *, reply="model reply"):
    _patch_cheap_boundaries(monkeypatch, reply=reply)
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: rec.setdefault("replies", []).append((store.user_id, text)) or {"id": "r1"})


def test_run_worker_loop_drains_pending_then_stops(monkeypatch):
    uid = "u_w_4"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    rec = {}
    _patch_loop_boundaries(monkeypatch, rec)
    stop = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(worker.run_worker_loop(
            "w-loop", max_workers=1, poll_interval=0.02, stop_event=stop, deps=_ok_deps(rec),
        ))
        for _ in range(200):
            with db.get_pool().connection() as conn:
                st = conn.execute(
                    "SELECT status FROM agent_jobs WHERE user_id=%s", (uid,)
                ).fetchone()
            if st and st[0] == "completed":
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_driver())
    assert rec.get("replies") == [(uid, "model reply")]


def test_run_worker_loop_survives_transient_claim_error(monkeypatch):
    """Robustness fix: a transient exception raised inside a slot's per-iteration
    work (claim_next_job here, standing in for any DB hiccup around claim/
    mark_running) must not propagate out of _slot_loop and crash run_worker_loop.
    The slot logs it and continues; the very next poll re-claims and completes
    the job normally."""
    uid = "u_w_5"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    rec = {}
    _patch_loop_boundaries(monkeypatch, rec)
    stop = asyncio.Event()
    calls = {"n": 0}
    orig_claim = jobs_store.claim_next_job

    def _flaky_claim(worker_id, lanes=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db error")
        return orig_claim(worker_id, lanes=lanes)

    monkeypatch.setattr(jobs_store, "claim_next_job", _flaky_claim)

    async def _driver():
        task = asyncio.create_task(worker.run_worker_loop(
            "w-loop2", max_workers=1, poll_interval=0.02, stop_event=stop, deps=_ok_deps(rec),
        ))
        for _ in range(300):
            with db.get_pool().connection() as conn:
                st = conn.execute(
                    "SELECT status FROM agent_jobs WHERE user_id=%s", (uid,)
                ).fetchone()
            if st and st[0] == "completed":
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(_driver())
    assert calls["n"] >= 2  # first raised, a later call actually claimed
    assert rec.get("replies") == [(uid, "model reply")]


def test_slot_exception_path_backs_off_on_persistent_failure(monkeypatch):
    """Verify that when claim_next_job persistently fails (e.g., DB outage),
    the exception handler waits poll_interval before retrying, rather than
    hot-looping and flooding logs/connection pool."""
    rec = {}
    stop = asyncio.Event()
    calls = {"n": 0}

    def _always_fail(worker_id, lanes=None):
        calls["n"] += 1
        raise RuntimeError("persistent db outage")

    monkeypatch.setattr(jobs_store, "claim_next_job", _always_fail)

    async def _driver():
        task = asyncio.create_task(worker.run_worker_loop(
            "w-backoff", max_workers=1, poll_interval=0.05, stop_event=stop, deps=_ok_deps(rec),
        ))
        # Let it run for ~0.1s (enough for 2-3 poll_interval cycles if backing off,
        # but would be ~20+ attempts if hot-looping).
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_driver())
    # With backoff: ~2-3 attempts in 0.1s (0.05s poll_interval + overhead).
    # Without backoff: would be 20+ attempts.
    assert calls["n"] <= 4, f"Too many attempts ({calls['n']}) suggests hot-loop without backoff"


def test_bounded_gates_exist():
    assert isinstance(worker.MAX_WORKERS, int) and worker.MAX_WORKERS >= 1
    assert isinstance(worker.MAX_READ_ACTION_PARALLELISM, int)
    assert isinstance(worker.ENCLAVE_SEMAPHORE, asyncio.Semaphore)


# ------------------------------------------------------------------
# D3 Task 5: lane reservation wiring — _reserved_lane_slots() picks the
# per-slot lane allowlist that run_worker_loop hands to each _slot_loop.
# ------------------------------------------------------------------

def test_reserved_lane_slots_explicit_reserved_count():
    assert worker._reserved_lane_slots(4, 2) == [
        {"chat", "manual_wake"}, {"chat", "manual_wake"}, None, None,
    ]


def test_reserved_lane_slots_default_is_half_rounded_down():
    result = worker._reserved_lane_slots(4, None)
    assert result == [{"chat", "manual_wake"}, {"chat", "manual_wake"}, None, None]


def test_reserved_lane_slots_single_worker_defaults_to_fully_reserved():
    assert worker._reserved_lane_slots(1, None) == [{"chat", "manual_wake"}]


def test_reserved_lane_slots_reserved_clamped_to_max_workers():
    assert worker._reserved_lane_slots(3, 99) == [
        {"chat", "manual_wake"}, {"chat", "manual_wake"}, {"chat", "manual_wake"},
    ]


def test_reserved_lane_slots_reserved_zero_means_all_unrestricted():
    assert worker._reserved_lane_slots(3, 0) == [None, None, None]


# ------------------------------------------------------------------
# Task 7: worker dispatch by lane — chat reads summary+tail via deps and
# enqueues a maintenance compaction job when over budget; maintenance lane
# runs the self-contained compaction path.
# ------------------------------------------------------------------

def test_process_job_reply_uses_deps_summary_and_tail_and_enqueues_compaction_when_over_budget(monkeypatch):
    """D1 responder call site: the reply must be produced from `deps.read_summary`/
    `deps.read_tail` (not the coalesced/runtime_state shape the old call site used).
    When the tail returned by `deps.read_tail` is over `_TAIL_BUDGET`, the turn must
    best-effort enqueue a maintenance-lane compaction job — without blocking or
    failing the reply that was already written."""
    uid = "u_w_compact_enqueue"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    big_tail = [
        {"id": f"m{i}", "ts": float(i), "role": "user" if i % 2 == 0 else "openclaw", "content": f"msg {i}"}
        for i in range(worker._TAIL_BUDGET + 5)
    ]
    respond_calls = []

    async def _spy_respond(*, provider_config, summary, tail, action_results=None, usage_out=None):
        respond_calls.append({"summary": summary, "tail": tail})
        return "REPLY"

    monkeypatch.setattr(v2_responder, "respond", _spy_respond)

    enqueue_calls = []
    orig_enqueue = jobs_store.enqueue_job

    def _spy_enqueue(user_id_, lane, *, reason=None, **k):
        enqueue_calls.append((user_id_, lane, reason))
        return orig_enqueue(user_id_, lane, reason=reason, **k)

    monkeypatch.setattr(jobs_store, "enqueue_job", _spy_enqueue)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: big_tail,
        read_summary=lambda uid_: ("prior summary", 0.0, 3),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(respond_calls) == 1
    assert respond_calls[0]["summary"] == "prior summary"
    assert respond_calls[0]["tail"] == big_tail
    assert (uid, "maintenance", "compaction") in enqueue_calls


def test_process_job_reply_skips_compaction_enqueue_when_under_budget(monkeypatch):
    """Symmetric negative case: a short tail (under `_TAIL_BUDGET`) must NOT
    trigger a maintenance enqueue."""
    uid = "u_w_compact_skip"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    async def _spy_respond(*, provider_config, summary, tail, action_results=None, usage_out=None):
        return "REPLY"

    monkeypatch.setattr(v2_responder, "respond", _spy_respond)

    enqueue_calls = []
    orig_enqueue = jobs_store.enqueue_job

    def _spy_enqueue(user_id_, lane, *, reason=None, **k):
        enqueue_calls.append((user_id_, lane, reason))
        return orig_enqueue(user_id_, lane, reason=reason, **k)

    monkeypatch.setattr(jobs_store, "enqueue_job", _spy_enqueue)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        read_summary=lambda uid_: ("", 0.0, 0),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert enqueue_calls == []


def test_run_compaction_folds_oldest_batch_and_advances_watermark(monkeypatch):
    """maintenance lane, happy path: the oldest batch (everything but the last
    `_TAIL_KEEP` messages) is folded via `v2_compaction.compact` and CAS-written
    with `expected_version` from `read_summary` and a watermark equal to the
    folded batch's last ts. No chat bubble is ever written and no user-facing
    error surface fires for a background compaction job."""
    uid = "u_w_maintenance"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance")
    job = jobs_store.claim_next_job("w")

    tail = [
        {"id": f"m{i}", "ts": float(i), "role": "user", "content": f"msg {i}"}
        for i in range(worker._TAIL_KEEP + 6)
    ]

    async def _fake_compact(*, provider_config, current_summary, old_messages, llm):
        return current_summary + "\n- new"

    monkeypatch.setattr(v2_compaction, "compact", _fake_compact)

    write_calls = []

    def _write_summary(uid_, summary, watermark_ts, expected_version):
        write_calls.append((uid_, summary, watermark_ts, expected_version))
        return True

    write_bubble_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_bubble_called.update(n=write_bubble_called["n"] + 1) or {"id": "r"})

    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: tail,
        read_summary=lambda uid_: ("- old", 0.0, 2),
        write_summary=_write_summary,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert write_bubble_called["n"] == 0
    assert surface_called["n"] == 0
    assert len(write_calls) == 1
    _wuid, wsummary, wwatermark, wversion = write_calls[0]
    assert wversion == 2  # expected_version came straight from read_summary's version
    old_count = len(tail) - worker._TAIL_KEEP
    assert wwatermark == tail[old_count - 1]["ts"]
    assert wsummary == "- old\n- new"
    assert _job_status(job_id)[0] == "completed"


def test_run_compaction_failure_marks_failed_without_surfacing_user_error(monkeypatch):
    """A background compaction failure (e.g. summary decrypt blows up) must be a
    silent `mark_failed` — NEVER a chat bubble, NEVER an "error"-kind status
    event, NEVER a `_surface_terminal_error` call. That machinery is for
    user-initiated chat turns; maintenance is a background job."""
    uid = "u_w_maintenance_fail"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance")
    job = jobs_store.claim_next_job("w")

    def _boom_read_summary(uid_):
        raise RuntimeError("summary decrypt exploded")

    write_bubble_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_bubble_called.update(n=write_bubble_called["n"] + 1) or {"id": "r"})

    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: [],
        read_summary=_boom_read_summary,
        write_summary=lambda *a, **k: True,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert write_bubble_called["n"] == 0
    assert surface_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert "compaction_failed" in (row[1] or "")
    events = _status_events(uid)
    assert not any(e["kind"] == "error" for e in events)


# ------------------------------------------------------------------
# Task 4: TurnDeps.record_turn_metric — worker records per-turn provider token
# usage + latency after a SUCCESSFUL respond() call (chat lane only; the
# metric records at the worker call site, not inside responder, to keep
# responder.respond hosted-free/job-context-free — see D0 rollout plan Task 4
# note and responder.py's usage_out docstring).
# ------------------------------------------------------------------

def test_process_job_records_turn_metric_after_successful_respond(monkeypatch):
    uid = "u_w_turnmetric"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    async def _spy_respond(*, provider_config, summary, tail, action_results=None, usage_out=None):
        if usage_out is not None:
            usage_out["prompt_tokens"] = 42
            usage_out["completion_tokens"] = 8
        return "REPLY"

    monkeypatch.setattr(v2_responder, "respond", _spy_respond)

    metric_calls = []
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        record_turn_metric=lambda **kwargs: metric_calls.append(kwargs),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(metric_calls) == 1
    call = metric_calls[0]
    assert call["job_id"] == job_id
    assert call["user_id"] == uid
    assert call["lane"] == "chat"
    assert call["prompt_tokens"] == 42
    assert call["completion_tokens"] == 8
    assert isinstance(call["latency_ms"], int) and call["latency_ms"] >= 0


def test_process_job_does_not_record_turn_metric_on_responder_error(monkeypatch):
    """No-filler-adjacent invariant for metrics: a failed turn (ResponderError)
    never happened as a "turn" from D4's perspective — only successful respond()
    calls get metered."""
    uid = "u_w_turnmetric_err"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult({}))

    async def _raise_respond(*, provider_config, summary, tail, action_results=None, usage_out=None):
        raise v2_responder.ResponderError("empty_reply")

    monkeypatch.setattr(v2_responder, "respond", _raise_respond)

    metric_calls = []
    deps = worker.TurnDeps(
        read_messages=lambda uid_: [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        record_turn_metric=lambda **kwargs: metric_calls.append(kwargs),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert metric_calls == []


def test_process_job_tolerates_missing_record_turn_metric_callback(monkeypatch):
    """record_turn_metric defaults to None like the other optional TurnDeps
    callbacks — the happy path must not crash when it's absent."""
    uid = "u_w_turnmetric_none"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="R")
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})
    deps = _deps(messages=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    assert deps.record_turn_metric is None

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"


def test_run_compaction_under_keep_threshold_completes_as_noop(monkeypatch):
    """A tail no longer than `_TAIL_KEEP` has nothing worth folding yet — the
    maintenance job should cleanly complete without calling compact/write_summary."""
    uid = "u_w_maintenance_noop"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance")
    job = jobs_store.claim_next_job("w")

    compact_calls = {"n": 0}

    async def _counting_compact(*a, **k):
        compact_calls["n"] += 1
        return "should not be called"

    monkeypatch.setattr(v2_compaction, "compact", _counting_compact)
    write_calls = {"n": 0}

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: [
            {"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        read_summary=lambda uid_: ("", 0.0, 0),
        write_summary=lambda *a, **k: write_calls.update(n=write_calls["n"] + 1) or True,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert compact_calls["n"] == 0
    assert write_calls["n"] == 0
    assert _job_status(job_id)[0] == "completed"
