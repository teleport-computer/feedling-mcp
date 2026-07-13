"""D3 Task 6 (wake-lane processing: heartbeat/scheduled/manual_wake) + PR C Task 8
(spec C8: the wake lane migrated onto the unified `tool_loop.run_tool_loop`,
replacing the old wake-only `v2_responder.respond` call and its
`ResponderError("empty_reply"/"no_user_messages")` branches).

`_run_wake` mirrors `_run_compaction`'s self-contained shape (own try/except,
silent `mark_failed`, never `_surface_terminal_error`, never a chat bubble on
failure) but on a SUCCESSFUL model-authored reply it DOES write an encrypted
chat bubble via the PR A effect outbox (`on_reply` -> `enqueue_effect` ->
`apply_pending_effects`, same mechanism the chat lane uses) — the whole point
of a wake lane is letting the companion reach out proactively.

"Weak wake sleeps": an empty terminal reply (the model chose to stay silent)
is NOT a failure — it's `mark_completed` with zero bubbles. This is the
OPPOSITE of the chat lane's no-filler rule. There is no longer a distinct
"no_user_messages" failure mode: `_run_wake` always seeds a fixed
`_WAKE_NUDGE` user-role turn, so `build_turn_messages` never produces a
tail with zero non-system turns — a wake turn is valid even with a
completely empty coalesce/read_tail.

A real provider failure (`provider_client.chat_completion_async` raising) IS
a failure — silent `mark_failed`, still no user-visible error chip
(background job, same isolation as maintenance).

Style: real jobs_store/core_store (real DB claim/mark_*/status events) +
stubbed `provider_client.chat_completion_async` (the LLM wire boundary
`tool_loop.run_tool_loop` calls once per round) + stubbed
`worker._write_encrypted_reply` (spy, no real envelope/enclave round-trip in
a unit test) reached through a real PR A effect-outbox drain wired via
`TurnDeps.apply_pending_effects`."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from core import store as core_store
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import planner as v2_planner
from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import worker


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """Same rationale as test_v2_worker.py's identical fixture: claim_next_job()
    is a global work-queue claim with no user_id filter, so a pending row left
    behind by another test module would otherwise get claimed here instead of
    this test's own row."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))


_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


def _status_events(uid):
    return jobs_store.list_status_events(uid, after_id=0, limit=100)


def _claim(job_id: int) -> str:
    job = jobs_store.claim_next_job("wake-test")
    assert job is not None and job["id"] == job_id
    return str(job["claimed_by"])


def _reply_effect_dispatch(user_id):
    """Test-local production-shaped sink for the wake lane's `reply` effect_type
    (mirrors `serve_worker._sink_reply`'s real write, `worker._write_encrypted_
    reply`, without pulling in serve_worker's hosted-adjacent wiring — same
    pattern test_v2_worker.py/test_v2_worker_tool_loop.py use for the chat
    lane)."""
    def dispatch(effect_type, payload):
        if effect_type == "reply":
            worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
    return dispatch


def _apply_effects(user_id):
    return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_reply_effect_dispatch(user_id))


def _wake_deps(*, summary="", tail=None):
    return worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after_ts, limit: list(tail if tail is not None else []),
        read_summary=lambda uid: (summary, 0.0, 0),
        apply_pending_effects=_apply_effects,
    )


def _script_provider(monkeypatch, responses):
    """Monkeypatch `provider_client.chat_completion_async` — what
    `tool_loop.run_tool_loop` calls once per round (the wake lane's LLM wire
    boundary since Task 8)."""
    it = iter(responses)
    calls = []

    async def _fake(config, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _text_round(text, *, prompt_tokens=1, completion_tokens=1):
    return {"reply": text, "tool_calls": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


# ------------------------------------------------------------------
# _run_wake direct unit coverage
# ------------------------------------------------------------------

def test_run_wake_reply_written_and_job_completed(monkeypatch):
    uid = "u_wake_reply"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    seen = {}

    async def _fake(config, messages, *, tools=None):
        seen["messages"] = messages
        return _text_round("hey, how did that go?")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text, user_id=store.user_id) or {"id": "r1"})

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "completed"
    assert written == {"text": "hey, how did that go?", "user_id": uid}
    assert _job_status(job_id)[0] == "completed"
    system_msg = next(m for m in seen["messages"] if m["role"] == "system")
    assert worker._WAKE_SYSTEM_PROMPT in system_msg["content"]


