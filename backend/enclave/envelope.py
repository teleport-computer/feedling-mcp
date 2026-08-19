"""Pure envelope decryption — zero I/O, sync-only.

This module implements iOS-compatible E2E envelope decryption.
All functions are pure (no I/O, no side effects). Routes must use
to_thread() to call decrypt_envelope() on thread pool to avoid blocking.

See docs/DESIGN_E2E.md for the full spec.
"""

from __future__ import annotations

import base64
import hashlib
import logging

import nacl.bindings
import nacl.exceptions
import nacl.public
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import serialization

log = logging.getLogger("feedling.enclave.envelope")


class DecryptFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


BOX_SEAL_INFO = b"feedling-box-seal-v1"


def build_aead_aad(owner_user_id: str, v: int, item_id: str) -> bytes:
    """Per docs/DESIGN_E2E.md §3.4, content's AEAD additional-data must
    authenticate (owner_user_id, v, item_id). The enclave recomputes this
    from the plaintext metadata the server claims + the user_id it
    resolved the api_key to — mismatch → AEAD verification fails →
    cross-user ciphertext substitution attack is detected."""
    payload = f"{owner_user_id}|{v}|{item_id}".encode("utf-8")
    return payload


def box_seal_open_hkdf(blob: bytes, recipient_sk_bytes: bytes) -> bytes:
    """iOS-compatible sealed-box open.

    Matches testapp/FeedlingTest/ContentEncryption.swift's BoxSeal:
      blob = ek_pub (32 bytes) || ciphertext || tag (16 bytes)
      shared = ECDH(recipient_sk, ek_pub)
      K_wrap = HKDF-SHA256(shared, info=BOX_SEAL_INFO, len=32)
      nonce  = sha256(ek_pub || recipient_pub)[:12]
      plaintext = ChaCha20-Poly1305-decrypt(ct||tag, K_wrap, nonce)

    This is NOT wire-compatible with libsodium's crypto_box_seal (which
    uses XSalsa20 + Blake2b) — we reimplement both sides to use only
    primitives CryptoKit supports natively, so iOS doesn't need a
    separate libsodium SPM dep.
    """
    if len(blob) < 32 + 16:
        raise DecryptFailure(f"box_seal blob too short: {len(blob)}")
    ek_pub = blob[:32]
    ct_plus_tag = blob[32:]

    try:
        sk = X25519PrivateKey.from_private_bytes(recipient_sk_bytes)
        ephemeral = X25519PublicKey.from_public_bytes(ek_pub)
        shared = sk.exchange(ephemeral)
    except Exception as e:
        raise DecryptFailure(f"ECDH failed: {e}")

    k_wrap = HKDF(algorithm=SHA256(), length=32, salt=None,
                  info=BOX_SEAL_INFO).derive(shared)

    recipient_pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    nonce = hashlib.sha256(ek_pub + recipient_pub).digest()[:12]

    try:
        return ChaCha20Poly1305(k_wrap).decrypt(nonce, ct_plus_tag, None)
    except InvalidTag as e:
        raise DecryptFailure(f"box_seal tag invalid: {e}")


def decrypt_envelope(env: dict, authorized_user_id: str, content_sk: nacl.public.PrivateKey) -> bytes:
    """Given a v1 envelope dict (from Flask chat history), return the
    plaintext body. Raises DecryptFailure on any integrity problem —
    missing fields, wrong enclave pubkey fingerprint, AEAD tag mismatch,
    owner_user_id ≠ authorized_user_id (cross-user substitution attack).
    """
    # Shape checks
    for field in ("body_ct", "nonce", "K_enclave", "owner_user_id"):
        if not env.get(field):
            raise DecryptFailure(f"envelope missing {field}")

    # Binding: whoever authorized this call must be the same user who wrote it.
    if env["owner_user_id"] != authorized_user_id:
        raise DecryptFailure(
            f"owner mismatch: envelope claims owner={env['owner_user_id']} "
            f"but caller is {authorized_user_id}"
        )

    try:
        k_enclave_sealed = base64.b64decode(env["K_enclave"])
        body_ct = base64.b64decode(env["body_ct"])
        nonce = base64.b64decode(env["nonce"])
    except Exception as e:
        raise DecryptFailure(f"base64 decode: {e}")

    # 1. Unseal K_enclave → K
    # Use the iOS-compatible HKDF+ChaCha scheme (see box_seal_open_hkdf).
    K = box_seal_open_hkdf(k_enclave_sealed, bytes(content_sk))

    if len(K) != 32:
        raise DecryptFailure(f"unexpected K length: {len(K)}")

    # 2. AEAD-decrypt body_ct with K + nonce, aad = owner||v||id
    # We use IETF ChaCha20-Poly1305 (12-byte nonce) because it's the AEAD
    # Apple's CryptoKit supports natively on iOS — no extra SPM dep needed.
    # See docs/DESIGN_E2E.md §3.1.
    v = int(env.get("v", 1))
    item_id = env.get("id", "")
    aad = build_aead_aad(env["owner_user_id"], v, item_id)
    if len(nonce) != 12:
        raise DecryptFailure(f"expected 12-byte nonce, got {len(nonce)}")
    try:
        plaintext = nacl.bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
            body_ct, aad, nonce, K
        )
    except nacl.exceptions.CryptoError as e:
        # AEAD failure — either tampering, wrong aad, or wrong K
        log.warning(
            "envelope_aead_verify_failed authorized_user_id=%s "
            "owner_user_id=%s item_id=%s version=%s",
            authorized_user_id,
            env.get("owner_user_id", ""),
            item_id,
            v,
        )
        raise DecryptFailure(f"AEAD verify: {e}")

    return plaintext


def read_envelope(env: dict, authorized_user_id: str,
                  content_sk: nacl.public.PrivateKey) -> bytes:
    """Return bytes from either supported persisted content shape.

    Enclave read routes sit behind the same authenticated TEE boundary for both
    account tiers.  Encrypted rows still take the authenticated decrypt path;
    plaintext-tier rows are already plaintext in the TEE-primary database and
    only need strict owner/shape validation here.  Keep ``decrypt_envelope``
    sealed-only so the public single-envelope decrypt endpoint does not silently
    broaden its contract.
    """
    if not isinstance(env, dict):
        raise DecryptFailure("envelope must be an object")
    if (
        env.get("body_ct")
        or env.get("K_enclave")
        or (env.get("body") is None and env.get("body_b64") is None)
    ):
        # The sealed implementation performs its own owner binding.  Delegate
        # unchanged so this router remains a zero-semantic-diff wrapper for all
        # encrypted rows (and so sealed-path test doubles keep working).
        return decrypt_envelope(env, authorized_user_id, content_sk)
    owner = env.get("owner_user_id")
    if not owner:
        raise DecryptFailure("envelope missing owner_user_id")
    if owner != authorized_user_id:
        raise DecryptFailure(
            f"owner mismatch: envelope claims owner={owner} "
            f"but caller is {authorized_user_id}"
        )
    if env.get("body") is not None:
        body = env["body"]
        if not isinstance(body, str):
            raise DecryptFailure("plaintext body must be a string")
        return body.encode("utf-8")
    if env.get("body_b64") is not None:
        body_b64 = env["body_b64"]
        if not isinstance(body_b64, str):
            raise DecryptFailure("plaintext body_b64 must be a string")
        try:
            return base64.b64decode(body_b64, validate=True)
        except Exception as e:
            raise DecryptFailure(f"body_b64 decode: {e}") from e
    raise DecryptFailure("envelope has no supported body shape")
