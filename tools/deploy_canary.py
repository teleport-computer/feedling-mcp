#!/usr/bin/env python3
"""§3 post-deploy canary — proves the whole write→seal→enclave-decrypt loop on a
freshly deployed CVM, end to end:

  register → whoami's advertised enclave pk == /attestation's attested pk
          → build a v1 envelope sealed to that pk
          → enclave /v1/envelope/decrypt returns 200 + the original plaintext
          → reset the canary account (finally: never accumulate canary users)

Exit 0 = healthy. Non-zero = a real finding (key drift, advertised-vs-attested
divergence, or enclave/backend auth breakage). Wire it into CI after the deploy
step; also runnable by hand against any environment.

Config (env):
  FEEDLING_API_URL       backend base url   (default http://127.0.0.1:5001)
  FEEDLING_ENCLAVE_URL   enclave base url   (default http://127.0.0.1:5003)
  FEEDLING_CANARY_LABEL  register label     (default deploy-canary-<GITHUB_SHA|local>)
  FEEDLING_CANARY_RETRIES  decrypt retries  (default 4)

The enclave terminates its own in-enclave self-signed TLS on :5003s, so enclave
HTTPS calls are made WITHOUT cert verification — exactly how the backend reaches
it (verify=False). The backend API URL is verified normally.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

API_URL = os.environ.get("FEEDLING_API_URL", "http://127.0.0.1:5001").rstrip("/")
ENCLAVE_URL = os.environ.get("FEEDLING_ENCLAVE_URL", "http://127.0.0.1:5003").rstrip("/")
SHA = os.environ.get("GITHUB_SHA", "local")[:12]
LABEL = os.environ.get("FEEDLING_CANARY_LABEL", f"deploy-canary-{SHA}")
DECRYPT_RETRIES = int(os.environ.get("FEEDLING_CANARY_RETRIES", "4"))
HTTP_TRIES = int(os.environ.get("FEEDLING_CANARY_HTTP_TRIES", "5"))

# Transient right after a CVM (re)deploy: 0 = transport layer (TLS EOF, timeout,
# connection refused — the run #664 / PR #703 class), 502/503/504 = gateway while
# the backend is still coming up or wedged by the runner respawn storm.
_RETRYABLE = {0, 502, 503, 504}

_RAW = serialization.Encoding.Raw, serialization.PublicFormat.Raw
_INSECURE = ssl._create_unverified_context()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _http(method: str, url: str, *, body: dict | None = None, api_key: str | None = None,
          insecure: bool = False, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # Enclave calls (insecure=True) skip verification for the in-enclave
    # self-signed TLS; the backend API keeps normal cert verification.
    ctx = _INSECURE if (insecure and url.startswith("https")) else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw.decode("utf-8", "replace")[:300]}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # transport-layer failure (connection refused, DNS, TLS, timeout):
        # status 0 so callers treat it as a non-200 (fail or retry).
        return 0, {"error": f"transport: {getattr(e, 'reason', e)}"}


def _http_retry(method: str, url: str, **kw) -> tuple[int, dict]:
    """_http with exponential backoff on transient statuses (_RETRYABLE). Sleeps
    2,4,8,16,32s between the default 5 tries (~62s total) — sized to outlast the
    ~60s backend wedge after a CVM restart. A status that persists through every
    try is returned to the caller as a real finding."""
    for attempt in range(1, HTTP_TRIES + 1):
        st, body = _http(method, url, **kw)
        if st not in _RETRYABLE or attempt == HTTP_TRIES:
            return st, body
        print(f"[canary] {method} {url} -> {st}: {body.get('error')} "
              f"(attempt {attempt}/{HTTP_TRIES}, retrying)")
        time.sleep(2 ** attempt)
    return st, body  # unreachable; keeps type-checkers happy


def _box_seal(pt: bytes, recipient_pk: X25519PublicKey) -> bytes:
    """X25519 ECDH → HKDF-SHA256(salt=None, info='feedling-box-seal-v1') →
    nonce = SHA256(ek_pub || recipient_pub)[:12] → ChaCha20-Poly1305.
    Output ek_pub(32) || ct || tag(16). Byte-for-byte matches iOS BoxSeal.seal,
    backend/content_encryption.box_seal, and the enclave's _box_seal_open_hkdf —
    the enclave rejects any other scheme with 'box_seal tag invalid'."""
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(*_RAW)
    shared = ek.exchange(recipient_pk)
    recipient_raw = recipient_pk.public_bytes(*_RAW)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"feedling-box-seal-v1").derive(shared)
    nonce = hashlib.sha256(ek_pub + recipient_raw).digest()[:12]
    return ek_pub + ChaCha20Poly1305(key).encrypt(nonce, pt, None)


def _fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"CANARY FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    print(f"[canary] api={API_URL} enclave={ENCLAVE_URL} label={LABEL}")

    # 1. attested enclave content pk (ground truth from the TDX quote path)
    st, att = _http_retry("GET", f"{ENCLAVE_URL}/attestation", insecure=True)
    if st != 200:
        _fail(f"/attestation returned {st}: {att.get('error')}")
    attested_pk = str(att.get("enclave_content_pk_hex") or "")
    if len(attested_pk) != 64:
        _fail(f"attested enclave_content_pk_hex looks wrong: {attested_pk!r}")
    print(f"[canary] attested enclave pk = {attested_pk[:16]}…")

    # 2. register a throwaway user (fresh keypair → no orphan-backstop collision)
    user_sk = X25519PrivateKey.generate()
    user_pk = user_sk.public_key()
    st, reg = _http_retry("POST", f"{API_URL}/v1/users/register",
                    body={"public_key": _b64(user_pk.public_bytes(*_RAW)),
                          "platform": "deploy-canary", "label": LABEL})
    if st != 201:
        _fail(f"register returned {st}: {reg.get('error')}")
    user_id, api_key = reg["user_id"], reg["api_key"]
    print(f"[canary] registered {user_id}")

    try:
        # 3. whoami's advertised pk MUST equal the attested pk
        st, who = _http_retry("GET", f"{API_URL}/v1/users/whoami", api_key=api_key)
        if st != 200:
            _fail(f"whoami returned {st}: {who.get('error')}")
        advertised_pk = str(who.get("enclave_content_public_key_hex") or "")
        if advertised_pk != attested_pk:
            _fail(f"advertised pk != attested pk\n  advertised={advertised_pk}\n  attested ={attested_pk}")
        print("[canary] advertised pk == attested pk ✓")

        # 4. build a v1 shared envelope sealed to the enclave pk
        enclave_pk = X25519PublicKey.from_public_bytes(bytes.fromhex(attested_pk))
        item_id = secrets.token_hex(16)
        plaintext = f"deploy-canary {SHA} {item_id}".encode()
        K = secrets.token_bytes(32)
        nonce = secrets.token_bytes(12)
        aad = f"{user_id}|1|{item_id}".encode()
        body_ct = ChaCha20Poly1305(K).encrypt(nonce, plaintext, aad)
        envelope = {
            "id": item_id, "v": 1, "owner_user_id": user_id, "visibility": "shared",
            "body_ct": _b64(body_ct), "nonce": _b64(nonce),
            "K_user": _b64(_box_seal(K, user_pk)),
            "K_enclave": _b64(_box_seal(K, enclave_pk)),
            "enclave_pk_fpr": "",
        }

        # 5. enclave decrypt — retry: the enclave→backend whoami hop can 502 under
        #    load. One 502 is noise; persistent 502 is a real finding.
        last = ""
        for attempt in range(1, DECRYPT_RETRIES + 1):
            st, dec = _http("POST", f"{ENCLAVE_URL}/v1/envelope/decrypt",
                            body={"envelope": envelope}, api_key=api_key, insecure=True)
            if st == 200:
                got = base64.b64decode(dec.get("plaintext_b64", ""))
                if got != plaintext:
                    _fail(f"decrypt plaintext mismatch: {got!r} != {plaintext!r}")
                print(f"[canary] enclave decrypt round-trip ✓ (attempt {attempt})")
                break
            last = f"{st}: {dec.get('error')}"
            print(f"[canary] decrypt attempt {attempt}/{DECRYPT_RETRIES} -> {last}")
            if attempt < DECRYPT_RETRIES:
                time.sleep(2 ** attempt)  # 2,4,8s backoff
        else:
            _fail(f"enclave decrypt never succeeded after {DECRYPT_RETRIES} tries; last={last}")
    finally:
        # 6. always delete the canary account — must never accumulate
        reset_status, _ = _http_retry("POST", f"{API_URL}/v1/account/reset",
                                      body={"confirm": "delete-all-data"}, api_key=api_key)
        print(f"[canary] account reset -> {reset_status}")

    # Only reached on the SUCCESS path: a _fail() inside the try raises SystemExit
    # that propagates straight through the finally, skipping everything below. So a
    # non-200 here means the round-trip otherwise passed but the throwaway account
    # was NOT cleaned up — the canary's self-clean guarantee is broken, so fail.
    if reset_status != 200:
        _fail(f"canary round-trip passed but /v1/account/reset returned "
              f"{reset_status} — the throwaway account was NOT deleted")
    print("CANARY OK")


if __name__ == "__main__":
    main()
