"""
Regression tests for tools/chat_resident_consumer.py
=====================================================

Run with: pytest tests/test_chat_resident_consumer.py -v
"""

import importlib
import base64
import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars before the module is imported.
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

# Add repo root + backend dir to path (needed for real import in non-test environments).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Stub out content_encryption only when the backend tree is unavailable. In the
# full backend suite, the backend needs the real module; poisoning sys.modules here
# makes later envelope tests import a fake build_envelope.
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(role="user", content="hello", ts=None, timestamp=None):
    msg = {"role": role, "content": content}
    if ts is not None:
        msg["ts"] = ts
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


def _make_image_msg(ts=1.0, image_bytes=b"fake-jpeg"):
    msg = _make_msg(role="user", content="", ts=ts)
    msg["content_type"] = "image"
    msg["image_b64"] = base64.b64encode(image_bytes).decode("ascii")
    return msg


# ---------------------------------------------------------------------------
# Case 1: user message with empty content → no fallback, checkpoint advances
# ---------------------------------------------------------------------------

def test_empty_content_no_fallback():
    """poll returns user message with empty content (encrypted envelope) —
    _process_messages must skip it without calling post_reply."""
    msgs = [_make_msg(role="user", content="", ts=1000.0)]

    with patch.object(crc, "call_agent") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages(msgs)

    mock_agent.assert_not_called()
    mock_post.assert_not_called()
    assert result_ts == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Case 2: message has only "ts" key (no "timestamp") → checkpoint advances
# ---------------------------------------------------------------------------

def test_ts_key_only_advances_checkpoint():
    """API returns {"ts": 1234.5} with no "timestamp" key.
    _process_messages must still return 1234.5 so the checkpoint advances."""
    msgs = [_make_msg(role="user", content="what time is it?", ts=1234.5)]
    # ts=1234.5, no "timestamp" key

    with patch.object(crc, "call_agent", return_value="It's noon."), \
         patch.object(crc, "post_reply"):
        result_ts = crc._process_messages(msgs)

    assert result_ts == pytest.approx(1234.5)


def test_timestamp_key_only_advances_checkpoint():
    """API returns {"timestamp": 5678.9} with no "ts" key — same result."""
    msgs = [_make_msg(role="user", content="hi", timestamp=5678.9)]

    with patch.object(crc, "call_agent", return_value="hey"), \
         patch.object(crc, "post_reply"):
        result_ts = crc._process_messages(msgs)

    assert result_ts == pytest.approx(5678.9)


def test_filter_messages_to_poll_ids_keeps_only_claimed_rows():
    decrypted = [
        {"id": "msg-a", "role": "user", "content": "ours"},
        {"id": "msg-b", "role": "user", "content": "claimed by someone else"},
    ]
    poll_messages = [{"id": "msg-a", "role": "user"}]

    assert crc._filter_messages_to_poll_ids(decrypted, poll_messages) == [decrypted[0]]


def test_process_messages_posts_reply_with_source_message_id():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    msg = {"id": "user-msg-1", "role": "user", "content": "hi", "ts": 1111.0}

    with patch.object(crc, "call_agent", return_value="hey"), \
         patch.object(crc, "post_reply", return_value={"id": "reply-msg-1"}) as mock_post:
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(1111.0)
    assert mock_post.call_args.kwargs["reply_to_message_id"] == "user-msg-1"


def test_process_messages_runtime_v2_uses_native_agent_without_tools_prompt(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    crc._update_chat_runtime_v2_profile({crc.RESIDENT_CHAT_RUNTIME_V2_FLAG: True})
    msg = {"id": "user-msg-v2", "role": "user", "content": "天气怎么样？", "ts": 1112.0}
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id="", lane="background"):
        captured["message"] = message
        return {"messages": ["外面下雨。"]}

    try:
        with patch.object(crc, "call_agent", side_effect=fake_call) as mock_agent, \
             patch.object(crc, "post_reply", return_value={"id": "reply-msg-v2"}) as mock_post:
            result_ts = crc._process_messages([msg])
    finally:
        crc._update_chat_runtime_v2_profile({})

    assert result_ts == pytest.approx(1112.0)
    mock_agent.assert_called_once()
    assert captured["message"].endswith("天气怎么样？")  # time anchor prepended, no tool-prompt
    assert "current_time:" in captured["message"]
    assert "Available tools JSON" not in captured["message"]
    assert "tool_calls" not in captured["message"]
    assert "perception.weather" not in captured["message"]
    assert "memory.fetch" not in captured["message"]
    assert "perception.steps" not in captured["message"]
    assert "screen.read" not in captured["message"]
    assert mock_post.call_args.args[0] == "外面下雨。"
    assert mock_post.call_args.kwargs["reply_to_message_id"] == "user-msg-v2"


def test_process_messages_prompt_does_not_request_custom_thinking_summary(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    msg = {"id": "user-msg-thinking", "role": "user", "content": "刚才怎么没思考过程？", "ts": 1112.5}
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id="", lane="background"):
        captured["message"] = message
        return {"messages": ["我查一下。"]}

    with patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "reply-msg-thinking"}):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(1112.5)
    assert '"thinking_summary"' not in captured["message"]
    assert "display-safe" not in captured["message"]
    assert "hidden chain-of-thought" not in captured["message"]


def test_process_messages_v2_drops_needs_background_without_ack(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    crc._update_chat_runtime_v2_profile({crc.RESIDENT_CHAT_RUNTIME_V2_FLAG: True})
    msg = {"id": "user-msg-bg", "role": "user", "content": "今天多少步？", "ts": 1113.0}

    try:
        with patch.object(
            crc,
            "call_agent",
            return_value={"actions": [{"type": "needs_background", "request": {"tool": "perception.steps", "args": {}}}], "messages": []},
        ), patch.object(crc, "execute_agent_actions") as mock_actions, \
             patch.object(crc, "post_reply", return_value={"id": "reply-bg"}) as mock_post:
            result_ts = crc._process_messages([msg])
    finally:
        crc._update_chat_runtime_v2_profile({})

    assert result_ts == pytest.approx(1113.0)
    mock_actions.assert_not_called()
    mock_post.assert_not_called()


def test_process_messages_keeps_checkpoint_when_post_reply_fails():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    msg = {"id": "user-msg-2", "role": "user", "content": "hi", "ts": 2222.0}

    with patch.object(crc, "call_agent", return_value="hey"), \
         patch.object(crc, "post_reply", side_effect=RuntimeError("write failed")):
        result_ts = crc._process_messages([msg])

    assert result_ts == 0.0


def test_checkpoint_records_owner_and_resets_when_user_changes(tmp_path, monkeypatch):
    checkpoint_file = tmp_path / "checkpoint.json"
    monkeypatch.setattr(crc, "CHECKPOINT_FILE", checkpoint_file)
    monkeypatch.setattr(crc, "CHECKPOINT_API_KEY_FINGERPRINT", "key-a")
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_a", "user_pk": None, "enclave_pk": None},
    )

    crc._save_checkpoint(111.0)
    crc._save_proactive_checkpoint(222.0)
    saved = json.loads(checkpoint_file.read_text())

    assert saved["last_ts"] == pytest.approx(111.0)
    assert saved["last_job_ts"] == pytest.approx(222.0)
    assert saved["api_key_fingerprint"] == "key-a"
    assert saved["user_id"] == "usr_a"

    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_b", "user_pk": None, "enclave_pk": None},
    )

    assert crc._load_checkpoint() == 0.0
    assert crc._load_proactive_checkpoint() == 0.0


def test_checkpoint_resets_when_api_key_fingerprint_changes(tmp_path, monkeypatch):
    checkpoint_file = tmp_path / "checkpoint.json"
    checkpoint_file.write_text(json.dumps({
        "last_ts": 333.0,
        "last_job_ts": 444.0,
        "api_key_fingerprint": "key-a",
        "user_id": "usr_a",
    }))
    monkeypatch.setattr(crc, "CHECKPOINT_FILE", checkpoint_file)
    monkeypatch.setattr(crc, "CHECKPOINT_API_KEY_FINGERPRINT", "key-b")
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_a", "user_pk": None, "enclave_pk": None},
    )

    assert crc._load_checkpoint() == 0.0
    assert crc._load_proactive_checkpoint() == 0.0


# ---------------------------------------------------------------------------
# Case 3: invalid API key → run() exits non-zero
# ---------------------------------------------------------------------------

def test_invalid_key_exits_on_startup():
    """If whoami returns 401 / can't get user_id at startup, run() must
    call sys.exit(1) rather than entering the poll loop silently."""
    with patch.object(crc, "_load_whoami", return_value=False), \
         patch.object(crc, "WHOAMI_STARTUP_RETRIES", 1), \
         patch.object(crc, "_ENCRYPTION_AVAILABLE", True), \
         pytest.raises(SystemExit) as exc_info:
        crc.run()

    assert exc_info.value.code != 0


def test_whoami_startup_retries_transient_failure(monkeypatch):
    """Startup whoami should tolerate transient network failures."""
    calls = []

    def _load():
        calls.append(1)
        return len(calls) >= 3

    monkeypatch.setattr(crc, "_load_whoami", _load)
    monkeypatch.setattr(crc, "WHOAMI_STARTUP_RETRIES", 4)
    monkeypatch.setattr(crc, "WHOAMI_STARTUP_RETRY_DELAY_SEC", 0)

    assert crc._load_whoami_with_retries() is True
    assert len(calls) == 3


def test_whoami_startup_retries_keep_fixed_delay(monkeypatch):
    sleeps = []

    monkeypatch.setattr(crc, "_load_whoami", lambda: False)
    monkeypatch.setattr(crc, "WHOAMI_STARTUP_RETRIES", 3)
    monkeypatch.setattr(crc, "WHOAMI_STARTUP_RETRY_DELAY_SEC", 5)
    monkeypatch.setattr(crc.time, "sleep", lambda delay: sleeps.append(delay))

    assert crc._load_whoami_with_retries() is False
    assert sleeps == [5, 5]


def test_whoami_reply_refresh_retries_use_exponential_backoff(monkeypatch):
    sleeps = []

    monkeypatch.setattr(crc, "_load_whoami", lambda: False)
    monkeypatch.setattr(crc.time, "sleep", lambda delay: sleeps.append(delay))

    assert crc._load_whoami_with_retries(
        attempts=3,
        delay_sec=0.5,
        context="reply refresh",
        backoff_multiplier=2.0,
    ) is False
    assert sleeps == [0.5, 1.0]


def test_post_reply_retries_whoami_refresh_before_encrypted_write(monkeypatch):
    calls = []
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "m_retry", "ts": 1.0}

    def _load():
        calls.append(1)
        if len(calls) < 3:
            return False
        crc._whoami_cache.update(
            user_id="usr_retry",
            user_pk=b"r" * 32,
            enclave_pk=None,
        )
        return True

    def _post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "", "user_pk": None, "enclave_pk": None})
    monkeypatch.setattr(crc, "_load_whoami", _load)
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_RETRIES", 3)
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(crc, "_build_envelope", lambda **kw: {"owner": kw["owner_user_id"]})
    monkeypatch.setattr(crc.httpx, "post", _post)

    body = crc.post_reply("hello")

    assert body["id"] == "m_retry"
    assert len(calls) == 3
    assert captured["json"]["envelope"]["owner"] == "usr_retry"


def test_post_reply_uses_cached_whoami_keys_when_refresh_fails(monkeypatch):
    captured = {}
    envelope_kwargs = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "m_cached", "ts": 2.0}

    def _load():
        crc._whoami_cache.update(user_id="", user_pk=None, enclave_pk=None)
        return False

    def _build(**kwargs):
        envelope_kwargs.update(kwargs)
        return {"owner": kwargs["owner_user_id"], "visibility": kwargs["visibility"]}

    def _post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_cached", "user_pk": b"c" * 32, "enclave_pk": b"e" * 32},
    )
    monkeypatch.setattr(crc, "_load_whoami", _load)
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_RETRIES", 2)
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(crc, "_build_envelope", _build)
    monkeypatch.setattr(crc.httpx, "post", _post)

    body = crc.post_reply("hello from cache")

    assert body["id"] == "m_cached"
    assert envelope_kwargs["owner_user_id"] == "usr_cached"
    assert envelope_kwargs["user_pk_bytes"] == b"c" * 32
    assert captured["json"]["envelope"]["visibility"] == "shared"


def test_post_reply_skips_when_whoami_refresh_fails_without_cache(monkeypatch):
    posted = []

    def _post(*args, **kwargs):
        posted.append((args, kwargs))
        raise AssertionError("post should not be called without encryption keys")

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "", "user_pk": None, "enclave_pk": None})
    monkeypatch.setattr(crc, "_load_whoami", lambda: False)
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_RETRIES", 2)
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(crc.httpx, "post", _post)

    body = crc.post_reply("lost")

    assert body == {"error": "whoami_refresh_failed"}
    assert posted == []


# ---------------------------------------------------------------------------
# Bonus: agent failures are fail-hard by default
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 2: enclave source + dedup
# ---------------------------------------------------------------------------

def test_enclave_history_used_when_configured(monkeypatch):
    """When FEEDLING_ENCLAVE_URL is set and enclave returns decrypted messages,
    _process_messages receives actual content (not the empty poll payload)."""
    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "https://127.0.0.1:5003")
    decrypted = [_make_msg(role="user", content="decrypted hello", ts=2000.0)]
    monkeypatch.setattr(crc, "get_decrypted_history", lambda since, limit=20: decrypted)

    with patch.object(crc, "call_agent", return_value="hi back") as mock_agent, \
         patch.object(crc, "post_reply"):
        result_ts = crc._process_messages(decrypted)

    mock_agent.assert_called_once()
    assert mock_agent.call_args[0][0].endswith("decrypted hello")  # time anchor prepended
    assert result_ts == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# Verify-loop liveness ping must be answered WITHOUT a full agent turn.
# Regression for the prod 2026-06-03 wedge (account stuck at
# needs_live_connection): the synthetic ping was routed through the hermes
# agent (slow / SIGTERM-fragile) and, over the enclave decrypt path, arrived
# with content=None (the ping is visibility=local_only so the enclave returns
# null) — falling into the empty-content skip and never replying. Detection
# keys off `source == "verify_ping"`, which survives BOTH the poll and the
# enclave/MCP decrypt paths.
# ---------------------------------------------------------------------------

def test_verify_ping_enclave_path_probes_real_agent():
    """Enclave path delivers the local_only ping with content=None. The
    consumer must still recognise it (via source) — not crash on None and not
    skip it as empty content — and now exercise the REAL agent path (bounded
    probe) so a broken reply pipeline is caught instead of being masked by a
    canned short-circuit (commit 11279e9)."""
    ping = {
        "role": "user",
        "ts": 4242.0,
        "source": "verify_ping",
        "content": None,            # enclave returns null for local_only
        "content_type": "text",
    }
    with patch.object(crc, "call_agent", return_value="收到") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([ping])

    mock_agent.assert_called_once_with(crc.VERIFY_PROBE_MESSAGE)
    mock_post.assert_called_once()
    assert result_ts == pytest.approx(4242.0)


def test_verify_ping_poll_marker_probes_real_agent():
    """Direct /v1/chat/poll path carries the plaintext __VERIFY_PING__ marker
    (source still verify_ping). Still detected via source and routed through the
    real bounded probe."""
    ping = _make_msg(role="user", content="__VERIFY_PING__:deadbeef0001", ts=4343.0)
    ping["source"] = "verify_ping"

    with patch.object(crc, "call_agent", return_value="收到") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([ping])

    mock_agent.assert_called_once_with(crc.VERIFY_PROBE_MESSAGE)
    mock_post.assert_called_once()
    assert result_ts == pytest.approx(4343.0)


def test_verify_ping_success_reply_suppresses_push():
    """A successful probe posts the agent's real reply but must suppress the
    user-visible push. A private liveness ack must never surface as an APNs
    notification while the app is backgrounded — the verify GC removes the chat
    row but cannot recall an already-delivered push."""
    ping = {
        "role": "user",
        "ts": 4444.0,
        "source": "verify_ping",
        "content": None,
        "content_type": "text",
    }
    with patch.object(crc, "call_agent", return_value="我在") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        crc._process_messages([ping])

    mock_agent.assert_called_once()
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs.get("suppress_push") is True
    # source="verify_ping" so the visible history feed filters the liveness reply
    assert mock_post.call_args.kwargs.get("source") == "verify_ping"


def test_verify_ping_slow_agent_falls_back_to_canned_ack():
    """A slow-but-healthy agent (probe exceeds VERIFY_PROBE_TIMEOUT_SEC) must
    NOT falsely fail verify: the consumer falls back to the canned ack (push
    suppressed) so verify_loop still passes."""
    ping = {
        "role": "user",
        "ts": 4547.0,  # unique ts — the global _seen_ids dedup keys on ts:role
        "source": "verify_ping",
        "content": None,
        "content_type": "text",
    }

    # Block the probe on an Event (not a bare sleep) so the timeout fires
    # deterministically AND the background probe thread is released before the
    # test returns — a leaked sleeping thread would wake mid-next-test and
    # pollute its call_agent mock.
    release = threading.Event()

    def _slow(_msg):
        release.wait(timeout=2)
        return "too late"

    try:
        with patch.object(crc, "VERIFY_PROBE_TIMEOUT_SEC", 0.01), \
             patch.object(crc, "call_agent", side_effect=_slow), \
             patch.object(crc, "post_reply") as mock_post:
            crc._process_messages([ping])
            mock_post.assert_called_once_with(
                crc.VERIFY_PING_REPLY, source="verify_ping", suppress_push=True
            )
    finally:
        release.set()


def test_verify_ping_no_usable_reply_does_not_ack():
    """A COMPLETED probe that yields no usable reply (consumer can't parse the
    agent output → ValueError) is a real failure: the consumer posts NOTHING so
    verify_loop stays unsatisfied and onboarding does not green-light a dead
    loop."""
    ping = {
        "role": "user",
        "ts": 4646.0,
        "source": "verify_ping",
        "content": None,
        "content_type": "text",
    }
    with patch.object(crc, "call_agent", side_effect=ValueError("no usable reply")) as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([ping])

    mock_agent.assert_called_once()
    mock_post.assert_not_called()
    assert result_ts == pytest.approx(4646.0)


def test_post_reply_suppress_push_omits_alert_and_push_fields(monkeypatch):
    """post_reply(..., suppress_push=True) sends an empty alert_body and no
    push_body / push_live_activity, so /v1/chat/response's push policy is a
    no-op. The envelope still posts, so verify_loop still sees the reply."""
    captured: dict = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"id": "m1", "ts": 1.0, "v": 1}

    def _post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(crc, "_load_whoami", lambda: True)
    monkeypatch.setattr(
        crc, "_whoami_cache",
        {"user_id": "usr_abc", "user_pk": b"\x01" * 32, "enclave_pk": None},
    )
    monkeypatch.setattr(crc, "_build_envelope", lambda **kw: {"stub": "env"})
    monkeypatch.setattr(crc.httpx, "post", _post)

    crc.post_reply(crc.VERIFY_PING_REPLY, suppress_push=True)
    body = captured["json"]
    assert body["alert_body"] == ""
    assert not body.get("push_body")
    assert not body.get("push_live_activity")

    # Contrast: an ordinary reply still carries a visible alert_body.
    crc.post_reply("hello there")
    assert captured["json"]["alert_body"] == "hello there"


