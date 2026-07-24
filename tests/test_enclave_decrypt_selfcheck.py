"""GET /v1/decrypt/selfcheck —— 不绑用户的解密自检探针（runner-shared
decrypt-health 阶段二，2026-07-24）。

端点优先还原开启动时以 content_pk seal 的固定明文（缺失则当场 seal-then-open），
用 content_sk 走真实解密引擎断言相等；再打 backend /healthz 做 enclave→backend
回环 ping（任何 HTTP 应答都算可达，只有传输失败才 fail）。鉴权要求一个本地可验证
的 runtime token（挡公网匿名放大）。不解析 user、不解任何用户信封。

Run:  python -m pytest tests/test_enclave_decrypt_selfcheck.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import nacl.public  # noqa: E402
import pytest  # noqa: E402

import content_encryption  # noqa: E402
from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402

# A valid runtime token is now REQUIRED; the endpoint verifies it locally, so we
# stub the verifier to accept exactly this opaque string.
_GOOD_TOKEN = "valid-runtime-token"
_AUTH = {"X-Feedling-Runtime-Token": _GOOD_TOKEN}


@pytest.fixture()
def sk():
    """A fresh content keypair; SELFCHECK_PLAINTEXT sealed to its pubkey is
    staged into _state exactly as bootstrap() would."""
    return nacl.public.PrivateKey.generate()


@pytest.fixture()
def client(monkeypatch, sk):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    sealed = content_encryption.box_seal(
        enclave_state.SELFCHECK_PLAINTEXT, bytes(sk.public_key)
    )
    monkeypatch.setitem(enclave_state._state, "decrypt_selfcheck_sealed", sealed)
    enclave_auth.reset_cache()

    # Local runtime-token verification: accept only _GOOD_TOKEN.
    monkeypatch.setattr(
        enclave_auth, "local_user_id_from_token",
        lambda t: "usr_probe" if t == _GOOD_TOKEN else None,
    )

    async def fake_get_content_sk():
        return sk

    monkeypatch.setattr(keys, "get_content_sk", fake_get_content_sk)

    async def fake_backend_get(path, headers, params=None):
        assert path == "/healthz"       # loopback ping only ever hits /healthz
        return {"ok": True}

    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    return _AsgiTestClient(build_app())


def test_decrypt_and_loopback_both_ok(client):
    r = client.get("/v1/decrypt/selfcheck", headers=_AUTH)
    assert r.status_code == 200, r.data
    assert r.json == {"decrypt": "ok", "loopback": "ok"}


def test_tampered_ciphertext_reports_decrypt_fail(client, monkeypatch):
    bad = bytearray(enclave_state._state["decrypt_selfcheck_sealed"])
    bad[-1] ^= 0xFF                     # corrupt the auth tag
    monkeypatch.setitem(enclave_state._state, "decrypt_selfcheck_sealed", bytes(bad))
    r = client.get("/v1/decrypt/selfcheck", headers=_AUTH)
    assert r.status_code == 200
    assert r.json["decrypt"] == "fail"  # verdict, endpoint still ran


def test_wrong_key_reports_decrypt_fail(client, monkeypatch):
    # A content_sk that does NOT match the (boot-sealed) blob's recipient → open
    # fails (the "key drifted since boot" case). The boot seal is present, so the
    # on-demand fallback does not kick in.
    other = nacl.public.PrivateKey.generate()

    async def wrong_sk():
        return other

    monkeypatch.setattr(keys, "get_content_sk", wrong_sk)
    r = client.get("/v1/decrypt/selfcheck", headers=_AUTH)
    assert r.json["decrypt"] == "fail"


def test_missing_boot_seal_falls_back_to_on_demand_seal(client, monkeypatch):
    # The boot seal is best-effort and may be absent (state.py tolerates it and
    # must never block boot). A missing blob must NOT masquerade as a decrypt
    # outage: the endpoint seals on demand with content_sk's pubkey and the
    # round trip still succeeds → decrypt ok.
    monkeypatch.setitem(enclave_state._state, "decrypt_selfcheck_sealed", None)
    r = client.get("/v1/decrypt/selfcheck", headers=_AUTH)
    assert r.status_code == 200
    assert r.json["decrypt"] == "ok"


def test_loopback_transport_failure_reports_fail(client, monkeypatch):
    async def boom(path, headers, params=None):
        raise RuntimeError("connection refused")   # transport-level failure

    monkeypatch.setattr(backend_client, "backend_get", boom)
    r = client.get("/v1/decrypt/selfcheck", headers=_AUTH)
    assert r.status_code == 200
    assert r.json == {"decrypt": "ok", "loopback": "fail"}


def test_loopback_503_counts_as_reachable(client, monkeypatch):
    # backend /healthz 503 (DB pool busy) is still an HTTP RESPONSE — the enclave
    # reached the backend, so loopback is ok. A load hiccup must not read as a
    # decrypt-infra outage.
    async def busy(path, headers, params=None):
        req = httpx.Request("GET", "http://backend/healthz")
        resp = httpx.Response(503, request=req)
        raise httpx.HTTPStatusError("busy", request=req, response=resp)

    monkeypatch.setattr(backend_client, "backend_get", busy)
    r = client.get("/v1/decrypt/selfcheck", headers=_AUTH)
    assert r.json == {"decrypt": "ok", "loopback": "ok"}


def test_unauthorized_without_credentials(client):
    r = client.get("/v1/decrypt/selfcheck")
    assert r.status_code == 401


def test_api_key_only_is_rejected(client):
    # Presence of any X-API-Key string used to pass — the amplification hole.
    # Now only a locally-verifiable runtime token is accepted.
    r = client.get("/v1/decrypt/selfcheck", headers={"X-API-Key": "anything"})
    assert r.status_code == 401


def test_invalid_runtime_token_is_rejected(client):
    r = client.get("/v1/decrypt/selfcheck",
                   headers={"X-Feedling-Runtime-Token": "forged"})
    assert r.status_code == 401


def test_not_ready_returns_503(client, monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", False)
    r = client.get("/v1/decrypt/selfcheck", headers=_AUTH)
    assert r.status_code == 503


def test_endpoint_binds_no_user_identity(client, monkeypatch):
    # The runtime token is used only as a boolean authenticity gate — the
    # endpoint must never resolve a user via a backend whoami round trip.
    seen_paths = []

    async def record(path, headers, params=None):
        seen_paths.append(path)
        return {"ok": True}

    monkeypatch.setattr(backend_client, "backend_get", record)
    r = client.get("/v1/decrypt/selfcheck", headers=_AUTH)
    assert r.status_code == 200
    assert seen_paths == ["/healthz"]          # no /v1/users/whoami