def test_run_wake_weak_wake_sleeps_no_bubble_no_error(monkeypatch):
    """Model declines to speak (empty terminal text) -> job completes cleanly,
    zero bubbles, and the D3 no-filler-adjacent invariant: no error
    status/callback. Unlike the chat lane, an empty terminal reply is NOT a
    failure for wake — it's a legitimate silence."""
    uid = "u_wake_weak"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    _script_provider(monkeypatch, [_text_round("")])
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "completed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"
    assert not any(e["kind"] == "error" for e in _status_events(uid))


def test_run_wake_empty_tail_also_sleeps_silently(monkeypatch):
    """A degenerate prompt (zero real tail rows, only the fixed `_WAKE_NUDGE`)
    must be treated identically to any other weak-wake silence — there is no
    longer a distinct "no_user_messages" failure mode (Task 8 removed that
    guard: `wake_tail` always has at least the nudge, so `build_turn_messages`
    never sees zero non-system turns for a wake lane)."""
    uid = "u_wake_nouser"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)

    seen = {}

    async def _fake(config, messages, *, tools=None):
        seen["messages"] = messages
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    deps = _wake_deps(tail=[])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "scheduled", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "completed"
    assert write_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"
    non_system = [m for m in seen["messages"] if m.get("role") != "system"]
    assert len(non_system) == 1  # just the nudge — no real tail rows


def test_run_wake_provider_error_silent_mark_failed(monkeypatch):
    """A real provider failure (BYOK 402, enclave hiccup, etc.) must NOT be
    confused with a weak-wake sleep — it's a real failure, silently marked,
    never surfaced to the user (background job, same isolation as
    maintenance)."""
    uid = "u_wake_provider_err"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "manual_wake", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "failed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert row[1] == "wake_failed:runtimeerror"
    assert not any(e["kind"] == "error" for e in _status_events(uid))


def test_run_wake_unexpected_exception_also_silent_mark_failed(monkeypatch):
    """Any other unexpected exception during the wake turn (e.g. read_tail
    blowing up) must be caught by _run_wake's own try/except, same as
    _run_compaction — never propagate, never surface a user error chip."""
    uid = "u_wake_boom"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    def _boom_read_tail(uid_, after_ts, limit):
        raise RuntimeError("tail read exploded")

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=_boom_read_tail,
        read_summary=lambda uid_: ("", 0.0, 0),
    )
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "failed"
    assert surface_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert "wake_failed" in (row[1] or "")


def test_run_wake_tolerates_missing_read_summary_read_tail(monkeypatch):
    """Mirrors _run_compaction's degrade-gracefully contract for deps without
    read_summary/read_tail wired (defaults None): falls back to empty summary/
    tail rather than crashing on a None call."""
    uid = "u_wake_nodeps"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    seen = {}

    async def _fake(config, messages, *, tools=None):
        seen["messages"] = messages
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        # read_tail/read_summary left at their TurnDeps default of None.
    )

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "completed"
    non_system = [m for m in seen["messages"] if m.get("role") != "system"]
    # tail should be just the wake nudge (no real tail entries to prepend).
    assert len(non_system) == 1
    assert non_system[0]["role"] == "user"


# ------------------------------------------------------------------
# D3 Task 7: a "provider_config"-kind failure (dead/broke BYOK key: 402/401/403
# — classified via provider_client.classify_provider_error, Task 8's
# replacement for the old ResponderError.kind mechanism) must write a
# payment_cooldown_until on the wake schedule BEFORE the silent mark_failed,
# so the scheduler's due_heartbeat_users stops re-firing wakes at a key that
# cannot succeed until the user fixes it. A "transient"-kind error must NOT
# set a cooldown — it's just a blip, not a config problem.
# ------------------------------------------------------------------

