"""Python implementation of the Feedling v1 envelope scheme.

Wire-compatible with:
  - testapp/FeedlingTest/ContentEncryption.swift (iOS side)
  - backend/enclave_app.py _box_seal_open_hkdf / _decrypt_envelope (enclave side)

Scope:
  - `box_seal(plaintext, recipient_pk)` — wrap a symmetric key for a recipient.
    Matches iOS BoxSeal.seal exactly. Output is `ek_pub (32) || ct || tag (16)`.
  - `build_envelope(plaintext, user_pk, enclave_pk, owner_user_id, …)` — wrap
    a whole body into an envelope dict ready to POST as `{"envelope": {...}}`.

Trust model (Phase A, with MCP still on VPS):
  Agent sends plaintext → MCP process wraps (plaintext only in VPS memory
  briefly) → backend stores ciphertext. The AEAD AAD binds
  `owner_user_id || v || id`; any mismatch on read-back is rejected by the
  enclave's decrypt path. Once MCP moves into the TEE (Phase C), even
  the brief in-memory exposure on the VPS goes away.

Primitives used (matches iOS + enclave):
  - BoxSeal: X25519 ECDH → HKDF-SHA256(salt=None, info="feedling-box-seal-v1")
    → nonce = SHA256(ek_pub || recipient_pub)[:12] → ChaCha20-Poly1305.
  - Body: ChaCha20-Poly1305 IETF (12-byte random nonce), AAD = UTF-8
    "owner_user_id|1|item_id".

If any of those drift, the enclave's AEAD verify fails and the agent
can't read that item back. Keep this module in lockstep with
`ContentEncryption.swift` and the enclave's `_box_seal_open_hkdf`.
"""
from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives import serialization


_BOX_SEAL_INFO = b"feedling-box-seal-v1"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def random_item_id() -> str:
    """16-byte hex id matching ContentEncryption.randomItemID on iOS."""
    return secrets.token_bytes(16).hex()


def box_seal(plaintext: bytes, recipient_pk_bytes: bytes) -> bytes:
    """Seal `plaintext` for a recipient who holds the X25519 private key
    matching `recipient_pk_bytes` (raw 32-byte encoding).

    Wire-compatible with testapp/FeedlingTest/ContentEncryption.swift's
    BoxSeal.seal and backend/enclave_app.py's `_box_seal_open_hkdf`.
    """
    if len(recipient_pk_bytes) != 32:
        raise ValueError(f"recipient pubkey must be 32 bytes, got {len(recipient_pk_bytes)}")
    recipient_pk = X25519PublicKey.from_public_bytes(recipient_pk_bytes)

    # Fresh ephemeral keypair, ECDH to get shared secret.
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    shared = ek.exchange(recipient_pk)

    # HKDF: salt=None, info="feedling-box-seal-v1", 32 bytes.
    # `salt=None` matches iOS `salt: Data()` and the enclave's
    # _box_seal_open_hkdf — all three resolve to a zero-filled salt of
    # the hash's output length (32 bytes for SHA-256).
    k_wrap = HKDF(algorithm=SHA256(), length=32, salt=None,
                  info=_BOX_SEAL_INFO).derive(shared)

    # Deterministic nonce bound to the ephemeral + recipient pubkeys.
    nonce = hashlib.sha256(ek_pub + recipient_pk_bytes).digest()[:12]

    # ChaCha20-Poly1305, no AAD on the sealed K itself — AAD belongs to
    # the BODY layer. output: ciphertext || 16-byte tag
    ct_plus_tag = ChaCha20Poly1305(k_wrap).encrypt(nonce, plaintext, None)

    return ek_pub + ct_plus_tag


def build_envelope(
    *,
    plaintext: bytes,
    owner_user_id: str,
    user_pk_bytes: bytes,
    enclave_pk_bytes: bytes | None,
    visibility: str = "shared",
    item_id: str | None = None,
    v: int = 1,
) -> dict:
    """Produce the JSON shape POSTed as `{"envelope": {...}}` to
    /v1/chat/message, /v1/memory/add, and /v1/identity/init.

    - `visibility="shared"` → K is sealed to BOTH the user and the enclave;
      an agent can read via the enclave's decrypt endpoint.
    - `visibility="local_only"` → K sealed only to the user; the agent
      (and the enclave) cannot read. `enclave_pk_bytes` may be None.
    """
    if visibility not in ("shared", "local_only"):
        raise ValueError(f"visibility must be 'shared' or 'local_only', got {visibility!r}")
    if visibility == "shared" and not enclave_pk_bytes:
        raise ValueError("shared visibility requires enclave_pk_bytes")

    item_id = item_id or random_item_id()

    # Per-item body key + 12-byte random nonce.
    K = secrets.token_bytes(32)
    body_nonce = secrets.token_bytes(12)

    # AEAD AAD binds owner_user_id || v || item_id. The enclave recomputes
    # this from (resolved user_id, v, id) on read; any mismatch fails AEAD.
    aad = f"{owner_user_id}|{v}|{item_id}".encode("utf-8")
    body_ct = ChaCha20Poly1305(K).encrypt(body_nonce, plaintext, aad)

    k_user = box_seal(K, user_pk_bytes)
    env: dict = {
        "v": v,
        "id": item_id,
        "owner_user_id": owner_user_id,
        "visibility": visibility,
        "body_ct": _b64(body_ct),
        "nonce": _b64(body_nonce),
        "K_user": _b64(k_user),
        "enclave_pk_fpr": "",
        # Which user pk K_user was sealed to. Lets the rewrap endpoint skip
        # already-current envelopes and lets chat ingest bounce a writer whose
        # cached key is stale (2026-07-16 usr_f13f: a consumer sealed two days
        # of replies to a retired key; unlabeled rows can't be told apart).
        "content_pk_fpr": hashlib.sha256(user_pk_bytes).hexdigest()[:16],
    }
    if visibility == "shared":
        env["K_enclave"] = _b64(box_seal(K, enclave_pk_bytes))
    return env
