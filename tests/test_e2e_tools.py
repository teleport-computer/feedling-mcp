from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from nacl.public import PrivateKey

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.e2e import config, hosted, p0
from tools.e2e.client import E2EClient, TEST_API, _refuse_prod


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _client() -> E2EClient:
    return E2EClient(
        TEST_API,
        "e2e-user",
        "e2e-key",
        PrivateKey.generate(),
        bytes(PrivateKey.generate().public_key),
    )


def test_refuse_prod_allows_test_api() -> None:
    _refuse_prod(TEST_API)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.feedling.app",
        "https://mcp.feedling.app",
        "https://example.test",
    ],
)
def test_refuse_prod_rejects_every_non_test_host(url: str) -> None:
    with pytest.raises(RuntimeError, match="only permits the test API"):
        _refuse_prod(url)


def test_envelope_seal_open_roundtrip() -> None:
    client = _client()
    try:
        envelope = client._seal("青色 continuity")
        assert client.open_envelope(envelope) == "青色 continuity"
    finally:
        client._http.close()


def test_message_text_accepts_plaintext_and_encrypted_history_shapes() -> None:
    client = _client()
    try:
        envelope = client._seal("sealed reply")
        assert client.message_text({"content": "plain reply"}) == "plain reply"
        assert client.message_text({"body": "plaintext-tier reply"}) == "plaintext-tier reply"
        assert client.message_text(envelope) == "sealed reply"
        assert client.message_text({"envelope": envelope}) == "sealed reply"
    finally:
        client._http.close()


def test_decrypt_reply_strict_decrypts_inline_and_nested_envelopes() -> None:
    # The P0 hard-blocker path: prove the USER can read the reply with THEIR key.
    client = _client()
    try:
        envelope = client._seal("sealed reply 青色")
        # inline envelope fields (the /v1/chat/history row shape)
        assert client.decrypt_reply(envelope) == "sealed reply 青色"
        assert client.decrypt_reply({**envelope, "role": "agent", "content": ""}) \
            == "sealed reply 青色"
        # nested envelope
        assert client.decrypt_reply({"envelope": envelope}) == "sealed reply 青色"
    finally:
        client._http.close()


def test_decrypt_reply_never_falls_back_to_server_plaintext() -> None:
    # THE regression guard: unlike message_text, decrypt_reply must NOT accept a
    # server-provided `content` shortcut — that would mask usr_f13f922a (reply
    # arrives, user still cannot decrypt it). No envelope ⇒ hard raise.
    client = _client()
    try:
        with pytest.raises(Exception):
            client.decrypt_reply({"content": "server said it's fine", "role": "agent"})
        with pytest.raises(Exception):
            client.decrypt_reply({"role": "agent"})           # nothing to decrypt
    finally:
        client._http.close()


def test_decrypt_reply_raises_on_undecryptable_envelope() -> None:
    # A present-but-corrupt envelope (AEAD tag mismatch) must surface as a failure,
    # not a silent empty string — the user genuinely cannot read this reply.
    import base64

    client = _client()
    try:
        envelope = dict(client._seal("sealed reply"))
        envelope["body_ct"] = base64.b64encode(b"garbage-ciphertext-not-valid").decode()
        with pytest.raises(Exception):
            client.decrypt_reply(envelope)
    finally:
        client._http.close()


def test_read_reply_strict_enforces_plaintext_tier_shape() -> None:
    client = _client()
    client.content_encryption_effective = "off"
    try:
        assert client.read_reply_strict({"body": "plain reply"}) == "plain reply"
        with pytest.raises(RuntimeError, match="no body"):
            client.read_reply_strict({"content": "shortcut must not pass"})
        with pytest.raises(RuntimeError, match="retains crypto fields"):
            client.read_reply_strict({"body": "mixed", "body_ct": "sealed"})
    finally:
        client._http.close()


