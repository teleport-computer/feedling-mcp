"""Storage-layer re-encryption (D4) — pure AES-256-GCM, zero I/O.

The enclave decrypts a user's v1 envelope to plaintext, then re-seals that
plaintext under a KMS-derived *storage* key before it is parked in R2. The
plaintext never leaves the enclave; only this storage ciphertext does. This is
a distinct key family from the E2E content key — the storage key protects
data-at-rest in object storage and is derived deterministically from the
enclave's KMS secret (see keys.get_storage_key), so a re-derivation on any CVM
running the same image can open it again.

Wire format: ``nonce(12) || AESGCM_ciphertext_with_tag``. AES-256-GCM (not the
E2E path's ChaCha20-Poly1305) because storage-at-rest has no iOS/CryptoKit
constraint and AES-GCM has hardware acceleration on the CVM host.
"""
from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


def seal(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM encrypt; return nonce || ciphertext||tag."""
    nonce = os.urandom(_NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def open_(key: bytes, blob: bytes) -> bytes:
    """Inverse of ``seal`` — used by the TEE read path and tests."""
    return AESGCM(key).decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], None)
