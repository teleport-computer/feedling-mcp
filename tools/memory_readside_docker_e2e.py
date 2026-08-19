#!/usr/bin/env python3
"""Docker-backed IO Memory readside product E2E.

Starts the local sandbox stack, registers a throwaway user, writes encrypted
memory fixtures, then exercises /v1/memory/index and /v1/memory/fetch.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import hashlib
import httpx
import nacl.bindings
import nacl.public
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import serialization

from memory_readside_sandbox import fixture_cards
from memory_readside_smoke import _print_acceptance, _print_fetch, _print_index


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "docker-compose.memory-sandbox.yaml"
BOX_SEAL_INFO = b"feedling-box-seal-v1"
DEFAULT_TRACE_QUERY = "我不是服务端，我想看流程和例子，知道这次 memory 改动真实发生了什么，数据怎么流动。"

sys.path.insert(0, str(ROOT / "backend"))
from memory_garden.scoring.selector import select_memory_index_items  # noqa: E402


def _run(cmd: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, env=env, text=True, check=check)


def _wait_for(url: str, timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=2)
            if resp.status_code < 500:
                return
            last = f"HTTP {resp.status_code}: {resp.text[:160]}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(0.5)
    raise SystemExit(f"Timed out waiting for {url}: {last}")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _box_seal_hkdf(plaintext: bytes, recipient_pk_bytes: bytes) -> bytes:
    ek = X25519PrivateKey.generate()
    recipient = X25519PublicKey.from_public_bytes(recipient_pk_bytes)
    shared = ek.exchange(recipient)
    wrap_key = HKDF(algorithm=SHA256(), length=32, salt=None, info=BOX_SEAL_INFO).derive(shared)
    ek_pub = ek.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    nonce = hashlib.sha256(ek_pub + recipient_pk_bytes).digest()[:12]
    ciphertext = ChaCha20Poly1305(wrap_key).encrypt(nonce, plaintext, None)
    return ek_pub + ciphertext


def _build_aead_aad(owner_user_id: str, v: int, item_id: str) -> bytes:
    return f"{owner_user_id}|{v}|{item_id}".encode("utf-8")


def _post(url: str, api_key: str, body: dict) -> dict:
    resp = httpx.post(url, headers={"X-API-Key": api_key}, json=body, timeout=20)
    if resp.status_code >= 400:
        raise SystemExit(f"HTTP {resp.status_code} from {url}: {resp.text[:300]}")
    return resp.json()


def _build_memory_envelope(
    *,
    user_id: str,
    user_pk: nacl.public.PublicKey,
    enclave_pk: nacl.public.PublicKey,
    memory_id: str,
    inner: dict,
) -> dict:
    body = json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    aad = _build_aead_aad(user_id, 1, memory_id)
    body_ct = nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(body, aad, nonce, key)
    return {
        "v": 1,
        "id": memory_id,
        "body_ct": _b64(body_ct),
        "nonce": _b64(nonce),
        "K_user": _b64(_box_seal_hkdf(key, bytes(user_pk))),
        "K_enclave": _b64(_box_seal_hkdf(key, bytes(enclave_pk))),
        "enclave_pk_fpr": enclave_pk.encode()[:16].hex(),
        "visibility": "shared",
        "owner_user_id": user_id,
        "type": str(inner.get("type") or "fact"),
        "occurred_at": str(inner.get("occurred_at") or "2026-06-20T15:30:00"),
        "source": str(inner.get("source") or "memory_readside_sandbox"),
        "salience": str(inner.get("salience") or "medium"),
        "importance": float(inner.get("importance") or 0.5),
        "is_open_thread": bool(inner.get("is_open_thread")),
    }


def _seed_memories(base_url: str, api_key: str, user_id: str, enclave_pk: nacl.public.PublicKey) -> list[str]:
    user_sk = nacl.public.PrivateKey.generate()
    user_pk = user_sk.public_key
    ids: list[str] = []
    for idx, card in enumerate(fixture_cards(), start=1):
        inner = {
            "summary": card.summary,
            "verbatim": card.verbatim,
            "bucket_refs": card.bucket_refs,
            "status": card.status,
            "salience": card.salience,
            "follow_up": card.follow_up,
            "context": card.context,
            "source_type": card.source_type,
            "is_open_thread": card.is_open_thread,
            "sensitive_scope": card.sensitive_scope,
            "importance": card.importance,
            "type": "fact",
            "occurred_at": f"2026-06-20T15:{idx:02d}:00",
            "source": "memory_readside_sandbox",
        }
        envelope = _build_memory_envelope(
            user_id=user_id,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            memory_id=card.id,
            inner=inner,
        )
        _post(f"{base_url}/v1/memory/add", api_key, {"envelope": envelope})
        ids.append(card.id)
    return ids


def _print_trace(query: str, index_items: list[dict], selector_trace: dict, fetch: dict) -> None:
    print("\n=== 0. trace: 这次 agent 读 memory 的真实数据流 ===")
    print(f"用户当前问题: {query}")
    print("\nStep 1 server/backend 做什么：")
    print("- 从当前 user_id 的 memory_moments 里筛候选。")
    print("- 排除 local_only、没有 K_enclave、archived/deleted/superseded。")
    print("- 按 open_thread、salience、importance、时间排序，最多取 top 50。")
    print("\nStep 2 enclave 做什么：")
    print("- backend 把候选 envelope 发给 enclave。")
    print("- enclave 解密密文正文，只生成安全 index。")
    print("- index 只给 summary/bucket/status/salience/is_sensitive，不给原话。")
    print("\nStep 3 agent 在 index 里看到这些候选：")
    for idx, item in enumerate(index_items, start=1):
        print(f"{idx:02d}. {item.get('id')} -> {item.get('summary')}")
    print("\nStep 4 agent 根据当前问题选择要打开的 id：")
    print(f"selected_ids={[item.get('id') for item in selector_trace.get('selected', [])]}")
    for item in selector_trace.get("selected", []):
        print(
            f"- selected {item.get('id')}: score={item.get('score')} "
            f"confidence={item.get('confidence')} reason={item.get('reason')}"
        )
    skipped = selector_trace.get("skipped_sample") or []
    if skipped:
        print("跳过样例：")
        for item in skipped[:5]:
            print(f"- skipped {item.get('id')}: reason={item.get('reason')}")
    print("\nStep 5 server/enclave fetch 这些 id 的正文：")
    for item in fetch.get("items") or []:
        print(f"- {item.get('id')}: verbatim={item.get('verbatim')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Docker-backed memory readside E2E sandbox.")
    parser.add_argument("--no-up", action="store_true", help="Reuse already running containers.")
    parser.add_argument("--down", action="store_true", help="Stop and remove sandbox containers after the run.")
    parser.add_argument("--backend-url", default="http://127.0.0.1:5001")
    parser.add_argument("--enclave-url", default="http://127.0.0.1:5003")
    parser.add_argument("--trace-query", default=DEFAULT_TRACE_QUERY)
    args = parser.parse_args()

    env = dict(os.environ)
    compose_cmd = ["docker", "compose", "-f", str(COMPOSE)]
    if not args.no_up:
        _run([*compose_cmd, "up", "-d", "--build"], env=env)
    try:
        _wait_for(f"{args.backend_url}/healthz")
        _wait_for(f"{args.enclave_url}/healthz")

        att = httpx.get(f"{args.enclave_url}/attestation", timeout=20).json()
        enclave_pk = nacl.public.PublicKey(bytes.fromhex(att["enclave_content_pk_hex"]))

        user = httpx.post(f"{args.backend_url}/v1/users/register", json={}, timeout=20).json()
        user_id = user["user_id"]
        api_key = user["api_key"]
        _seed_memories(args.backend_url, api_key, user_id, enclave_pk)

        index = _post(f"{args.backend_url}/v1/memory/index", api_key, {"limit": 10})
        selection = select_memory_index_items(args.trace_query, index.get("items", []), cap=3)
        fetch_ids = selection["selected_ids"]
        fetch = _post(f"{args.backend_url}/v1/memory/fetch", api_key, {"ids": fetch_ids})

        print(f"\nLocal sandbox user_id: {user_id}")
        print(f"Local sandbox api_key: {api_key}")
        print(f"iOS self-hosted API URL: {args.backend_url}")
        _print_trace(args.trace_query, index.get("items", []), selection["trace"], fetch)
        _print_index(index.get("items", []))
        _print_fetch(fetch.get("items", []), fetch.get("missing_ids", []), fetch.get("unavailable_ids", []))
        _print_acceptance(index.get("items", []), fetch)
    finally:
        if args.down:
            _run([*compose_cmd, "down"], env=env, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
