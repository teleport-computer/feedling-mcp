"""Task 3 (screen_watch round): the `screen_watch` lane is a WAKE — it may write
a chat bubble — so it joins `worker._WAKE_LANES` and reuses `_run_wake`, but with
its own system prompt (`_SCREEN_WATCH_SYSTEM_PROMPT`) and grounded on recent
screen frames (`screen_recent` capability) instead of a perception snapshot.

PR C Task 8 (spec C8): `_run_wake` (and therefore `screen_watch`) now runs on
the unified `tool_loop.run_tool_loop` — the LLM wire boundary is
`provider_client.chat_completion_async`, not `v2_responder.respond`. The
`screen_recent` prefetch flows in as a STATIC `extra_context` string (rendered
via `responder._action_context_str`, resolved once before the loop starts —
see `_make_build_messages_fn`'s `extra_context` parameter) rather than as the
old `action_results=` kwarg to `respond()`.

Contract (from the task brief):
- `"screen_watch" in worker._WAKE_LANES`.
- The turn fetches ONLY `screen_recent` — NEVER `perception_snapshot` (the
  resident sets `perception_digest=None` for screen-watch jobs,
  chat_resident_consumer.py:6611), and hands the frames to the loop's system
  prompt/context under `_SCREEN_WATCH_SYSTEM_PROMPT`.
- Silence is SUCCESS: an empty terminal reply completes the job with zero
  bubbles (weak wake sleeps) — inherited from `_run_wake`, not a new path.

Style mirrors test_v2_wake_worker.py: real jobs_store/core_store (real DB
claim/mark_*) + stubbed `worker._cap_data` (the enclave/DB capability
boundary), stubbed `provider_client.chat_completion_async` (the LLM wire
boundary), stubbed `worker._write_encrypted_reply` (spy, reached through a
real PR A effect-outbox drain, no real envelope/enclave round-trip)."""
from __future__ import annotations

import asyncio
import sys
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
    conftest.set_v2_runtime_owner(uid)


_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


def _status_events(uid):
    return jobs_store.list_status_events(uid, after_id=0, limit=100)


def _reply_effect_dispatch(user_id):
    """Test-local production-shaped `reply` sink — mirrors
    `serve_worker._sink_reply` without pulling in hosted-adjacent wiring."""
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


def _text_round(text, *, prompt_tokens=1, completion_tokens=1):
    return {"reply": text, "tool_calls": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


# ------------------------------------------------------------------
# Dispatch: screen_watch is a wake lane.
# ------------------------------------------------------------------

def test_screen_watch_is_dispatched_to_the_wake_path():
    assert "screen_watch" in worker._WAKE_LANES


# ------------------------------------------------------------------
# The turn grounds on recent frames and its own prompt, and must NOT
# fetch a perception snapshot.
# ------------------------------------------------------------------

def test_screen_watch_turn_passes_screen_context_and_its_own_prompt(monkeypatch):
    """It must ground on recent frames and must NOT fetch a perception snapshot."""
    uid = "u_sw_context"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    job = jobs_store.claim_next_job("w")

    seen = {}

    async def _fake(config, messages, *, tools=None):
        seen["messages"] = messages
        return _text_round("你在看这个报错？")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    caps = []

    async def _fake_cap_data(store, action_type, **kw):
        caps.append(action_type)
        return {"frames": [{"frame_id": "f1", "caption": "a stack trace"}]}

    monkeypatch.setattr(worker, "_cap_data", _fake_cap_data)

    # The planner must never run for a wake-lane job.
    async def _boom_plan(*a, **k):
        raise AssertionError("planner must not run for a screen_watch job")

    monkeypatch.setattr(v2_planner, "plan", _boom_plan)

    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text) or {"id": "r"})

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    system_msg = next(m for m in seen["messages"] if m["role"] == "system")
    assert system_msg["content"] is not None
    assert "watching the screen" in system_msg["content"]  # _SCREEN_WATCH_SYSTEM_PROMPT, not _WAKE_SYSTEM_PROMPT
    joined = " ".join(str(m.get("content", "")) for m in seen["messages"])
    assert "a stack trace" in joined                        # screen_recent frame caption flowed through
    assert "perception_snapshot" not in caps                # resident sets perception_digest=None
    assert caps == ["screen_recent"]
    assert written["text"] == "你在看这个报错？"
    assert _job_status(job_id)[0] == "completed"


# ------------------------------------------------------------------
# Silence is SUCCESS. Most ticks produce nothing.
# ------------------------------------------------------------------

def test_screen_watch_silence_completes_without_a_bubble(monkeypatch):
    """Most ticks produce nothing. An empty terminal reply is SUCCESS (weak
    wake sleeps), never a chip and never a bubble."""
    uid = "u_sw_silence"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    job = jobs_store.claim_next_job("w")

    _script_calls = []

    async def _fake(config, messages, *, tools=None):
        _script_calls.append(messages)
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    async def _fake_cap_data(store, action_type, **kw):
        return {"frames": [{"frame_id": "f1", "caption": "idle desktop"}]}

    monkeypatch.setattr(worker, "_cap_data", _fake_cap_data)

    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    deps = _wake_deps(tail=[])
    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert write_called["n"] == 0                     # no chat bubble
    assert surface_called["n"] == 0                   # no user-visible error chip
    assert _job_status(job_id)[0] == "completed"
    assert not any(e["kind"] == "error" for e in _status_events(uid))


# ------------------------------------------------------------------
# Concurrency-1 anti-deadlock guard: `_cap_data` acquires enclave_sem
# INTERNALLY (worker.py, `async with enclave_sem`). asyncio.Semaphore is NOT
# reentrant, so the screen_recent fetch MUST sit OUTSIDE `_run_wake`'s own
# `async with enclave_sem` block. This test drives the real `_cap_data`
# semaphore acquisition (only run_capability is stubbed) so a nested call would
# deadlock here at FEEDLING_V2_ENCLAVE_CONCURRENCY=1. Uses a bounded
# asyncio.run(...) with a wait_for so a regression fails LOUD (timeout) instead
# of hanging the suite.
# ------------------------------------------------------------------

def test_screen_watch_does_not_deadlock_when_cap_data_reacquires_enclave_sem(monkeypatch):
    uid = "u_sw_nodeadlock"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    job = jobs_store.claim_next_job("w")

    from capabilities import registry as cap_registry

    class _FakeCapResult:
        def to_dict(self):
            return {"ok": True, "data": {"frames": [{"frame_id": "f1", "caption": "cap"}]},
                    "error": None, "trace": {}, "warnings": []}

    # Stub only the capability dispatch — the REAL worker._cap_data (with its own
    # `async with enclave_sem`) runs, so a nested acquisition would deadlock.
    monkeypatch.setattr(
        cap_registry, "run_capability", lambda action_type, store, **k: _FakeCapResult())

    async def _fake(config, messages, *, tools=None):
        return _text_round("spotted it")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    # A concurrency-1 semaphore is the killer case: if the screen_recent fetch is
    # nested inside `_run_wake`'s `async with enclave_sem`, _cap_data's inner
    # acquire can never succeed.
    sem = asyncio.Semaphore(1)

    async def _drive():
        return await asyncio.wait_for(
            worker.process_job(
                job, _wake_deps(tail=[]), provider_config=_BYOK, is_official=False,
                api_key=None, runtime_token="rt", enclave_sem=sem),
            timeout=10.0)

    status = asyncio.run(_drive())
    assert status == "completed"
    assert _job_status(job_id)[0] == "completed"
