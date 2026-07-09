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
    """A plan without final_response (sleep — e.g. a heartbeat wake with nothing
    visible to react to) must never call the responder and must never write a
    reply bubble, yet still cleanly completes the job."""
    uid = "u_w_sleep"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
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

    # heartbeat lane, no coalesced messages -> has_user_text=False and lane != "chat"
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

    def _flaky_claim(worker_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db error")
        return orig_claim(worker_id)

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

    def _always_fail(worker_id):
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
