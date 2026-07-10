"""Enclave key derivation: content (X25519) + signing (Ed25519) keypairs.

Derives deterministic keys from either dstack's KMS (production/CVM) or a
local dev seed (Docker sandboxes, tests). See enclave_app.py's historical
"Key derivation" section for the design rationale — this module is a
verbatim extraction.
"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Any

import anyio.to_thread
import nacl.public
import nacl.signing
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from dstack_sdk import DstackClient

CONTENT_KEY_PATH = "feedling-content-v1"
SIGNING_KEY_PATH = "feedling-signing-v1"
# Storage-at-rest key family (D4): a per-version symmetric key HKDF-derived from
# a KMS seed on a dedicated path — same derivation family as content/signing, so
# it inherits the (compose_hash, app_id, path) determinism. ``version`` tags the
# path AND the HKDF info for domain separation, so "v1" and a future "v2" never
# collide.
FRAME_STORAGE_KEY_PREFIX = "feedling-frame-storage"
# TLS_KEY_PATH is imported from dstack_tls so enclave_app
# derive from the same KMS-bound path.


def _dev_seed_bytes(path: str) -> bytes:
    seed = os.environ.get("FEEDLING_DEV_DSTACK_SEED", "").strip()
    if not seed:
        raise RuntimeError("FEEDLING_DEV_DSTACK_SEED is not set")
    return hashlib.sha256(f"{seed}:{path}".encode("utf-8")).digest()


def derive_keys_from_dev_seed() -> dict[str, Any]:
    """Derive deterministic local-only keys for Docker sandboxes.

    This is intentionally opt-in via FEEDLING_DEV_DSTACK_SEED. Production and
    test deployments do not set it, so they still require dstack KMS.
    """
    content_sk = nacl.public.PrivateKey(_dev_seed_bytes(CONTENT_KEY_PATH))
    content_pk = content_sk.public_key
    signing_sk = nacl.signing.SigningKey(_dev_seed_bytes(SIGNING_KEY_PATH))
    signing_pk = signing_sk.verify_key
    return {
        "content_sk": content_sk,
        "content_pk": content_pk,
        "content_pk_bytes": bytes(content_pk),
        "signing_sk": signing_sk,
        "signing_pk": signing_pk,
        "signing_pk_bytes": bytes(signing_pk),
    }


def derive_keys(dstack: DstackClient) -> dict[str, Any]:
    """Derive the enclave's long-lived keypairs from dstack's KMS.

    These derivations are deterministic per (compose_hash, app_id, path) —
    so the same image running on two CVMs produces the same keys, but a
    different compose_hash produces a different key automatically.
    """
    # Content keypair: X25519 for libsodium sealed-box decryption.
    # dstack's get_key returns 32 bytes of seed which we use as the
    # X25519 private scalar directly.
    content_resp = dstack.get_key(CONTENT_KEY_PATH, "")
    content_seed = bytes.fromhex(content_resp.key) if isinstance(content_resp.key, str) else content_resp.key
    content_sk = nacl.public.PrivateKey(content_seed[:32])
    content_pk = content_sk.public_key

    # Signing keypair: Ed25519 for per-request signed decryption proofs.
    signing_resp = dstack.get_key(SIGNING_KEY_PATH, "")
    signing_seed = bytes.fromhex(signing_resp.key) if isinstance(signing_resp.key, str) else signing_resp.key
    signing_sk = nacl.signing.SigningKey(signing_seed[:32])
    signing_pk = signing_sk.verify_key

    return {
        "content_sk": content_sk,
        "content_pk": content_pk,
        "content_pk_bytes": bytes(content_pk),
        "signing_sk": signing_sk,
        "signing_pk": signing_pk,
        "signing_pk_bytes": bytes(signing_pk),
    }


def get_or_derive_content_sk() -> nacl.public.PrivateKey:
    """Return the process-lifetime content X25519 private key.

    bootstrap() derives this once and stores it in memory. The fallback derive
    path is kept for defensive compatibility, but normal request handling
    should not make a fresh dstack KMS round-trip.
    """
    global _cached_content_sk
    if _cached_content_sk is not None:
        return _cached_content_sk
    # Double-checked lock: the server runs threaded, so two concurrent first
    # callers must not both derive. Derivation is deterministic (same key
    # either way), but the lock keeps it to a single DstackClient round-trip.
    with _content_sk_lock:
        if _cached_content_sk is not None:
            return _cached_content_sk
        dev_seed = os.environ.get("FEEDLING_DEV_DSTACK_SEED", "").strip()
        keys = derive_keys_from_dev_seed() if dev_seed else derive_keys(DstackClient())
        _cached_content_sk = keys["content_sk"]
    return _cached_content_sk


_cached_content_sk: nacl.public.PrivateKey | None = None
_content_sk_lock = threading.Lock()

# Per-version cache for the AES-256 storage keys. Derivation is deterministic,
# so concurrent first callers at worst repeat it (same bytes either way); the
# lock keeps the dstack round-trip to once per version.
_storage_keys: dict[str, bytes] = {}
_storage_key_lock = threading.Lock()


def _storage_key_path(version: str) -> str:
    return f"{FRAME_STORAGE_KEY_PREFIX}-{version}"


def _derive_storage_key(version: str) -> bytes:
    """32-byte AES-256 storage key for ``version``, HKDF'd from a KMS seed.

    Mirrors derive_keys: dev seed in sandboxes/tests (FEEDLING_DEV_DSTACK_SEED),
    dstack KMS in production. HKDF domain-separates by the versioned path so the
    stored ciphertext is bound to its key_version tag."""
    path = _storage_key_path(version)
    dev_seed = os.environ.get("FEEDLING_DEV_DSTACK_SEED", "").strip()
    if dev_seed:
        seed = _dev_seed_bytes(path)
    else:
        resp = DstackClient().get_key(path, "")
        raw = bytes.fromhex(resp.key) if isinstance(resp.key, str) else resp.key
        seed = raw[:32]
    return HKDF(algorithm=SHA256(), length=32, salt=None,
                info=path.encode("utf-8")).derive(seed)


def get_or_derive_storage_key(version: str = "v1") -> bytes:
    """Return the process-lifetime AES-256 storage key for ``version``."""
    hit = _storage_keys.get(version)
    if hit is not None:
        return hit
    with _storage_key_lock:
        hit = _storage_keys.get(version)
        if hit is not None:
            return hit
        key = _derive_storage_key(version)
        _storage_keys[version] = key
    return key


async def get_storage_key(version: str = "v1") -> bytes:
    """async accessor for the storage key (dstack socket I/O off the loop)."""
    hit = _storage_keys.get(version)
    if hit is not None:
        return hit
    return await anyio.to_thread.run_sync(get_or_derive_storage_key, version)


def set_cached_content_sk(sk: nacl.public.PrivateKey) -> None:
    """bootstrap() 派生完成后注入进程级缓存（旧 _cached_content_sk 赋值的显式接口）。"""
    global _cached_content_sk
    _cached_content_sk = sk


async def get_content_sk() -> nacl.public.PrivateKey:
    """async 路由取 content_sk 的唯一入口。缓存命中直接返回（常态路径，
    bootstrap 已派生）；未命中时经 to_thread 走 get_or_derive_content_sk
    （dstack socket 回环是阻塞 I/O，不能挂在事件循环上）。

    刻意不加 asyncio 锁：模块级 asyncio.Lock 会绑死在首个事件循环上，在
    多 loop 测试环境（每个 asyncio.run 一个新 loop）会炸；而并发首派生
    最多多跑几次**确定性**派生（结果相同），get_or_derive_content_sk 内部
    的 threading.Lock 已把 dstack round-trip 收敛为单次。"""
    if _cached_content_sk is not None:
        return _cached_content_sk
    return await anyio.to_thread.run_sync(get_or_derive_content_sk)