def test_user_message_containing_verify_marker_is_not_short_circuited():
    """A real user message that merely CONTAINS the literal __VERIFY_PING__
    text (e.g. someone debugging this very feature) must still reach the agent.
    Detection keys ONLY on source — the server stamps source='verify_ping' on
    the synthetic probe across all three delivery paths (poll, enclave, MCP),
    so matching arbitrary content would just create false positives that
    silently swallow real chat input."""
    msg = _make_msg(
        role="user",
        content="why does __VERIFY_PING__ keep showing up in my logs?",
        ts=4545.0,
    )  # NB: no source key → ordinary chat, not a verify probe

    with patch.object(crc, "call_agent", return_value="here's why") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    mock_agent.assert_called_once()
    mock_post.assert_called_once()
    assert mock_post.call_args.args == ("here's why",)
    assert "thinking_summary" not in mock_post.call_args.kwargs
    assert "thinking_source" not in mock_post.call_args.kwargs
    assert result_ts == pytest.approx(4545.0)


def test_enclave_fetch_logs_response_body_on_http_error(monkeypatch, caplog):
    """When the enclave returns a non-2xx, the consumer must log the response
    BODY, not just the bare status line.

    Regression (2026-06-03): the enclave now maps transient dependency
    failures to self-describing codes — 502 `backend_unreachable` vs 503
    `key_derivation_unavailable`. httpx's HTTPStatusError string carries the
    status + URL but NOT the body, so `log.warning("... %s", e)` hid exactly
    the field that distinguishes the two failure modes. The operator was left
    with an opaque "all decrypt sources failed" and no way to tell which
    dependency broke without shelling into the CVM.
    """
    import httpx as _httpx

    url = "https://127.0.0.1:5003/v1/chat/history"
    resp = _httpx.Response(
        503,
        json={"error": "key_derivation_unavailable: dstack socket unavailable"},
        request=_httpx.Request("GET", url),
    )
    mock_client = MagicMock()
    mock_client.get.return_value = resp
    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "https://127.0.0.1:5003")
    monkeypatch.setattr(crc, "_ENCLAVE_CLIENT", mock_client)

    with caplog.at_level("WARNING"):
        result = crc._fetch_from_enclave(since=0.0, limit=20)

    assert result is None
    assert "503" in caplog.text
    assert "key_derivation_unavailable" in caplog.text


def test_image_message_passes_image_context_to_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(crc, "IMAGE_TEMP_DIR", tmp_path)
    msg = _make_image_msg(ts=2100.0)

    with patch.object(crc, "call_agent", return_value="I can see it.") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    mock_post.assert_called_once()
    assert result_ts == pytest.approx(2100.0)
    _, kwargs = mock_agent.call_args
    assert kwargs["images"][0]["data"] == msg["image_b64"]
    assert kwargs["images"][0]["data_url"].startswith("data:image/jpeg;base64,")
    assert kwargs["image_paths"]
    assert Path(kwargs["image_paths"][0]).exists()


def test_screen_question_attaches_decrypted_screen_context(monkeypatch):
    msg = _make_msg(role="user", content="你能看到我的屏幕吗", ts=2200.0)
    screen_image = {
        "mime_type": "image/jpeg",
        "data": base64.b64encode(b"screen-jpeg").decode("ascii"),
        "data_url": "data:image/jpeg;base64,c2NyZWVuLWpwZWc=",
    }
    monkeypatch.setattr(
        crc,
        "_screen_context_for_message",
        lambda content: (
            "[Live Feedling screen-sharing context]\napp: com.feedling.mcp\nocr_text:\nhello screen",
            [screen_image],
            ["/tmp/feedling_chat_images/screen.jpg"],
        ),
    )

    with patch.object(crc, "call_agent", return_value="I can see it.") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    mock_post.assert_called_once()
    assert result_ts == pytest.approx(2200.0)
    args, kwargs = mock_agent.call_args
    assert "你能看到我的屏幕吗" in args[0]  # time anchor prepended
    assert "hello screen" in args[0]
    assert kwargs["images"] == [screen_image]
    assert kwargs["image_paths"] == ["/tmp/feedling_chat_images/screen.jpg"]


def test_dedup_prevents_reprocessing_same_message():
    """The same message processed twice (e.g. on restart with stale checkpoint)
    must not trigger a second agent call."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = _make_msg(role="user", content="hello again", ts=3000.0)

    with patch.object(crc, "call_agent", return_value="reply") as mock_agent, \
         patch.object(crc, "post_reply"):
        crc._process_messages([msg])   # first time → processed
        crc._process_messages([msg])   # second time → deduped

    assert mock_agent.call_count == 1


def test_process_messages_executes_identity_actions_before_reply():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    events = []
    action = {
        "type": "identity.profile_patch",
        "patch": {"agent_name": "小秘"},
        "reason": "User asked for a displayed name change.",
    }
    msg = _make_msg(role="user", content="call yourself 小秘", ts=3100.0)

    def _execute(actions):
        events.append(("actions", actions))
        return {"status": "ok", "effects": [{"type": "identity_updated"}], "results": []}

    def _post(reply, **kwargs):
        events.append(("reply", reply))
        return {"id": "msg_1"}

    with patch.object(crc, "call_agent", return_value={"actions": [action], "messages": ["改好了。"]}), \
         patch.object(crc, "execute_agent_actions", side_effect=_execute), \
         patch.object(crc, "post_reply", side_effect=_post):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(3100.0)
    assert events[0] == ("actions", [action])
    assert events[1] == ("reply", "改好了。")


def test_process_messages_does_not_post_optimistic_reply_when_identity_action_fails():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    action = {
        "type": "identity.profile_patch",
        "patch": {"agent_name": "小秘"},
    }
    msg = _make_msg(role="user", content="把你的名字改成小秘", ts=3200.0)

    with patch.object(crc, "call_agent", return_value={"actions": [action], "messages": ["改好了。"]}), \
         patch.object(crc, "execute_agent_actions", side_effect=RuntimeError("write failed")), \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(3200.0)
    mock_post.assert_called_once()
    assert "没能" in mock_post.call_args.args[0]
    assert mock_post.call_args.args[0] != "改好了。"


def test_process_messages_executes_memory_actions_before_reply():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    events = []
    action = {
        "type": "memory.content_patch",
        "memory_id": "mom_1",
        "patch": {"description": "corrected"},
    }
    msg = _make_msg(role="user", content="这张记忆改一下", ts=3300.0)

    def _execute(actions):
        events.append(("actions", actions))
        return {"status": "ok", "effects": [{"type": "memory_updated"}], "results": []}

    def _post(reply, **kwargs):
        events.append(("reply", reply))
        return {"id": "msg_1"}

    with patch.object(crc, "call_agent", return_value={"actions": [action], "messages": ["改好了。"]}), \
         patch.object(crc, "execute_agent_actions", side_effect=_execute), \
         patch.object(crc, "post_reply", side_effect=_post):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(3300.0)
    assert events[0] == ("actions", [action])
    assert events[1] == ("reply", "改好了。")


# ---------------------------------------------------------------------------
# Phase 3: decrypt source unavailable cases
# ---------------------------------------------------------------------------

def test_empty_content_decrypt_source_available_replies(monkeypatch):
    """poll returns content="" but decrypt source is available and returns
    plaintext — consumer must reply using the decrypted content."""
    # Simulate poll returning empty-content message
    empty_msg = _make_msg(role="user", content="", ts=4000.0)
    # Decrypt source returns the plaintext version
    decrypted_msg = _make_msg(role="user", content="what's the weather?", ts=4000.0)

    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "https://127.0.0.1:5003")
    monkeypatch.setattr(
        crc, "get_decrypted_history",
        lambda since, limit=20: [decrypted_msg],
    )

    with patch.object(crc, "call_agent", return_value="sunny") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        # Consumer uses get_decrypted_history result, not the empty poll message
        result_ts = crc._process_messages([decrypted_msg])

    mock_agent.assert_called_once()
    assert mock_agent.call_args[0][0].endswith("what's the weather?")  # time anchor prepended
    mock_post.assert_called_once()
    assert result_ts == pytest.approx(4000.0)


def test_empty_content_no_decrypt_source_no_reply_no_fallback(monkeypatch):
    """poll returns content="" and no decrypt source is configured —
    consumer must skip the message silently (no reply, no fallback)."""
    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "")

    msg = _make_msg(role="user", content="", ts=5000.0)

    with patch.object(crc, "call_agent") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    mock_agent.assert_not_called()
    mock_post.assert_not_called()
    assert result_ts == pytest.approx(5000.0)


def test_agent_failure_posts_visible_fallback_by_default(monkeypatch):
    """Agent backend failure must not silently drop a user turn: the visible
    fallback reply is still posted, and (new behavior) a separate system
    notice carrying the technical reason is posted alongside it via
    ``_notify_agent_turn_failure``."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)

    with patch.object(crc, "call_agent", side_effect=RuntimeError("agent down")), \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([
            {"id": "agent-failure-1", "role": "user", "content": "msg1", "ts": 100.0}
        ])

    fallback_calls = [c for c in mock_post.call_args_list if c.kwargs.get("role") != "system"]
    notice_calls = [c for c in mock_post.call_args_list if c.kwargs.get("role") == "system"]

    # Original contract, unweakened: the user-visible fallback must still go out.
    assert len(fallback_calls) == 1
    assert fallback_calls[0].args[0] == crc.FALLBACK_REPLY
    assert fallback_calls[0].kwargs["reply_to_message_id"] == "agent-failure-1"

    # New contract: a suppressed-push system notice with the technical reason.
    assert len(notice_calls) == 1
    assert notice_calls[0].kwargs["notice_kind"] == "upstream_error"
    assert notice_calls[0].kwargs["suppress_push"] is True
    assert "agent down" in notice_calls[0].args[0]

    assert mock_post.call_count == 2
    assert result_ts == pytest.approx(100.0)


def test_no_error_notice_when_fallback_rejected_already_answered(monkeypatch):
    """Failover 去重（Codex review）：claim 过期后另一家已回复，本家兜底被
    already_answered 409 拒 → system 错误通知也必须一并压掉，不许出现
    重复的技术错误气泡。通知只跟随被服务端接受的那次回复。"""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: True)
    crc._reset_system_notice_state()
    calls = []

    def fake_post(reply, **kw):
        calls.append((reply, kw))
        if kw.get("role") == "system":
            return {}
        return {"error": "already_answered", "reply_status": "replied"}

    with patch.object(crc, "call_agent", side_effect=RuntimeError("agent down")), \
         patch.object(crc, "post_reply", side_effect=fake_post):
        crc._process_messages([
            {"id": "agent-failure-409-1", "role": "user", "content": "msg1", "ts": 100.0}
        ])

    # 兜底尝试发出但被拒；system 通知绝不能跟着发出去。
    assert [kw.get("role") for _, kw in calls] == [None]


def test_agent_failure_reported_even_with_fallback_disabled(monkeypatch):
    """SEND_FALLBACK_ON_AGENT_ERROR=false 只关兜底话术，不关错误透出（Codex
    review）：前台失败仍要发 system 通知 + 上报设置页 runtime_error，只是
    没有 FALLBACK_REPLY、该回合照旧静默跳过。"""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", False)
    reported = []
    monkeypatch.setattr(crc, "_report_runtime_error",
                        lambda *a, **kw: reported.append(a) or True)
    crc._reset_system_notice_state()

    with patch.object(crc, "call_agent", side_effect=RuntimeError("agent down")), \
         patch.object(crc, "post_reply") as mock_post:
        crc._process_messages([
            {"id": "agent-failure-nofb-1", "role": "user", "content": "msg1", "ts": 100.0}
        ])

    # 没有兜底话术……
    fallback_calls = [c for c in mock_post.call_args_list if c.kwargs.get("role") != "system"]
    assert fallback_calls == []
    # ……但 system 通知和设置页上报都要发。
    notice_calls = [c for c in mock_post.call_args_list if c.kwargs.get("role") == "system"]
    assert len(notice_calls) == 1
    assert "agent down" in notice_calls[0].args[0]
    assert len(reported) == 1


def test_agent_failure_fallback_posts_every_turn_banner_windowed(monkeypatch):
    """Each failed user turn still receives its visible in-chat fallback (never a
    silent drop), but the system error banner is rate-limited (Seven 2026-07-11):
    same-class foreground failures inside FOREGROUND_NOTICE_WINDOW_SEC post only
    ONE banner — a user chatting through a bad patch must not get a banner per
    message. So two failed turns = two fallbacks + one banner."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    crc._reset_system_notice_state()

    with patch.object(crc, "call_agent", side_effect=RuntimeError("agent down")), \
         patch.object(crc, "post_reply") as mock_post:
        crc._process_messages([
            {"id": "agent-failure-2", "role": "user", "content": "msg1", "ts": 101.0},
            {"id": "agent-failure-3", "role": "user", "content": "msg2", "ts": 102.0},
        ])

    fallback_calls = [c for c in mock_post.call_args_list if c.kwargs.get("role") != "system"]
    notice_calls = [c for c in mock_post.call_args_list if c.kwargs.get("role") == "system"]

    assert [c.args[0] for c in fallback_calls] == [crc.FALLBACK_REPLY, crc.FALLBACK_REPLY]

    assert len(notice_calls) == 1
    c = notice_calls[0]
    assert c.kwargs["notice_kind"] == "upstream_error"
    assert c.kwargs["suppress_push"] is True
    assert "agent down" in c.args[0]

    assert mock_post.call_count == 3


def test_sanitize_reply_text_strips_leaks_and_duplicates():
    raw = """— ✵ Hermes

------
在，Seven。
在，Seven。
我在这儿，直接说你要我现在做什么。
Reasoning:
"""
    cleaned = crc._sanitize_reply_text(raw)
    assert "Hermes" not in cleaned
    assert "Reasoning" not in cleaned
    assert cleaned == "在，Seven。\n我在这儿，直接说你要我现在做什么。"


def test_sanitize_reply_text_prefers_cjk_and_drops_english_reasoning():
    raw = """I need to interpret this as a greeting.
I'm thinking a warm tone is best.
在呢，Seven。
你继续说，我在听。"""
    cleaned = crc._sanitize_reply_text(raw)
    assert cleaned == "在呢，Seven。\n你继续说，我在听。"


def test_sanitize_reply_text_pure_english_reasoning_returns_empty():
    raw = """The user wrote \"甜！\" which likely means they are giving a compliment.
I think it's best to respond warmly and playfully."""
    cleaned = crc._sanitize_reply_text(raw)
    assert cleaned == ""


def test_sanitize_reply_text_allows_direct_english_reply():
    raw = """Hello Seven — I see your message now.
Tell me what you want to work on next."""
    cleaned = crc._sanitize_reply_text(raw)
    assert cleaned == "Hello Seven — I see your message now.\nTell me what you want to work on next."


def test_sanitize_reply_text_drops_unlabeled_english_meta_before_cjk_answer():
    raw = """specific tool is required for this factual question, so I can rely on my memory
or general knowledge up to 2024. I remember Philip Daian as an Ethereum researcher
who analyzed the DAO exploit. I'll craft a concise answer.
Philip Daian 主要是把「区块链里看不见的交易操控」这件事讲明白、定义清楚，并推动行业修它。
最核心的几件事：
1) 把 MEV 这件事系统化
2) 揭示 DEX 和链上交易排序的结构性风险
"""
    cleaned = crc._sanitize_reply_text(raw)
    assert "specific tool is required" not in cleaned
    assert "general knowledge up to 2024" not in cleaned
    assert cleaned.startswith("Philip Daian 主要是")
    assert "把 MEV 这件事系统化" in cleaned


def test_extract_cli_output_preserves_full_answer_after_reasoning_block():
    raw = """💭 Reasoning:
```copy
**Executing updates**
I need to locate the repo root and think through the answer.
It's important to keep this concise.
```

我看到了。

这张图里有一张搜索结果卡片，下面还有一条学术资料链接。
Project founder
Research profile

如果你愿意，我可以继续帮你拆图里的关键信息。

session_id: sess_123
"""
    extracted = crc._extract_text_from_cli_output(raw)
    cleaned = crc._sanitize_reply_text(extracted)

    assert "Reasoning" not in cleaned
    assert "I need to" not in cleaned
    assert "Project founder" in cleaned
    assert "Research profile" in cleaned
    assert "如果你愿意" in cleaned


def test_agent_turn_splits_tagged_thinking_from_cli_text():
    raw = """<think>
比较了用户最新问题和已有上下文。
</think>

这是最终回复。"""

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["这是最终回复。"]
    assert turn.thinking_summary == "比较了用户最新问题和已有上下文。"
    # Inlined <think> is display material, not provider-native reasoning.
    assert turn.thinking_kind == "provider_reasoning_summary"
    assert turn.thinking_source == "tagged_content"
    assert turn.thinking_native is False


def test_agent_turn_splits_reasoning_and_thought_tags_from_cli_text():
    raw = """<reasoning>先查记忆。</reasoning>
<thought>再组织语气。</thought>
好，我在。"""

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["好，我在。"]
    assert turn.thinking_summary == "先查记忆。\n再组织语气。"
    assert turn.thinking_kind == "provider_reasoning_summary"
    assert turn.thinking_source == "tagged_content"
    assert turn.thinking_native is False


def test_agent_turn_native_reasoning_wins_over_tagged_content():
    raw = {
        "reply": "<think>内联摘要。</think>\n最终回复。",
        "reasoning_content": "原生 reasoning 摘要。",
        "reasoning_source": "openrouter",
    }

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["最终回复。"]
    assert turn.thinking_summary == "原生 reasoning 摘要。"
    assert turn.thinking_source == "openrouter"
    assert turn.thinking_native is True


def test_call_agent_passes_message_without_thinking_protocol(monkeypatch):
    captured = {}

    def _fake_http(message, images=None, raw_text=False):
        captured["message"] = message
        return "ok"

    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(crc, "call_agent_http", _fake_http)

    assert crc.call_agent("hi")["messages"] == ["ok"]
    assert captured["message"] == "hi"

    captured.clear()
    assert crc.call_agent("hi", raw_text=True) == "ok"
    assert captured["message"] == "hi"


def test_extract_cli_output_prefers_structured_json_reply():
    raw = json.dumps(
        {
            "session_id": "sess_json",
            "reasoning": "internal text that must never be shown",
            "reply": "在，Seven。\n我看到了你发来的屏幕。",
        },
        ensure_ascii=False,
    )

    assert crc._extract_text_from_cli_output(raw) == "在，Seven。\n我看到了你发来的屏幕。"
    assert crc._extract_session_id(raw) == "sess_json"


def test_extract_cli_output_reads_jsonl_final_answer():
    raw = """
debug line for human terminal
{"event":"thinking","content":"do not use this"}
{"session_id":"sess_jsonl","final_answer":"这是最终回复。"}
"""

    assert crc._extract_text_from_cli_output(raw) == "这是最终回复。"
    assert crc._extract_session_id(raw) == "sess_jsonl"


def test_extract_cli_output_ignores_non_final_json_events():
    raw = """
{"session_id":"sess_jsonl","final_answer":"这是最终回复。"}
{"event":"thinking","content":"do not use this even if it appears last"}
"""

    assert crc._extract_text_from_cli_output(raw) == "这是最终回复。"
    assert crc._extract_session_id(raw) == "sess_jsonl"


def test_extract_cli_output_reads_openai_style_json():
    raw = json.dumps(
        {"choices": [{"message": {"content": "structured reply"}}]},
        ensure_ascii=False,
    )

    assert crc._extract_text_from_cli_output(raw) == "structured reply"


