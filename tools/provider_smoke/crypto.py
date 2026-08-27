"""Headless mirror of the iOS/enclave content crypto (no Secure Enclave needed).

AUTHORITATIVE SOURCE: ``backend/content_encryption.py`` (which is itself kept in
lockstep with ContentEncryption.swift and the enclave's ``_box_seal_open_hkdf``).
Round-trip tools have their own cross-implementation drift guards, but this
module must still follow the backend implementation rather than copying a test
helper.

box_seal:
  X25519 ECDH → HKDF-SHA256(salt=None, info=b"feedling-box-seal-v1")
  → nonce = SHA256(ek_pub || recipient_pk)[:12]   (NOT a zero nonce)
  → ChaCha20-Poly1305, no AAD.  wire = ek_pub(32) || ct || tag(16).
body: ChaCha20-Poly1305 IETF (12-byte nonce), AAD = f"{owner}|{v}|{id}".
"""
import base64
import hashlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_BOX_SEAL_INFO = b"feedling-box-seal-v1"
_RAW = serialization.Encoding.Raw
_RAWPUB = serialization.PublicFormat.Raw


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def generate_keypair() -> tuple[bytes, bytes]:
    sk = X25519PrivateKey.generate()
    sk_raw = sk.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pk_raw = sk.public_key().public_bytes(_RAW, _RAWPUB)
    return sk_raw, pk_raw


def _wrap_key(shared: bytes) -> bytes:
    # salt=None (iOS passes an empty Data(); both resolve to a zero-filled salt of
    # the hash length). The ephemeral+recipient binding lives in the nonce, not the salt.
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_BOX_SEAL_INFO,
    ).derive(shared)


def _wrap_nonce(ek_pub: bytes, recipient_pk_raw: bytes) -> bytes:
    return hashlib.sha256(ek_pub + recipient_pk_raw).digest()[:12]


def box_seal(pt: bytes, recipient_pk_raw: bytes) -> bytes:
    recipient_pk = X25519PublicKey.from_public_bytes(recipient_pk_raw)
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(_RAW, _RAWPUB)
    key = _wrap_key(ek.exchange(recipient_pk))
    ct = ChaCha20Poly1305(key).encrypt(_wrap_nonce(ek_pub, recipient_pk_raw), pt, None)
    return ek_pub + ct


def box_open(blob: bytes, sk_raw: bytes, recipient_pk_raw: bytes) -> bytes:
    sk = X25519PrivateKey.from_private_bytes(sk_raw)
    ek_pub = blob[:32]
    ct = blob[32:]
    shared = sk.exchange(X25519PublicKey.from_public_bytes(ek_pub))
    key = _wrap_key(shared)
    return ChaCha20Poly1305(key).decrypt(_wrap_nonce(ek_pub, recipient_pk_raw), ct, None)


def decrypt_reply(env: dict, sk_raw: bytes, pk_raw: bytes) -> str:
    K = box_open(base64.b64decode(env["K_user"]), sk_raw, pk_raw)
    aad = f"{env['owner_user_id']}|{env.get('v', 1)}|{env['id']}".encode("utf-8")
    pt = ChaCha20Poly1305(K).decrypt(
        base64.b64decode(env["nonce"]),
        base64.b64decode(env["body_ct"]),
        aad,
    )
    return pt.decode("utf-8")
