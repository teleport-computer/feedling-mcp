"""Perception grounding: chat and wake turns prefetch a `perception_snapshot`
and hand it to the loop as static `extra_context` (rendered by
`context.action_context_str`), so the agent can answer "how am I doing / how many
steps today" WITHOUT having to guess that a tool exists.

Before this, V2 injected perception on NO lane (only `screen_watch` got
`screen_recent`), and `CHAT_SYSTEM_PROMPT` never mentions perception — so the
model answered "获取不到数据" while `perception_snapshot(signals=["steps"])` would
have returned the step count immediately.

Contract:
- chat lane: fetches `perception_snapshot` over the FULL agent signal catalog
  (health/steps/sleep live in the SLOW set, which the capability's default
  `FAST_AGENT_PERCEPTION_SIGNALS` omits — passing signals explicitly is the whole
  point) and renders it into the prompt.
- wake lanes: same, EXCEPT `screen_watch`, which keeps grounding on
  `screen_recent` only (the resident sets perception_digest=None there —
  see test_v2_screen_watch_lane.py).
- A failed/empty prefetch is never fatal: no grounding, turn still completes
  (the model can still call the tool itself).

Harness mirrors test_v2_screen_watch_lane.py / test_v2_worker_mcp.py: real
jobs_store/core_store, stubbed `worker._cap_data` (capability boundary) and
`provider_client.chat_completion_async` (LLM wire).
"""
from __future__ import annotations

import asyncio
import os
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
from model_api_runtime.v2 import worker
from perception.agent_fields import (
    AGENT_PERCEPTION_SIGNALS,
    FAST_AGENT_PERCEPTION_SIGNALS,
)

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")

# What a real perception_snapshot returns: live values AND explicit disabled/null
# signals (the agent must be told what it CANNOT see, not left to infer from a gap).
_SNAPSHOT = {
    "ok": True,
    "signals": {
        "steps": {"step_count": 365},
        "sleep": {"asleep_minutes": 389, "deep_minutes": 76},
        "now": {"disabled": True, "reason": "not_permitted"},
    },
}


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


def _apply_effects(user_id):
    def dispatch(effect_type, payload):
        if effect_type == "reply":
            worker._write_encrypted_reply(
                core_store.get_store(user_id), str(payload.get("text") or ""))
    return v2_effect_outbox.apply_pending_effects(user_id, dispatch=dispatch)


def _text_round(text):
    return {"reply": text, "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _spy_provider(monkeypatch, seen):
    async def _fake(config, messages, *, tools=None):
        seen["messages"] = messages
        return _text_round("ok")
    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)


def _spy_cap_data(monkeypatch, calls, *, data=None):
    async def _fake_cap_data(store, action_type, **kw):
        calls.append({"action": action_type, "params": kw.get("params")})
        if action_type == "perception_snapshot":
            return _SNAPSHOT if data is None else data
        return {"frames": [{"frame_id": "f1", "caption": "a stack trace"}]}
    monkeypatch.setattr(worker, "_cap_data", _fake_cap_data)


def _chat_deps(messages):
    return worker.TurnDeps(
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt-enclave",
        apply_pending_effects=_apply_effects,
    )


def _wake_deps(tail):
    return worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after_ts, limit: list(tail),
        read_summary=lambda uid: ("", 0.0, 0),
        apply_pending_effects=_apply_effects,
    )


def _perception_call(calls):
    return next((c for c in calls if c["action"] == "perception_snapshot"), None)


def _joined(seen):
    return " ".join(str(m.get("content", "")) for m in seen["messages"])


def test_chat_turn_injects_perception_grounding(monkeypatch):
    uid = "u_pg_chat"
    conftest.seed_user(uid)
    _reset(uid)
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: {"id": "r"})
    seen, calls = {}, []
    _spy_provider(monkeypatch, seen)
    _spy_cap_data(monkeypatch, calls)

    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    status = asyncio.run(worker.process_job(
        job, _chat_deps([{"id": "m1", "ts": 1.0, "role": "user", "content": "我今天走了多少步？"}]),
        provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    call = _perception_call(calls)
    assert call is not None, "chat turn must prefetch perception_snapshot"
    # The FULL catalog, not the capability's FAST default — steps/sleep are SLOW
    # signals and would be missing otherwise (the exact bug this fixes).
    assert set(call["params"]["signals"]) == set(AGENT_PERCEPTION_SIGNALS)
    assert set(FAST_AGENT_PERCEPTION_SIGNALS) < set(call["params"]["signals"])
    joined = _joined(seen)
    assert "step_count" in joined and "365" in joined     # live value reached the prompt
    assert "not_permitted" in joined                       # disabled signals injected too


def test_wake_turn_injects_perception_grounding(monkeypatch):
    uid = "u_pg_wake"
    conftest.seed_user(uid)
    _reset(uid)
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: {"id": "r"})
    seen, calls = {}, []
    _spy_provider(monkeypatch, seen)
    _spy_cap_data(monkeypatch, calls)

    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    job = jobs_store.claim_next_job("w")
    status = asyncio.run(worker.process_job(
        job, _wake_deps([{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]),
        provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert _perception_call(calls) is not None, "wake turn must prefetch perception_snapshot"
    assert "step_count" in _joined(seen)


def test_screen_watch_still_grounds_on_frames_only(monkeypatch):
    """Regression guard for test_v2_screen_watch_lane.py's contract: the resident
    sets perception_digest=None for screen-watch, so this lane must NOT gain a
    perception prefetch."""
    uid = "u_pg_sw"
    conftest.seed_user(uid)
    _reset(uid)
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: {"id": "r"})
    seen, calls = {}, []
    _spy_provider(monkeypatch, seen)
    _spy_cap_data(monkeypatch, calls)

    job_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    job = jobs_store.claim_next_job("w")
    status = asyncio.run(worker.process_job(
        job, _wake_deps([{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]),
        provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert [c["action"] for c in calls] == ["screen_recent"]
    assert "a stack trace" in _joined(seen)


def test_empty_perception_prefetch_is_not_fatal(monkeypatch):
    """`_cap_data` degrades to {} on failure — the turn must still complete with no
    perception grounding (the model can call the tool itself)."""
    uid = "u_pg_empty"
    conftest.seed_user(uid)
    _reset(uid)
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: {"id": "r"})
    seen, calls = {}, []
    _spy_provider(monkeypatch, seen)
    _spy_cap_data(monkeypatch, calls, data={})

    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    status = asyncio.run(worker.process_job(
        job, _chat_deps([{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]),
        provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert _perception_call(calls) is not None
    assert "perception_snapshot" not in _joined(seen)   # nothing rendered, no crash