def test_teardown_failure_is_a_hard_failure() -> None:
    class FailingHTTP:
        def __init__(self):
            self.closed = False

        def request(self, *_args, **_kwargs) -> FakeResponse:
            # the transport-retry wrapper routes every verb through .request()
            return FakeResponse(500, {"error": "reset_failed"})

        def post(self, *_args, **_kwargs) -> FakeResponse:
            return self.request()

        def close(self) -> None:
            self.closed = True

    client = _client()
    client._http.close()
    failing_http = FailingHTTP()
    client._http = failing_http  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.__exit__(None, None, None)
    assert failing_http.closed


def test_hosted_503_retry_reuses_client_message_id(monkeypatch) -> None:
    responses = [
        FakeResponse(503, {"error": "workers_unavailable"}),
        FakeResponse(202, {"user_message": {"id": "m1", "ts": 123.5}}),
    ]

    class FakeClient:
        def __init__(self):
            self.payloads: list[dict] = []

        def post(self, _path: str, *, json: dict) -> FakeResponse:
            self.payloads.append(json)
            return responses.pop(0)

    client = FakeClient()
    monkeypatch.setattr(hosted.time, "sleep", lambda _seconds: None)

    sent_at, error = hosted._hosted_send(client, "hello")  # type: ignore[arg-type]

    assert error == ""
    assert sent_at == 123.5
    assert len(client.payloads) == 2
    first_id, second_id = [payload["client_msg_id"] for payload in client.payloads]
    assert first_id == second_id
    assert str(uuid.UUID(first_id)) == first_id


def test_load_keys_parses_pool_file(monkeypatch, tmp_path) -> None:
    keys_file = tmp_path / "keys.env"
    keys_file.write_text(
        "# release key pool\n"
        "E2E_KEY_ANTHROPIC='sk-ant-test'\n"
        'E2E_RELAY_BASE="https://relay.test/v1"\n'
        "E2E_RELAY_MODEL=model=tagged\n"
        "ignored-line\n"
    )
    monkeypatch.setattr(config, "KEYS_FILE", keys_file)

    assert config.load_keys() == {
        "E2E_KEY_ANTHROPIC": "sk-ant-test",
        "E2E_RELAY_BASE": "https://relay.test/v1",
        "E2E_RELAY_MODEL": "model=tagged",
    }


def test_p0_blocking_verdict() -> None:
    assert not p0.p0_blocks_release([])
    assert not p0.p0_blocks_release([
        {"cell": "missing-key", "result": "skip"},
        {"cell": "memory-late", "result": "ok"},
    ])
    assert p0.p0_blocks_release([
        {"cell": "healthy", "result": "ok"},
        {"cell": "chat-failed", "result": "fail"},
    ])


# --- hardening batch (codex2 R1 on the shakedown fixes) ----------------------

def _sleepless(monkeypatch):
    slept: list[float] = []
    import tools.e2e.client as client_mod
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))
    return slept


def test_transport_retry_three_attempts_backoff_only_between(monkeypatch) -> None:
    import httpx

    slept = _sleepless(monkeypatch)
    client = _client()
    client._http.close()
    calls = {"n": 0}

    class FlakyHTTP:
        def request(self, *_a, **_kw):
            calls["n"] += 1
            raise httpx.ConnectError("flap")

        def close(self):
            pass

    client._http = FlakyHTTP()  # type: ignore[assignment]
    with pytest.raises(httpx.ConnectError):
        client.get("/v1/chat/history")
    assert calls["n"] == 3
    assert slept == [3, 6]          # NO backoff after the final attempt


