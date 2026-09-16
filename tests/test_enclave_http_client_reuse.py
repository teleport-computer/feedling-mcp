"""Enclave HTTP calls must reuse one pooled client (no per-call TLS handshake).

Why this exists: `_decrypt_chat_rows` decrypts a V2 prompt tail one row at a
time, so a `httpx.Client` built per call meant one TCP connect + TLS handshake
per chat message — 60 handshakes for a full `FEEDLING_V2_TAIL_HARD_CAP` window,
measured at ~82ms each on test. These tests pin the pooling contract: one client
per process, rebuilt after fork, with each call still choosing its own timeout.
"""

import hashlib
import hmac
import base64
import json
import os
from types import SimpleNamespace

import httpx
import pytest

import core.enclave as core_enclave
from core import runtime_token as runtime_tokens


class _FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def _body_for(url: str) -> dict:
    """Each enclave endpoint has its own response shape the caller validates."""
    if url.endswith("/attestation"):
        return {"enclave_content_pk_hex": "ab", "compose_hash": "cd"}
    if url.endswith("/v1/storage/reencrypt-frame"):
        return {"body_ct_storage": "sealed", "key_version": "v1"}
    return {"plaintext_b64": "cGxhaW50ZXh0"}


class _RecordingClient:
    """Stands in for httpx.Client and records construction + per-call kwargs."""

    instances: list["_RecordingClient"] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[dict] = []
        self.closed = False
        _RecordingClient.instances.append(self)

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse(body=_body_for(url))

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse(body=_body_for(url))

    def close(self):
        self.closed = True


@pytest.fixture
def recording_client(monkeypatch):
    _RecordingClient.instances = []
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setattr(core_enclave.httpx, "Client", _RecordingClient)
    core_enclave.reset_http_client()
    yield _RecordingClient
    core_enclave.reset_http_client()


def _decrypt(purpose="v2_chat_read"):
    return core_enclave._decrypt_envelope_via_enclave(
        {"owner_user_id": "usr_pool", "body_ct": "ct"},
        "api-key",
        purpose=purpose,
        caller_user_id="usr_pool",
    )


def test_repeated_decrypts_share_one_client(recording_client):
    """A 60-row prompt tail must not build 60 clients (= 60 TLS handshakes)."""
    for _ in range(60):
        assert _decrypt() == b"plaintext"

    assert len(recording_client.instances) == 1
    assert len(recording_client.instances[0].calls) == 60


def test_pooled_client_is_not_closed_between_calls(recording_client):
    _decrypt()
    _decrypt()

    assert recording_client.instances[0].closed is False


def test_each_call_keeps_its_own_timeout(recording_client):
    """Shared pool, but decrypt (20s), attestation (5s) and reencrypt (30s)
    must each keep the timeout they had when every call built its own client."""
    _decrypt()
    core_enclave._get_enclave_info()
    core_enclave._reencrypt_frame_via_enclave(
        {"owner_user_id": "usr_pool", "body_ct": "ct"},
        "api-key",
    )

    client = recording_client.instances[0]
    assert len(recording_client.instances) == 1
    timeouts = [call.get("timeout") for call in client.calls]
    assert timeouts == [20, 5, 30]


def test_client_verifies_nothing_and_is_built_once(recording_client):
    """verify=False is the pre-existing contract (self-signed in-cluster cert
    trusted via REPORT_DATA, not a CA) — pooling must not silently change it."""
    _decrypt()
    _decrypt()

    assert recording_client.instances[0].init_kwargs.get("verify") is False


def test_fork_gets_a_fresh_client(recording_client, monkeypatch):
    """A client inherited across fork carries sockets owned by the parent.
    Rebuild when the pid changes instead of reusing a poisoned pool."""
    _decrypt()
    assert len(recording_client.instances) == 1

    child_pid = os.getpid() + 1
    monkeypatch.setattr(core_enclave.os, "getpid", lambda: child_pid)
    _decrypt()

    assert len(recording_client.instances) == 2


def test_reset_closes_and_rebuilds(recording_client):
    _decrypt()
    first = recording_client.instances[0]

    core_enclave.reset_http_client()
    _decrypt()

    assert first.closed is True
    assert len(recording_client.instances) == 2


# A long-running worker keeps the job's original token past its 900s expiry.
# Drive the real pooled-client accessor and HTTP response handling below.
_REFRESH_SECRET = b"decrypt-refresh-test-secret"
_REFRESH_NOW = 2000.75