def test_extract_cli_output_reads_claude_code_json_result():
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "123e4567-e89b-12d3-a456-426614174000",
            "result": "在，我接上了。",
        },
        ensure_ascii=False,
    )

    assert crc._extract_text_from_cli_output(raw) == "在，我接上了。"
    assert crc._extract_session_id(raw) == "123e4567-e89b-12d3-a456-426614174000"


def test_extract_cli_output_strips_resumed_session_banner():
    raw = """↻ Resumed session
20260522_222908_60d12e (1 user message, 2 total messages)
在，Seven。
要我现在用「小哆啦」模式，陪你过一遍今天最关键的三件事吗？
"""
    extracted = crc._extract_text_from_cli_output(raw)
    cleaned = crc._sanitize_reply_text(extracted)

    assert "Resumed session" not in cleaned
    assert "20260522_222908_60d12e" not in cleaned
    assert "total messages" not in cleaned
    assert cleaned == "在，Seven。\n要我现在用「小哆啦」模式，陪你过一遍今天最关键的三件事吗？"


def test_sanitize_mixed_cli_output_drops_leading_non_cjk_block_and_duplicate_answer():
    raw = """Some unlabelled English transcript text from a CLI wrapper.
It may span multiple lines before the actual answer starts.
└──────────────────────────────────────────────────────────────────────────────┘
看到了，这次可以明确说：我已经看到了你共享出来的屏幕内容（至少这一帧）。
我看到的是一张社交平台帖子详情页（界面像小红书），主题在讲 Mentra Live 智能眼镜。
所以结论是：能看到你共享的画面内容了。
看到了，这次可以明确说：我已经看到了你共享出来的屏幕内容（至少这一帧）。
我看到的是一张社交平台帖子详情页（界面像小红书），主题在讲 Mentra Live 智能眼镜。
所以结论是：能看到你共享的画面内容了。
但严格说这是“已收到并读到共享帧”，不是持续实时遥控视角。
"""
    cleaned = crc._sanitize_reply_text(raw)

    assert "Some unlabelled English transcript" not in cleaned
    assert "actual answer starts" not in cleaned
    assert "└" not in cleaned
    assert cleaned.count("看到了，这次可以明确说") == 1
    assert "Mentra Live 智能眼镜" in cleaned
    assert cleaned.endswith("不是持续实时遥控视角。")


def test_normalize_agent_replies_supports_messages_array_json():
    raw = '{"messages":["在。","我在听。","继续说。"]}'
    out = crc._normalize_agent_replies(raw)
    assert out == ["在。", "我在听。", "继续说。"]


def test_extract_session_id_from_cli_output():
    raw = "some line\nsession_id: 20260503_024038_b526cf\n"
    assert crc._extract_session_id(raw) == "20260503_024038_b526cf"


def test_resolve_cli_executable_uses_agent_cli_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "hermes"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)

    monkeypatch.setattr(crc, "AGENT_CLI_PATH", str(bin_dir))
    monkeypatch.setenv("PATH", "")

    resolved = crc._resolve_cli_executable(["hermes", "chat"])
    assert resolved == [str(exe), "chat"]


def test_resolve_cli_executable_error_mentions_systemd(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_PATH", "")
    monkeypatch.setenv("PATH", "")

    with pytest.raises(FileNotFoundError, match="systemd service"):
        crc._resolve_cli_executable(["missing-agent", "chat"])


def test_agent_session_file_scoped_by_user_id(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_SESSION_FILE_TEMPLATE", "/tmp/feedling_{user_id}.txt")
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_abc", "user_pk": None, "enclave_pk": None})
    p = crc._agent_session_file_for_user()
    assert str(p) == "/tmp/feedling_usr_abc.txt"


def test_agent_session_meta_rotates_when_turn_bound_reached(monkeypatch, tmp_path):
    crc._agent_session_id_cache.clear()
    crc._agent_session_meta_cache.clear()
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_bounded", "user_pk": None, "enclave_pk": None})
    monkeypatch.setattr(crc, "AGENT_SESSION_FILE_TEMPLATE", str(tmp_path / "feedling_{user_id}.json"))
    monkeypatch.setattr(crc, "AGENT_SESSION_MAX_TURNS", 2)
    monkeypatch.setattr(crc, "AGENT_SESSION_MAX_BYTES", 0)

    crc._save_agent_session_id("sess_a")
    crc._record_agent_session_turn("sess_a", sent_bytes=10, received_bytes=5)
    assert crc._load_agent_session_id() == "sess_a"

    crc._record_agent_session_turn("sess_a", sent_bytes=10, received_bytes=5)

    assert crc._load_agent_session_id() == ""
    assert not crc._agent_session_file_for_user().exists()


def test_prepare_cli_replaces_fixed_session_id_after_rotation(monkeypatch, tmp_path):
    crc._agent_session_id_cache.clear()
    crc._agent_session_meta_cache.clear()
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_openclaw", "user_pk": None, "enclave_pk": None})
    monkeypatch.setattr(crc, "AGENT_SESSION_FILE_TEMPLATE", str(tmp_path / "feedling_{user_id}.json"))
    monkeypatch.setattr(crc, "AGENT_SESSION_MAX_TURNS", 1)
    monkeypatch.setattr(crc, "AGENT_SESSION_MAX_BYTES", 0)
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'openclaw agent --json --session-id feedling-io-seven "{message}"')
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    crc._save_agent_session_id("feedling-old")
    crc._record_agent_session_turn("feedling-old", sent_bytes=100, received_bytes=100)

    cmd = crc._prepare_cli_command("hello")
    sid = crc._cli_flag_value(cmd, "--session-id")

    assert sid.startswith("feedling-io-")
    assert sid not in {"feedling-io-seven", "feedling-old"}
    assert "hello" in cmd


def test_prepare_hermes_cli_strips_continue_and_injects_resume(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --continue --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_123")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert "--continue" not in cmd
    assert cmd[:4] == ["hermes", "--resume", "sess_123", "chat"]
    assert "hello" in cmd


def test_prepare_hermes_cli_injects_resume_before_chat_with_top_level_flags(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes --yolo chat -Q --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_123")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert cmd[:5] == ["hermes", "--resume", "sess_123", "--yolo", "chat"]
    assert "hello" in cmd


def test_prepare_hermes_cli_strips_unsupported_output_mode(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat --output-mode json -Q --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_123")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert "--output-mode" not in cmd
    assert "json" not in cmd
    assert cmd[:4] == ["hermes", "--resume", "sess_123", "chat"]
    assert "hello" in cmd


def test_prepare_hermes_cli_first_turn_removes_continue(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --continue --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert "--continue" not in cmd
    assert "--resume" not in cmd


def _setup_hermes_session_cli(monkeypatch, tmp_path, *, session_id: str, session_doc: str | None):
    crc._agent_session_id_cache.clear()
    crc._agent_session_meta_cache.clear()
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_hermes", "user_pk": None, "enclave_pk": None})
    monkeypatch.setattr(crc, "AGENT_SESSION_FILE_TEMPLATE", str(tmp_path / "feedling_{user_id}.json"))
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    hermes_home = tmp_path / "hermes_home"
    sessions = hermes_home / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    if session_doc is not None:
        (sessions / f"session_{session_id}.json").write_text(session_doc, encoding="utf-8")

    class _R:
        returncode = 0
        stdout = f"在。\nsession_id: {session_id}\n"
        stderr = ""

    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _R())


def test_call_agent_cli_hermes_reads_native_reasoning_from_session_json(monkeypatch, tmp_path):
    session_doc = json.dumps({
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "older", "reasoning": "older reasoning"},
            {
                "role": "assistant",
                "content": "在。",
                "reasoning": "**Checking token validity**\nI'm checking the current session before replying.",
                "codex_reasoning_items": [{"type": "reasoning", "encrypted_content": "gAAAA"}],
            },
        ],
    })
    _setup_hermes_session_cli(monkeypatch, tmp_path, session_id="sess_reasoning", session_doc=session_doc)

    raw = crc.call_agent_cli("hi")
    turn = crc._agent_turn_from_raw(raw)

    assert turn.messages == ["在。"]
    assert "current session before replying" in turn.thinking_summary
    assert "older reasoning" not in turn.thinking_summary
    assert turn.thinking_kind == "provider_reasoning"
    assert turn.thinking_source == "hermes_session_json"
    assert turn.thinking_native is True


@pytest.mark.parametrize("session_doc", [
    json.dumps({"messages": [{"role": "assistant", "content": "在。", "reasoning": None}]}),
    None,
    "{not valid json",
])
def test_call_agent_cli_hermes_skips_missing_bad_or_null_reasoning(monkeypatch, tmp_path, session_doc):
    _setup_hermes_session_cli(monkeypatch, tmp_path, session_id="sess_skip", session_doc=session_doc)

    raw = crc.call_agent_cli("hi")
    turn = crc._agent_turn_from_raw(raw)

    assert turn.messages == ["在。"]
    assert turn.thinking_summary == ""


def test_hermes_session_reasoning_skips_oversized_file(monkeypatch, tmp_path):
    session_id = "sess_big"
    hermes_home = tmp_path / "hermes_home"
    sessions = hermes_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"session_{session_id}.json").write_text(
        json.dumps({"messages": [{"role": "assistant", "reasoning": "too large"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(crc, "HERMES_SESSION_REASONING_MAX_BYTES", 8)

    assert crc._hermes_session_reasoning(session_id) == ""


def test_prepare_claude_cli_first_turn_forces_print_json_and_strips_continue(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'claude --continue "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert "--continue" not in cmd
    assert "--resume" not in cmd
    assert "--print" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "hello" in cmd


def test_prepare_claude_cli_injects_stored_resume(monkeypatch):
    # Resume is the fallback continuity path, kept only when foreground history
    # injection is disabled. With injection on (the auto default for claude) the
    # resident drops --resume — see test_claude_resume_injection_skipped_*.
    sid = "123e4567-e89b-12d3-a456-426614174000"
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'claude -p "{message}"',
    )
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "off")
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: sid)
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert cmd[:3] == ["claude", "--resume", sid]
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "hello" in cmd


def test_warn_if_hermes_cli_may_drift_logs_profile_and_turns(monkeypatch, caplog):
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --max-turns 1 -q "You are Dora. User message: {message}"',
    )
    monkeypatch.delenv("HERMES_HOME", raising=False)

    crc._warn_if_agent_entry_may_drift()

    text = caplog.text
    assert "wrap {message}" in text
    assert "without HERMES_HOME" in text
    assert "--max-turns 1" in text


def test_warn_if_hermes_cli_good_profile_is_quiet(monkeypatch, caplog):
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setenv("HERMES_HOME", "/home/openclaw/.hermes/profiles/daily")

    crc._warn_if_agent_entry_may_drift()

    assert "wrap {message}" not in caplog.text
    assert "without HERMES_HOME" not in caplog.text
    assert "Very small turn" not in caplog.text


def test_prepare_cli_preserves_message_with_quotes(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command('say "hello" now')

    assert cmd == ["mycli", "ask", 'say "hello" now']


def test_prepare_cli_appends_image_path_when_template_has_no_image_slot(monkeypatch, tmp_path):
    image_path = str(tmp_path / "photo.jpg")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("look at this", image_paths=[image_path])

    assert cmd[:2] == ["mycli", "ask"]
    assert "look at this" in cmd[2]
    assert image_path in cmd[2]


def test_message_for_agent_steers_claude_to_read_not_authorize():
    # For claude (no native --image), the ONLY way pixels reach the model is the Read
    # tool on the injected path. Live transcripts showed the model either (a) reaching
    # for io_cli photo-recent instead of Read, or (b) refusing with a hallucinated
    # "click allow to authorize" flow (there is no such UI) and then FABRICATING the
    # image contents. The prose must: name the Read tool, assert permission is already
    # granted (no approval step), and forbid asking the user to authorize.
    out = crc._message_for_agent("what is this", ["/agent-data/users/u/images/x.jpg"])
    assert "/agent-data/users/u/images/x.jpg" in out
    assert "Read" in out  # names the tool to use
    low = out.lower()
    # must not instruct the model to seek authorization / a click-to-allow step
    assert "authoriz" in low or "no approval" in low or "already have permission" in low
    # steer away from the historical-photo tools for a live attachment
    assert "photo-recent" in out or "photo-read" in out


def test_prepare_cli_uses_image_path_template(monkeypatch, tmp_path):
    image_path = str(tmp_path / "photo.jpg")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask --image "{image_path}" "{message}"')
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("look at this", image_paths=[image_path])

    assert cmd == ["mycli", "ask", "--image", image_path, "look at this"]


def test_prepare_cli_injects_codex_image_flags(monkeypatch, tmp_path):
    # codex exec supports `-i/--image <FILE>`; the default managed codex template
    # has no image slot, so the resident must attach the decrypted image files
    # natively — otherwise the model only ever sees a text file-path (can't see it).
    img1 = str(tmp_path / "a.jpg")
    img2 = str(tmp_path / "b.jpg")
    monkeypatch.setattr(
        crc, "AGENT_CLI_CMD",
        "codex exec --skip-git-repo-check --json {message}",
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("look", image_paths=[img1, img2])

    # each image attached via the =-bound --image form, so clap's variadic
    # --image cannot swallow the positional prompt (see the regression test below)
    assert cmd.count(f"--image={img1}") == 1
    assert cmd.count(f"--image={img2}") == 1
    # image flags sit after the `exec` subcommand
    assert cmd.index("exec") < cmd.index(f"--image={img1}")
    # the message stays clean — no misleading "Decrypted image file(s)" path prose
    assert "Decrypted image file(s)" not in " ".join(cmd)
    assert "look" in cmd


def test_prepare_cli_codex_image_flags_do_not_swallow_prompt(monkeypatch, tmp_path):
    # Regression (Codex review P2): with a minimal `codex exec {message}` template
    # the prompt immediately follows the injected image flags. A bare `-i <path>`
    # would let clap's variadic --image consume the prompt token as another image;
    # the =-bound form keeps the prompt a standalone positional.
    img = str(tmp_path / "a.jpg")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", "codex exec {message}")
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello there", image_paths=[img])

    assert cmd == ["codex", "exec", f"--image={img}", "hello there"]
    # never a bare `-i` whose value the prompt could be mistaken for
    assert "-i" not in cmd


def test_prepare_cli_codex_respects_explicit_image_flag(monkeypatch, tmp_path):
    # A VPS operator who already wired {image_path} into their codex template owns
    # image handling; the resident must not double-inject a second -i.
    img = str(tmp_path / "a.jpg")
    monkeypatch.setattr(
        crc, "AGENT_CLI_CMD",
        "codex exec -i {image_path} --json {message}",
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("look", image_paths=[img])

    assert cmd == ["codex", "exec", "-i", img, "--json", "look"]


def test_cli_nonzero_exit_fails_even_with_stdout(monkeypatch):
    class _Result:
        returncode = 2
        stdout = "stale text that must not be posted"
        stderr = "bad command"

    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": ["mycli", "ask", message])
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _Result())

    with pytest.raises(RuntimeError, match="cli agent exited 2"):
        crc.call_agent_cli("hi")


def test_cli_failure_surfaces_claude_json_error_from_stdout(monkeypatch):
    # claude --output-format json reports API failures on STDOUT (is_error+result)
    # with stderr empty/just a warning. The raised error must carry the real
    # reason so `cli agent exited 1:` is diagnosable, not blank.
    class _Result:
        returncode = 1
        stdout = '{"type":"result","is_error":true,"api_error_status":429,"result":"Overloaded"}'
        stderr = ""

    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'claude -p {message}')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": ["claude", "-p", message])
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _Result())

    with pytest.raises(RuntimeError) as ei:
        crc.call_agent_cli("hi")
    assert "Overloaded" in str(ei.value)
    assert "429" in str(ei.value)


def test_cli_failure_surfaces_codex_stream_error_from_stdout(monkeypatch):
    # codex --json emits the failure as `error` events on STDOUT; stderr is just
    # the "Reading additional input…" banner. Surface the event message.
    class _Result:
        returncode = 1
        stdout = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"error","message":"unexpected status 401 Unauthorized: Incorrect API key"}\n'
            '{"type":"turn.failed"}'
        )
        stderr = "Reading additional input from stdin..."

    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'codex exec --json {message}')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": ["codex", "exec", "--json", message])
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _Result())

    with pytest.raises(RuntimeError) as ei:
        crc.call_agent_cli("hi")
    assert "401 Unauthorized" in str(ei.value)


def test_openai_http_protocol_uses_session_headers(monkeypatch):
    captured = {}

    class _Resp:
        headers = {"X-Hermes-Session-Id": "sess_new"}
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "choices": [
                    {"message": {"content": "real reply"}}
                ]
            }

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Resp()

    saved = []
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://127.0.0.1:8642/v1/chat/completions")
    monkeypatch.setattr(crc, "AGENT_HTTP_PROTOCOL", "openai")
    monkeypatch.setattr(crc, "AGENT_HTTP_MODEL", "hermes-agent")
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_abc", "user_pk": None, "enclave_pk": None})
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_old")
    monkeypatch.setattr(crc, "_save_agent_session_id", lambda sid: saved.append(sid))
    monkeypatch.setattr(crc.httpx, "post", _post)

    assert crc.call_agent_http("hi") == "real reply"
    assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["headers"]["X-Hermes-Session-Id"] == "sess_old"
    assert captured["headers"]["X-Hermes-Session-Key"] == "feedling:usr_abc"
    assert saved == ["sess_new"]


def test_memory_lane_raw_text_survives_chat_sanitizer(monkeypatch):
    """memory_dream / memory_capture must parse the model's RAW output.

    Prod regression: the agent-runner (http) path routed dream/capture output
    through _sanitize_reply_text, the chat-bubble cleaner. A pretty-printed JSON
    with an ASCII preamble and Chinese values gets decapitated by
    _strip_leading_non_cjk_preamble (all ASCII structure lines before the first
    CJK char are dropped), so the robust _extract_json_block then sees a broken
    fragment -> no_json_object / json_decode_error, and every claimed dream job
    failed. call_agent(..., raw_text=True) must bypass the chat sanitizer.
    """
    from memory.dream_prompt_v1 import parse_dream_consolidations

    dream_json = (
        "Here is the consolidation result:\n"
        "{\n"
        '  "consolidations": [\n'
        "    {\n"
        '      "op": "merge",\n'
        '      "card_ids": ["a", "b"],\n'
        '      "result": {\n'
        '        "bucket": "工作",\n'
        '        "threads": ["加班"],\n'
        '        "summary": "合并卡",\n'
        '        "content": "他最近一直在加班，压力很大",\n'
        '        "importance": 0.7,\n'
        '        "pulse": 0.3\n'
        "      }\n"
        "    }\n"
        "  ],\n"
        '  "questions_to_ask": ["要不要问问他周末有没有休息"]\n'
        "}"
    )

    class _Resp:
        headers: dict = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": dream_json}}]}

    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://127.0.0.1:8642/v1/chat/completions")
    monkeypatch.setattr(crc, "AGENT_HTTP_PROTOCOL", "openai")
    monkeypatch.setattr(crc, "AGENT_HTTP_MODEL", "hermes-agent")
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_abc", "user_pk": None, "enclave_pk": None})
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_save_agent_session_id", lambda sid: None)
    monkeypatch.setattr(crc.httpx, "post", lambda url, json=None, headers=None, timeout=None: _Resp())

    # raw_text path: the literal model output reaches the dream parser intact.
    raw = crc._capture_agent_reply_text(crc.call_agent("dream prompt", raw_text=True))
    cons, questions, err = parse_dream_consolidations(raw)
    assert err is None
    assert len(cons) == 1
    assert cons[0]["op"] == "merge"
    assert cons[0]["result"]["content"] == "他最近一直在加班，压力很大"
    assert questions == ["要不要问问他周末有没有休息"]

    # Guard: the default chat path still mangles it — documents the prod bug and
    # proves raw_text is what fixes it (not some unrelated parser change).
    chat = crc._capture_agent_reply_text(crc.call_agent("dream prompt"))
    _c, _q, chat_err = parse_dream_consolidations(chat)
    assert chat_err is not None


