"""Keypair proof-of-possession account recovery.

A device that still holds the content X25519 keypair (it syncs via iCloud
Keychain) but lost its api_key (stored device-local-only) must recover its
EXISTING account instead of registering a new one — otherwise it orphans the
account, which is the register-orphan bug. Recovery proves possession of the
private key by decrypting a challenge sealed to the account's public_key.
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives import serialization

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import recover as accounts_recover  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_recover._recover_challenges.clear()
    registry._save_users()
    with make_client() as c:
        yield c


def _new_keypair() -> tuple[X25519PrivateKey, bytes, str]:
    priv = X25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return priv, pub_bytes, base64.b64encode(pub_bytes).decode("ascii")


def _register_with_pubkey(client, pub_b64: str) -> tuple[str, str]:
    res = client.post("/v1/users/register",
                      json={"public_key": pub_b64, "archive_language": "en"})
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _solve_challenge(env: dict, priv: X25519PrivateKey, pub_bytes: bytes) -> str:
    """Decrypt a local_only envelope (mirror of content_encryption.box_seal)."""
    k_user = base64.b64decode(env["K_user"])
    ek_pub, sealed = k_user[:32], k_user[32:]
    shared = priv.exchange(X25519PublicKey.from_public_bytes(ek_pub))
    k_wrap = HKDF(algorithm=SHA256(), length=32, salt=None,
                  info=b"feedling-box-seal-v1").derive(shared)
    seal_nonce = hashlib.sha256(ek_pub + pub_bytes).digest()[:12]
    K = ChaCha20Poly1305(k_wrap).decrypt(seal_nonce, sealed, None)
    aad = f'{env["owner_user_id"]}|{env["v"]}|{env["id"]}'.encode("utf-8")
    body = ChaCha20Poly1305(K).decrypt(
        base64.b64decode(env["nonce"]), base64.b64decode(env["body_ct"]), aad)
    return body.decode("utf-8")


def test_register_with_new_pubkey_succeeds(client):
    _, _, pub_b64 = _new_keypair()
    res = client.post("/v1/users/register", json={"public_key": pub_b64})
    assert res.status_code == 201
    assert len(registry._users) == 1


def test_register_refuses_duplicate_pubkey(client):
    # Server-side backstop: registering a public_key that already has an account
    # must NOT mint a second (orphan) account — it returns 409 so the client
    # recovers instead. Closes the gap when the client's recover-first guard is
    # bypassed (offline at first launch, keychain sync lag, old app version).
    _, _, pub_b64 = _new_keypair()
    first = client.post("/v1/users/register", json={"public_key": pub_b64})
    assert first.status_code == 201
    second = client.post("/v1/users/register", json={"public_key": pub_b64})
    assert second.status_code == 409, second.get_data(as_text=True)
    assert len(registry._users) == 1  # no orphan minted


def test_register_without_pubkey_still_allowed(client):
    # Legacy clients that don't send a public_key can't be deduped — must still
    # be able to register.
    one = client.post("/v1/users/register", json={"archive_language": "en"})
    two = client.post("/v1/users/register", json={"archive_language": "en"})
    assert one.status_code == 201
    assert two.status_code == 201
    assert len(registry._users) == 2


def test_recover_challenge_unknown_pubkey_returns_404(client):
    _, _, pub_b64 = _new_keypair()  # never registered
    res = client.post("/v1/account/recover/challenge", json={"public_key": pub_b64})
    assert res.status_code == 404


def test_recover_full_flow_issues_working_key_for_same_user(client):
    priv, pub_bytes, pub_b64 = _new_keypair()
    user_id, _original_key = _register_with_pubkey(client, pub_b64)

    # Challenge sealed to the account's public_key.
    ch = client.post("/v1/account/recover/challenge", json={"public_key": pub_b64})
    assert ch.status_code == 200, ch.get_data(as_text=True)
    cbody = ch.get_json()
    answer = _solve_challenge(cbody["envelope"], priv, pub_bytes)

    # Verify proof → fresh api_key for the SAME existing account (no new user).
    vr = client.post("/v1/account/recover/verify",
                     json={"challenge_id": cbody["challenge_id"], "answer": answer})
    assert vr.status_code == 200, vr.get_data(as_text=True)
    vbody = vr.get_json()
    assert vbody["user_id"] == user_id
    recovered_key = vbody["api_key"]
    assert recovered_key

    # The recovered key authenticates as the same account.
    who = client.get("/v1/users/whoami", headers={"X-API-Key": recovered_key})
    assert who.status_code == 200
    assert who.get_json()["user_id"] == user_id

    # No new account was minted.
    assert len(registry._users) == 1


def test_recover_lands_on_newest_active_account_when_pubkey_shared(client):
    # Orphan lineage: two accounts share the same public_key. Recovery must land
    # on the newest active account (the survivor the merge tool consolidates
    # into), not an arbitrary/older orphan.
    priv, pub_bytes, pub_b64 = _new_keypair()
    # Pre-existing orphan lineage (minted before the dedup backstop existed):
    # build it directly, since the register endpoint now refuses duplicates.
    registry._register_user(public_key=pub_b64)
    new_user = registry._register_user(public_key=pub_b64)["user_id"]
    registry._save_users()
    assert len(registry._users) == 2

    ch = client.post("/v1/account/recover/challenge", json={"public_key": pub_b64}).get_json()
    answer = _solve_challenge(ch["envelope"], priv, pub_bytes)
    vr = client.post("/v1/account/recover/verify",
                     json={"challenge_id": ch["challenge_id"], "answer": answer})
    assert vr.status_code == 200
    assert vr.get_json()["user_id"] == new_user


def test_recover_verify_wrong_answer_rejected(client):
    _priv, _pub_bytes, pub_b64 = _new_keypair()
    _register_with_pubkey(client, pub_b64)
    ch = client.post("/v1/account/recover/challenge", json={"public_key": pub_b64})
    challenge_id = ch.get_json()["challenge_id"]

    vr = client.post("/v1/account/recover/verify",
                     json={"challenge_id": challenge_id, "answer": "wrong-answer"})
    assert vr.status_code == 401


def test_recover_challenge_is_single_use(client):
    priv, pub_bytes, pub_b64 = _new_keypair()
    _register_with_pubkey(client, pub_b64)
    ch = client.post("/v1/account/recover/challenge", json={"public_key": pub_b64}).get_json()
    answer = _solve_challenge(ch["envelope"], priv, pub_bytes)

    first = client.post("/v1/account/recover/verify",
                        json={"challenge_id": ch["challenge_id"], "answer": answer})
    assert first.status_code == 200
    # Replaying the same challenge_id must fail (one-time use).
    second = client.post("/v1/account/recover/verify",
                         json={"challenge_id": ch["challenge_id"], "answer": answer})
    assert second.status_code == 401