def test_run_wake_provider_config_error_sets_payment_cooldown(monkeypatch):
    uid = "u_wake_provider_config"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None):
        raise provider_client.ProviderError("out of credits", status_code=402)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    cooldown_calls = []
    orig_upsert = jobs_store.upsert_wake_schedule

    def _spy_upsert(user_id_, **kw):
        cooldown_calls.append((user_id_, kw))
        return orig_upsert(user_id_, **kw)

    monkeypatch.setattr(jobs_store, "upsert_wake_schedule", _spy_upsert)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    before = time.time()
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))
    after = time.time()

    assert status == "failed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    assert _job_status(job_id)[0] == "failed"
    assert not any(e["kind"] == "error" for e in _status_events(uid))

    assert len(cooldown_calls) == 1
    called_uid, kwargs = cooldown_calls[0]
    assert called_uid == uid
    assert "payment_cooldown_until" in kwargs
    cooldown_at = kwargs["payment_cooldown_until"]
    assert before + worker._WAKE_COOLDOWN_SEC - 5 <= cooldown_at <= after + worker._WAKE_COOLDOWN_SEC + 5

    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is not None
    assert schedule["payment_cooldown_until"] is not None


def test_run_wake_rollback_blocks_provider_cooldown_write(monkeypatch):
    uid = "u_wake_provider_rollback"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None):
        raise provider_client.ProviderError("credits", status_code=402)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    # First check (the fence right before the tool loop starts) passes; the
    # SECOND check (inside the payment-cooldown fence, after the provider
    # call fails) is where the rollback lands and must block the write.
    mode_checks = iter([True, False])
    cooldown_calls = []
    monkeypatch.setattr(
        jobs_store,
        "upsert_wake_schedule",
        lambda *a, **k: cooldown_calls.append((a, k)),
    )
    deps = _wake_deps(tail=[])
    deps.runtime_mode_enabled = lambda _uid: next(mode_checks)

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "failed"
    assert cooldown_calls == []


def test_run_wake_lost_lease_blocks_provider_cooldown_write(monkeypatch):
    uid = "u_wake_provider_lost_lease"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None):
        raise provider_client.ProviderError("credits", status_code=402)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *a, **k: False)
    cooldown_calls = []
    monkeypatch.setattr(
        jobs_store,
        "upsert_wake_schedule",
        lambda *a, **k: cooldown_calls.append((a, k)),
    )

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", _wake_deps(tail=[]), _BYOK,
        worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "failed"
    assert cooldown_calls == []


def test_run_wake_transient_error_does_not_set_payment_cooldown(monkeypatch):
    uid = "u_wake_transient"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None):
        raise provider_client.ProviderError("timed out", status_code=503)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)

    cooldown_calls = []
    orig_upsert = jobs_store.upsert_wake_schedule

    def _spy_upsert(user_id_, **kw):
        cooldown_calls.append((user_id_, kw))
        return orig_upsert(user_id_, **kw)

    monkeypatch.setattr(jobs_store, "upsert_wake_schedule", _spy_upsert)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "failed"
    assert _job_status(job_id)[0] == "failed"
    assert cooldown_calls == []
    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is None or schedule["payment_cooldown_until"] is None


# ------------------------------------------------------------------
# process_job dispatch: heartbeat/scheduled/manual_wake route to _run_wake,
# NOT the chat coalesce/planner path.
# ------------------------------------------------------------------

@pytest.mark.parametrize("lane", ["heartbeat", "scheduled", "manual_wake"])
def test_process_job_dispatches_wake_lanes_to_run_wake_not_chat_path(monkeypatch, lane):
    uid = f"u_wake_dispatch_{lane}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    plan_calls = {"n": 0}

    async def _boom_plan(*a, **k):
        plan_calls["n"] += 1
        raise AssertionError("planner must not run for a wake-lane job")

    monkeypatch.setattr(v2_planner, "plan", _boom_plan)

    coalesce_calls = {"n": 0}
    orig_coalesce = v2_coalesce.coalesce_pending

    def _counting_coalesce(*a, **k):
        coalesce_calls["n"] += 1
        return orig_coalesce(*a, **k)

    monkeypatch.setattr(v2_coalesce, "coalesce_pending", _counting_coalesce)

    _script_provider(monkeypatch, [_text_round("a proactive nudge")])
    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text) or {"id": "r"})

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: [],
        read_summary=lambda uid_: ("", 0.0, 0),
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert plan_calls["n"] == 0
    assert coalesce_calls["n"] == 0
    assert written["text"] == "a proactive nudge"
    assert _job_status(job_id)[0] == "completed"
