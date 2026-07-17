"""worker.process_job chat branch wires user-MCP tools into the tool loop:
load_turn_mcp is called with the turn's store/credentials, its specs are offered
to the provider, and mcp__ calls are routed to its dispatcher (not the platform
executor). Mirrors test_v2_worker_tool_loop.py's real-DB harness."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from provider_types import ToolResult, ToolSpec
from core import store as core_store
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"), reason="needs PG")

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")

_MCP_SPEC = ToolSpec(name="mcp__test__ping", description="ping the test server",
                     parameters={"type": "object", "properties": {}})


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _patch_real_write(monkeypatch):
    def _real_write(store, text):
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test"}
        return store.append_chat("openclaw", "model_api", envelope, strict=True)
    monkeypatch.setattr(worker, "_write_encrypted_reply", _real_write)


def _apply_effects(user_id):
    from model_api_runtime.v2 import effect_outbox as v2_effect_outbox

    def dispatch(effect_type, payload):
        if effect_type == "reply":
            worker._write_encrypted_reply(
                core_store.get_store(user_id), str(payload.get("text") or ""))
    return v2_effect_outbox.apply_pending_effects(user_id, dispatch=dispatch)


def _script_provider(monkeypatch, responses):
    it = iter(responses)
    calls = []

    async def _fake(config, messages, *, tools=None):
        calls.append({"tools": tools})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _deps(messages, *, load_mcp_turn=None):
    return worker.TurnDeps(
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt-enclave",
        apply_pending_effects=_apply_effects,
        load_mcp_turn=load_mcp_turn,
    )


def _bubbles(uid):
    store = core_store.get_store(uid)
    store.reload()
    return [m for m in store.chat_messages
            if m.get("role") == "openclaw" and m.get("source") == "model_api"]


class _FakeMcpTurn:
    """Stands in for a loaded McpTurn without enclave/network. Records dispatches."""
    def __init__(self, specs, recorder):
        self.tool_specs = specs
        self._rec = recorder

    @property
    def is_empty(self):
        return not self.tool_specs

    def handles(self, name):
        return str(name).startswith("mcp__")

    async def dispatch(self, call):
        self._rec.append((call.name, dict(call.args or {})))
        return ToolResult(call_id=call.id, content="pong from test server")


def _make_load_turn_mcp(turn, seen=None):
    async def _fake_load(store, *, api_key=None, runtime_token=""):
        if seen is not None:
            seen.append({"user_id": store.user_id, "runtime_token": runtime_token})
        return turn
    return _fake_load


def test_chat_turn_offers_and_dispatches_configured_mcp_tool(monkeypatch):
    uid = "u_mcp_wired"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    dispatched = []
    seen_load = []
    load_fn = _make_load_turn_mcp(_FakeMcpTurn([_MCP_SPEC], dispatched), seen=seen_load)

    calls = _script_provider(monkeypatch, [
        {"reply": "", "tool_calls": [{"id": "m1", "name": "mcp__test__ping", "args": {}}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        {"reply": "the server said pong", "tool_calls": [],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    ])
    deps = _deps([{"id": "m1", "ts": 10.0, "role": "user", "content": "ping it"}],
                 load_mcp_turn=load_fn)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt-turn"))

    assert status == "completed"
    # load_turn_mcp was called with this turn's user + runtime token
    assert seen_load and seen_load[0]["user_id"] == uid
    assert seen_load[0]["runtime_token"] == "rt-turn"
    # the MCP tool was offered to the provider in round 1
    assert any(s.name == "mcp__test__ping" for s in calls[0]["tools"])
    # the mcp__ call was routed to the MCP dispatcher (not the platform executor)
    assert dispatched == [("mcp__test__ping", {})]
    # the turn produced the model's final reply
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1 and bubbles[0]["body_ct"] == "the server said pong"


def test_chat_turn_without_mcp_offers_no_mcp_tools(monkeypatch):
    uid = "u_mcp_none"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    load_fn = _make_load_turn_mcp(_FakeMcpTurn([], []))  # zero servers

    calls = _script_provider(monkeypatch, [
        {"reply": "plain answer", "tool_calls": [],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}])
    deps = _deps([{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
                 load_mcp_turn=load_fn)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert not any(s.name.startswith("mcp__") for s in calls[0]["tools"])
    assert _bubbles(uid)[0]["body_ct"] == "plain answer"
