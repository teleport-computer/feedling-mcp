from __future__ import annotations

import base64
import hashlib
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import nacl.bindings  # noqa: E402
import nacl.public  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: E402
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402
from cryptography.hazmat.primitives.hashes import SHA256  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from enclave import envelope as envmod  # noqa: E402


def _seal(recipient_pk: bytes, key32: bytes) -> bytes:
    """iOS 兼容 seal（ContentEncryption.swift / spec §2）：ek_pub||ct||tag。"""
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = ek.exchange(X25519PublicKey.from_public_bytes(recipient_pk))
    k_wrap = HKDF(algorithm=SHA256(), length=32, salt=None,
                  info=envmod.BOX_SEAL_INFO).derive(shared)
    nonce = hashlib.sha256(ek_pub + recipient_pk).digest()[:12]
    return ek_pub + ChaCha20Poly1305(k_wrap).encrypt(nonce, key32, None)


def _make_envelope(owner: str, item_id: str, body: bytes, recipient_pk: bytes,
                   v: int = 1) -> dict:
    K = os.urandom(32)
    nonce = os.urandom(12)
    aad = f"{owner}|{v}|{item_id}".encode("utf-8")
    ct = nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(body, aad, nonce, K)
    return {
        "id": item_id, "v": v, "owner_user_id": owner,
        "body_ct": base64.b64encode(ct).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "K_enclave": base64.b64encode(_seal(recipient_pk, K)).decode(),
    }


@pytest.fixture()
def sk():
    return nacl.public.PrivateKey.generate()


def test_round_trip(sk):
    env = _make_envelope("usr_a", "itm_1", b'{"hello": 1}', bytes(sk.public_key))
    assert envmod.decrypt_envelope(env, "usr_a", sk) == b'{"hello": 1}'


def test_owner_mismatch_rejected(sk):
    env = _make_envelope("usr_a", "itm_1", b"x", bytes(sk.public_key))
    with pytest.raises(envmod.DecryptFailure) as ei:
        envmod.decrypt_envelope(env, "usr_b", sk)
    assert "owner mismatch" in ei.value.reason


def test_tampered_ciphertext_rejected_and_traced(sk, caplog):
    env = _make_envelope("usr_a", "itm_1", b"x", bytes(sk.public_key))
    raw = bytearray(base64.b64decode(env["body_ct"]))
    raw[0] ^= 0xFF
    env["body_ct"] = base64.b64encode(bytes(raw)).decode()
    caplog.set_level(logging.WARNING)
    with pytest.raises(envmod.DecryptFailure) as ei:
        envmod.decrypt_envelope(env, "usr_a", sk)
    assert "AEAD verify" in ei.value.reason
    assert any(
        "envelope_aead_verify_failed" in record.getMessage()
        and "usr_a" in record.getMessage()
        and "itm_1" in record.getMessage()
        for record in caplog.records
    )


def test_aad_binds_item_id(sk):
    # id 变了 → AAD 不匹配 → 拒绝（防跨条目替换）
    env = _make_envelope("usr_a", "itm_1", b"x", bytes(sk.public_key))
    env["id"] = "itm_2"
    with pytest.raises(envmod.DecryptFailure):
        envmod.decrypt_envelope(env, "usr_a", sk)


def test_missing_field_rejected(sk):
    env = _make_envelope("usr_a", "itm_1", b"x", bytes(sk.public_key))
    env.pop("nonce")
    with pytest.raises(envmod.DecryptFailure) as ei:
        envmod.decrypt_envelope(env, "usr_a", sk)
    assert "missing nonce" in ei.value.reason


def test_read_envelope_accepts_plaintext_text_and_binary(sk):
    base = {"id": "itm_plain", "owner_user_id": "usr_a", "visibility": "shared"}
    assert envmod.read_envelope({**base, "body": "hello 明文"}, "usr_a", sk) == \
        "hello 明文".encode()
    assert envmod.read_envelope(
        {**base, "body_b64": base64.b64encode(b"\x00\x01file").decode()},
        "usr_a", sk,
    ) == b"\x00\x01file"


def test_read_envelope_plaintext_still_binds_owner(sk):
    with pytest.raises(envmod.DecryptFailure) as ei:
        envmod.read_envelope(
            {"id": "itm_plain", "owner_user_id": "usr_a", "body": "secret"},
            "usr_b", sk,
        )
    assert "owner mismatch" in ei.value.reason


def test_read_envelope_rejects_malformed_plaintext_shapes(sk):
    base = {"id": "itm_plain", "owner_user_id": "usr_a"}
    with pytest.raises(envmod.DecryptFailure):
        envmod.read_envelope({**base, "body": 123}, "usr_a", sk)
    with pytest.raises(envmod.DecryptFailure):
        envmod.read_envelope({**base, "body_b64": "%%%"}, "usr_a", sk)


def test_module_is_pure():
    import enclave.envelope as m
    src = Path(m.__file__).read_text()
    for banned in ("import flask", "import httpx", "import fastapi"):
        assert banned not in src