def test_openai_http_protocol_sends_multimodal_image_block(monkeypatch):
    captured = {}

    class _Resp:
        headers = {}
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": "vision reply"}}]}

    def _post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://127.0.0.1:8642/v1/chat/completions")
    monkeypatch.setattr(crc, "AGENT_HTTP_PROTOCOL", "openai")
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc.httpx, "post", _post)

    image = {"data_url": "data:image/jpeg;base64,abcd", "data": "abcd", "mime_type": "image/jpeg"}

    assert crc.call_agent_http("see image", images=[image]) == "vision reply"
    content = captured["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "see image"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abcd"}}


def test_process_proactive_wake_routes_through_agent_and_posts_metadata(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {}

    def _agent(message, images=None, image_paths=None):
        captured["message"] = message
        captured["images"] = images
        captured["image_paths"] = image_paths
        return ["我看到了这个时机。"]

    def _post(reply, **kwargs):
        captured["reply"] = reply
        captured["post_kwargs"] = kwargs
        return {"id": "msg_1"}

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", _post)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured.setdefault("statuses", []).append(
            (job_id, status, reason, kwargs)
        ),
    )
    monkeypatch.setattr(
        crc,
        "_screen_context_for_frame_ids",
        lambda frame_ids: ("screen: user is reading docs", [{"data": "x"}], ["/tmp/frame.jpg"]),
    )
    monkeypatch.setattr(
        crc,
        "recent_chat_context_for_proactive",
        lambda limit=None: "- user: 你刚刚问我这段要不要压成一句话。\n- agent: 我可以帮你压。",
    )

    job = {
        "schema_version": 2,
        "job_id": "pj_1",
        "wake_id": "wake_1",
        "gate_decision_id": "gd_1",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 123.0,
        "trigger": "screen_tick",
        "wake_kind": "screen",
        "user_state": "default",
        "ai_state": "present",
        "broadcast_state": "on",
        "current_app": "Docs",
        "frame_ids": ["frame_1"],
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(123.0)
    assert "Feedling proactive wake" in captured["message"]
    assert "presence check" in captured["message"].lower()
    assert "wake_kind:" in captured["message"]
    assert "wake_kind: screen" in captured["message"]
    assert "recent_chat_context" in captured["message"]
    assert "你刚刚问我这段要不要压成一句话" in captured["message"]
    assert "own runtime identity" in captured["message"]
    assert "screen: user is reading docs" in captured["message"]
    assert "possible_connections" not in captured["message"]
    assert captured["images"] == [{"data": "x"}]
    assert captured["image_paths"] == ["/tmp/frame.jpg"]
    assert captured["reply"] == "我看到了这个时机。"
    assert captured["post_kwargs"] == {
        "source": crc.PROACTIVE_JOB_SOURCE,
        "gate_decision_id": "gd_1",
        "proactive_job_id": "pj_1",
    }
    assert any(s[:3] == ("pj_1", "realizing", "") for s in captured["statuses"])
    assert any(s[0] == "pj_1" and s[1] == "posted" for s in captured["statuses"])


def _install_capture_job_harness(monkeypatch, agent_reply):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    captured = {
        "statuses": [],
        "proactive_called": False,
        "prompts": [],
        "actions": [],
        "envelope_plaintexts": [],
        "envelope_kwargs": [],
    }

    history = [
        {"id": "msg_a", "role": "user", "content": "before window", "ts": 100.0},
        {"id": "msg_b", "role": "user", "content": "这次会议让我压力很大。", "ts": 150.0},
        {"id": "msg_c", "role": "assistant", "content": "我记得你说心率也上来了。", "ts": 200.0},
    ]
    job = {
        "job_id": "cap_dispatch",
        "job_kind": "memory_capture",
        "source": "memory_capture",
        "status": "pending",
        "trigger": "session_break",
        "capture_key": "window:dispatch",
        "window": {
            "after_message_id": "msg_a",
            "until_message_id": "msg_c",
            "until_ts": 200.0,
            "message_count": 2,
        },
        "ts": 222.0,
    }

    def _agent(prompt, *_args, **_kwargs):
        captured["prompts"].append(prompt)
        return agent_reply

    def _fail_post(*_args, **_kwargs):
        raise AssertionError("capture job must not write chat")

    def _proactive_handler(_jobs):
        captured["proactive_called"] = True
        return 999.0

    def _status(job_id, status, reason="", **kwargs):
        captured["statuses"].append((job_id, status, reason, kwargs))

    def _build_envelope(**kwargs):
        captured["envelope_kwargs"].append(kwargs)
        captured["envelope_plaintexts"].append(json.loads(kwargs["plaintext"].decode("utf-8")))
        return {
            "v": 1,
            "id": f"env_{len(captured['envelope_plaintexts'])}",
            "visibility": kwargs["visibility"],
            "owner_user_id": kwargs["owner_user_id"],
        }

    def _memory_actions(actions):
        captured["actions"].extend(actions)
        return {
            "status": "ok",
            "results": [{"status": "ok", "action": action.get("type")} for action in actions],
            "effects": [{"type": "memory_added"} for _action in actions],
        }

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", _fail_post)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(crc, "update_proactive_job_status", _status)
    monkeypatch.setattr(crc, "_process_proactive_jobs", _proactive_handler)
    monkeypatch.setattr(crc, "get_decrypted_history", lambda since, limit=20: history)
    monkeypatch.setattr(crc, "_capture_memory_terms_context", lambda: ("buckets: work", "threads: meeting"))
    monkeypatch.setattr(
        crc,
        "_capture_identity_context",
        lambda: (
            {"agent_name": "IO", "user_preferred_name": "Seven"},
            "IO",
            "Seven",
            "identity: IO is Seven's companion",
        ),
    )
    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_capture", "user_pk": b"u" * 32, "enclave_pk": b"e" * 32},
    )
    monkeypatch.setattr(crc, "_refresh_whoami_for_encrypted_reply", lambda: True)
    monkeypatch.setattr(crc, "_build_envelope", _build_envelope)
    monkeypatch.setattr(crc, "execute_memory_actions", _memory_actions)
    return captured, job


def _capture_final_status(captured):
    return captured["statuses"][-1]


def _install_dream_job_harness(monkeypatch, agent_reply):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    captured = {
        "statuses": [],
        "proactive_called": False,
        "prompts": [],
        "actions": [],
        "envelope_plaintexts": [],
        "envelope_kwargs": [],
        "post_called": False,
    }

    history = [
        {"id": "msg_dream_a", "role": "user", "content": "我最近都喝燕麦奶。", "ts": 300.0},
        {"id": "msg_dream_b", "role": "assistant", "content": "我记住了。", "ts": 360.0},
    ]
    index_items = [
        {"id": "mem_a", "summary": "Seven likes oat milk.", "bucket": "life", "threads": ["coffee"]},
        {"id": "mem_b", "summary": "Seven often orders oat latte.", "bucket": "life", "threads": ["coffee"]},
        {"id": "mem_c", "summary": "Seven drinks coffee in the morning.", "bucket": "routine", "threads": ["coffee"]},
    ]
    fetch_items = [
        {**item, "content": item["summary"] + " Full card."}
        for item in index_items
    ]
    job = {
        "job_id": "dream_dispatch",
        "job_kind": "memory_dream",
        "source": "memory_dream",
        "status": "pending",
        "trigger": "nightly_dream",
        "dream_key": "dream:dispatch",
        "dream_stats": {"card_count": 3, "new_cards": 3},
        "dream_until": {"signature": "sig_dispatch"},
        "ts": 333.0,
    }

    def _agent(prompt, *_args, **_kwargs):
        captured["prompts"].append(prompt)
        return agent_reply

    def _fail_post(*_args, **_kwargs):
        captured["post_called"] = True
        raise AssertionError("dream job must not write chat")

    def _proactive_handler(_jobs):
        captured["proactive_called"] = True
        return 999.0

    def _status(job_id, status, reason="", **kwargs):
        captured["statuses"].append((job_id, status, reason, kwargs))

    def _post_json(path, *, payload=None, **_kwargs):
        if path == "/v1/memory/index":
            return {"items": index_items}
        if path == "/v1/memory/fetch":
            ids = set((payload or {}).get("ids") or [])
            return {"items": [item for item in fetch_items if item["id"] in ids]}
        return {}

    def _build_envelope(**kwargs):
        captured["envelope_kwargs"].append(kwargs)
        captured["envelope_plaintexts"].append(json.loads(kwargs["plaintext"].decode("utf-8")))
        return {
            "v": 1,
            "id": f"dream_env_{len(captured['envelope_plaintexts'])}",
            "visibility": kwargs["visibility"],
            "owner_user_id": kwargs["owner_user_id"],
            "body_ct": "ct",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
        }

    def _memory_actions(actions):
        captured["actions"].extend(actions)
        return {
            "status": "ok",
            "results": [{"status": "ok", "action": action.get("type")} for action in actions],
            "effects": [{"type": "memory_superseded"} for _action in actions],
        }

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", _fail_post)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(crc, "update_proactive_job_status", _status)
    monkeypatch.setattr(crc, "_process_proactive_jobs", _proactive_handler)
    monkeypatch.setattr(crc, "_capture_post_json", _post_json)
    monkeypatch.setattr(crc, "get_decrypted_history", lambda since, limit=20: history)
    monkeypatch.setattr(
        crc,
        "_capture_identity_context",
        lambda: (
            {"agent_name": "IO", "user_preferred_name": "Seven"},
            "IO",
            "Seven",
            "identity: IO is Seven's companion",
        ),
    )
    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_dream", "user_pk": b"u" * 32, "enclave_pk": b"e" * 32},
    )
    monkeypatch.setattr(crc, "_refresh_whoami_for_encrypted_reply", lambda: True)
    monkeypatch.setattr(crc, "_build_envelope", _build_envelope)
    monkeypatch.setattr(crc, "execute_memory_actions", _memory_actions)
    return captured, job


def _dream_final_status(captured):
    return captured["statuses"][-1]


def test_capture_get_json_disables_tls_verification_for_enclave_only(monkeypatch):
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def _get(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp()

    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "https://enclave.local")
    monkeypatch.setattr(crc, "FEEDLING_API_URL", "https://backend.local")
    monkeypatch.setattr(crc.httpx, "get", _get)

    assert crc._capture_get_json("/v1/identity/get", base_url="https://enclave.local") == {"ok": True}
    assert calls[-1][0] == "https://enclave.local/v1/identity/get"
    assert calls[-1][1]["verify"] is False

    assert crc._capture_get_json("/v1/memory/buckets") == {"ok": True}
    assert calls[-1][0] == "https://backend.local/v1/memory/buckets"
    assert calls[-1][1]["verify"] is True


def test_capture_json_helpers_refresh_runtime_token_before_each_request(monkeypatch, tmp_path):
    calls = []
    token_file = tmp_path / "runtime.jwt"
    token_file.write_text("fresh-token")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def _get(url, **kwargs):
        calls.append(("GET", url, dict(kwargs.get("headers") or {})))
        return _Resp()

    def _post(url, **kwargs):
        calls.append(("POST", url, dict(kwargs.get("headers") or {})))
        return _Resp()

    monkeypatch.setattr(crc, "FEEDLING_RUNTIME_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(crc, "_runtime_token_exp", lambda token: time.time() + 60)
    monkeypatch.setitem(crc._HEADERS, "X-API-Key", "stale-api-key")
    crc._HEADERS.pop("X-Feedling-Runtime-Token", None)
    monkeypatch.setattr(crc.httpx, "get", _get)
    monkeypatch.setattr(crc.httpx, "post", _post)

    assert crc._capture_get_json("/v1/memory/buckets") == {"ok": True}
    assert crc._capture_post_json("/v1/memory/legacy_batch", payload={"batch_size": 8}) == {"ok": True}
    assert calls[0][2].get("X-Feedling-Runtime-Token") == "fresh-token"
    assert calls[1][2].get("X-Feedling-Runtime-Token") == "fresh-token"
    assert "X-API-Key" not in calls[0][2]
    assert "X-API-Key" not in calls[1][2]


def test_migrate_job_fails_when_legacy_batch_response_missing(monkeypatch):
    job = {
        "job_id": "migr_missing_batch",
        "job_kind": "memory_migrate",
        "source": "memory_migrate",
        "status": "pending",
        "migrate_key": "migrate:v1:u:w1",
        "ts": 123.0,
    }
    statuses = []

    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(crc, "update_proactive_job_status",
                        lambda job_id, status, reason="", **kwargs: statuses.append((job_id, status, reason, kwargs)))
    monkeypatch.setattr(crc, "_capture_post_json", lambda path, **kwargs: {})
    monkeypatch.setattr(crc, "_seen_ids", set())
    monkeypatch.setattr(crc, "_seen_ids_order", [])
    monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", "1")

    assert crc._process_migrate_jobs([job]) == pytest.approx(123.0)
    assert statuses[0][:3] == ("migr_missing_batch", "realizing", "")
    assert statuses[-1][0] == "migr_missing_batch"
    assert statuses[-1][1] == "failed"
    assert "legacy_batch_unavailable" in statuses[-1][2]
    assert all(row[2] != "migrate_no_legacy" for row in statuses)


def test_capture_identity_context_prefers_enclave_plaintext_and_filters_ciphertext(monkeypatch):
    calls = []

    def _get_json(path, **kwargs):
        calls.append((path, kwargs.get("base_url")))
        return {
            "identity": {
                "agent_name": "IO",
                "user_preferred_name": "Seven",
                "self_introduction": "陪 Seven 一起生活。",
                "dimensions": [{"key": "tone", "value": "direct"}],
                "body_ct": "ciphertext-must-not-enter-prompt",
                "K_user": "wrapped-key-must-not-enter-prompt",
            }
        }

    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "http://enclave.local")
    monkeypatch.setattr(crc, "_capture_get_json", _get_json)

    identity, ai_name, user_name, identity_text = crc._capture_identity_context()

    assert calls == [("/v1/identity/get", "http://enclave.local")]
    assert ai_name == "IO"
    assert user_name == "Seven"
    assert identity["self_introduction"] == "陪 Seven 一起生活。"
    assert "body_ct" not in identity_text
    assert "K_user" not in identity_text
    assert "ciphertext-must-not-enter-prompt" not in identity_text


def test_capture_job_add_card_writes_envelope_without_chat_or_delivery(monkeypatch):
    reply = json.dumps({
        "cards": [{
            "action": "add",
            "type": "event",
            "bucket": "work",
            "threads": ["meeting"],
            "summary": "Seven had a stressful meeting.",
            "content": "Seven said the meeting was stressful and mentioned elevated heart rate.",
            "importance": 0.7,
            "pulse": 0.4,
        }]
    }, ensure_ascii=False)
    captured, job = _install_capture_job_harness(monkeypatch, reply)

    assert crc._process_resident_jobs([job]) == pytest.approx(222.0)
    assert captured["proactive_called"] is False
    assert captured["statuses"][0][:3] == ("cap_dispatch", "realizing", "")
    assert _capture_final_status(captured)[:3] == (
        "cap_dispatch",
        "completed",
        "capture_memory_actions_applied",
    )
    action = captured["actions"][0]
    assert action["type"] == "memory.add"
    assert action["capture_mode"] == "memory_capture"
    assert action["source_chat_message_ids"] == ["msg_b", "msg_c"]
    assert action["envelope"]["visibility"] == "shared"
    assert action["envelope"]["type"] == "event"
    assert action["envelope"]["occurred_at"] == "1970-01-01T00:03:20Z"
    assert action["envelope"]["importance"] == pytest.approx(0.7)
    assert action["envelope"]["pulse"] == pytest.approx(0.4)
    assert action["envelope"]["anchor_memory_ids"] == []
    assert captured["envelope_plaintexts"] == [{
        "summary": "Seven had a stressful meeting.",
        "content": "Seven said the meeting was stressful and mentioned elevated heart rate.",
        "bucket": "work",
        "threads": ["meeting"],
    }]
    assert captured["envelope_kwargs"][0]["visibility"] == "shared"
    assert "buckets: work" in captured["prompts"][0]
    assert "threads: meeting" in captured["prompts"][0]
    assert "identity: IO is Seven's companion" in captured["prompts"][0]
    assert "这次会议让我压力很大" in captured["prompts"][0]
    extra = _capture_final_status(captured)[3]["extra"]
    assert extra["cards_added"] == 1
    assert extra["cards_superseded"] == 0
    assert extra["memory_action_status"] == {"status": "ok", "results": 1, "effects": 1}


def test_capture_job_supersede_card_writes_supersede_action(monkeypatch):
    reply = json.dumps({
        "cards": [{
            "action": "supersede",
            "target_id": "mem_old",
            "type": "event",
            "bucket": "work",
            "threads": ["meeting"],
            "summary": "Seven reframed the meeting stress.",
            "content": "Seven clarified the stressful meeting was mostly about a boundary issue.",
            "importance": 0.8,
            "pulse": 0.6,
        }]
    }, ensure_ascii=False)
    captured, job = _install_capture_job_harness(monkeypatch, reply)

    assert crc._process_resident_jobs([job]) == pytest.approx(222.0)
    action = captured["actions"][0]
    assert action["type"] == "memory.supersede"
    assert action["supersedes"] == "mem_old"
    extra = _capture_final_status(captured)[3]["extra"]
    assert extra["cards_added"] == 0
    assert extra["cards_superseded"] == 1


def test_capture_job_supersede_without_target_falls_back_to_add(monkeypatch):
    reply = json.dumps({
        "cards": [{
            "action": "supersede",
            "type": "event",
            "bucket": "work",
            "threads": ["meeting"],
            "summary": "Missing target.",
            "content": "The agent tried to supersede without naming the old card.",
            "importance": 0.5,
            "pulse": 0.2,
        }]
    }, ensure_ascii=False)
    captured, job = _install_capture_job_harness(monkeypatch, reply)

    assert crc._process_resident_jobs([job]) == pytest.approx(222.0)
    assert captured["actions"][0]["type"] == "memory.add"
    assert _capture_final_status(captured)[:3] == (
        "cap_dispatch",
        "completed",
        "capture_memory_actions_applied",
    )
    extra = _capture_final_status(captured)[3]["extra"]
    assert extra["cards_added"] == 1
    assert extra["cards_superseded"] == 0


def test_capture_job_empty_cards_completes_noop_without_memory_write(monkeypatch):
    captured, job = _install_capture_job_harness(monkeypatch, '{"cards":[]}')

    assert crc._process_resident_jobs([job]) == pytest.approx(222.0)
    assert captured["actions"] == []
    assert _capture_final_status(captured)[:3] == (
        "cap_dispatch",
        "completed",
        "nothing_worth_keeping",
    )
    extra = _capture_final_status(captured)[3]["extra"]
    assert extra["cards_added"] == 0
    assert extra["cards_superseded"] == 0
    assert extra["noop_reason"] == "nothing_worth_keeping"


