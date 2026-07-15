import base64
import json
import secrets
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from tools.provider_smoke import client, crypto


def _session() -> client.Session:
    sk, pk = crypto.generate_keypair()
    return client.Session("usr_test", "feedling-key", sk, pk)


def _envelope(plaintext: str, sess: client.Session, *, item_id: str) -> dict:
    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    aad = f"{sess.user_id}|1|{item_id}".encode()
    body_ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext.encode(), aad)
    return {
        "owner_user_id": sess.user_id,
        "v": 1,
        "id": item_id,
        "body_ct": base64.b64encode(body_ct).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "K_user": base64.b64encode(crypto.box_seal(key, sess.pk)).decode(),
    }


def _user_turn(item_id: str, ts: float, *, reply_message_id: str = "") -> dict:
    row = {"id": item_id, "role": "user", "ts": ts}
    if reply_message_id:
        row["reply_message_id"] = reply_message_id
    return row


def _assistant_turn(
    plaintext: str,
    sess: client.Session,
    *,
    item_id: str,
    ts: float,
    reply_to_message_id: str = "",
) -> dict:
    row = {
        **_envelope(plaintext, sess, item_id=item_id),
        "role": "openclaw",
        "ts": ts,
    }
    if reply_to_message_id:
        row["reply_to_message_id"] = reply_to_message_id
    return row


def test_is_hosted_response_true_for_202_contract():
    body = {
        "status": "processing",
        "runtime": {"engine": "feedling_agent_runtime", "mode": "hosted_agent"},
    }
    assert client.is_hosted_response(body)


def test_is_hosted_response_false_for_native_200():
    body = {"status": "ok", "reply": "hi", "runtime": {"engine": "native"}}
    assert not client.is_hosted_response(body)


def test_newest_openclaw_after_filters_and_picks_latest():
    msgs = [
        {"role": "user", "ts": 100, "body_ct": "x"},  # not openclaw
        {"role": "openclaw", "ts": 90, "body_ct": "old"},  # before cutoff
        {"role": "openclaw", "ts": 110, "body_ct": "a"},  # candidate
        {"role": "openclaw", "ts": 120, "body_ct": "b"},  # newest candidate
        {"role": "openclaw", "ts": 130, "body_ct": ""},  # no body -> skip
    ]
    picked = client.newest_openclaw_after(msgs, after_ts=100)
    assert picked["ts"] == 120 and picked["body_ct"] == "b"


def test_newest_openclaw_after_returns_none_when_empty():
    assert client.newest_openclaw_after([], after_ts=0) is None


def test_newest_openclaw_after_accepts_assistant_and_agent_roles():
    msgs = [
        {"role": "assistant", "ts": 110, "body_ct": "a"},
        {"role": "agent", "ts": 120, "body_ct": "b"},
    ]
    picked = client.newest_openclaw_after(msgs, after_ts=100)
    assert picked["ts"] == 120


def test_correlated_reply_uses_parent_reply_message_id():
    sess = _session()
    reply = _assistant_turn("exact", sess, item_id="reply-1", ts=11)
    messages = [_user_turn("user-1", 10, reply_message_id="reply-1"), reply]

    assert (
        client.correlated_openclaw_reply(
            messages,
            "user-1",
            user_message_ts=10,
        )["id"]
        == "reply-1"
    )


def test_correlated_reply_accepts_direct_reply_to_link():
    sess = _session()
    reply = _assistant_turn(
        "exact",
        sess,
        item_id="reply-1",
        ts=11,
        reply_to_message_id="user-1",
    )

    assert (
        client.correlated_openclaw_reply(
            [_user_turn("user-1", 10), reply],
            "user-1",
        )["id"]
        == "reply-1"
    )


def test_correlated_reply_waits_when_turn_or_reply_is_not_visible_yet():
    assert client.correlated_openclaw_reply([], "user-1") is None
    assert (
        client.correlated_openclaw_reply(
            [_user_turn("user-1", 10)],
            "user-1",
        )
        is None
    )


