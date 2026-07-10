"""POST /v1/storage/reencrypt-frame (D4) — enclave storage re-encryption.

Contract: open a v1 envelope (reuse the decrypt path), hash+size the PLAINTEXT
inside the enclave, then AES-256-GCM re-encrypt it under a KMS-derived storage
key. Plaintext never leaves the enclave; the response is the storage ciphertext
+ key version + sha256/size of the plaintext.

The storage-key derivation is stubbed to a fixed 32-byte key so the crypto
roundtrip runs for real: the returned body_ct_storage must decrypt back to the
plaintext under that same key.
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave import storage_crypto  # noqa: E402
from enclave.routes import build_app  # noqa: E402

_FIXED_KEY = bytes(range(32))
_PLAINTEXT = b"the decrypted screenshot bytes" * 40


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _authed(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        return {"user_id": "usr_a"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    async def fake_storage_key(version="v1"):
        assert version == "v1"
        return _FIXED_KEY
    monkeypatch.setattr(keys, "get_storage_key", fake_storage_key)


def _env():
    return {"v": 1, "id": "fr1", "owner_user_id": "usr_a", "K_enclave": "x",
            "body_ct": "x", "nonce": "x", "visibility": "shared"}


def test_missing_credentials_401(client):
    r = client.post("/v1/storage/reencrypt-frame",
                    json={"envelope": _env(), "key_version": "v1"})
    assert r.status_code == 401
    assert r.get_json() == {"error": "missing_api_key"}


def test_envelope_required_400(client, _authed):
    r = client.post("/v1/storage/reencrypt-frame", json={"key_version": "v1"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "envelope required"}


def test_reencrypt_roundtrip_and_contract(client, _authed, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope",
                        lambda e, u, s: _PLAINTEXT)
    r = client.post("/v1/storage/reencrypt-frame",
                    json={"envelope": _env(), "key_version": "v1"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"body_ct_storage", "key_version", "sha256", "size"}
    assert body["key_version"] == "v1"
    assert body["size"] == len(_PLAINTEXT)
    assert body["sha256"] == hashlib.sha256(_PLAINTEXT).hexdigest()
    # storage ciphertext decrypts back to the plaintext under the same key —
    # plaintext never left the enclave, but the seal is real AES-256-GCM.
    blob = base64.b64decode(body["body_ct_storage"])
    assert storage_crypto.open_(_FIXED_KEY, blob) == _PLAINTEXT
    # the storage ciphertext is NOT the plaintext in the clear
    assert _PLAINTEXT not in blob


def test_default_key_version_v1(client, _authed, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: _PLAINTEXT)
    r = client.post("/v1/storage/reencrypt-frame", json={"envelope": _env()},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.get_json()["key_version"] == "v1"


def test_owner_mismatch_403(client, _authed):
    """whoami 解析出的调用者 ≠ envelope.owner_user_id → 真实 decrypt_envelope 的
    所有权检查必须拒绝（403 decrypt_failed: owner mismatch）。这里刻意不打桩
    decrypt_envelope——让真实的跨用户替换防线跑起来（owner 检查在任何密码学
    操作之前，桩掉的 content_sk 不会被触碰）。"""
    env = {**_env(), "owner_user_id": "usr_b"}  # caller resolves to usr_a
    r = client.post("/v1/storage/reencrypt-frame",
                    json={"envelope": env, "key_version": "v1"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 403
    body = r.get_json()
    assert body["error"].startswith("decrypt_failed")
    assert "owner mismatch" in body["error"]


def test_unknown_key_version_400(client, _authed, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: _PLAINTEXT)
    r = client.post("/v1/storage/reencrypt-frame",
                    json={"envelope": _env(), "key_version": "v9"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json()["error"].startswith("unsupported key_version: v9")


def test_decrypt_failure_403(client, _authed, monkeypatch):
    def boom(e, u, s):
        raise envmod.DecryptFailure("bad tag")
    monkeypatch.setattr(envmod, "decrypt_envelope", boom)
    r = client.post("/v1/storage/reencrypt-frame",
                    json={"envelope": _env(), "key_version": "v1"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 403
    assert r.get_json()["error"].startswith("decrypt_failed")


def test_not_ready_503(client, _authed, monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", False)
    monkeypatch.setitem(enclave_state._state, "error", "booting")
    r = client.post("/v1/storage/reencrypt-frame",
                    json={"envelope": _env(), "key_version": "v1"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 503
    assert r.get_json() == {"error": "not_ready", "detail": "booting"}