def test_capture_job_bad_json_fails_without_crash_or_memory_write(monkeypatch):
    captured, job = _install_capture_job_harness(monkeypatch, "not json")

    assert crc._process_resident_jobs([job]) == pytest.approx(222.0)
    assert captured["actions"] == []
    assert _capture_final_status(captured)[:3] == ("cap_dispatch", "failed", "no_json_object")
    assert _capture_final_status(captured)[3]["extra"]["noop_reason"] == "no_json_object"


def test_dream_job_merge_writes_multi_supersede_without_chat_or_delivery(monkeypatch):
    reply = json.dumps({
        "consolidations": [{
            "op": "merge",
            "card_ids": ["mem_a", "mem_b"],
            "result": {
                "bucket": "life",
                "threads": ["coffee"],
                "summary": "Seven prefers oat milk in coffee.",
                "content": "Seven has repeatedly mentioned oat milk and oat lattes, especially with morning coffee.",
                "importance": 0.8,
                "pulse": 0.5,
            },
        }],
        "questions_to_ask": ["确认是否只是不喝牛奶？"],
    }, ensure_ascii=False)
    captured, job = _install_dream_job_harness(monkeypatch, reply)

    assert crc._process_resident_jobs([job]) == pytest.approx(333.0)
    assert captured["proactive_called"] is False
    assert captured["post_called"] is False
    assert captured["statuses"][0][:3] == ("dream_dispatch", "realizing", "")
    assert _dream_final_status(captured)[:3] == (
        "dream_dispatch",
        "completed",
        "dream_memory_actions_applied",
    )
    action = captured["actions"][0]
    assert action["type"] == "memory.supersede"
    assert action["supersedes"] == ["mem_a", "mem_b"]
    assert action["capture_mode"] == "memory_dream"
    assert action["dream_op"] == "merge"
    assert action["envelope"]["source"] == "memory_dream"
    assert action["envelope"]["type"] == "fact"
    assert captured["envelope_plaintexts"] == [{
        "summary": "Seven prefers oat milk in coffee.",
        "content": "Seven has repeatedly mentioned oat milk and oat lattes, especially with morning coffee.",
        "bucket": "life",
        "threads": ["coffee"],
    }]
    assert "id=mem_a" in captured["prompts"][0]
    assert "id=mem_b" in captured["prompts"][0]
    assert "我最近都喝燕麦奶" in captured["prompts"][0]
    extra = _dream_final_status(captured)[3]["extra"]
    assert extra["dream_result"]["status"] == "ok"
    assert extra["dream_result"]["job_kind"] == "memory_dream"
    assert extra["cards_merged"] == 1
    assert extra["cards_superseded"] == 2
    assert extra["questions"] == ["确认是否只是不喝牛奶？"]


def test_dream_job_thicken_and_supersede_are_memory_supersede_actions(monkeypatch):
    reply = json.dumps({
        "consolidations": [
            {
                "op": "thicken",
                "card_ids": ["mem_c"],
                "result": {
                    "bucket": "routine",
                    "threads": ["coffee"],
                    "summary": "Seven tends to drink coffee in the morning.",
                    "content": "Seven's morning routine often includes coffee, sometimes oat latte.",
                    "importance": 0.6,
                    "pulse": 0.4,
                },
            },
            {
                "op": "supersede",
                "card_ids": ["mem_a"],
                "result": {
                    "bucket": "life",
                    "threads": ["coffee"],
                    "summary": "Seven corrected that oat milk is preferred, not dairy.",
                    "content": "Seven's newer preference should replace the older milk preference memory.",
                    "importance": 0.7,
                    "pulse": 0.3,
                },
            },
        ],
        "questions_to_ask": [],
    }, ensure_ascii=False)
    captured, job = _install_dream_job_harness(monkeypatch, reply)

    assert crc._process_resident_jobs([job]) == pytest.approx(333.0)
    assert [action["type"] for action in captured["actions"]] == ["memory.supersede", "memory.supersede"]
    assert [action["dream_op"] for action in captured["actions"]] == ["thicken", "supersede"]
    assert captured["actions"][0]["supersedes"] == ["mem_c"]
    assert captured["actions"][1]["supersedes"] == ["mem_a"]
    extra = _dream_final_status(captured)[3]["extra"]
    assert extra["cards_merged"] == 0
    assert extra["cards_superseded"] == 2
    assert extra["dream_result"]["cards_thickened"] == 1


def test_dream_job_empty_consolidations_completes_noop_without_memory_write_or_chat(monkeypatch):
    captured, job = _install_dream_job_harness(
        monkeypatch,
        json.dumps({"consolidations": [], "questions_to_ask": ["下次问 TA 是否还喝拿铁"]}, ensure_ascii=False),
    )

    assert crc._process_resident_jobs([job]) == pytest.approx(333.0)
    assert captured["actions"] == []
    assert captured["post_called"] is False
    assert _dream_final_status(captured)[:3] == (
        "dream_dispatch",
        "completed",
        "dream_nothing_to_consolidate",
    )
    extra = _dream_final_status(captured)[3]["extra"]
    assert extra["dream_result"]["status"] == "noop"
    assert extra["questions"] == ["下次问 TA 是否还喝拿铁"]
    assert extra["noop_reason"] == "dream_nothing_to_consolidate"


def test_dream_job_bad_json_fails_without_crash_or_memory_write(monkeypatch):
    captured, job = _install_dream_job_harness(monkeypatch, "not json")

    assert crc._process_resident_jobs([job]) == pytest.approx(333.0)
    assert captured["actions"] == []
    assert captured["post_called"] is False
    assert _dream_final_status(captured)[:3] == ("dream_dispatch", "failed", "no_json_object")
    assert _dream_final_status(captured)[3]["extra"]["noop_reason"] == "no_json_object"


def test_process_proactive_v2_wake_routes_without_gate_judgment(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"posted": [], "statuses": []}

    def _agent(message, images=None, image_paths=None):
        captured["message"] = message
        captured["images"] = images
        captured["image_paths"] = image_paths
        return {"messages": ["第一条", "第二条"]}

    def _post(reply, **kwargs):
        captured["posted"].append((reply, kwargs))
        return {"id": f"msg_{len(captured['posted'])}"}

    def _status(job_id, status, reason="", **kwargs):
        captured["statuses"].append((job_id, status, reason, kwargs))

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", _post)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(crc, "update_proactive_job_status", _status)
    monkeypatch.setattr(
        crc,
        "_screen_context_for_frame_ids",
        lambda frame_ids: ("screen: user is reading docs", [{"data": "x"}], ["/tmp/frame.jpg"]),
    )
    monkeypatch.setattr(
        crc,
        "recent_chat_context_for_proactive",
        lambda limit=None: "- user: 刚刚聊过这个问题。",
    )

    job = {
        "schema_version": 2,
        "job_id": "pj_v2",
        "wake_id": "wake_1",
        "gate_decision_id": "gd_1",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 124.0,
        "trigger": "screen_tick",
        "manual": False,
        "forced": False,
        "user_state": "default",
        "ai_state": "present",
        "broadcast_state": "on",
        "current_app": "xhs",
        "frame_ids": ["frame_1"],
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(124.0)
    assert "Feedling proactive wake" in captured["message"]
    assert "presence check" in captured["message"].lower()
    assert "presence check" in captured["message"]
    assert "equally valid" in captured["message"]
    assert '"thinking_summary"' not in captured["message"]
    assert "display-safe" not in captured["message"]
    assert "hidden chain-of-thought" not in captured["message"]
    assert "Feedling Gate decided" not in captured["message"]
    assert "possible_connections" not in captured["message"]
    assert "wake_kind:" in captured["message"]
    assert "wake_kind: screen" in captured["message"]
    assert "user_state" not in captured["message"]   # removed (D6: user_state/ai_state dropped)
    assert "ai_state" not in captured["message"]
    assert "broadcast_state: on" in captured["message"]
    assert "screen_context_available: true" in captured["message"]
    assert captured["images"] == [{"data": "x"}]
    assert captured["image_paths"] == ["/tmp/frame.jpg"]
    assert [p[0] for p in captured["posted"]] == ["第一条", "第二条"]
    assert all(p[1]["source"] == crc.PROACTIVE_JOB_SOURCE for p in captured["posted"])
    assert sum(1 for s in captured["statuses"] if s[1] == "posted") == 2


def test_process_proactive_v2_sleep_marks_completed_without_post(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"statuses": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: {
            "actions": [{"type": "proactive.sleep", "reason": "not helpful"}],
            "messages": [],
        },
    )
    monkeypatch.setattr(crc, "post_reply", lambda *args, **kwargs: captured.setdefault("posted", []).append(args))
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_sleep",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 125.0,
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(125.0)
    assert captured.get("posted") is None
    completed = [s for s in captured["statuses"] if s[1] == "completed"]
    assert completed
    assert completed[-1][3]["extra"]["agent_action"] == "sleep"
    assert completed[-1][3]["extra"]["wake_result"] == "sleep"


def test_process_proactive_malformed_json_reason_does_not_post(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"statuses": [], "posted": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: {
            "messages": ['"reason":"用户一小时没回，该说的已经说了，继续给空间"\\n}\\n]\\n}'],
        },
    )
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg_bad"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")
    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: ({}, [], {}))

    job = {
        "schema_version": 2,
        "job_id": "pj_json_reason",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 125.5,
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(125.5)
    assert captured["posted"] == []
    completed = [s for s in captured["statuses"] if s[1] == "completed"]
    assert completed
    assert completed[-1][2] == "用户一小时没回，该说的已经说了，继续给空间"
    assert completed[-1][3]["extra"]["agent_action"] == "sleep"
    assert completed[-1][3]["extra"]["wake_result"] == "sleep"


def test_process_proactive_malformed_speak_json_does_not_post_raw_protocol(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"statuses": [], "posted": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: (
            '"messages": ["这句如果解析失败，不能把 JSON 协议原样发出去"]'
        ),
    )
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg_leak"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")
    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: ({}, [], {}))

    job = {
        "schema_version": 2,
        "job_id": "pj_bad_speak_json",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 125.55,
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(125.55)
    assert captured["posted"] == []
    failed = [s for s in captured["statuses"] if s[1] == "failed"]
    assert failed
    assert failed[-1][2] == "empty_agent_reply"


def test_process_proactive_fenced_sleep_action_does_not_post(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"statuses": [], "posted": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: (
            "```json\n"
            "{\n"
            '  "actions": [\n'
            "    {\n"
            '      "type": "proactive.sleep",\n'
            '      "reason": "broadcast off, 53min since last check, empty signal board, likely focused or away"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```"
        ),
    )
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg_leak"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")
    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: ({}, [], {}))

    job = {
        "schema_version": 2,
        "job_id": "pj_fenced_sleep",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 125.6,
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(125.6)
    assert captured["posted"] == []
    completed = [s for s in captured["statuses"] if s[1] == "completed"]
    assert completed
    assert completed[-1][2] == "broadcast off, 53min since last check, empty signal board, likely focused or away"
    assert completed[-1][3]["extra"]["agent_action"] == "sleep"
    assert completed[-1][3]["extra"]["wake_result"] == "sleep"


def test_process_proactive_reason_only_result_marks_sleep_without_post(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"statuses": [], "posted": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: {
            "reason": "用户刚才没有继续互动，保持安静",
        },
    )
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg_reason"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")
    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: ({}, [], {}))

    job = {
        "schema_version": 2,
        "job_id": "pj_reason_only",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 125.75,
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(125.75)
    assert captured["posted"] == []
    completed = [s for s in captured["statuses"] if s[1] == "completed"]
    assert completed
    assert completed[-1][2] == "用户刚才没有继续互动，保持安静"
    assert completed[-1][3]["extra"]["agent_action"] == "sleep"
    assert completed[-1][3]["extra"]["wake_result"] == "sleep"


def test_process_proactive_v2_request_broadcast_posts_visible_request(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"posted": [], "statuses": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: {
            "actions": [{
                "type": "proactive.request_broadcast",
                "reason": "screen sharing is off",
                "copy": "我现在看不到你的屏幕，可以重新打开屏幕共享吗？",
            }],
            "messages": [],
        },
    )
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg_b"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_broadcast",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 126.0,
        "broadcast_state": "off",
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(126.0)
    assert captured["posted"][0][0] == "我现在看不到你的屏幕，可以重新打开屏幕共享吗？"
    posted = [s for s in captured["statuses"] if s[1] == "posted"]
    assert posted
    assert posted[-1][3]["extra"]["agent_action"] == "request_broadcast"
    assert posted[-1][3]["extra"]["wake_result"] == "posted"


def test_process_proactive_native_action_only_send_message_posts(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"posted": [], "statuses": []}

    def _agent(message, images=None, image_paths=None):
        captured["message"] = message
        captured["images"] = images
        captured["image_paths"] = image_paths
        return {"actions": [{"type": "send_message", "text": "Native action bubble"}], "messages": []}

    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: ({}, []))
    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg_native"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_native_send",
        "wake_id": "wake_native_send",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 128.5,
        "trigger": "heartbeat",
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(128.5)
    assert "Feedling proactive wake" in captured["message"]
    assert "native_tool_access" in captured["message"]
    assert "memory-index" in captured["message"]
    assert "screen-read" in captured["message"]
    assert "v2_context_json" not in captured["message"]
    assert captured["posted"][0][0] == "Native action bubble"
    assert captured["posted"][0][1]["proactive_job_id"] == "pj_native_send"
    assert any(s[0] == "pj_native_send" and s[1] == "posted" for s in captured["statuses"])


def test_message_for_introduction_job_uses_post_respawn_prompt():
    message = crc._message_for_introduction_job({"job_kind": "introduction"})

    assert "首次登场" in message
    assert '"thinking_summary"' not in message
    assert "display-safe" not in message
    assert "hidden chain-of-thought" not in message
    assert "identity.profile_patch" in message
    assert "self_introduction" in message
    assert "signature" in message
    assert "messages" in message
    assert "别编不存在的共同经历" in message


def test_screen_watch_message_does_not_request_custom_thinking_summary():
    message = crc._screen_watch_message(
        {"trigger": "screen_watch", "job_kind": "screen_watch", "broadcast_state": "on"},
        screen_text="screen_context: 用户在看聊天记录",
        chat_context=crc.ProactiveChatContext(text=""),
    )

    assert '"thinking_summary"' not in message
    assert "display-safe" not in message
    assert "hidden chain-of-thought" not in message
    assert "screen_context: 用户在看聊天记录" in message