def test_correlated_reply_rejects_duplicate_replies():
    sess = _session()
    messages = [
        _user_turn("user-1", 10, reply_message_id="reply-1"),
        _assistant_turn(
            "one",
            sess,
            item_id="reply-1",
            ts=11,
            reply_to_message_id="user-1",
        ),
        _assistant_turn(
            "two",
            sess,
            item_id="reply-2",
            ts=12,
            reply_to_message_id="user-1",
        ),
    ]

    with pytest.raises(client.SmokeError, match="duplicate replies") as exc:
        client.correlated_openclaw_reply(messages, "user-1")
    assert exc.value.stage == "reply-correlation"


def test_correlated_reply_rejects_unrelated_assistant_record():
    sess = _session()
    messages = [
        _user_turn("user-1", 10),
        _assistant_turn(
            "wrong turn",
            sess,
            item_id="reply-other",
            ts=11,
            reply_to_message_id="user-other",
        ),
    ]

    with pytest.raises(client.SmokeError, match="unrelated assistant reply"):
        client.correlated_openclaw_reply(messages, "user-1")


def test_correlated_reply_rejects_out_of_order_history_records():
    sess = _session()
    messages = [
        _user_turn("user-1", 10, reply_message_id="reply-1"),
        _assistant_turn("exact", sess, item_id="reply-1", ts=9),
    ]

    with pytest.raises(client.SmokeError, match="out of timestamp order"):
        client.correlated_openclaw_reply(messages, "user-1")


def test_correlated_reply_rejects_later_user_turn_before_reply():
    sess = _session()
    messages = [
        _user_turn("user-1", 10, reply_message_id="reply-1"),
        _user_turn("user-2", 11),
        _assistant_turn("late", sess, item_id="reply-1", ts=12),
    ]

    with pytest.raises(client.SmokeError, match="later user turn"):
        client.correlated_openclaw_reply(messages, "user-1")


def test_is_hosted_response_false_when_processing_without_runtime():
    assert not client.is_hosted_response({"status": "processing"})


def test_identity_init_body_has_required_fields():
    body = client.identity_init_body()
    assert set(body) >= {"identity", "days_with_user", "relationship_anchor_evidence"}
    assert body["days_with_user"] == 0 and isinstance(body["days_with_user"], int)
    assert len(body["relationship_anchor_evidence"]) >= 8
    assert set(body["identity"]) >= {"agent_name", "self_introduction", "dimensions"}
    assert body["identity"]["dimensions"] == []


def test_decrypt_reply_record_includes_separate_thinking_envelope():
    sess = _session()
    message = {
        **_envelope("visible reply", sess, item_id="reply-1"),
        "role": "openclaw",
        "ts": 10,
    }
    thinking = _envelope("safe reasoning summary", sess, item_id="thinking-1")
    message.update(
        {
            "thinking_v": thinking["v"],
            "thinking_id": thinking["id"],
            "thinking_owner_user_id": thinking["owner_user_id"],
            "thinking_body_ct": thinking["body_ct"],
            "thinking_nonce": thinking["nonce"],
            "thinking_K_user": thinking["K_user"],
            "thinking_kind": "provider_reasoning_summary",
            "thinking_source": "openrouter",
            "thinking_model": "anthropic/claude-test",
            "thinking_native": True,
        }
    )

    record = client.decrypt_reply_record(message, sess.sk, sess.pk)

    assert record["reply"] == "visible reply"
    assert record["thinking"] == "safe reasoning summary"
    assert record["thinking_present"] is True
    assert record["thinking_kind"] == "provider_reasoning_summary"
    assert record["thinking_source"] == "openrouter"
    assert record["thinking_model"] == "anthropic/claude-test"
    assert record["thinking_native"] is True
    assert record["message"] == message and record["message"] is not message


def test_decrypt_reply_record_uses_parent_aad_fallback_for_thinking():
    sess = _session()
    message = {**_envelope("visible", sess, item_id="shared-id"), "role": "agent"}
    thinking = _envelope("thought", sess, item_id="shared-id")
    message.update(
        {
            "thinking_body_ct": thinking["body_ct"],
            "thinking_nonce": thinking["nonce"],
            "thinking_K_user": thinking["K_user"],
        }
    )

    assert (
        client.decrypt_reply_record(message, sess.sk, sess.pk)["thinking"] == "thought"
    )