def _expired_token(user_id="usr_pool", *, secret=_REFRESH_SECRET):
    return runtime_tokens.mint(
        secret, user_id=user_id, runtime_instance_id="v2-worker",
        scope=["envelope_decrypt"], now=1000.25, ttl=900,
    )


@pytest.fixture
def refresh_http(monkeypatch):
    requests, events = [], []
    replies = [401, 200]
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _REFRESH_SECRET.decode())
    # Replace module-local clocks, never process-global time functions.
    monkeypatch.setattr(core_enclave, "time", SimpleNamespace(time=lambda: _REFRESH_NOW))
    monkeypatch.setattr(runtime_tokens, "time", SimpleNamespace(time=lambda: _REFRESH_NOW))
    store = SimpleNamespace(user_id="usr_pool")
    monkeypatch.setattr(core_enclave, "_trace_store_from_user_id", lambda uid: store)
    monkeypatch.setattr(core_enclave.debug_trace, "trace_event", lambda store, **e: events.append(e))

    def respond(request):
        requests.append(request)
        outcome = replies.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(
            outcome,
            json={"plaintext_b64": "cGxhaW50ZXh0"} if outcome == 200 else {"error": "denied"},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(core_enclave, "_http_client", client)
        monkeypatch.setattr(core_enclave, "_http_client_pid", os.getpid())
        yield SimpleNamespace(requests=requests, events=events, replies=replies)


def _decrypt_with_token(token, *, caller="usr_pool", api_key=None):
    return core_enclave._decrypt_envelope_via_enclave(
        {"owner_user_id": "usr_pool", "body_ct": "ct"}, api_key,
        purpose="memory_action", caller_user_id=caller, runtime_token=token,
    )


def _events(http, event_type):
    return [e for e in http.events if e["type"] == event_type]


def test_expired_decrypt_token_refreshes_once_and_returns_plaintext(refresh_http):
    token = _expired_token()
    with pytest.raises(runtime_tokens.TokenError, match="token_expired"):
        runtime_tokens.verify(_REFRESH_SECRET, token)
    assert _decrypt_with_token(token, api_key="must-not-fall-back") == b"plaintext"
    requests = refresh_http.requests
    assert len(requests) == 2
    assert requests[0].headers["X-Feedling-Runtime-Token"] == token
    new_token = requests[1].headers["X-Feedling-Runtime-Token"]
    claims = runtime_tokens.verify(_REFRESH_SECRET, new_token)
    assert claims == {
        "user_id": "usr_pool", "sub": "enclave_decrypt_refresh",
        "scope": ["envelope_decrypt"], "iat": _REFRESH_NOW, "exp": _REFRESH_NOW + 900,
    }
    assert requests[0].content == requests[1].content
    assert requests[0].url == requests[1].url
    assert all("X-API-Key" not in r.headers for r in requests)
    assert all(r.extensions["timeout"]["read"] == 20 for r in requests)
    errors = _events(refresh_http, "enclave.call.error")
    assert len(errors) == 1
    assert errors[0]["detail"]["status_code"] == 401
    assert errors[0]["detail"]["token_age_sec"] == 1000
    refreshed = _events(refresh_http, "enclave.call.token_refreshed")
    assert len(refreshed) == 1
    assert refreshed[0]["detail"] == {
        "purpose": "memory_action", "path": "/v1/envelope/decrypt",
        "token_age_sec": 1000, "attempt": 2,
    }
    assert core_enclave.debug_trace._safe_detail(refreshed[0]["detail"]) == refreshed[0]["detail"]
    serialized = json.dumps(refresh_http.events)
    assert token not in serialized and new_token not in serialized
    assert "plaintext_b64" not in serialized and "body_ct" not in serialized


@pytest.mark.parametrize("caller,token_user", [("usr_pool", "usr_other"), ("", "")])
def test_decrypt_token_user_mismatch_does_not_refresh(refresh_http, caller, token_user):
    with pytest.raises(RuntimeError, match="enclave_http_401"):
        _decrypt_with_token(_expired_token(token_user), caller=caller)
    assert len(refresh_http.requests) == 1
    assert not _events(refresh_http, "enclave.call.token_refreshed")


def test_decrypt_token_without_secret_does_not_refresh(refresh_http, monkeypatch):
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET")
    with pytest.raises(RuntimeError, match="enclave_http_401"):
        _decrypt_with_token(_expired_token())
    assert len(refresh_http.requests) == 1
    assert "token_age_sec" not in _events(refresh_http, "enclave.call.error")[0]["detail"]
    assert not _events(refresh_http, "enclave.call.token_refreshed")


def test_decrypt_token_second_401_stops_after_two_requests(refresh_http):
    refresh_http.replies[:] = [401, 401]
    with pytest.raises(RuntimeError, match='enclave_http_401:') as err:
        _decrypt_with_token(_expired_token())
    assert core_enclave.decrypt_failure_metadata(err.value) == ("enclave_http_error", "")
    assert len(refresh_http.requests) == 2
    assert len(_events(refresh_http, "enclave.call.token_refreshed")) == 1
    errors = _events(refresh_http, "enclave.call.error")
    assert [e["detail"]["token_age_sec"] for e in errors] == [1000, 0]


def test_decrypt_api_key_401_does_not_refresh(refresh_http):
    with pytest.raises(RuntimeError, match="enclave_http_401"):
        _decrypt_with_token("", api_key="api-key")
    assert len(refresh_http.requests) == 1
    assert refresh_http.requests[0].headers["X-API-Key"] == "api-key"
    assert "X-Feedling-Runtime-Token" not in refresh_http.requests[0].headers
    assert "token_age_sec" not in _events(refresh_http, "enclave.call.error")[0]["detail"]
    assert not _events(refresh_http, "enclave.call.token_refreshed")


@pytest.mark.parametrize("token", ["malformed", "forged.sig", _expired_token(secret=b"other-secret")])
def test_decrypt_unverified_token_is_not_refreshed(refresh_http, token):
    with pytest.raises(RuntimeError, match="enclave_http_401"):
        _decrypt_with_token(token)
    assert len(refresh_http.requests) == 1
    assert "token_age_sec" not in _events(refresh_http, "enclave.call.error")[0]["detail"]
    assert not _events(refresh_http, "enclave.call.token_refreshed")


@pytest.mark.parametrize("status", [200, 403, 500])
def test_decrypt_non_401_never_refreshes(refresh_http, status):
    refresh_http.replies[:] = [status]
    if status == 200:
        assert _decrypt_with_token(_expired_token()) == b"plaintext"
    else:
        with pytest.raises(RuntimeError, match=f"enclave_http_{status}"):
            _decrypt_with_token(_expired_token())
    assert len(refresh_http.requests) == 1
    assert not _events(refresh_http, "enclave.call.token_refreshed")


def test_decrypt_refreshed_request_timeout_keeps_transport_error(refresh_http):
    refresh_http.replies[:] = [401, httpx.ReadTimeout("timed out")]
    with pytest.raises(RuntimeError, match="enclave_error:ReadTimeout") as err:
        _decrypt_with_token(_expired_token())
    assert len(refresh_http.requests) == 2
    assert core_enclave.decrypt_failure_metadata(err.value) == ("enclave_transport_error", "ReadTimeout")
    assert len(_events(refresh_http, "enclave.call.timeout")) == 1


def test_decode_claims_authenticates_signature_without_relaxing_verify():
    token = _expired_token()
    claims = runtime_tokens.decode_claims(_REFRESH_SECRET, token)
    assert claims["user_id"] == "usr_pool" and claims["iat"] == 1000.25
    with pytest.raises(runtime_tokens.TokenError, match="token_expired"):
        runtime_tokens.verify(_REFRESH_SECRET, token, now=2000)
    with pytest.raises(runtime_tokens.TokenError, match="bad_signature"):
        runtime_tokens.decode_claims(b"wrong-secret", token)


@pytest.mark.parametrize("iat", [None, "not-a-time", float("nan"), float("inf")])
def test_decrypt_unknown_token_age_is_omitted(refresh_http, iat):
    claims = runtime_tokens.decode_claims(_REFRESH_SECRET, _expired_token())
    claims["iat"] = iat
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    signature = hmac.new(_REFRESH_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    assert _decrypt_with_token(f"{payload}.{signature}") == b"plaintext"
    assert len(refresh_http.requests) == 2
    for event in refresh_http.events:
        assert "token_age_sec" not in event["detail"]


@pytest.mark.parametrize("payload", [b"[]", b"null", b"not-json"])
def test_decode_claims_rejects_signed_non_object_payload(payload):
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(_REFRESH_SECRET, encoded.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(runtime_tokens.TokenError, match="bad_payload"):
        runtime_tokens.decode_claims(_REFRESH_SECRET, f"{encoded}.{sig}")