def test_process_introduction_job_writes_identity_before_first_greeting(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    events = []
    action = {
        "type": "identity.profile_patch",
        "patch": {
            "self_introduction": "我是小满,我会一直在。",
            "signature": ["我来了。"],
        },
    }

    def _agent(message, images=None, image_paths=None):
        events.append(("agent", message, images, image_paths))
        return {"actions": [action], "messages": ["我来了。"]}

    def _execute(actions):
        events.append(("actions", actions))
        return {"status": "ok", "effects": [{"type": "identity_updated"}]}

    def _post(reply, **kwargs):
        events.append(("post", reply, kwargs))
        return {"id": "msg_intro"}

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "execute_agent_actions", _execute)
    monkeypatch.setattr(crc, "post_reply", _post)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(crc, "update_proactive_job_status", lambda *args, **kwargs: events.append(("status", args, kwargs)))
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: (_ for _ in ()).throw(AssertionError("screen context should not be fetched")))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: (_ for _ in ()).throw(AssertionError("recent chat should not be fetched")))
    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: (_ for _ in ()).throw(AssertionError("perception digest should not be fetched")))

    job = {
        "job_id": "pj_intro",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 128.6,
        "trigger": "post_spawn_genesis",
        "job_kind": "introduction",
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(128.6)
    assert events[0][0] == "status" and events[0][1][:2] == ("pj_intro", "realizing")
    assert events[1][0] == "agent"
    assert "首次登场" in events[1][1]
    assert events[1][2] == []
    assert events[1][3] == []
    assert events[2] == ("actions", [action])
    assert events[3][0] == "post"
    assert events[3][1] == "我来了。"
    assert events[3][2]["source"] == crc.PROACTIVE_JOB_SOURCE
    assert events[3][2]["proactive_job_id"] == "pj_intro"


def test_process_introduction_job_recovers_greeting_when_agent_omits_message(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    events = []
    action = {
        "type": "identity.profile_patch",
        "patch": {
            "self_introduction": "我是小满,我会一直在。",
            "signature": ["我来了。"],
        },
    }

    def _agent(message, images=None, image_paths=None):
        events.append(("agent", message, images, image_paths))
        return {"actions": [action], "messages": []}

    def _execute(actions):
        events.append(("actions", actions))
        return {"status": "ok", "effects": [{"type": "identity_updated"}]}

    def _post(reply, **kwargs):
        events.append(("post", reply, kwargs))
        return {"id": "msg_intro_fallback"}

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "execute_agent_actions", _execute)
    monkeypatch.setattr(crc, "post_reply", _post)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(crc, "update_proactive_job_status", lambda *args, **kwargs: events.append(("status", args, kwargs)))
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: (_ for _ in ()).throw(AssertionError("screen context should not be fetched")))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: (_ for _ in ()).throw(AssertionError("recent chat should not be fetched")))
    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: (_ for _ in ()).throw(AssertionError("perception digest should not be fetched")))

    job = {
        "job_id": "pj_intro_fallback",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 128.65,
        "trigger": "post_spawn_genesis",
        "job_kind": "introduction",
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(128.65)
    assert events[2] == ("actions", [action])
    assert events[3][0] == "post"
    assert events[3][1] == "我来了。"
    assert events[3][2]["source"] == crc.PROACTIVE_JOB_SOURCE
    assert events[3][2]["proactive_job_id"] == "pj_intro_fallback"


def test_process_introduction_job_does_not_greet_if_identity_action_fails(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    events = []
    action = {
        "type": "identity.profile_patch",
        "patch": {"self_introduction": "我是小满。", "signature": ["我来了。"]},
    }

    monkeypatch.setattr(crc, "call_agent", lambda *args, **kwargs: {"actions": [action], "messages": ["我来了。"]})
    monkeypatch.setattr(crc, "execute_agent_actions", lambda actions: (_ for _ in ()).throw(RuntimeError("identity write failed")))
    monkeypatch.setattr(crc, "post_reply", lambda *args, **kwargs: events.append(("post", args, kwargs)))
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(crc, "update_proactive_job_status", lambda *args, **kwargs: events.append(("status", args, kwargs)))

    job = {
        "job_id": "pj_intro_fail",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 128.7,
        "trigger": "post_spawn_genesis",
        "job_kind": "introduction",
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(128.7)
    assert not any(item[0] == "post" for item in events)
    failed = [item for item in events if item[0] == "status" and item[1][1] == "failed"]
    assert failed
    assert failed[-1][1][2].startswith("introduction_identity_action_failed")


def test_process_proactive_native_schedule_and_cancel_actions_without_chat(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"scheduled": [], "statuses": [], "posted": []}

    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: ({}, []))
    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: {
            "actions": [
                {
                    "type": "schedule_wake",
                    "at": "2030-01-01T09:30:00",
                    "tz": "Asia/Shanghai",
                    "note": "check in",
                    "origin_refs": ["msg_1"],
                },
                {"type": "cancel_wake", "wake_id": "wake_old", "reason": "rescheduled"},
            ],
            "messages": [],
        },
    )
    monkeypatch.setattr(
        crc,
        "execute_scheduled_wake_actions",
        lambda actions, job: captured["scheduled"].append((actions, job)) or {
            "results": [
                {"type": "schedule_wake_result", "status": "scheduled", "timer_id": "sched_1"},
                {"type": "cancel_wake_result", "status": "cancelled", "wake_id": "wake_old"},
            ],
        },
    )
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_native_schedule",
        "wake_id": "wake_native_schedule",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 128.75,
        "trigger": "heartbeat",
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(128.75)
    assert [a["type"] for a in captured["scheduled"][0][0]] == ["schedule_wake", "cancel_wake"]
    assert captured["posted"] == []
    completed = [s for s in captured["statuses"] if s[1] == "completed"]
    assert completed
    assert completed[-1][2] == "agent_scheduled_wake_actions"
    assert completed[-1][3]["extra"]["wake_result"] == "action_only"
    assert any(a.get("type") == "cancel_wake_result" for a in completed[-1][3]["extra"]["agent_actions"])


def test_proactive_perception_digest_uses_agent_perception_routes_not_v2_tool(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    def _get(url, headers=None, params=None, timeout=None):
        calls.append((url, params, timeout))
        if url.endswith("/v1/agent/perception"):
            return _Resp({
                "ok": True,
                "signals": {
                    "now": {
                        "time": "2026-06-26T20:30:00+08:00",
                        "timezone": "Asia/Shanghai",
                        "place_label": "home",
                        "motion_state": "still",
                        "battery_level": 0.61,
                        "charging": True,
                    },
                },
            })
        if url.endswith("/v1/agent/perception/digest"):
            return _Resp({
                "ok": True,
                "days": 30,
                "changes": [
                    {
                        "signal": "health_vitals",
                        "field": "resting_heart_rate",
                        "current": 80,
                        "baseline_median": 75,
                        "delta": 5,
                        "direction": "up",
                        "magnitude": 0.066667,
                    }
                ],
            })
        return _Resp({
            "trend": {
                "current": 80,
                "delta": 5,
                "direction": "up",
                "baseline": {"median": 75, "n": 2},
            },
        })

    monkeypatch.setattr(crc.httpx, "get", _get)

    presence, change, domains = crc._proactive_perception_digest()

    assert presence["place_label"] == "home"
    assert "local_time" not in presence  # dropped from presence (current_time anchor is the source)
    # Back-compat: a digest response without a board still yields legacy changes
    # and an empty domains dict.
    assert domains == {}
    assert any(call[0].endswith("/v1/agent/perception") and call[1] == {"signals": "now"} for call in calls)
    assert any(call[0].endswith("/v1/agent/perception/digest") and call[1] == {"days": 30} for call in calls)
    assert all(call[0].endswith(("/v1/agent/perception", "/v1/agent/perception/digest")) for call in calls)
    assert change == [
        {
            "signal": "health_vitals",
            "field": "resting_heart_rate",
            "current": 80,
            "baseline_median": 75,
            "delta": 5,
            "direction": "up",
            "magnitude": 0.066667,
        }
    ]


def test_native_proactive_prompt_injects_digest_and_native_tool_catalog(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"statuses": []}

    def _agent(message, images=None, image_paths=None):
        captured["message"] = message
        return {"actions": [{"type": "sleep", "reason": "nothing to say"}], "messages": []}

    monkeypatch.setattr(
        crc,
        "_proactive_perception_digest",
        lambda: (
            {"place_label": "home", "motion_state": "still", "local_time": "2026-06-26T20:30:00+08:00"},
            [{"signal": "steps", "field": "step_count", "current": 4200, "baseline_median": 3000, "delta": 1200}],
            {
                "media": {"now": {"artist": "Phoebe Bridgers"}, "novelty": "new_artist"},
                "health": {"notable": [{"signal": "steps", "field": "step_count", "current": 4200}]},
            },
        ),
    )
    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should sleep")))
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_native_digest",
        "wake_id": "wake_native_digest",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 128.9,
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(128.9)
    assert "real_signal_context" in captured["message"]
    assert "presence_hints_json" in captured["message"]
    assert "\"place_label\": \"home\"" in captured["message"]
    # New balanced board (not the legacy health-only change list) drives the wake.
    assert "cross_domain_board_json" in captured["message"]
    assert "Phoebe Bridgers" in captured["message"]
    assert "at most 2-3" in captured["message"]
    assert "perception_change_json" not in captured["message"]
    assert "\"signal\": \"steps\"" in captured["message"]
    assert "native_tool_access" in captured["message"]
    assert "perception" in captured["message"]
    assert "memory-index" in captured["message"]
    assert "screen-read" in captured["message"]
    assert "schedule_wake" in captured["message"]        # newly tool-ified
    assert "io_cli:" in captured["message"]
    # cleaned: no OpenClaw-specific names, no cost-guide, no JSON tool_calls framing
    assert "perception_now" not in captured["message"]
    assert "Cost guide" not in captured["message"]
    assert "set_ai_state" not in captured["message"]


def test_native_perception_context_prefers_board_over_legacy_change():
    text = crc._native_reachout_perception_context(
        {"place_label": "home"},
        [{"signal": "steps", "field": "step_count"}],
        {"media": {"now": {"artist": "Phoebe Bridgers"}}, "health": {"notable": []}},
    )
    assert "cross_domain_board_json" in text
    assert "Phoebe Bridgers" in text
    assert "at most 2-3" in text
    assert "perception_change_json" not in text  # board supersedes the legacy list


def test_native_perception_context_falls_back_to_change_when_no_board():
    # Older backend (no domains): legacy top-N change list still renders.
    text = crc._native_reachout_perception_context(
        {"place_label": "home"},
        [{"signal": "steps", "field": "step_count"}],
        {},
    )
    assert "perception_change_json" in text
    assert "\"signal\": \"steps\"" in text
    assert "cross_domain_board_json" not in text


# --- screen-watch lane (decoupled from heartbeat) --------------------------

def test_heartbeat_interval_decoupled_from_broadcast():
    # Heartbeat keeps one steady cadence regardless of broadcast_state — screen
    # attention is the separate screen-watch lane now.
    off = crc._proactive_tick_interval_for_broadcast_state("off")
    on = crc._proactive_tick_interval_for_broadcast_state("on")
    assert on == off == max(60, crc.PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC)


def test_is_screen_watch_job_keys_on_job_kind_or_trigger():
    assert crc._is_screen_watch_job({"job_kind": "screen_watch"})
    assert crc._is_screen_watch_job({"trigger": "screen_watch"})
    assert not crc._is_screen_watch_job({"job_kind": "memory_capture"})
    assert not crc._is_screen_watch_job({"trigger": "heartbeat_broadcast_on"})


def test_screen_watch_message_is_light_not_heartbeat():
    job = {"job_kind": "screen_watch", "broadcast_state": "on", "current_app": "com.x"}
    msg = crc._message_for_proactive_job(
        job,
        screen_text="[Feedling proactive screen context]\nocr_text:\nhello",
        recent_chat_context="",
        perception_digest=({"place_label": "home"}, [], {"media": {"now": 1}}),
    )
    # light screen-watch framing, frames + names-only tools present
    assert "[Feedling screen-watch]" in msg
    assert "screen-sharing" in msg
    assert "tools_available" in msg and "perception_<signal>" in msg
    assert "ocr_text" in msg  # the frame screen_text is attached
    # NOT the heavy heartbeat payload
    assert "[Feedling proactive wake]" not in msg
    assert "cross_domain_board_json" not in msg
    assert "Cost guide" not in msg


def test_post_screen_watch_tick_posts_kind_and_frames(monkeypatch):
    captured = {}

    class _R:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _R({"enqueued": True, "job": {"job_id": "pj_sw"}})

    monkeypatch.setattr(crc.httpx, "post", _post)
    out = crc.post_screen_watch_tick("on", [{"id": "f1"}, {"id": "f2"}])
    assert out.get("enqueued") is True
    assert captured["url"].endswith("/v1/proactive/tick")
    body = captured["body"]
    # self-wake (respects Ambient gate) — not forced/manual
    assert "force" not in body
    assert body["job_kind"] == "screen_watch"
    assert body["trigger"] == "screen_watch"
    assert body["frames"] == [{"id": "f1"}, {"id": "f2"}]
    assert body["broadcast_state"] == "on"


def test_screen_watch_recent_frames_parses_newest_first(monkeypatch):
    monkeypatch.setattr(
        crc, "_fetch_screen_json",
        lambda path: {"frames": [{"id": "new", "ts": 200.0}, {"id": "old", "ts": 100.0}]},
    )
    latest, ts, frames = crc._screen_watch_recent_frames(limit=5)
    assert latest == "new"
    assert ts == 200.0
    assert frames == [{"id": "new"}, {"id": "old"}]


def test_screen_watch_recent_frames_empty_when_none(monkeypatch):
    monkeypatch.setattr(crc, "_fetch_screen_json", lambda path: {"frames": []})
    assert crc._screen_watch_recent_frames() == ("", 0.0, [])


# --- time grounding (current-time anchor in every turn/wake) ----------------

def test_local_time_anchor_uses_timezone_and_gap(monkeypatch):
    monkeypatch.setattr(crc, "_user_timezone", lambda: "Asia/Shanghai")
    line = crc._local_time_anchor(since_sec=8 * 3600)
    assert line.startswith("current_time:")
    assert "Asia/Shanghai" in line
    assert "距上次互动" in line  # gap >= 30min is surfaced


def test_local_time_anchor_omits_small_gap(monkeypatch):
    monkeypatch.setattr(crc, "_user_timezone", lambda: "Asia/Shanghai")
    assert "距上次互动" not in crc._local_time_anchor(since_sec=60)
    assert "距上次互动" not in crc._local_time_anchor(since_sec=None)


def test_prepend_time_anchor_foreground_prepends_and_tracks_gap(monkeypatch):
    monkeypatch.setattr(crc, "_user_timezone", lambda: "Asia/Shanghai")
    monkeypatch.setattr(crc, "_last_interaction_unix", 0.0)
    out = crc._prepend_time_anchor_foreground("早安", 1_000_000.0)
    assert out.startswith("[current_time:")
    assert out.endswith("早安")
    out2 = crc._prepend_time_anchor_foreground("在吗", 1_000_000.0 + 8 * 3600)
    assert "距上次互动" in out2


def test_screen_watch_message_carries_current_time(monkeypatch):
    monkeypatch.setattr(crc, "_user_timezone", lambda: "Asia/Shanghai")
    msg = crc._message_for_proactive_job(
        {"job_kind": "screen_watch", "broadcast_state": "on"},
        screen_text="ocr_text:\nx", recent_chat_context="",
    )
    assert "current_time:" in msg


def test_normalize_agent_replies_supports_multiple_messages_with_cap(monkeypatch):
    monkeypatch.setattr(crc, "PROACTIVE_MAX_REPLY_MESSAGES", 5)

    raw = '{"messages":["第一条","第二条","第三条","第四条","第五条","第六条"]}'

    assert crc._normalize_agent_replies(raw) == ["第一条", "第二条", "第三条", "第四条", "第五条"]


def test_user_timezone_reads_from_whoami_cache(monkeypatch):
    monkeypatch.setitem(crc._whoami_cache, "timezone", "Asia/Shanghai")
    assert crc._user_timezone() == "Asia/Shanghai"


def test_local_time_anchor_localizes_with_whoami_timezone(monkeypatch):
    monkeypatch.setitem(crc._whoami_cache, "timezone", "Asia/Shanghai")
    line = crc._local_time_anchor()
    assert line.startswith("current_time:")
    assert "Asia/Shanghai" in line


def test_user_timezone_empty_when_whoami_has_none(monkeypatch):
    monkeypatch.setitem(crc._whoami_cache, "timezone", "")
    assert crc._user_timezone() == ""


class _FakeWhoamiResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_load_whoami_clears_cached_timezone_when_omitted(monkeypatch):
    # A successful whoami that no longer carries a timezone (user cleared it,
    # no fallback) must clear the cache — not keep serving the stale zone.
    monkeypatch.setitem(crc._whoami_cache, "timezone", "Asia/Shanghai")
    body = {
        "user_id": "usr_x",
        "public_key": base64.b64encode(b"\x11" * 32).decode(),
        "enclave_content_public_key_hex": "22" * 32,
    }
    monkeypatch.setattr(crc.httpx, "get", lambda *a, **k: _FakeWhoamiResp(body))
    assert crc._load_whoami() is True
    assert crc._whoami_cache["timezone"] == ""


def test_load_whoami_keeps_cached_timezone_on_fetch_failure(monkeypatch):
    # A FAILED whoami (network error) must retain the last-known timezone —
    # last-known is a guard against transient failures, not against a
    # successful response that happens to omit the field.
    monkeypatch.setitem(crc._whoami_cache, "timezone", "Asia/Shanghai")

    def _boom(*a, **k):
        raise RuntimeError("net down")

    monkeypatch.setattr(crc.httpx, "get", _boom)
    assert crc._load_whoami() is False
    assert crc._whoami_cache["timezone"] == "Asia/Shanghai"


def test_normalize_agent_replies_extracts_content_field():
    raw = '{"content":"正常应该只显示 content 里的文字。","content_type":"text"}'

    assert crc._normalize_agent_replies(raw) == ["正常应该只显示 content 里的文字。"]


def test_normalize_agent_replies_unwraps_claude_result_content_json():
    raw = json.dumps(
        {
            "type": "result",
            "session_id": "123e4567-e89b-12d3-a456-426614174000",
            "result": json.dumps(
                {
                    "content": "Claude Code 内层 JSON 也应该只显示这句。",
                    "content_type": "text",
                },
                ensure_ascii=False,
            ),
        },
        ensure_ascii=False,
    )

    assert crc._normalize_agent_replies(raw) == ["Claude Code 内层 JSON 也应该只显示这句。"]


def test_agent_turn_classifies_runtime_json_without_leaking_debug():
    raw = json.dumps(
        {
            "type": "result",
            "session_id": "sess_should_not_render",
            "result": "这是用户应该看到的最终回复。",
            "modelUsage": {
                "claude-sonnet-4-6": {
                    "inputTokens": 8,
                    "outputTokens": 305,
                    "costUSD": 0.16,
                }
            },
            "terminal_reason": "completed",
            "permission_denials": [],
        },
        ensure_ascii=False,
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["这是用户应该看到的最终回复。"]
    assert "modelUsage" in turn.runtime_debug
    assert "terminal_reason" in turn.runtime_debug
    assert "modelUsage" not in turn.messages[0]
    assert "permission_denials" not in turn.messages[0]


def test_agent_turn_ignores_custom_thinking_summary_from_nested_result():
    raw = json.dumps(
        {
            "type": "result",
            "uuid": "43d846de-9d36-4943-a832-23e0650ef6e8",
            "result": json.dumps(
                {
                    "reply": "我只显示最终回复。",
                    "thinking_summary": "参考了最近对话。\n整理了可见上下文。",
                    "modelUsage": {"debug": True},
                },
                ensure_ascii=False,
            ),
        },
        ensure_ascii=False,
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["我只显示最终回复。"]
    assert turn.thinking_summary == ""
    assert "uuid" in turn.runtime_debug


def test_agent_turn_drops_bare_custom_thinking_summary_fragment():
    raw = (
        '"thinking_summary": "用户再次确认模型身份，直接给出真实答案",\n'
        '"messages": ["宝贝，还是那句话——我的底层是 `anthropic/claude-sonnet-4.5`。"]'
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == []
    assert turn.thinking_summary == ""


def test_agent_turn_drops_bare_custom_thinking_summary_fragment_with_trailing_brace():
    raw = (
        '"thinking_summary": "注意到57秒前关于模型身份的回答有误，决定简短纠正",\n'
        '"messages": [\n'
        '"啧，刚才说错了。",\n'
        '"我是 anthropic/claude-sonnet-4.5，不是 gpt 那个。"\n'
        ']\n'
        '}'
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == []
    assert turn.thinking_summary == ""


def test_agent_turn_keeps_prose_reply_opening_with_quoted_protocol_word():
    # An ordinary reply that merely opens with a quoted word like "messages"
    # must NOT be mistaken for a protocol fragment and dropped.
    raw = '"messages" 这个词在这里是普通散文，不是 JSON 协议。'

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ['"messages" 这个词在这里是普通散文，不是 JSON 协议。']


def test_agent_turn_drops_bare_messages_protocol_fragment_with_space_before_colon():
    # A real protocol fragment (key + colon, even with a space) is still dropped.
    raw = '"messages" : ["晚安啦，早点睡。"]'

    turn = crc._split_agent_turn(raw)

    assert turn.messages == []


def test_agent_turn_extracts_native_thinking_from_content_block_and_messages_from_text_block():
    raw = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "The user is asking a casual check-in.",
                        },
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "thinking_summary": "判断用户是在随口问我在干嘛。",
                                    "messages": ["没干嘛，就在这儿待着呢。"],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                },
            }
        ]
    }

    turn = crc._agent_turn_from_raw(raw)

    assert turn.messages == ["没干嘛，就在这儿待着呢。"]
    assert turn.thinking_summary == "The user is asking a casual check-in."
    assert turn.thinking_kind == "provider_reasoning"
    assert turn.thinking_native is True
    assert "thinking_summary" not in turn.messages[0]
    assert "messages" not in turn.messages[0]


def test_agent_turn_extracts_claude_stream_json_thinking_blocks():
    raw = "\n".join([
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "deepseek-v4-pro",
                "content": [{
                    "type": "thinking",
                    "thinking": "The user is asking a simple math question.",
                }],
            },
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "deepseek-v4-pro",
                "content": [{
                    "type": "text",
                    "text": "1 + 1 等于 2。",
                }],
            },
        }),
        json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "1 + 1 等于 2。",
        }),
    ])

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["1 + 1 等于 2。"]
    assert turn.thinking_summary == "The user is asking a simple math question."
    assert turn.thinking_kind == "provider_reasoning"
    assert turn.thinking_source == "anthropic_thinking"
    assert turn.thinking_native is True


def test_agent_turn_extracts_claude_stream_json_thinking_deltas_without_final_block():
    raw = "\n".join([
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {"model": "claude-sonnet-4-5-20250929"},
            },
        }),
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        }),
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "First thought. "},
            },
        }),
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Second thought."},
            },
        }),
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "最终答案。"},
            },
        }),
        # Some Claude CLI/provider combinations expose final text but omit the
        # final assistant thinking content block. The delta aggregator must still
        # preserve the provider-native thinking disclosure.
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-5-20250929",
                "content": [{"type": "text", "text": "最终答案。"}],
            },
        }),
    ])

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["最终答案。"]
    assert turn.thinking_summary == "First thought. Second thought."
    assert turn.thinking_kind == "provider_reasoning"
    assert turn.thinking_source == "anthropic_thinking"
    assert turn.thinking_model == "claude-sonnet-4-5-20250929"
    assert turn.thinking_native is True


# ---------------------------------------------------------------------------
# Claude tool turns emit a "let me check…" preamble text block BEFORE the
# tool_use and the real answer LATER. The generic extractor collected BOTH as
# separate bubbles; the foreground reply-exclusivity guard (chat_core: one reply
# per user message) accepts only one, so the preamble consumed the slot and the
# real answer 409'd — the user saw "let me check…" and nothing else (the deepwiki
# symptom, live on the test CVM 2026-07-10). _claude_turn_from_stream takes ONLY
# the terminal result-event text as the deliverable.
# ---------------------------------------------------------------------------