def test_decrypt_reply_record_rejects_partial_thinking_envelope():
    sess = _session()
    message = {
        **_envelope("visible", sess, item_id="reply-2"),
        "thinking_body_ct": "ciphertext",
    }

    with pytest.raises(client.SmokeError, match="incomplete thinking envelope") as exc:
        client.decrypt_reply_record(message, sess.sk, sess.pk)
    assert exc.value.stage == "thinking-decrypt"


def test_explicit_legacy_polling_preserves_timestamp_only_behavior(monkeypatch):
    sess = _session()
    message = {
        **_envelope("hello", sess, item_id="reply-3"),
        "role": "openclaw",
        "ts": 20,
    }
    smoke = client.SmokeClient("https://example.test")
    monkeypatch.setattr(
        smoke, "_req", lambda *args, **kwargs: (200, {"messages": [message]})
    )

    record = smoke.poll_reply_record_legacy(sess, after_ts=10, timeout=1, interval=0)
    assert record["reply"] == "hello"
    assert record["message"]["id"] == "reply-3"
    assert smoke.poll_reply_legacy(sess, after_ts=10, timeout=1, interval=0) == "hello"


@pytest.mark.parametrize("method_name", ("poll_reply", "poll_reply_record"))
def test_qualification_polling_never_falls_back_to_timestamp(monkeypatch, method_name):
    sess = _session()
    tempting_unrelated_reply = {
        **_envelope("wrong turn", sess, item_id="reply-unrelated"),
        "role": "openclaw",
        "ts": 20,
    }
    smoke = client.SmokeClient("https://example.test")
    calls = []

    def fake_req(*args, **kwargs):
        calls.append((args, kwargs))
        return 200, {"messages": [tempting_unrelated_reply]}

    monkeypatch.setattr(smoke, "_req", fake_req)

    with pytest.raises(
        client.SmokeError, match="exact user_message_id is required"
    ) as exc:
        getattr(smoke, method_name)(sess, after_ts=10, timeout=1, interval=0)

    assert exc.value.stage == "reply-correlation"
    assert calls == []


