"""Pure-unit tests for backend/hosted/agent_runtime_cutover.py.

The hosted /v1/model_api/chat/send endpoint can route a user to the out-of-process
agent runtime instead of the inline LLM call, behind a per-user flag, while
keeping the external contract stable (short turn → synchronous reply, slow turn →
processing). No flask/DB here — the wait helper takes an injected clock/sleep and
a store-like object exposing ``chat_messages``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import agent_runtime_cutover as cutover


class FakeStore:
    def __init__(self, messages=None):
        self.chat_messages = messages or []


# ---- flag resolution ----

def test_driver_for_provider_is_derived_not_chosen():
    # anthropic + deepseek → Claude Code (Anthropic-wire); openai → Codex.
    assert cutover.driver_for_provider("anthropic") == "claude"
    assert cutover.driver_for_provider("claude") == "claude"      # alias → anthropic
    assert cutover.driver_for_provider("deepseek") == "claude"    # via its /anthropic endpoint
    assert cutover.driver_for_provider("openai") == "codex"
    # no native hosted-agent fit today
    for p in ("gemini", "openrouter", "openai_compatible", "", "bogus"):
        assert cutover.driver_for_provider(p) == "legacy"


def test_resolve_driver_defaults_to_legacy_when_disabled():
    assert cutover.resolve_driver({}) == "legacy"
    assert cutover.resolve_driver(None) == "legacy"
    # provider present but hosting not enabled → still legacy (gradual-rollout gate)
    assert cutover.resolve_driver({"provider": "anthropic"}) == "legacy"


def test_resolve_driver_derives_agent_from_provider_when_enabled():
    on = {"agent_runtime_driver": "auto"}
    assert cutover.resolve_driver({**on, "provider": "anthropic"}) == "claude"
    assert cutover.resolve_driver({**on, "provider": "deepseek"}) == "claude"
    assert cutover.resolve_driver({**on, "provider": "openai"}) == "codex"
    # enabled but unsupported provider → legacy (no fit)
    assert cutover.resolve_driver({**on, "provider": "gemini"}) == "legacy"


def test_resolve_driver_ignores_stale_chosen_value_and_rederives():
    # A legacy stored "codex" for an anthropic key is treated as enabled and
    # re-derived to claude — the user never picks the agent.
    assert cutover.resolve_driver({"agent_runtime_driver": "codex", "provider": "anthropic"}) == "claude"


def test_hosting_enabled_gate():
    assert cutover.hosting_enabled({"agent_runtime_driver": "auto"}) is True
    assert cutover.hosting_enabled({"agent_runtime_driver": "legacy"}) is False
    assert cutover.hosting_enabled({}) is False


def test_is_enabled():
    assert cutover.is_enabled("claude") is True
    assert cutover.is_enabled("legacy") is False


def test_should_route_only_for_enabled_text_turns():
    assert cutover.should_route("claude", has_image=False) is True
    assert cutover.should_route("codex", has_image=False) is True
    # legacy never routes
    assert cutover.should_route("legacy", has_image=False) is False
    # image turns stay on the legacy multimodal path (runtime is text-only today)
    assert cutover.should_route("claude", has_image=True) is False


# ---- reply lookup ----

def test_find_reply_uses_reply_message_id_link():
    msgs = [
        {"id": "u1", "role": "user", "ts": 1.0, "reply_message_id": "a1"},
        {"id": "a1", "role": "openclaw", "ts": 2.0, "body_ct": "..."},
    ]
    row = cutover.find_reply_row(FakeStore(msgs), "u1")
    assert row["id"] == "a1"


def test_find_reply_falls_back_to_reply_to_message_id():
    msgs = [
        {"id": "u1", "role": "user", "ts": 1.0},
        {"id": "a1", "role": "openclaw", "ts": 2.0, "reply_to_message_id": "u1"},
    ]
    assert cutover.find_reply_row(FakeStore(msgs), "u1")["id"] == "a1"


def test_find_reply_none_when_not_yet_answered():
    msgs = [{"id": "u1", "role": "user", "ts": 1.0}]
    assert cutover.find_reply_row(FakeStore(msgs), "u1") is None


# ---- wait loop ----

def test_wait_returns_reply_when_it_arrives():
    store = FakeStore([{"id": "u1", "role": "user", "ts": 1.0}])
    ticks = {"n": 0}

    def fake_sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:  # reply lands on the 2nd poll
            store.chat_messages.append({"id": "a1", "role": "openclaw", "ts": 2.0,
                                        "reply_to_message_id": "u1"})

    clock = {"t": 0.0}
    row = cutover.wait_for_reply(store, "u1", timeout=10.0, poll_interval=0.5,
                                 sleep=fake_sleep, now=lambda: clock.__setitem__("t", clock["t"] + 0.5) or clock["t"])
    assert row is not None and row["id"] == "a1"


def test_wait_times_out_to_none():
    store = FakeStore([{"id": "u1", "role": "user", "ts": 1.0}])
    clock = {"t": 0.0}

    def advancing_now():
        clock["t"] += 1.0
        return clock["t"]

    row = cutover.wait_for_reply(store, "u1", timeout=2.0, poll_interval=0.5,
                                 sleep=lambda _: None, now=advancing_now)
    assert row is None


# ---- response shaping ----
# Under E2E the server holds no plaintext, so we never fake a legacy `reply`
# field: the agent-runtime path is always async (202) and the client reads the
# (ciphertext) reply via chat poll + enclave decrypt. `reply_ready` +
# `assistant_message` are a latency hint when the reply already landed.

def test_processing_response_is_202_with_no_reply_field():
    body, status = cutover.build_processing_response({"id": "u1", "ts": 1.0}, driver="claude")
    assert status == 202
    assert body["status"] == "processing"
    assert body["reply_ready"] is False
    assert "reply" not in body          # never a fake plaintext reply
    assert body["user_message"]["id"] == "u1"
    assert body["runtime"]["driver"] == "claude"


def test_ready_response_is_202_with_assistant_ref_not_200_ok():
    body, status = cutover.build_ready_response(
        {"id": "u1", "ts": 1.0}, {"id": "a1", "ts": 2.0}, driver="claude")
    assert status == 202                # NOT 200 — there is no synchronous plaintext
    assert body["reply_ready"] is True
    assert body["assistant_message"]["id"] == "a1"
    assert "reply" not in body
    assert body["runtime"]["driver"] == "claude"


def test_handle_send_returns_ready_when_reply_present():
    store = FakeStore([
        {"id": "u1", "role": "user", "ts": 1.0, "reply_message_id": "a1"},
        {"id": "a1", "role": "openclaw", "ts": 2.0},
    ])
    body, status = cutover.handle_send(store, {"id": "u1", "ts": 1.0}, "claude", timeout=0.0)
    assert status == 202 and body["reply_ready"] is True
    assert body["assistant_message"]["id"] == "a1"


def test_handle_send_returns_processing_when_slow():
    store = FakeStore([{"id": "u1", "role": "user", "ts": 1.0}])
    body, status = cutover.handle_send(store, {"id": "u1", "ts": 1.0}, "claude", timeout=0.0)
    assert status == 202 and body["reply_ready"] is False