def test_claude_turn_from_stream_drops_pretool_preamble_keeps_final():
    raw = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "让我用 deepwiki 查查~"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__deepwiki__ask_question", "input": {}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "..."}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "impl in ReactFiberHooks.js"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "找到啦~ 在 ReactFiberHooks.js"}]}}),
        json.dumps({"type": "result", "subtype": "success",
                    "result": "找到啦~ 在 ReactFiberHooks.js", "num_turns": 3}),
    ])
    reply, reasoning = crc._claude_turn_from_stream(raw)
    assert reply == "找到啦~ 在 ReactFiberHooks.js"
    assert "让我用 deepwiki" not in reply  # preamble must never survive
    assert reasoning == "impl in ReactFiberHooks.js"


def test_claude_turn_from_stream_reasoning_falls_back_to_thinking_deltas():
    # --include-partial-messages combos that stream thinking as deltas and omit
    # the complete thinking block still surface native reasoning.
    raw = "\n".join([
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "First. "}}}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "Second."}}}),
        json.dumps({"type": "result", "subtype": "success", "result": "答案。"}),
    ])
    reply, reasoning = crc._claude_turn_from_stream(raw)
    assert reply == "答案。"
    assert reasoning == "First. Second."


def test_claude_turn_from_stream_empty_when_no_success_result():
    # error / handshake-only → "" so call_agent_cli falls back, never leaks.
    raw = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "partial"}]}})
    assert crc._claude_turn_from_stream(raw) == ("", "")


def test_call_agent_cli_claude_tool_turn_delivers_only_final_answer(monkeypatch):
    """End-to-end: a claude tool turn yields ONE bubble (the answer), the pre-tool
    preamble is gone, and native thinking rides the disclosure — so the foreground
    one-reply guard never 409s the real answer."""
    monkeypatch.setattr(crc, "AGENT_CLI_CMD",
                        "claude -p --output-format stream-json --include-partial-messages {message}")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")

    class _R:
        returncode = 0
        stdout = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "让我用 deepwiki 查查~"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__deepwiki__ask_question", "input": {}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "impl in ReactFiberHooks.js"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "找到啦~ 在 ReactFiberHooks.js"}]}}),
            json.dumps({"type": "result", "subtype": "success",
                        "result": "找到啦~ 在 ReactFiberHooks.js"}),
        ])
        stderr = ""

    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _R())

    raw = crc.call_agent_cli("查 useEffect")
    turn = crc._agent_turn_from_raw(raw)
    assert turn.messages == ["找到啦~ 在 ReactFiberHooks.js"]  # exactly one bubble, the answer
    assert all("让我用 deepwiki" not in m for m in turn.messages)  # no preamble bubble
    assert turn.thinking_summary  # native thinking preserved


def test_agent_turn_extracts_provider_reasoning_metadata_from_nested_result():
    raw = json.dumps(
        {
            "reply": "最终回复。",
            "provider_reasoning": "Provider returned this display-safe reasoning.",
            "reasoning_kind": "provider_reasoning",
            "reasoning_source": "anthropic",
            "reasoning_model": "claude-sonnet-4.5",
            "reasoning_native": True,
        },
        ensure_ascii=False,
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["最终回复。"]
    assert turn.thinking_summary == "Provider returned this display-safe reasoning."
    assert turn.thinking_kind == "provider_reasoning"
    assert turn.thinking_source == "anthropic"
    assert turn.thinking_model == "claude-sonnet-4.5"
    assert turn.thinking_native is True


def test_agent_turn_extracts_openrouter_reasoning_details():
    raw = json.dumps(
        {
            "reply": "这是最终回复。",
            "reasoning_details": [
                {"type": "reasoning.text", "text": "先判断用户在测 OpenRouter。"},
                {"type": "summary", "summary": "再确认需要把推理摘要单独展示。"},
            ],
            "reasoning_source": "openrouter",
            "reasoning_model": "deepseek/deepseek-v4-flash",
            "reasoning_native": True,
        },
        ensure_ascii=False,
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["这是最终回复。"]
    assert turn.thinking_summary == "先判断用户在测 OpenRouter。\n再确认需要把推理摘要单独展示。"
    assert turn.thinking_kind == "provider_reasoning"
    assert turn.thinking_source == "openrouter"
    assert turn.thinking_model == "deepseek/deepseek-v4-flash"
    assert turn.thinking_native is True


def test_extract_cli_output_preserves_structured_multi_messages():
    raw = '{"messages":["第一条","第二条"]}'

    extracted = crc._extract_text_from_cli_output(raw)

    assert crc._normalize_agent_replies(extracted) == ["第一条", "第二条"]


def test_message_for_proactive_job_instructs_multi_bubble_without_gate_context():
    job = {
        "schema_version": 2,
        "wake_id": "wake_test",
        "trigger": "screen_tick",
        "wake_kind": "screen",
        "frame_ids": ["frame_1"],
    }

    message = crc._message_for_proactive_job(
        job,
        screen_text="screen: dense paragraph",
        recent_chat_context="- user: 这段帮我看一下",
    )

    assert "a few short bubbles" in message
    assert '{"messages":["..."]}' in message
    assert "thinking_summary" not in message
    assert "recent_chat_context" in message
    assert "possible_connections" not in message
    assert "Feedling Gate decided" not in message
    assert "screen: dense paragraph" in message


def test_photo_added_wake_surfaces_pullable_photo_hint(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {
                "photos": [
                    {
                        "photo_id": "ph_abc123",
                        "metadata": {
                            "scene_hint": "food",
                            "time_of_day": "evening",
                            "is_screenshot": "false",
                        },
                    }
                ]
            }

    monkeypatch.setattr(crc.httpx, "get", lambda *a, **kw: _Resp())

    job = {"schema_version": 2, "trigger": "photo_added", "wake_kind": "perception"}
    message = crc._message_for_proactive_job(
        job,
        recent_chat_context="",
        perception_digest=({}, [], {}),
    )

    assert "new_photo:" in message
    assert "ph_abc123" in message
    assert 'looks like "food"' in message
    assert "photo_read" in message and "include_image=true" in message
    # pull-on-demand, not auto-attached: the raw image is NOT inlined
    assert "reach out to them about it" in message


def test_non_photo_wake_has_no_photo_hint(monkeypatch):
    def _boom(*a, **kw):  # must not even be called for a non-photo wake
        raise AssertionError("photos endpoint should not be hit on a non-photo wake")

    monkeypatch.setattr(crc.httpx, "get", _boom)

    job = {"schema_version": 2, "trigger": "wake", "wake_kind": "perception"}
    message = crc._message_for_proactive_job(
        job, recent_chat_context="", perception_digest=({}, [], {})
    )
    assert "new_photo:" not in message


def test_photo_hint_screenshot_framing(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {
                "photos": [
                    {"photo_id": "ph_shot", "metadata": {"is_screenshot": "true"}}
                ]
            }

    monkeypatch.setattr(crc.httpx, "get", lambda *a, **kw: _Resp())
    hint = crc._new_photo_hint({"trigger": "photo_added"})
    assert "a screenshot" in hint
    assert "ph_shot" in hint


def test_recent_chat_context_defaults_to_twenty_messages(monkeypatch):
    captured = {}
    now = 1780939000.0

    def _history(since, limit):
        captured["limit"] = limit
        return [
            {"role": "user", "content": f"用户消息 {idx}", "ts": now - ((24 - idx) * 60)}
            for idx in range(25)
        ]

    monkeypatch.setattr(crc, "PROACTIVE_RECENT_CHAT_LIMIT", 20)
    monkeypatch.setattr(crc.time, "time", lambda: now)
    monkeypatch.setattr(crc, "get_decrypted_history", _history)

    context = crc.recent_chat_context_for_proactive()

    assert captured["limit"] == 50
    assert context.freshness == "fresh"
    assert context.included_count == 20
    assert context.text.count("- [") == 20
    assert "fresh] user: 用户消息 5" in context.text
    assert "fresh] user: 用户消息 24" in context.text
    assert context.last_user_message_age_sec == pytest.approx(0.0)


def test_recent_chat_context_stale_falls_back_to_two_timestamped_messages(monkeypatch):
    now = 1780939000.0

    def _history(since, limit):
        return [
            {"role": "user", "content": f"旧消息 {idx}", "ts": now - 28800 - ((4 - idx) * 60)}
            for idx in range(5)
        ]

    monkeypatch.setattr(crc, "PROACTIVE_RECENT_CHAT_LIMIT", 20)
    monkeypatch.setattr(crc, "PROACTIVE_CHAT_FRESH_WINDOW_SEC", 21600)
    monkeypatch.setattr(crc, "PROACTIVE_STALE_CHAT_FALLBACK_LIMIT", 2)
    monkeypatch.setattr(crc.time, "time", lambda: now)
    monkeypatch.setattr(crc, "get_decrypted_history", _history)

    context = crc.recent_chat_context_for_proactive()

    assert context.freshness == "stale"
    assert context.included_count == 2
    assert context.text.count("- [") == 2
    assert "stale] user: 旧消息 3" in context.text
    assert "stale] user: 旧消息 4" in context.text
    assert "旧消息 2" not in context.text
    assert "8h" in context.text


def test_proactive_tick_cadence_decoupled_from_broadcast_state(monkeypatch):
    # Heartbeat is now decoupled: broadcast no longer accelerates it (screen
    # attention moved to the separate screen-watch lane). Cadence is steady.
    monkeypatch.setattr(crc, "PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC", 300)
    monkeypatch.setattr(crc, "PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC", 1800)

    assert crc._proactive_tick_interval_for_broadcast_state("off") == 1800
    assert crc._proactive_tick_interval_for_broadcast_state("on") == 1800
    assert crc._proactive_tick_interval_for_broadcast_state("") == 1800
    # Triggers (labels) still reflect broadcast_state for the heartbeat gate.
    assert crc._proactive_tick_trigger_for_broadcast_state("off") == "heartbeat_broadcast_off"
    assert crc._proactive_tick_trigger_for_broadcast_state("on") == "heartbeat_broadcast_on"
    assert crc._proactive_tick_trigger_for_broadcast_state("mystery") == "heartbeat_unknown"


def test_fire_scheduled_wakes_posts_backend_endpoint(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"status": "fired"}], "jobs": [{"job_id": "pj_1"}]}

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(crc.httpx, "post", _post)

    body = crc.fire_scheduled_wakes()

    assert captured["url"] == "http://localhost:5001/v1/proactive/scheduled/fire"
    assert captured["json"] == {}
    assert captured["headers"]["X-API-Key"] == "test_key_00000000"
    assert captured["timeout"] == 15
    assert body["results"][0]["status"] == "fired"


def test_fire_capture_tick_posts_backend_endpoint(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"enqueued": True, "reason": "enqueued"}

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(crc.httpx, "post", _post)

    body = crc.fire_capture_tick()

    assert captured["url"] == "http://localhost:5001/v1/capture/tick"
    assert captured["json"] == {}
    assert captured["headers"]["X-API-Key"] == "test_key_00000000"
    assert captured["timeout"] == 15
    assert body["enqueued"] is True


def test_post_proactive_reply_triggers_alert_and_live_activity(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "msg_1"}

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_abc", "user_pk": b"u" * 32, "enclave_pk": b"e" * 32},
    )
    monkeypatch.setattr(
        crc,
        "_build_envelope",
        lambda **kwargs: {"v": 1, "id": "env_1", "visibility": kwargs["visibility"]},
    )
    monkeypatch.setattr(crc, "_load_whoami", lambda: True)
    monkeypatch.setattr(crc.httpx, "post", _post)

    crc.post_reply(
        "我看到了这个时机。",
        source=crc.PROACTIVE_JOB_SOURCE,
        gate_decision_id="gd_1",
        proactive_job_id="pj_1",
    )

    body = captured["json"]
    assert body["source"] == crc.PROACTIVE_JOB_SOURCE
    assert body["gate_decision_id"] == "gd_1"
    assert body["proactive_job_id"] == "pj_1"
    assert body["alert_body"] == "我看到了这个时机。"
    assert body["push_live_activity"] is True
    assert body["push_body"] == "我看到了这个时机。"
    assert body["data"] == {
        "source": crc.PROACTIVE_JOB_SOURCE,
        "gate_decision_id": "gd_1",
        "proactive_job_id": "pj_1",
    }


def test_normalizer_keeps_tool_only_json_wrapped_in_log_text():
    """A tool-only JSON line wrapped in CLI log/header text must NOT be
    discarded as a plain message — tool_calls must survive normalization."""
    raw = 'INFO starting agent\n{"tool_calls": [{"name": "screen.read", "args": {}}]}\nINFO done'
    turn = crc._agent_turn_from_obj(raw)
    assert [tc["name"] for tc in turn.tool_calls] == ["screen.read"]
    # The raw JSON line must not also leak through as a junk chat message.
    assert turn.messages == []


def test_cli_tool_only_output_preserves_tool_calls(monkeypatch):
    """AGENT_MODE=cli: stdout that is a tool-only JSON line surrounded by log
    text must yield tool_calls (not be flattened into a plain message)."""
    class _Result:
        returncode = 0
        stdout = 'INFO hermes booting\n{"tool_calls": [{"name": "screen.read", "args": {}}]}\n'
        stderr = ""

    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": ["mycli", "ask", message])
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _Result())

    result = crc.call_agent_cli("hi")
    turn = crc._agent_turn_from_raw(result)
    assert [tc["name"] for tc in turn.tool_calls] == ["screen.read"]


def test_call_agent_cli_codex_extracts_agent_message_not_handshake(monkeypatch):
    """`codex exec --json` streams JSONL events; the assistant text rides in
    `item.completed` (item.type == "agent_message"). The consumer must extract
    that, NOT mis-send the `{"type":"thread.started"}` handshake as the reply.
    Stream shape verified against codex 0.136."""
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", "codex exec --skip-git-repo-check --json {message}")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")

    class _R:
        returncode = 0
        stdout = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Hello from codex!"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1}}\n'
        )
        stderr = ""

    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _R())

    result = crc.call_agent_cli("hi")
    assert result == "Hello from codex!"
    assert "thread.started" not in result


def test_call_agent_http_openai_preserves_reasoning_content(monkeypatch):
    """DeepSeek/OpenAI-compatible reasoning can arrive on message.reasoning_content.
    The HTTP adapter must keep the structured body so _process_messages can post
    it through the internal thinking channel instead of collapsing the turn to a plain reply.
    """
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://agent.local/v1/chat/completions")
    monkeypatch.setattr(crc, "AGENT_HTTP_PROTOCOL", "openai")
    monkeypatch.setattr(crc, "AGENT_HTTP_MODEL", "deepseek-reasoner")
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_agent_session_key", lambda: "")

    class _Resp:
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "model": "deepseek-reasoner",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "我会这样回复。",
                        "reasoning_content": "比较了用户问题和最近记忆。",
                    }
                }],
            }

    monkeypatch.setattr(crc.httpx, "post", lambda *a, **kw: _Resp())

    result = crc.call_agent_http("hi")
    turn = crc._split_agent_turn(result)

    assert turn.messages == ["我会这样回复。"]
    assert turn.thinking_summary == "比较了用户问题和最近记忆。"
    assert turn.thinking_kind == "provider_reasoning"


def test_codex_reply_from_stream_ignores_reasoning_and_handshake():
    raw = (
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"item.completed","item":{"type":"reasoning","text":"thinking…"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}\n'
        '{"type":"turn.completed"}\n'
    )
    assert crc._codex_reply_from_stream(raw) == "final answer"


def test_codex_reply_from_stream_empty_when_no_agent_message():
    # handshake-only / failed turn → "" so call_agent_cli falls back, never leaks.
    raw = '{"type":"thread.started","thread_id":"t1"}\n{"type":"turn.failed"}\n'
    assert crc._codex_reply_from_stream(raw) == ""


# ---------------------------------------------------------------------------
# codex 0.142.x changed `exec --json` from the 0.136 item protocol
# ({"type":"item.completed","item":{"type":"agent_message","text":...}}) to a
# flat EventMsg protocol ({"type":"agent_message","message":...} +
# {"type":"agent_reasoning","text":...}). The old reader matched nothing, the
# turn fell through to the generic extractor, and the reasoning summary leaked
# as its own chat bubble. Verified against codex-cli 0.142.4 on the test CVM.
# ---------------------------------------------------------------------------

def test_codex_turn_from_stream_0142_flat_protocol_splits_reasoning():
    raw = (
        '{"type":"task_started"}\n'
        '{"type":"agent_reasoning","text":"**Clarifying meaning**\\nThe user asked what this means; I will keep it concise."}\n'
        '{"type":"agent_message","message":"It means hello."}\n'
        '{"type":"task_complete"}\n'
    )
    reply, reasoning = crc._codex_turn_from_stream(raw)
    assert reply == "It means hello."
    assert "keep it concise" in reasoning


def test_codex_turn_from_stream_0136_item_protocol_still_works():
    raw = (
        '{"type":"item.completed","item":{"type":"reasoning","text":"thinking…"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}\n'
    )
    reply, reasoning = crc._codex_turn_from_stream(raw)
    assert reply == "final answer"
    assert reasoning == "thinking…"


def test_call_agent_cli_codex_0142_routes_reasoning_to_thinking_not_bubble(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        "codex exec --skip-git-repo-check --json --dangerously-bypass-approvals-and-sandbox {message}",
    )
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")

    reasoning_text = (
        'The user has asked, "What does this mean?" but there\'s a typo, so it likely '
        "refers to a previous message. I'm planning to keep my response concise."
    )

    class _R:
        returncode = 0
        stdout = (
            '{"type":"task_started"}\n'
            '{"type":"agent_reasoning","text":' + json.dumps(reasoning_text) + "}\n"
            '{"type":"agent_message","message":"It means hello."}\n'
            '{"type":"task_complete"}\n'
        )
        stderr = ""

    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _R())

    raw = crc.call_agent_cli("hi")
    turn = crc._agent_turn_from_raw(raw)
    # The real reply is the only chat bubble; reasoning never leaks as a message.
    assert turn.messages == ["It means hello."]
    assert reasoning_text not in turn.messages
    # Reasoning rides the thinking disclosure instead.
    assert turn.thinking_summary
    assert "previous message" in turn.thinking_summary or "concise" in turn.thinking_summary


def test_call_agent_cli_codex_actions_reply_preserved_with_reasoning(monkeypatch):
    """A codex agent_message that is itself an actions JSON keeps its actions
    while native reasoning is folded into the internal thinking channel (no bubble leak)."""
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        "codex exec --skip-git-repo-check --json --dangerously-bypass-approvals-and-sandbox {message}",
    )
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")

    actions_msg = '{"actions":[{"type":"proactive.sleep","reason":"broadcast off"}]}'

    class _R:
        returncode = 0
        stdout = (
            '{"type":"agent_reasoning","text":"deciding whether to check in"}\n'
            '{"type":"agent_message","message":' + json.dumps(actions_msg) + "}\n"
        )
        stderr = ""

    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _R())

    raw = crc.call_agent_cli("hi")
    turn = crc._agent_turn_from_raw(raw)
    assert turn.actions == [{"type": "proactive.sleep", "reason": "broadcast off"}]
    assert turn.messages == []
    assert turn.thinking_summary


