"""Task 3 (screen_watch round): the `screen_watch` lane is a WAKE — it may write
a chat bubble — so it joins `worker._WAKE_LANES` and reuses `_run_wake`, but with
its own system prompt (`_SCREEN_WATCH_SYSTEM_PROMPT`) and grounded on recent
screen frames (`screen_recent` capability) instead of a perception snapshot.

Contract (from the task brief):
- `"screen_watch" in worker._WAKE_LANES`.
- The turn fetches ONLY `screen_recent` — NEVER `perception_snapshot` (the
  resident sets `perception_digest=None` for screen-watch jobs,
  chat_resident_consumer.py:6611), and hands the frames to
  `v2_responder.respond(..., action_results={"screen_recent": [...]}, ...)`
  under `_SCREEN_WATCH_SYSTEM_PROMPT`.
- Silence is SUCCESS: a `ResponderError("empty_reply")` completes the job with
  zero bubbles (weak wake sleeps) — inherited from `_run_wake`, not a new path.

Style mirrors test_v2_wake_worker.py: real jobs_store/core_store (real DB
claim/mark_*) + stubbed `worker._cap_data` (the enclave/DB capability boundary),
stubbed `v2_responder.respond` (the LLM boundary), stubbed
`worker._write_encrypted_reply` (spy, no real envelope/enclave round-trip)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import planner as v2_planner
from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import responder as v2_responder
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


_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


def _status_events(uid):
    return jobs_store.list_status_events(uid, after_id=0, limit=100)


def _wake_deps(*, summary="", tail=None):
    return worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after_ts, limit: list(tail if tail is not None else []),
        read_summary=lambda uid: (summary, 0.0, 0),
    )


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

    async def _fake_respond(*, provider_config, summary, tail, system_prompt=None,
                            action_results=None, **kw):
        seen["system_prompt"] = system_prompt
        seen["action_results"] = action_results
        return "你在看这个报错？"

    monkeypatch.setattr(v2_responder, "respond", _fake_respond)

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
    assert seen["system_prompt"] is worker._SCREEN_WATCH_SYSTEM_PROMPT
    assert "screen_recent" in (seen["action_results"] or {})
    # frames flow through as a single ok run: {"screen_recent": [{"ok": True, "data": ...}]}
    runs = seen["action_results"]["screen_recent"]
    assert runs == [{"ok": True, "data": {"frames": [{"frame_id": "f1", "caption": "a stack trace"}]}}]
    assert "perception_snapshot" not in caps          # resident sets perception_digest=None
    assert caps == ["screen_recent"]
    assert written["text"] == "你在看这个报错？"
    assert _job_status(job_id)[0] == "completed"


# ------------------------------------------------------------------
# Silence is SUCCESS. Most ticks produce nothing.
# ------------------------------------------------------------------

def test_screen_watch_silence_completes_without_a_bubble(monkeypatch):
    """Most ticks produce nothing. empty_reply is SUCCESS (weak wake sleeps),
    never a chip and never a bubble."""
    uid = "u_sw_silence"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    job = jobs_store.claim_next_job("w")

    async def _fake_respond(*, provider_config, summary, tail, system_prompt=None,
                            action_results=None, **kw):
        raise v2_responder.ResponderError("empty_reply")

    monkeypatch.setattr(v2_responder, "respond", _fake_respond)

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

    async def _fake_respond(*, provider_config, summary, tail, system_prompt=None,
                            action_results=None, **kw):
        return "spotted it"

    monkeypatch.setattr(v2_responder, "respond", _fake_respond)
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