def test_qualification_polling_accepts_explicit_exact_turn_id(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    reply = _assistant_turn("exact", sess, item_id="reply-1", ts=11)
    monkeypatch.setattr(
        smoke,
        "_req",
        lambda *args, **kwargs: (
            200,
            {
                "messages": [
                    _user_turn("user-1", 10, reply_message_id="reply-1"),
                    reply,
                ],
            },
        ),
    )

    record = smoke.poll_reply_record(
        sess,
        after_ts=10,
        timeout=1,
        interval=0,
        user_message_id="user-1",
    )

    assert record["reply"] == "exact"
    assert record["message"]["id"] == "reply-1"


def test_qualification_polling_passes_remaining_deadline_to_single_http_attempt(
    monkeypatch,
):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    reply = _assistant_turn("exact", sess, item_id="reply-1", ts=11)
    calls = []

    def fake_req(*args, **kwargs):
        calls.append((args, kwargs))
        return 200, {
            "messages": [
                _user_turn("user-1", 10, reply_message_id="reply-1"),
                reply,
            ]
        }

    monkeypatch.setattr(smoke, "_req", fake_req)

    smoke.poll_reply_record(
        sess,
        after_ts=10,
        timeout=3,
        interval=0,
        user_message_id="user-1",
    )

    assert len(calls) == 1
    assert calls[0][1]["attempts"] == 1
    assert 0 < calls[0][1]["read_timeout"] <= 3


def test_send_then_qualification_poll_uses_cached_exact_turn_id(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    reply = _assistant_turn("exact", sess, item_id="reply-1", ts=11)
    calls = []

    def fake_req(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/v1/model_api/chat/send":
            return 202, {
                "status": "processing",
                "runtime": {"engine": "feedling_agent_runtime"},
                "user_message": {"id": "user-1", "ts": 10},
            }
        return 200, {
            "messages": [
                _user_turn("user-1", 10, reply_message_id="reply-1"),
                reply,
            ],
        }

    monkeypatch.setattr(smoke, "_req", fake_req)
    sent = smoke.send(sess, "hello")

    assert (
        smoke.poll_reply(
            sess,
            sent["user_message"]["ts"],
            timeout=1,
            interval=0,
        )
        == "exact"
    )
    assert calls[1][0] == "GET"
    assert "since=9.999000" in calls[1][1]
    assert "limit=200" in calls[1][1]


def test_send_does_not_replay_post_when_response_is_lost():
    sess = _session()
    smoke = client.SmokeClient("https://example.test")

    class LostResponseOpener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout):  # noqa: ANN001, ARG002
            self.calls += 1
            raise urllib.error.URLError("response lost after server accepted request")

    opener = LostResponseOpener()
    smoke._opener = opener

    with pytest.raises(client.SmokeError, match="failed after 1 tries") as exc:
        smoke.send(sess, "hello")

    assert exc.value.stage == "network"
    assert opener.calls == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:password@example.test",
        "https://example.test/prefix",
        "https://example.test?next=https://evil.test",
        "https://example.test#fragment",
        "ftp://example.test",
    ],
)
def test_smoke_client_requires_bare_http_origin(base_url):
    with pytest.raises(ValueError, match=r"http\(s\) origin"):
        client.SmokeClient(base_url)


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/v1/users/register",
        "//evil.test/v1/users/register",
        "v1/users/register",
        "/v1\\evil.test",
    ],
)
def test_smoke_client_rejects_request_target_escape(path):
    smoke = client.SmokeClient("https://example.test")

    with pytest.raises(client.SmokeError, match="configured Feedling origin") as exc:
        smoke._req("GET", path, attempts=1)
    assert exc.value.stage == "request"


def test_smoke_client_does_not_forward_user_or_provider_keys_on_redirect():
    source_requests = []
    target_requests = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            target_requests.append(
                {
                    "api_key": self.headers.get("X-API-Key"),
                    "body": self.rfile.read(
                        int(self.headers.get("Content-Length") or 0)
                    ),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):  # noqa: ANN002
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_url = f"http://127.0.0.1:{target.server_port}/stolen"

    class SourceHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            source_requests.append(
                {
                    "api_key": self.headers.get("X-API-Key"),
                    "body": self.rfile.read(
                        int(self.headers.get("Content-Length") or 0)
                    ),
                }
            )
            self.send_response(307)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, *args):  # noqa: ANN002
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (source, target)
    ]
    for thread in threads:
        thread.start()

    try:
        smoke = client.SmokeClient(f"http://127.0.0.1:{source.server_port}")
        sess = _session()
        with pytest.raises(client.SmokeError, match="refused status=307") as exc:
            smoke.setup_raw(
                sess,
                "openrouter",
                "model-x",
                "https://openrouter.ai/api/v1",
                "provider-secret",
            )
        assert exc.value.stage == "redirect"
    finally:
        source.shutdown()
        target.shutdown()
        source.server_close()
        target.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert len(source_requests) == 1
    assert source_requests[0]["api_key"] == sess.api_key
    assert json.loads(source_requests[0]["body"])["api_key"] == "provider-secret"
    assert target_requests == []