def test_call_agent_cli_codex_raw_text_lane_returns_literal_reply(monkeypatch):
    """Background memory lanes (raw_text=True) parse the model's literal JSON
    with their own extractors; reasoning must NOT rewrap their output."""
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        "codex exec --skip-git-repo-check --json --dangerously-bypass-approvals-and-sandbox {message}",
    )
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")

    cards_json = '{"cards":[{"title":"x"}]}'

    class _R:
        returncode = 0
        stdout = (
            '{"type":"agent_reasoning","text":"deciding what to remember"}\n'
            '{"type":"agent_message","message":' + json.dumps(cards_json) + "}\n"
        )
        stderr = ""

    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _R())

    out = crc.call_agent_cli("hi", raw_text=True)
    assert out == cards_json


def test_agent_turn_skips_codex_reasoning_event_as_non_final():
    """Defense in depth: even if a raw codex reasoning event reaches the generic
    extractor, it must not be emitted as a chat bubble."""
    raw = '{"type":"agent_reasoning","text":"internal planning the user should never see"}'
    turn = crc._split_agent_turn(raw)
    assert turn.messages == []


def test_agent_turn_keeps_codex_agent_message_flat_event():
    """The flat agent_message event is still a real reply (not skipped)."""
    raw = '{"type":"agent_message","message":"你好"}'
    turn = crc._split_agent_turn(raw)
    assert turn.messages == ["你好"]


def test_openai_http_tool_only_response_preserved(monkeypatch):
    """AGENT_MODE=http openai protocol: a tool-only model reply must be returned
    as structured output (not raise 'no usable reply text')."""
    class _Resp:
        headers = {}
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content":
                    '{"tool_calls": [{"name": "screen.read", "args": {}}]}'}}]}

    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://127.0.0.1:8642/v1/chat/completions")
    monkeypatch.setattr(crc, "AGENT_HTTP_PROTOCOL", "openai")
    monkeypatch.setattr(crc, "AGENT_HTTP_MODEL", "hermes-agent")
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_abc"})
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_save_agent_session_id", lambda sid: None)
    monkeypatch.setattr(crc.httpx, "post", lambda *a, **kw: _Resp())

    result = crc.call_agent_http("hi")
    turn = crc._agent_turn_from_raw(result)
    assert [tc["name"] for tc in turn.tool_calls] == ["screen.read"]


# ---------------------------------------------------------------------------
# OpenClaw `agent --json` output: reply text nests under result.payloads[].text.
# Regression for the VPS onboarding failure where a valid OpenClaw reply was
# reported as "no usable reply after sanitization".
# ---------------------------------------------------------------------------

def test_openclaw_payloads_reply_is_extracted():
    obj = {"runId": "x", "status": "ok", "summary": "completed",
           "result": {"payloads": [{"text": "能看到，这条消息收到了。", "mediaUrl": None}],
                      "meta": {"agentMeta": {"sessionId": "s"}}}}
    # single-reply extractor
    assert crc._reply_from_json_obj(obj) == "能看到，这条消息收到了。"
    # turn from dict and from the raw JSON string (the actual CLI stdout path)
    assert crc._agent_turn_from_raw(obj).messages == ["能看到，这条消息收到了。"]
    assert crc._agent_turn_from_raw(json.dumps(obj)).messages == ["能看到，这条消息收到了。"]


def test_openclaw_nested_session_id_is_extracted():
    raw = json.dumps(
        {
            "runId": "x",
            "result": {
                "payloads": [{"text": "收到。"}],
                "meta": {"agentMeta": {"sessionId": "openclaw_sess_1"}},
            },
        }
    )

    assert crc._extract_session_id(raw) == "openclaw_sess_1"


def test_openclaw_multi_payload_preserves_bubbles():
    obj = {"status": "ok", "result": {"payloads": [{"text": "第一句"}, {"text": "第二句"}]}}
    assert crc._agent_turn_from_raw(obj).messages == ["第一句", "第二句"]


def test_non_openclaw_shapes_unaffected():
    # plain multi-bubble and a bare string still work (no regression)
    assert crc._agent_turn_from_raw({"messages": ["你好"]}).messages == ["你好"]
    assert crc._reply_from_json_obj({"reply": "hi"}) == "hi"


# ---------------------------------------------------------------------------
# whoami TTL cache tests
# ---------------------------------------------------------------------------


def _seed_valid_whoami_cache():
    crc._whoami_cache.update(
        user_id="usr_test", user_pk=b"\x01" * 32, enclave_pk=b"\x02" * 32
    )


def test_refresh_whoami_skips_network_when_fresh(monkeypatch):
    _seed_valid_whoami_cache()
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_TTL_SEC", 300.0)
    monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic())
    called = MagicMock(return_value=True)
    monkeypatch.setattr(crc, "_load_whoami_with_retries", called)

    assert crc._refresh_whoami_for_encrypted_reply() is True
    called.assert_not_called()


def test_refresh_whoami_fetches_when_stale(monkeypatch):
    _seed_valid_whoami_cache()
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_TTL_SEC", 300.0)
    monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic() - 10_000)
    called = MagicMock(return_value=True)
    monkeypatch.setattr(crc, "_load_whoami_with_retries", called)

    assert crc._refresh_whoami_for_encrypted_reply() is True
    called.assert_called_once()


def test_refresh_whoami_ttl_zero_always_fetches(monkeypatch):
    _seed_valid_whoami_cache()
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_TTL_SEC", 0.0)
    monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic())
    called = MagicMock(return_value=True)
    monkeypatch.setattr(crc, "_load_whoami_with_retries", called)

    assert crc._refresh_whoami_for_encrypted_reply() is True
    called.assert_called_once()


def test_refresh_whoami_refetches_when_enclave_pk_missing(monkeypatch):
    # Partial whoami: identity present but enclave_pk missing must NOT be
    # treated as fresh — the gate must fall through and refetch so the
    # missing enclave key can heal.
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_test", "user_pk": b"\x01" * 32, "enclave_pk": None})
    monkeypatch.setattr(crc, "WHOAMI_REFRESH_TTL_SEC", 300.0)
    monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic())
    called = MagicMock(return_value=True)
    monkeypatch.setattr(crc, "_load_whoami_with_retries", called)

    assert crc._refresh_whoami_for_encrypted_reply() is True
    called.assert_called_once()


def test_load_whoami_caches_archive_language(monkeypatch):
    # /v1/users/whoami now returns archive_language (backend/accounts/routes.py)
    # — the consumer must cache it so proactive/introduction wakes with no
    # perception locale still have a language to anchor on.
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "user_id": "usr_lang",
                "public_key": base64.b64encode(b"p" * 32).decode(),
                "enclave_content_public_key_hex": (b"e" * 32).hex(),
                "archive_language": "en",
            }

    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "", "user_pk": None, "enclave_pk": None, "archive_language": ""})
    monkeypatch.setattr(crc.httpx, "get", lambda *a, **kw: _Resp())

    assert crc._load_whoami() is True
    assert crc._whoami_cache["archive_language"] == "en"


def test_load_whoami_defaults_archive_language_to_empty_when_absent(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "user_id": "usr_nolang",
                "public_key": base64.b64encode(b"p" * 32).decode(),
                "enclave_content_public_key_hex": (b"e" * 32).hex(),
            }

    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "", "user_pk": None, "enclave_pk": None, "archive_language": "stale"})
    monkeypatch.setattr(crc.httpx, "get", lambda *a, **kw: _Resp())

    assert crc._load_whoami() is True
    assert crc._whoami_cache["archive_language"] == ""


# ---------------------------------------------------------------------------
# Reply language fallback chain: locale → archive_language → 简体中文 default
# ---------------------------------------------------------------------------


def test_reply_language_line_prefers_presence_locale(monkeypatch):
    monkeypatch.setattr(crc, "_whoami_cache", {"archive_language": "en"})
    line = crc._reply_language_line({"locale": "zh-Hans"})
    assert "zh-Hans" in line


def test_reply_language_line_falls_back_to_archive_language(monkeypatch):
    monkeypatch.setattr(crc, "_whoami_cache", {"archive_language": "en"})
    line = crc._reply_language_line(None)
    assert "en" in line


def test_reply_language_line_treats_empty_locale_as_missing(monkeypatch):
    monkeypatch.setattr(crc, "_whoami_cache", {"archive_language": "en"})
    line = crc._reply_language_line({"locale": ""})
    assert "en" in line


def test_reply_language_line_defaults_to_chinese_with_no_locale_or_archive(monkeypatch):
    monkeypatch.setattr(crc, "_whoami_cache", {"archive_language": ""})
    line = crc._reply_language_line(None)
    assert "中文" in line
    assert "Always reply in the user's own language." != line


def test_reply_language_line_defaults_to_chinese_when_archive_language_key_missing(monkeypatch):
    monkeypatch.setattr(crc, "_whoami_cache", {})
    line = crc._reply_language_line(None)
    assert "中文" in line


# ---------------------------------------------------------------------------
# Provider payment (402) circuit breaker tests
# ---------------------------------------------------------------------------


def test_is_provider_payment_error_matches_402_runtimeerror():
    exc = RuntimeError(
        "cli agent exited 1: unexpected status 402 Payment Required: "
        "This request requires more credits"
    )
    assert crc._is_provider_payment_error(exc) is True


def test_is_provider_payment_error_ignores_other_errors():
    assert crc._is_provider_payment_error(RuntimeError("connection reset")) is False


def test_provider_payment_cooldown_lifecycle(monkeypatch):
    monkeypatch.setattr(crc, "PROVIDER_PAYMENT_COOLDOWN_SEC", 600.0)
    crc._clear_provider_payment_cooldown()
    assert crc._provider_payment_cooling_down() is False

    crc._note_provider_payment_failure()
    assert crc._provider_payment_cooling_down() is True

    # Simulate cooldown expiry.
    monkeypatch.setattr(crc, "_provider_payment_cooldown_until", time.monotonic() - 1)
    assert crc._provider_payment_cooling_down() is False

    crc._clear_provider_payment_cooldown()
    assert crc._provider_payment_cooling_down() is False


# ---------------------------------------------------------------------------
# Foreground chat context injection (codex / claude hosted BYOK)
# ---------------------------------------------------------------------------
# codex has no --resume and hosted claude's default command carries no session,
# so cross-turn continuity is injected by the resident: a short recent-chat
# transcript is prepended to the current turn. pi resumes natively and is skipped
# to avoid double context. See _foreground_history_injection_enabled /
# _foreground_agent_message.

_CODEX_CLI = (
    "codex exec --skip-git-repo-check --json "
    "--dangerously-bypass-approvals-and-sandbox {message}"
)
_CLAUDE_CLI = "claude --allowed-tools 'Bash' --append-system-prompt-file /h/p -p {message}"
_PI_CLI = "pi --mode json -t bash --session-id {session_id} {message}"


def test_foreground_injection_enabled_for_codex(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _CODEX_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    assert crc._foreground_history_injection_enabled() is True


def test_foreground_injection_enabled_for_claude(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _CLAUDE_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    assert crc._foreground_history_injection_enabled() is True


def test_foreground_injection_skipped_for_pi(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _PI_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    assert crc._foreground_history_injection_enabled() is False


def test_foreground_injection_skipped_for_claude_with_resume_in_template(monkeypatch):
    # An operator-configured claude command that already carries native
    # continuity (--resume) must NOT also get a resident-prepended transcript.
    monkeypatch.setattr(
        crc, "AGENT_CLI_CMD", "claude --resume {session_id} -p {message}"
    )
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    assert crc._foreground_history_injection_enabled() is False


def test_foreground_injection_skipped_for_claude_with_session_id_in_template(monkeypatch):
    monkeypatch.setattr(
        crc, "AGENT_CLI_CMD", "claude --session-id {session_id} -p {message}"
    )
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    assert crc._foreground_history_injection_enabled() is False


def test_foreground_injection_off_mode_disables_even_codex(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _CODEX_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "off")
    assert crc._foreground_history_injection_enabled() is False


def test_foreground_injection_on_mode_forces_even_pi(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _PI_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "on")
    assert crc._foreground_history_injection_enabled() is True


def test_foreground_message_prepends_recent_transcript(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _CODEX_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    now = time.time()
    hist = [
        {"role": "user", "content": "今天北京天气怎么样", "ts": now - 120},
        {"role": "agent", "content": "晴，十八度", "ts": now - 118},
        {"role": "user", "content": "那要穿外套吗", "ts": now},  # current turn
    ]
    monkeypatch.setattr(crc, "get_decrypted_history", lambda since, limit=20: list(hist))

    out = crc._foreground_agent_message("那要穿外套吗", current_ts=now)

    assert "那要穿外套吗" in out          # the current message is still present
    assert "今天北京天气" in out          # prior user turn injected
    assert "十八度" in out               # prior agent turn injected
    assert out.count("那要穿外套吗") == 1  # current turn not duplicated in transcript
    # transcript (older context) appears before the current message
    assert out.index("今天北京天气") < out.index("那要穿外套吗")


def test_foreground_transcript_default_keeps_50_prior_messages(monkeypatch):
    # Default limit is 50 messages (~25 full rounds), sitting exactly at the
    # clamp in _recent_chat_context_for_foreground — raising it further needs
    # both the env default AND that cap changed together.
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _CODEX_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    now = time.time()
    hist = [
        {
            "role": "user" if i % 2 else "agent",
            "content": f"历史消息-{i:02d}",
            "ts": now - (70 - i),
        }
        for i in range(1, 61)
    ] + [{"role": "user", "content": "当前这句", "ts": now}]
    monkeypatch.setattr(crc, "get_decrypted_history", lambda since, limit=20: list(hist))

    out = crc._foreground_agent_message("当前这句", current_ts=now)

    assert "历史消息-60" in out  # newest prior message injected
    assert "历史消息-11" in out  # 50th-from-last prior message still included
    assert "历史消息-10" not in out  # beyond the 50-message window


def test_foreground_message_no_decrypt_source_returns_plain_content(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _CODEX_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    monkeypatch.setattr(crc, "get_decrypted_history", lambda since, limit=20: None)
    assert crc._foreground_agent_message("你好", current_ts=time.time()) == "你好"


def test_foreground_message_first_turn_has_no_prior_returns_plain(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _CODEX_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    now = time.time()
    # history holds only the current message — nothing strictly older
    monkeypatch.setattr(
        crc, "get_decrypted_history",
        lambda since, limit=20: [{"role": "user", "content": "第一句", "ts": now}],
    )
    assert crc._foreground_agent_message("第一句", current_ts=now) == "第一句"


def test_foreground_message_skipped_for_pi_returns_plain(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", _PI_CLI)
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    monkeypatch.setattr(
        crc, "get_decrypted_history",
        lambda since, limit=20: [{"role": "user", "content": "早", "ts": 1.0}],
    )
    assert crc._foreground_agent_message("hi", current_ts=9.0) == "hi"


def test_claude_resume_skipped_when_transcript_was_injected(monkeypatch):
    # When THIS turn's message actually carries an injected transcript, the
    # resident drops the fragile --resume: the transcript is the single source.
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'claude -p "{message}"')
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_123")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    injected = f"{crc.FOREGROUND_CHAT_CONTEXT_HEADER}\n- prior turn\n\nhello"
    cmd = crc._prepare_cli_command(injected)

    assert "--resume" not in cmd
    assert "sess_123" not in cmd
    # print/json framing is unchanged
    assert "--output-format" in cmd and "json" in cmd


def test_claude_resume_kept_when_no_transcript_injected(monkeypatch):
    # Injection is configured (auto + claude) but this turn's history was
    # unavailable (no enclave / empty fetch), so the message is bare. Resume must
    # survive as the fallback continuity path instead of being dropped blindly.
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'claude -p "{message}"')
    monkeypatch.setattr(crc, "FOREGROUND_CHAT_CONTEXT_MODE", "auto")
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_123")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")  # no injected-transcript header

    assert cmd[:3] == ["claude", "--resume", "sess_123"]


def test_advance_past_unfetchable_skips_to_max_claimed_ts():
    # When decrypt-history never returns the poll-claimed rows, the cursor must jump
    # past the newest claimed message so a later, decryptable message isn't blocked.
    poll = [{"id": "a", "ts": 100.0}, {"id": "b", "ts": 250.5}, {"id": "c", "ts": 175.0}]
    assert crc._advance_past_unfetchable(100.0, poll) == 250.5


def test_advance_past_unfetchable_boundary_message_gets_nudged():
    # The wedge case: the stuck message sits exactly at the cursor (exclusive
    # `since` can never return it). Advancing to max==last_ts would loop forever, so
    # the cursor must be nudged strictly past it.
    ts = 1783080703.7508044
    out = crc._advance_past_unfetchable(ts, [{"id": "x", "ts": ts}])
    assert out > ts


# ---------------------------------------------------------------------------
# Per-user heartbeat cadence: _proactive_tick_interval_for_broadcast_state reads
# the user's wake_interval_sec (companionship frequency) from the tick decision.
# ---------------------------------------------------------------------------

def test_wake_interval_per_user_value_wins():
    # A valid per-user value is used verbatim (within clamp range).
    assert crc._proactive_tick_interval_for_broadcast_state("off", 900) == 900
    assert crc._proactive_tick_interval_for_broadcast_state("off", 3600) == 3600
    assert crc._proactive_tick_interval_for_broadcast_state("off", 43200) == 43200


def test_wake_interval_clamps_out_of_range():
    # Below the 15min floor is raised to 900; above the 12h ceiling capped at 43200.
    assert crc._proactive_tick_interval_for_broadcast_state("off", -5) == 900
    assert crc._proactive_tick_interval_for_broadcast_state("off", 0) == 900
    assert crc._proactive_tick_interval_for_broadcast_state("off", 100) == 900
    assert crc._proactive_tick_interval_for_broadcast_state("off", 899) == 900
    assert crc._proactive_tick_interval_for_broadcast_state("off", 43201) == 43200
    assert crc._proactive_tick_interval_for_broadcast_state("off", 999999) == 43200


def test_wake_interval_falls_back_when_absent_or_invalid():
    # Missing / non-numeric -> env fallback (aligned to 7200 default).
    fallback = max(60, crc.PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC)
    assert crc._proactive_tick_interval_for_broadcast_state("off", None) == fallback
    assert crc._proactive_tick_interval_for_broadcast_state("off", "abc") == fallback
    # Fallback default itself is the 2h product default when env is unset.
    assert crc.PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC == 7200


def test_wake_interval_independent_of_broadcast_state():
    # Heartbeat cadence is decoupled from screen-share broadcast_state: the
    # per-user value applies regardless of on/off/paused/unknown.
    for state in ("off", "on", "broadcasting", "paused", "unknown", ""):
        assert crc._proactive_tick_interval_for_broadcast_state(state, 3600) == 3600


def test_context_text_image_message_advertises_chat_image_pull():
    # Injected-history transcript can't carry image pixels (see summary), so an
    # image turn must advertise the exact io_cli command that lazily pulls it,
    # keyed by message id — otherwise the agent guesses (photo-read = wrong tool)
    # or fabricates the picture.
    text = crc._message_text_for_context(
        {"id": "msg_abc", "role": "user", "content_type": "image", "content": ""}
    )
    assert "msg_abc" in text
    assert "chat-image" in text


def test_context_text_image_with_caption_keeps_caption_and_hint():
    text = crc._message_text_for_context(
        {"id": "m1", "role": "user", "content_type": "image", "content": "这是什么?"}
    )
    assert "这是什么?" in text          # the user's question is preserved
    assert "chat-image --id m1" in text  # ...alongside the pull command


def test_context_text_plain_text_message_unchanged():
    assert crc._message_text_for_context({"content": "hello there"}) == "hello there"