def test_transport_retry_recovers_midway(monkeypatch) -> None:
    import httpx

    _sleepless(monkeypatch)
    client = _client()
    client._http.close()
    calls = {"n": 0}

    class RecoveringHTTP:
        def request(self, *_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadError("dropped")
            return FakeResponse(200, {"ok": True})

        def close(self):
            pass

    client._http = RecoveringHTTP()  # type: ignore[assignment]
    assert client.get("/x").status_code == 200
    assert calls["n"] == 2


def test_send_chat_retry_reuses_one_client_msg_id(monkeypatch) -> None:
    import httpx

    _sleepless(monkeypatch)
    client = _client()
    client._http.close()
    payloads: list[dict] = []
    state = {"n": 0}

    class CaptureHTTP:
        def request(self, _method, _url, *, headers=None, json=None, **_kw):
            state["n"] += 1
            payloads.append(json)
            if state["n"] == 1:
                raise httpx.ReadError("dropped mid-send")
            return FakeResponse(200, {"ts": 123.0})

        def close(self):
            pass

    monkeypatch.setattr(client, "_seal", lambda text: {"body_ct": "x", "id": "i1"})
    client._http = CaptureHTTP()  # type: ignore[assignment]
    assert client.send_chat("hello") == 123.0
    assert len(payloads) == 2
    ids = {p["client_msg_id"] for p in payloads}
    assert len(ids) == 1                      # minted once, reused on retry
    uuid.UUID(ids.pop())                      # and a legal UUID


def test_send_chat_uses_plaintext_envelope_when_effective_off() -> None:
    client = _client()
    client.content_encryption_effective = "off"
    client._http.close()
    payloads: list[dict] = []

    class CaptureHTTP:
        def request(self, _method, _url, *, headers=None, json=None, **_kw):
            payloads.append(json)
            return FakeResponse(200, {"ts": 456.0})

        def close(self):
            pass

    client._http = CaptureHTTP()  # type: ignore[assignment]
    assert client.send_chat("plain hello") == 456.0
    assert payloads[0]["envelope"] == {
        "body": "plain hello",
        "owner_user_id": "e2e-user",
        "visibility": "shared",
    }


def test_orphan_manifest_created_0600_and_removed_on_teardown(monkeypatch, tmp_path) -> None:
    import tools.e2e.client as client_mod

    monkeypatch.setattr(client_mod, "_ORPHANS_DIR", tmp_path / "orphans")
    path = client_mod._write_orphan_manifest(TEST_API, "usr_x", "key_x")
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600     # 0600 from birth
    body = path.read_text()
    assert "usr_x" in body and "key_x" in body

    client = _client()
    client._http.close()

    class OKHTTP:
        def request(self, *_a, **_kw):
            return FakeResponse(200, {"deleted": True})

        def close(self):
            pass

    client._http = OKHTTP()  # type: ignore[assignment]
    client._orphan_file = path
    client.teardown()
    assert not path.exists()                          # manifest gone with the account


def test_cleanup_orphans_semantics(monkeypatch, tmp_path) -> None:
    """200/404 → entry removed; 401 / transport error / bad JSON / non-test URL
    → kept, sweep continues, exit 1."""
    import json as _json

    import httpx

    import tools.e2e.client as client_mod

    orphans = tmp_path / "orphans"
    orphans.mkdir()
    monkeypatch.setattr(client_mod, "_ORPHANS_DIR", orphans)

    def manifest(name, api_url="https://test-api.feedling.app"):
        (orphans / f"{name}.json").write_text(_json.dumps(
            {"api_url": api_url, "user_id": name, "api_key": f"k-{name}"}))

    manifest("usr_ok200")
    manifest("usr_gone404")
    manifest("usr_401")
    manifest("usr_flap")
    manifest("usr_prod", api_url="https://api.feedling.app")   # must never be POSTed
    (orphans / "corrupt.json").write_text("{not json")

    posted: list[str] = []

    def fake_post(url, *, headers=None, json=None, timeout=None, verify=None):
        posted.append(url)
        if "usr" in headers["X-API-Key"]:
            pass
        key = headers["X-API-Key"]
        if key == "k-usr_ok200":
            return FakeResponse(200, {"deleted": True})
        if key == "k-usr_gone404":
            return FakeResponse(404, {"error": "user_not_found"})
        if key == "k-usr_401":
            return FakeResponse(401, {"error": "unauthorized"})
        raise httpx.ConnectError("flap")

    monkeypatch.setattr(httpx, "post", fake_post)
    rc = p0._cleanup_orphans()

    assert rc == 1
    remaining = {f.name for f in orphans.glob("*.json")}
    assert remaining == {"usr_401.json", "usr_flap.json", "usr_prod.json", "corrupt.json"}
    assert not any("api.feedling.app/v1" in u and "test-api" not in u for u in posted)
