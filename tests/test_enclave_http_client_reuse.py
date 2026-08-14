"""Enclave HTTP calls must reuse one pooled client (no per-call TLS handshake).

Why this exists: `_decrypt_chat_rows` decrypts a V2 prompt tail one row at a
time, so a `httpx.Client` built per call meant one TCP connect + TLS handshake
per chat message — 60 handshakes for the former 60-row prompt window,
measured at ~82ms each on test. These tests pin the pooling contract: one client
per process, rebuilt after fork, with each call still choosing its own timeout.
"""

import os

import pytest

import core.enclave as core_enclave


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