def test_setup_raw_exposes_invalid_key_response_and_setup_remains_strict(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    calls = []

    def fake_req(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return 401, {"error": "provider_auth_invalid"}

    monkeypatch.setattr(smoke, "_req", fake_req)
    status, body = smoke.setup_raw(sess, "openrouter", "model-x", "", "bad-key")
    assert status == 401 and body["error"] == "provider_auth_invalid"
    assert calls[0][2]["body"] == {
        "provider": "openrouter",
        "model": "model-x",
        "api_key": "bad-key",
    }
    with pytest.raises(client.SmokeError, match="provider_auth_invalid"):
        smoke.setup(sess, "openrouter", "model-x", "", "bad-key")


def test_setup_can_explicitly_request_reasoning_effort(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    calls = []

    def fake_req(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return 200, {
            "status": "configured",
            "config": {"reasoning_effort": kwargs["body"].get("reasoning_effort")},
        }

    monkeypatch.setattr(smoke, "_req", fake_req)

    assert smoke.setup(
        sess,
        "openrouter",
        "model-x",
        "https://openrouter.ai/api/v1",
        "valid-key",
        reasoning_effort="high",
    ) == {"reasoning_effort": "high"}
    assert calls == [
        (
            "POST",
            "/v1/model_api/setup",
            {
                "api_key": sess.api_key,
                "body": {
                    "provider": "openrouter",
                    "model": "model-x",
                    "api_key": "valid-key",
                    "base_url": "https://openrouter.ai/api/v1",
                    "reasoning_effort": "high",
                },
            },
        )
    ]


def test_setup_raw_omits_reasoning_effort_by_default(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    calls = []

    def fake_req(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return 200, {"status": "configured"}

    monkeypatch.setattr(smoke, "_req", fake_req)
    smoke.setup_raw(sess, "deepseek", "model-x", "", "valid-key")

    assert "reasoning_effort" not in calls[0][2]["body"]


def test_trace_helpers_use_user_scoped_endpoints(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    calls = []

    def fake_req(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/v1/debug/trace/enable":
            return 200, {"enabled": True, "deploy_enabled": True}
        if path.startswith("/v1/debug/trace?"):
            return 200, {"enabled": True, "events": [{"type": "agent.model.call.done"}]}
        return 200, {"status": "ok"}

    monkeypatch.setattr(smoke, "_req", fake_req)
    assert smoke.enable_trace(sess)["enabled"] is True
    assert smoke.read_trace(sess, limit=17, subsystem="model api")["events"]
    assert smoke.clear_trace(sess) == {"status": "ok"}

    assert calls[0] == (
        "POST",
        "/v1/debug/trace/enable",
        {"api_key": sess.api_key, "body": {"enabled": True}},
    )
    assert calls[1][0] == "GET"
    assert calls[1][1] == "/v1/debug/trace?limit=17&subsystem=model+api"
    assert calls[2][:2] == ("DELETE", "/v1/debug/trace")


def test_runtime_status_uses_user_scoped_endpoint(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    calls = []

    def fake_req(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return 200, {
            "configured": True,
            "runtime_mode": "hosted_resident",
            "runtime_version": 2,
        }

    monkeypatch.setattr(smoke, "_req", fake_req)

    assert smoke.runtime_status(sess)["runtime_version"] == 2
    assert calls == [
        (
            "GET",
            "/v1/model_api/runtime",
            {"api_key": sess.api_key},
        )
    ]


@pytest.mark.parametrize(
    "response",
    [
        (503, {"error": "unavailable"}),
        (200, ["not", "an", "object"]),
    ],
)
def test_runtime_status_rejects_unusable_response(monkeypatch, response):
    smoke = client.SmokeClient("https://example.test")
    monkeypatch.setattr(smoke, "_req", lambda *args, **kwargs: response)

    with pytest.raises(client.SmokeError) as exc:
        smoke.runtime_status(_session())

    assert exc.value.stage == "runtime"


def test_reset_account_requires_confirmed_success(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    calls = []

    def fake_req(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return 200, {"deleted": True, "user_id": sess.user_id}

    monkeypatch.setattr(smoke, "_req", fake_req)
    assert smoke.reset_account(sess)["deleted"] is True
    assert calls == [
        (
            "POST",
            "/v1/account/reset",
            {"api_key": sess.api_key, "body": {"confirm": "delete-all-data"}},
        )
    ]


def test_reset_account_raises_when_cleanup_did_not_complete(monkeypatch):
    sess = _session()
    smoke = client.SmokeClient("https://example.test")
    monkeypatch.setattr(
        smoke,
        "_req",
        lambda *args, **kwargs: (503, {"error": "archive_cleanup_failed"}),
    )

    with pytest.raises(client.SmokeError, match="archive_cleanup_failed") as exc:
        smoke.reset_account(sess)
    assert exc.value.stage == "cleanup"
