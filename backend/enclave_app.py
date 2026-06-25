#!/usr/bin/env python3
"""
Feedling enclave service — Phase 1 skeleton.

Runs inside the dstack TDX CVM (or the local dstack simulator during dev).
Exposes two endpoints:

    GET /attestation   — the TDX quote + published pubkeys + release info
    GET /healthz       — liveness probe (no auth)

Phase 1 scope (this file):
    - Derive the enclave content keypair via dstack KMS (bound to
      compose_hash + app_id — cannot be extracted outside this image).
    - Derive the enclave signing keypair.
    - Build REPORT_DATA binding the content pubkey + a placeholder
      TLS cert fingerprint (real TLS termination inside the enclave
      ships in Phase 3).
    - Request a TDX quote from dstack with that REPORT_DATA.
    - Serve the bundle at GET /attestation.

What's NOT here yet (future phases):
    - Phase 2: decryption tool handlers that unseal K_enclave and return
      plaintext to MCP.
    - Phase 3: the FastMCP SSE server itself moves in here; TLS terminates
      inside the enclave via rustls; cert issued via ACME-DNS-01.

See docs/DESIGN_E2E.md §5, §7 for the full architecture.
"""

from __future__ import annotations

import atexit
import base64
import datetime as _dt
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import time
from typing import Any

import httpx
import nacl.bindings
import nacl.encoding
import nacl.exceptions
import nacl.public
import nacl.signing
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidTag
from io import BytesIO

from flask import Flask, jsonify, Response, request, send_file
from flask_compress import Compress
from dstack_sdk import DstackClient

from dstack_tls import derive_tls_cert_and_key, TLS_KEY_PATH

import provider_client


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# For local dev we point at the simulator; in a real CVM, dstack-sdk defaults
# to /var/run/dstack.sock inside the container.
#
# dstack-sdk checks `"DSTACK_SIMULATOR_ENDPOINT" in os.environ` — presence,
# not truthiness. An env var set to "" counts as present and makes the SDK
# try to connect to "" (EINVAL). Drop it if it's empty so the SDK falls
# through to /var/run/dstack.sock. A non-empty value means "I really do
# want the simulator" and stays put.
if os.environ.get("DSTACK_SIMULATOR_ENDPOINT", "") == "":
    os.environ.pop("DSTACK_SIMULATOR_ENDPOINT", None)

ENCLAVE_PORT = int(os.environ.get("FEEDLING_ENCLAVE_PORT", 5003))

# Phase 3: in-enclave TLS. When true, bootstrap() derives an ECDSA P-256
# keypair from dstack-KMS, issues a self-signed cert for it, binds
# sha256(cert-DER) into REPORT_DATA, and serves Flask over HTTPS on
# ENCLAVE_PORT. Clients verify by matching the presented cert's DER
# hash against the attested fingerprint — not by PKI chain, since the
# cert is self-signed on purpose (key material is bound to compose_hash
# via dstack-KMS, which is stronger than LE trust).
#
# Off by default so the local dstack simulator + curl/httpx stay HTTP.
# docker-compose.phala.yaml sets this true on real deployments.
ENCLAVE_TLS = os.environ.get("FEEDLING_ENCLAVE_TLS", "false").lower() == "true"

# Internal HTTPS (or HTTP in dev) to the non-TEE Flask backend. This is the
# only network dependency the enclave has after boot. Requests carry the
# caller's api_key so Flask's require_user resolves to the right user's
# ciphertext. The enclave never sees users.json directly.
FLASK_URL = os.environ.get("FEEDLING_FLASK_URL", "http://127.0.0.1:5001")

# Screen VLM (caption route) — reads at startup; runtime re-reads os.environ
# so a secret rotation takes effect without a restart.
SCREEN_VLM_API_KEY = os.environ.get("FEEDLING_SCREEN_VLM_API_KEY", "")
SCREEN_VLM_MODEL = os.environ.get("FEEDLING_SCREEN_VLM_MODEL", "qwen/qwen3-vl-8b-instruct")
SCREEN_VLM_BASE_URL = os.environ.get("FEEDLING_SCREEN_VLM_BASE_URL", "https://openrouter.ai/api/v1")

# Release metadata — normally injected via build-time env or read from a
# sidecar file baked into the image. For Phase 1 we accept env values with
# obvious placeholders so it's clear this isn't fabricated content.
RELEASE = {
    "git_commit": os.environ.get("FEEDLING_GIT_COMMIT", "dev"),
    "image_digest": os.environ.get("FEEDLING_IMAGE_DIGEST", "sha256:dev"),
    "built_at": os.environ.get("FEEDLING_BUILT_AT", "dev"),
    "compose_yaml_url": os.environ.get(
        "FEEDLING_COMPOSE_YAML_URL",
        "https://github.com/teleport-computer/feedling-mcp/raw/main/deploy/docker-compose.yaml",
    ),
    "build_recipe_url": os.environ.get(
        "FEEDLING_BUILD_RECIPE_URL",
        "https://github.com/teleport-computer/feedling-mcp/blob/main/deploy/BUILD.md",
    ),
}

# Phase 1 testnet deployment (Ethereum Sepolia, chain 11155111). Will be
# redeployed to Base Sepolia (chain 84532) before Phase 2, then to Base
# mainnet (chain 8453) before Phase 5. The default is the live Phase 1
# testnet contract; env vars override when we bring up new chains.
APP_AUTH = {
    "contract": os.environ.get(
        "FEEDLING_APP_AUTH_CONTRACT",
        "0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F",
    ),
    "chain_id": int(os.environ.get("FEEDLING_APP_AUTH_CHAIN_ID", 11155111)),
    "deploy_tx": os.environ.get(
        "FEEDLING_APP_AUTH_DEPLOY_TX",
        "0x752f213ae95f6759a86750dab9545c79c6841ad7838082ddf6ad5271d117915f",
    ),
    "explorer_base_url": os.environ.get(
        "FEEDLING_APP_AUTH_EXPLORER",
        "https://sepolia.etherscan.io",
    ),
}

# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

CONTENT_KEY_PATH = "feedling-content-v1"
SIGNING_KEY_PATH = "feedling-signing-v1"
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


# ---------------------------------------------------------------------------
# TLS cert material (Phase 3)
# ---------------------------------------------------------------------------


# Sentinel "no TLS binding" fingerprint. Before Phase 3 the bundle always
# carried this (Caddy/gateway terminated TLS). Post-Phase 3 this appears
# only when ENCLAVE_TLS=false (local dev). iOS treats all-zeros as
# "operator terminates TLS" and surfaces the amber disclosure.
PHASE1_TLS_FINGERPRINT = b"\x00" * 32


# `derive_tls_cert_and_key` is imported from `dstack_tls` so enclave_app
# (which also terminates TLS inside the enclave in Phase C) derives from
# the same path and produces the same cert.


# ---------------------------------------------------------------------------
# Attestation assembly
# ---------------------------------------------------------------------------


def build_report_data(content_pk_bytes: bytes, tls_cert_fingerprint: bytes, version_tag: bytes) -> bytes:
    """Construct the 64-byte REPORT_DATA per docs/DESIGN_E2E.md §5.1.

    Layout:
        [0:32]  sha256(content_pk || sha256(tls_cert_der) || "feedling-v1")
        [32]    version_byte
        [33]    flag_byte (bit 0: phase-1 placeholder TLS fingerprint)
        [34:64] reserved (zeros)
    """
    if len(tls_cert_fingerprint) != 32:
        raise ValueError("tls_cert_fingerprint must be 32 bytes (sha256)")
    binding = hashlib.sha256(content_pk_bytes + tls_cert_fingerprint + version_tag).digest()
    version_byte = b"\x01"
    flag_byte = b"\x01" if tls_cert_fingerprint == PHASE1_TLS_FINGERPRINT else b"\x00"
    reserved = b"\x00" * 30
    return binding + version_byte + flag_byte + reserved


def fetch_quote_and_measurements(dstack: DstackClient, report_data: bytes) -> dict[str, Any]:
    """Ask dstack for a TDX quote over our report_data, and pull the live
    measurement registers out of /info for clients to cross-check."""
    quote_resp = dstack.get_quote(report_data)
    info = dstack.info()
    tcb = info.tcb_info

    # event_log on the quote response is a JSON-encoded string; forward
    # as-is so the iOS verifier can decode if it wants to cross-check
    # RTMR values against the event chain.
    event_log_raw = getattr(quote_resp, "event_log", "") or ""

    # Parse mr_config_id directly from the raw quote bytes — the dstack SDK's
    # TcbInfo doesn't expose it, but dstack encodes compose_hash there on
    # real deployments per the convention from dstack-tutorial:
    #   mr_config_id[0]    = 0x01 (version marker)
    #   mr_config_id[1:33] = sha256(canonical(app_compose))
    #   mr_config_id[33:]  = zero padding
    # The simulator leaves mr_config_id all zeros, so the iOS auditor
    # treats a non-zero mr_config_id[0]=0x01 as an additional independent
    # confirmation of compose_hash, not a mandatory check.
    quote_hex = quote_resp.quote if isinstance(quote_resp.quote, str) else quote_resp.quote.hex()
    mr_config_id_hex = ""
    try:
        qbytes = bytes.fromhex(quote_hex)
        # TD Report body starts at offset 48; mr_config_id at body+184, 48 bytes
        mr_config_id_hex = qbytes[48 + 184:48 + 184 + 48].hex()
    except Exception:
        pass

    return {
        "tdx_quote_hex": quote_hex,
        "event_log_json": event_log_raw,
        "measurements": {
            "mrtd": tcb.mrtd,
            "rtmr0": tcb.rtmr0,
            "rtmr1": tcb.rtmr1,
            "rtmr2": tcb.rtmr2,
            "rtmr3": tcb.rtmr3,
            "mr_aggregated": tcb.mr_aggregated,
            "mr_config_id": mr_config_id_hex,
        },
        "compose_hash": info.compose_hash,
        "app_id": info.app_id,
        "instance_id": info.instance_id,
    }


def dev_attestation(report_data: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(report_data + os.environ.get("FEEDLING_DEV_DSTACK_SEED", "").encode("utf-8")).hexdigest()
    zero_measurement = "00" * 48
    return {
        "tdx_quote_hex": digest,
        "event_log_json": "[]",
        "measurements": {
            "mrtd": zero_measurement,
            "rtmr0": zero_measurement,
            "rtmr1": zero_measurement,
            "rtmr2": zero_measurement,
            "rtmr3": zero_measurement,
            "mr_aggregated": zero_measurement,
            "mr_config_id": "",
        },
        "compose_hash": f"dev-memory-sandbox-{digest[:16]}",
        "app_id": "dev-memory-sandbox",
        "instance_id": "dev-memory-sandbox",
    }


# ---------------------------------------------------------------------------
# Cached attestation state
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {
    "ready": False,
    "error": None,
    "content_pk_hex": None,
    "signing_pk_hex": None,
    "tls_cert_fingerprint_hex": PHASE1_TLS_FINGERPRINT.hex(),
    # Always empty since the MCP user line was removed (2026-06-12) —
    # kept in the payload so existing iOS audit-card parsers fall through
    # to the "Pre-Phase-C.2 deployment" disclosure row.
    "mcp_tls_cert_pubkey_fingerprint_hex": "",
    "tls_enabled": False,
    "tls_cert_pem": None,  # bytes; only kept for the SSLContext load path
    "tls_key_pem": None,   # bytes; only kept for the SSLContext load path
    "attestation": None,
    "booted_at": None,
}


def bootstrap():
    """Derive keys + generate attestation once at startup. Cached thereafter.

    When ENCLAVE_TLS is true we also derive an ECDSA P-256 cert bound to
    compose_hash and bake its sha256(DER) into REPORT_DATA so iOS can
    pin the TLS cert against the quote. Off → the old zero placeholder
    stays, and iOS will surface the amber "operator-terminated TLS" row.
    """
    global _cached_content_sk
    try:
        dev_seed = os.environ.get("FEEDLING_DEV_DSTACK_SEED", "").strip()
        dstack = None
        if dev_seed:
            keys = derive_keys_from_dev_seed()
        else:
            dstack = DstackClient()
            keys = derive_keys(dstack)

        tls_fingerprint = PHASE1_TLS_FINGERPRINT
        if ENCLAVE_TLS:
            if dstack is None:
                raise RuntimeError("FEEDLING_ENCLAVE_TLS=true is not supported with FEEDLING_DEV_DSTACK_SEED")
            try:
                tls = derive_tls_cert_and_key(dstack)
                tls_fingerprint = tls["fingerprint"]
                _state["tls_cert_pem"] = tls["cert_pem"]
                _state["tls_key_pem"] = tls["key_pem"]
                _state["tls_enabled"] = True
            except Exception as e:
                # Refuse to boot silently without TLS when the operator
                # asked for it — iOS would show "operator terminates TLS"
                # without the operator realizing the enclave never set it
                # up. Fail loudly instead.
                raise RuntimeError(f"TLS derivation failed: {e}") from e

        report_data = build_report_data(
            content_pk_bytes=keys["content_pk_bytes"],
            tls_cert_fingerprint=tls_fingerprint,
            version_tag=b"feedling-v1",
        )
        attestation = dev_attestation(report_data) if dstack is None else fetch_quote_and_measurements(dstack, report_data)

        _state["content_pk_hex"] = keys["content_pk_bytes"].hex()
        _state["signing_pk_hex"] = keys["signing_pk_bytes"].hex()
        _cached_content_sk = keys["content_sk"]
        _state["tls_cert_fingerprint_hex"] = tls_fingerprint.hex()
        _state["attestation"] = attestation
        _state["booted_at"] = time.time()
        _state["ready"] = True
        print(
            f"[enclave] ready: content_pk={_state['content_pk_hex'][:16]}… "
            f"compose_hash={attestation['compose_hash'][:16]}… "
            f"tls={'yes' if _state['tls_enabled'] else 'no'}",
            flush=True,
        )
    except Exception as e:
        _state["error"] = repr(e)
        print(f"[enclave] bootstrap failed: {e}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

app = Flask(__name__)
# gzip large JSON responses (decrypt-with-image ships ~470 KB of
# base64-encoded JPEG inside JSON — compresses down ~35-45%).
Compress(app)


@app.route("/healthz", methods=["GET"])
def healthz():
    if _state["ready"]:
        return jsonify({"ok": True, "ready": True})
    return jsonify({"ok": False, "ready": False, "error": _state["error"]}), 503


@app.route("/attestation", methods=["GET"])
def attestation():
    if not _state["ready"]:
        return jsonify({"error": "not_ready", "detail": _state["error"]}), 503

    att = _state["attestation"]
    bundle = {
        "tdx_quote_hex": att["tdx_quote_hex"],
        "event_log_json": att["event_log_json"],
        "measurements": att["measurements"],
        "compose_hash": att["compose_hash"],
        "app_id": att["app_id"],
        "instance_id": att["instance_id"],
        "enclave_content_pk_hex": _state["content_pk_hex"],
        "enclave_signing_pk_hex": _state["signing_pk_hex"],
        "enclave_tls_cert_fingerprint_hex": _state["tls_cert_fingerprint_hex"],
        # Phase C.2: sha256(SubjectPublicKeyInfo DER) of the MCP port's cert key.
        # Derived independently from dstack-KMS so it's pre-computable without
        # talking to the MCP service. Stable across LE cert renewals because the
        # key doesn't change — only the CA-signed certificate wrapper does.
        "mcp_tls_cert_pubkey_fingerprint_hex": _state["mcp_tls_cert_pubkey_fingerprint_hex"],
        "enclave_release": RELEASE,
        "app_auth": APP_AUTH,
        "report_data_version": 1,
        "phase": 3 if _state["tls_enabled"] else 1,
        "tls_in_enclave": _state["tls_enabled"],
        "notes": (
            "phase-3: TLS terminated inside the enclave."
            " enclave_tls_cert_fingerprint_hex = sha256(cert.DER) of the"
            " cert the TLS handshake presents. Clients must compare the"
            " live cert's DER hash to this value; do not trust the"
            " self-signed chain on its own."
            if _state["tls_enabled"] else
            "phase-1 skeleton — TLS cert binding is a placeholder (all"
            " zeros). Operator-controlled infrastructure terminates TLS."
            " Until in-enclave TLS is enabled, clients must trust the"
            " dstack-gateway operator to forward traffic unmodified."
        ),
        "booted_at": _state["booted_at"],
    }
    resp = Response(json.dumps(bundle, indent=2), mimetype="application/json")
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


# ---------------------------------------------------------------------------
# Decryption helpers
# ---------------------------------------------------------------------------


def _extract_api_key() -> str:
    """Pull the caller's api_key from X-API-Key / Bearer / ?key=.
    Mirrors app.py's auth path so the enclave stays a thin tool-caller."""
    h = request.headers.get("X-API-Key", "").strip()
    if h:
        return h
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # LEGACY / compat only — `?key=` leaks into URLs/logs; prefer the headers
    # above. Mirrors accounts/auth.py.
    return request.args.get("key", "").strip()


def _caller_runtime_token() -> str:
    """The caller's runtime token (Stage D), if present. The enclave doesn't
    verify it — it forwards it to the backend whoami, which does. Returns "" when
    called outside a request context (so cached resolvers can call it safely)."""
    try:
        return request.headers.get("X-Feedling-Runtime-Token", "").strip()
    except RuntimeError:
        return ""


def _forward_auth_headers(api_key: str, runtime_token: str) -> dict:
    """Headers to forward the caller's credential to the backend: a runtime token
    (preferred when present) or the long-term API key — same scope, nothing more."""
    if runtime_token:
        return {"X-Feedling-Runtime-Token": runtime_token}
    if api_key:
        return {"X-API-Key": api_key}
    return {}


def _flask_get(path: str, api_key: str, params: dict | None = None) -> dict:
    """Fetch JSON from the backend Flask as the authenticated caller. The enclave
    forwards the caller's credential rather than a privileged one — same scope the
    user granted, nothing more. Stage D: when the request carries a runtime token
    (api_key absent), forwards the token so decrypt-and-serve routes work without
    the long-term key."""
    return _flask_get_headers(path, _forward_auth_headers(api_key, _caller_runtime_token()), params)


def _flask_get_headers(path: str, headers: dict, params: dict | None = None) -> dict:
    """Like ``_flask_get`` but forwarding caller-supplied auth headers verbatim
    (api_key OR runtime token) — see ``_forward_auth_headers``."""
    with httpx.Client(timeout=15) as client:
        r = client.get(f"{FLASK_URL}{path}", params=params, headers=headers)
        r.raise_for_status()
        return r.json()


# Short-TTL cache for api_key -> whoami. Every enclave route resolves the caller
# through the backend (`/v1/users/whoami`) first — but the enclave runs threaded
# and the backend is gunicorn -w 1, so a backend -> enclave -> backend re-entrant
# whoami per call exhausted threads under load (e.g. history import does N
# decrypts for one user → N whoami round-trips). This cache collapses the
# read-only decrypt-and-serve routes (chat history, memory, identity, frames) to
# one round-trip per key per TTL; threaded=True handles the rest of the
# concurrency. Tradeoff: within the TTL a just-revoked key is still honoured, so
# the cache is used ONLY by routes that read the user's own stored data. The
# sensitive unwrap route /v1/envelope/decrypt (which opens a caller-SUPPLIED
# envelope) deliberately bypasses this and resolves the caller live every call.
_WHOAMI_CACHE_TTL = 30.0
_whoami_cache: dict[str, tuple[float, dict]] = {}
# _whoami_cache_lock is a short-held mutex guarding both _whoami_cache and the
# _whoami_inflight registry — never held across a backend round-trip.
_whoami_cache_lock = threading.Lock()
# Per-key singleflight: a lock per in-flight key so a burst of cold misses for
# the same key (cold cache at startup, or right after TTL expiry) collapses to a
# single /v1/users/whoami call instead of fanning out N round-trips.
_whoami_inflight: dict[str, threading.Lock] = {}


def _prune_whoami_cache(now: float | None = None) -> None:
    """Drop cache entries past the TTL. Called on every write so rotating runtime
    tokens (each a new cache key) can't grow the dict unboundedly. Caller holds
    ``_whoami_cache_lock``."""
    clock = time.monotonic() if now is None else now
    for h in [h for h, (ts, _) in _whoami_cache.items() if clock - ts >= _WHOAMI_CACHE_TTL]:
        _whoami_cache.pop(h, None)


def _whoami_cached(api_key: str) -> dict:
    """whoami via the backend, memoised per sha256(credential) for a short TTL.

    Misses call `_flask_get`/`_flask_get_headers` and propagate their httpx errors
    uncached, so every caller keeps its existing 401-on-HTTPStatusError /
    502-on-HTTPError mapping. Concurrent misses for the same key are serialised
    via singleflight: the first does the round-trip, the rest wait and reuse its
    cached result.

    Stage D: when the caller presents a runtime token (read from the request
    here, so the 7 decrypt-and-serve routes need no change), resolve + cache by
    the token instead of the api_key — the api-key path is unchanged.
    """
    runtime_token = _caller_runtime_token()
    cred = ("rt:" + runtime_token) if runtime_token else ("ak:" + api_key)
    h = hashlib.sha256(cred.encode("utf-8")).hexdigest()
    with _whoami_cache_lock:
        hit = _whoami_cache.get(h)
        if hit is not None and time.monotonic() - hit[0] < _WHOAMI_CACHE_TTL:
            return hit[1]
        key_lock = _whoami_inflight.get(h)
        if key_lock is None:
            key_lock = threading.Lock()
            _whoami_inflight[h] = key_lock

    with key_lock:
        # Re-check under the per-key lock: the winner of the race may have
        # filled the cache while we were waiting, so we skip the backend call.
        with _whoami_cache_lock:
            hit = _whoami_cache.get(h)
            if hit is not None and time.monotonic() - hit[0] < _WHOAMI_CACHE_TTL:
                return hit[1]
        try:
            if runtime_token:
                whoami = _flask_get_headers("/v1/users/whoami", {"X-Feedling-Runtime-Token": runtime_token})
            else:
                whoami = _flask_get("/v1/users/whoami", api_key)
            if isinstance(whoami, dict) and whoami.get("user_id"):
                with _whoami_cache_lock:
                    now = time.monotonic()
                    _whoami_cache[h] = (now, whoami)
                    _prune_whoami_cache(now)  # evict expired so token rotation can't leak
            return whoami
        finally:
            # Retire this in-flight lock so the registry stays bounded and a
            # later miss starts a fresh round; guarded so we never drop a lock
            # another key generation already replaced.
            with _whoami_cache_lock:
                if _whoami_inflight.get(h) is key_lock:
                    _whoami_inflight.pop(h, None)


def _build_aead_aad(owner_user_id: str, v: int, item_id: str) -> bytes:
    """Per docs/DESIGN_E2E.md §3.4, content's AEAD additional-data must
    authenticate (owner_user_id, v, item_id). The enclave recomputes this
    from the plaintext metadata the server claims + the user_id it
    resolved the api_key to — mismatch → AEAD verification fails →
    cross-user ciphertext substitution attack is detected."""
    payload = f"{owner_user_id}|{v}|{item_id}".encode("utf-8")
    return payload


class DecryptFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_BOX_SEAL_INFO = b"feedling-box-seal-v1"


def _box_seal_open_hkdf(blob: bytes, recipient_sk_bytes: bytes) -> bytes:
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
                  info=_BOX_SEAL_INFO).derive(shared)

    recipient_pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    nonce = hashlib.sha256(ek_pub + recipient_pub).digest()[:12]

    try:
        return ChaCha20Poly1305(k_wrap).decrypt(nonce, ct_plus_tag, None)
    except InvalidTag as e:
        raise DecryptFailure(f"box_seal tag invalid: {e}")


def _decrypt_envelope(env: dict, authorized_user_id: str, content_sk: nacl.public.PrivateKey) -> bytes:
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
    # Use the iOS-compatible HKDF+ChaCha scheme (see _box_seal_open_hkdf).
    K = _box_seal_open_hkdf(k_enclave_sealed, bytes(content_sk))

    if len(K) != 32:
        raise DecryptFailure(f"unexpected K length: {len(K)}")

    # 2. AEAD-decrypt body_ct with K + nonce, aad = owner||v||id
    # We use IETF ChaCha20-Poly1305 (12-byte nonce) because it's the AEAD
    # Apple's CryptoKit supports natively on iOS — no extra SPM dep needed.
    # See docs/DESIGN_E2E.md §3.1.
    v = int(env.get("v", 1))
    item_id = env.get("id", "")
    aad = _build_aead_aad(env["owner_user_id"], v, item_id)
    if len(nonce) != 12:
        raise DecryptFailure(f"expected 12-byte nonce, got {len(nonce)}")
    try:
        plaintext = nacl.bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
            body_ct, aad, nonce, K
        )
    except nacl.exceptions.CryptoError as e:
        # AEAD failure — either tampering, wrong aad, or wrong K
        raise DecryptFailure(f"AEAD verify: {e}")

    return plaintext


def _parse_iso_calendar_date(value: str) -> _dt.date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        norm = raw.replace("Z", "+00:00")
        if "T" not in norm:
            norm = norm + "T00:00:00"
        return _dt.datetime.fromisoformat(norm).date()
    except Exception:
        return None


@app.route("/v1/envelope/decrypt", methods=["POST"])
def v1_envelope_decrypt():
    """Decrypt one caller-owned v1 envelope.

    This is intentionally narrow: callers authenticate with the normal
    Feedling API key, the enclave resolves the authorized user through Flask,
    and `_decrypt_envelope` enforces owner_user_id == authorized_user_id before
    opening the body. Flask uses this for Model API provider-key unwrapping in
    the IO-hosted runtime; raw plaintext is returned only to the authenticated
    internal caller and should never be logged.
    """
    if not _state["ready"]:
        return jsonify({"error": "not_ready", "detail": _state["error"]}), 503

    api_key = _extract_api_key()
    runtime_token = _caller_runtime_token()
    if not api_key and not runtime_token:
        return jsonify({"error": "missing_api_key"}), 401

    try:
        # Deliberately uncached: this endpoint decrypts a caller-SUPPLIED
        # envelope, and the whoami result is the only authorization gate. A
        # stale cache entry would keep unwrapping for a key that was just
        # revoked / a reset account for up to the TTL, so we resolve the caller
        # live every time. The decrypt-and-serve routes below only read the
        # user's own stored data, so they can tolerate the cached resolver.
        # The caller may present a runtime token instead of the api_key (Stage
        # D); the backend whoami resolves either — the enclave just forwards it.
        # The api-key path stays on _flask_get (unchanged); only a token request
        # takes the header-forwarding path.
        if runtime_token:
            whoami = _flask_get_headers("/v1/users/whoami", _forward_auth_headers(api_key, runtime_token))
        else:
            whoami = _flask_get("/v1/users/whoami", api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        return jsonify({"error": f"backend_error: {e}"}), 502

    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return jsonify({"error": "cannot_resolve_user_id"}), 401

    payload = request.get_json(silent=True) or {}
    env = payload.get("envelope")
    if not isinstance(env, dict):
        return jsonify({"error": "envelope required"}), 400

    try:
        content_sk = _get_or_derive_content_sk()
    except Exception as e:
        # The only runtime dstack round-trip. A socket hiccup deriving the
        # content key is a transient infra failure, not an enclave bug — return
        # a retryable 503 rather than a bare 500 the consumer can't interpret.
        return jsonify({"error": f"key_derivation_unavailable: {e}"}), 503
    try:
        plaintext = _decrypt_envelope(env, authorized_user_id, content_sk)
    except DecryptFailure as e:
        return jsonify({"error": f"decrypt_failed: {e.reason}"}), 403

    return jsonify({
        "owner_user_id": authorized_user_id,
        "id": env.get("id", ""),
        "v": int(env.get("v", 1)),
        "plaintext_b64": base64.b64encode(plaintext).decode("ascii"),
    })


# ---------------------------------------------------------------------------
# Agent-facing decrypt-and-serve handlers.
#
# These live at the SAME path as Flask's versions (/v1/chat/history,
# /v1/memory/list, /v1/identity/get) — Flask and the enclave are different
# services at different origins. Flask returns opaque envelopes; the enclave
# returns decrypted plaintext. Agents talk to the enclave; iOS + other
# internals talk to Flask. No "v2 API" — just a different service at the
# same path.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# context_memories — attached to every /v1/chat/history response.
# Selection logic is pure (no nacl / no Flask); lives in
# context_memory_selection.py so it can be unit-tested without this
# module's heavy native deps.
# ---------------------------------------------------------------------------

from context_memory_selection import (  # noqa: E402
    select_context_memories,
    select_context_memories_with_trace,
)
from memory_index_selector import select_memory_index_items  # noqa: E402


def _load_decrypted_moments(
    api_key: str,
    authorized_user_id: str,
    content_sk,
    limit: int = 200,
) -> list[dict]:
    """Fetch memory list from Flask, decrypt in-enclave, return plaintext
    dicts. Failures (local_only, decrypt errors) are silently dropped —
    context_memories is best-effort, never the source of error responses.
    """
    try:
        listing = _flask_get(
            "/v1/memory/list", api_key, params={"limit": str(limit)}
        )
    except httpx.HTTPError:
        return []
    out: list[dict] = []
    for m in listing.get("moments", []) or []:
        if m.get("visibility") == "local_only":
            continue  # enclave doesn't have K_enclave for these
        try:
            plaintext = _decrypt_envelope(m, authorized_user_id, content_sk)
            inner = json.loads(plaintext.decode("utf-8"))
        except (DecryptFailure, json.JSONDecodeError):
            continue
        out.append({
            "id": m.get("id"),
            "title": inner.get("title"),
            "description": inner.get("description"),
            "type": inner.get("type"),
            "source": m.get("source"),
            "occurred_at": m.get("occurred_at"),
            "created_at": m.get("created_at"),
            "her_quote": inner.get("her_quote"),
            "context": inner.get("context"),
            "linked_dimension": inner.get("linked_dimension"),
        })
    return out


def _env_flag_enabled(name: str, default: str = "false") -> bool:
    return str(os.environ.get(name, default) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _memory_readside_for_model_api_enabled() -> bool:
    return _env_flag_enabled("MEMORY_READSIDE_FOR_MODEL_API")


def _memory_readside_model_api_limit() -> int:
    raw = os.environ.get("MEMORY_READSIDE_MODEL_API_LIMIT", "50")
    try:
        value = int(str(raw or "50").strip())
    except (TypeError, ValueError):
        value = 50
    return max(1, min(value, 200))


def _memory_readside_hard_max() -> int:
    raw = os.environ.get("FEEDLING_MEMORY_READSIDE_HARD_MAX", "1000")
    try:
        value = int(str(raw or "1000").strip())
    except (TypeError, ValueError):
        value = 1000
    return max(1, value)


def _memory_readside_effective_limit(raw_limit=None) -> int:
    """Mirror backend readside limit semantics inside the enclave.

    FEEDLING_MEMORY_READSIDE_LIMIT controls index/fetch candidate windows:
    - unset: 50
    - positive integer: that many candidates, capped by HARD_MAX
    - 0: "full window", still capped by FEEDLING_MEMORY_READSIDE_HARD_MAX

    This is separate from MEMORY_READSIDE_MODEL_API_LIMIT, which belongs to the
    older route-B auto-recall path. Keep both knobs distinct.
    """
    if raw_limit is None or str(raw_limit).strip() == "":
        raw_limit = os.environ.get("FEEDLING_MEMORY_READSIDE_LIMIT", "50")
    try:
        requested = int(str(raw_limit).strip())
    except (TypeError, ValueError):
        requested = 50
    if requested < 0:
        requested = 50
    hard_max = _memory_readside_hard_max()
    if requested == 0:
        return hard_max
    return max(1, min(requested, hard_max))


def _context_moment_to_index_item(moment: dict) -> dict:
    """Convert the existing plaintext context card into a readside index item.

    Route B still decrypts in-enclave, but selection now goes through the same
    index selector used by readside/MCP. This avoids the backend top-50 prefilter
    while unifying the matching pipe.
    """

    title = _memory_readside_text(moment.get("title"), 500)
    description = _memory_readside_text(moment.get("description"), 500)
    linked = _memory_readside_text(moment.get("linked_dimension"), 160)
    context = _memory_readside_text(moment.get("context"), 240)
    summary = description or title or context
    bucket_refs = [item for item in (linked, _memory_readside_text(moment.get("type"), 40)) if item]
    return {
        "id": _memory_readside_text(moment.get("id"), 120),
        "summary": summary,
        "bucket_refs": bucket_refs,
        "status": "active",
        "salience": "medium",
        "is_open_thread": False,
        "is_sensitive": False,
        "score": 0,
        "occurred_at": _memory_readside_text(moment.get("occurred_at"), 80),
        "created_at": _memory_readside_text(moment.get("created_at"), 80),
    }


def _select_context_memories_via_readside(
    moments: list[dict],
    latest_user_text: str,
    *,
    cap: int = 8,
) -> tuple[list[dict], dict]:
    """Route B readside pipe: plaintext cards -> safe index -> ids -> cards."""

    if not moments:
        return [], {
            "mode": "model_api_readside_v1",
            "readside_enabled": True,
            "selected": [],
            "rejected_sample": [],
            "index_count": 0,
        }
    by_id = {str(moment.get("id") or ""): moment for moment in moments if str(moment.get("id") or "")}
    index_items = [
        item for item in (_context_moment_to_index_item(moment) for moment in moments)
        if item.get("id") and item.get("summary")
    ]
    selection = select_memory_index_items(
        latest_user_text,
        index_items,
        cap=cap,
        include_sensitive=False,
    )
    selected_ids = [memory_id for memory_id in selection.get("selected_ids", []) if memory_id in by_id]
    context_memories = [dict(by_id[memory_id]) for memory_id in selected_ids[:cap]]
    selector_trace = selection.get("trace") if isinstance(selection.get("trace"), dict) else {}
    selected_trace = selector_trace.get("selected") if isinstance(selector_trace.get("selected"), list) else []
    skipped = selector_trace.get("skipped_sample") if isinstance(selector_trace.get("skipped_sample"), list) else []
    trace = {
        "mode": "model_api_readside_v1",
        "readside_enabled": True,
        "index_count": len(index_items),
        "selected": [
            {
                "id": item.get("id", ""),
                "title": _memory_readside_text(by_id.get(str(item.get("id") or ""), {}).get("title"), 160),
                "type": _memory_readside_text(by_id.get(str(item.get("id") or ""), {}).get("type"), 40),
                "score": float(item.get("score") or 0.0),
                "confidence": _memory_readside_text(item.get("confidence"), 40),
                "matched_units": list(item.get("matched_units") or [])[:8],
                "matched_phrases": list(item.get("matched_phrases") or [])[:6],
                "reason": _memory_readside_text(item.get("reason"), 120),
                "bucket": "readside",
                "selected": True,
            }
            for item in selected_trace[:cap]
        ],
        "rejected_sample": [
            {
                "id": item.get("id", ""),
                "title": _memory_readside_text(by_id.get(str(item.get("id") or ""), {}).get("title"), 160),
                "type": _memory_readside_text(by_id.get(str(item.get("id") or ""), {}).get("type"), 40),
                "score": float(item.get("score") or 0.0),
                "confidence": _memory_readside_text(item.get("confidence"), 40),
                "matched_units": list(item.get("matched_units") or [])[:8],
                "matched_phrases": list(item.get("matched_phrases") or [])[:6],
                "reason": _memory_readside_text(item.get("reason"), 120),
                "bucket": "rejected",
                "selected": False,
            }
            for item in skipped[:8]
        ],
        "selector_trace": selector_trace,
    }
    for key in ("query_units", "query_strong_phrases", "query_rare_terms", "query_weak_terms"):
        value = selector_trace.get(key)
        if isinstance(value, list):
            trace[key] = value
    return context_memories, trace


def _memory_readside_text(value, max_chars: int = 2000) -> str:
    return str(value or "").strip()[:max_chars]


def _memory_readside_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip()[:160] for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:160]]
    return []


def _memory_readside_summary(inner: dict) -> str:
    for key in ("summary", "description", "title"):
        text = _memory_readside_text(inner.get(key), 500)
        if text:
            return text
    return ""


def _memory_default_bucket(value) -> str:
    mem_type = str(value or "").strip().lower()
    if mem_type in {"moment", "quote"}:
        return "我们的关系"
    if mem_type in {"fact", "event"}:
        return "未分类"
    return "未分类"


def _memory_inner_to_v1(inner: dict, envelope: dict | None = None) -> dict:
    """Adapt decrypted legacy memory body into the v1 inner shape."""
    envelope = envelope or {}
    if not isinstance(inner, dict):
        return {"summary": "", "content": "", "bucket": "未分类", "threads": []}
    if all(key in inner for key in ("summary", "content", "bucket", "threads")):
        return {
            "summary": _memory_readside_text(inner.get("summary"), 500),
            "content": _memory_readside_text(inner.get("content"), 5000),
            "bucket": _memory_readside_text(inner.get("bucket"), 80) or "未分类",
            "threads": _memory_readside_list(inner.get("threads"))[:8],
            **{
                key: inner[key]
                for key in ("is_sensitive", "sensitivity_class", "sensitive_scope")
                if key in inner
            },
        }

    summary = _memory_readside_summary(inner)
    description = _memory_readside_text(inner.get("description") or inner.get("title") or summary, 2000)
    quote = _memory_readside_text(inner.get("her_quote") or inner.get("verbatim") or inner.get("context"), 1000)
    follow_up = _memory_readside_text(inner.get("follow_up"), 1000)
    content = "\n".join([
        f"记忆: {description or summary}",
        f"上下文: {quote or '用户在对话中明确提到。'}",
        f"使用提示: {follow_up or '自然使用这条记忆，不要机械复述。'}",
    ])
    threads = _memory_readside_list(inner.get("threads"))
    if not threads:
        threads = _memory_readside_list(inner.get("linked_dimension"))
    if not threads:
        threads = _memory_readside_list(inner.get("anchor_memory_ids"))
    adapted = {
        "summary": summary,
        "content": content,
        "bucket": _memory_readside_text(inner.get("bucket"), 80)
        or _memory_default_bucket(inner.get("type") or envelope.get("type")),
        "threads": threads[:8],
    }
    for key in ("is_sensitive", "sensitivity_class", "sensitive_scope"):
        if key in inner:
            adapted[key] = inner[key]
    return adapted


def _memory_readside_bucket_refs(inner: dict) -> list[str]:
    refs = _memory_readside_list(inner.get("bucket_refs"))
    if refs:
        return refs
    refs = _memory_readside_list(inner.get("bucket_ids"))
    if refs:
        return refs
    linked = _memory_readside_text(inner.get("linked_dimension"), 160)
    return [linked] if linked else []


def _memory_readside_salience(envelope: dict, inner: dict) -> str:
    salience = str(envelope.get("salience") or inner.get("salience") or "medium").strip().lower()
    return salience if salience in {"critical", "high", "medium", "low"} else "medium"


def _memory_readside_status(envelope: dict, inner: dict) -> str:
    return str(envelope.get("status") or inner.get("status") or "active").strip().lower() or "active"


def _memory_readside_is_sensitive(envelope: dict, inner: dict) -> bool:
    for key in ("is_sensitive", "sensitivity_class", "sensitive_scope"):
        value = inner.get(key)
        if value:
            return True if key != "is_sensitive" else bool(value)
    for key in ("is_sensitive", "sensitivity_class"):
        value = envelope.get(key)
        if value:
            return True if key != "is_sensitive" else bool(value)
    return False


def _build_memory_index_item(envelope: dict, inner: dict) -> dict:
    adapted = _memory_inner_to_v1(inner, envelope)
    return {
        "id": envelope.get("id", ""),
        "summary": adapted.get("summary", ""),
        "bucket": adapted.get("bucket", ""),
        "threads": list(adapted.get("threads") or [])[:8],
        "importance": float(envelope.get("importance") or 0.5),
        "pulse": float(envelope.get("pulse") or 0.3),
        "status": _memory_readside_status(envelope, inner),
        "occurred_at": _memory_readside_text(envelope.get("occurred_at"), 80),
        "last_referenced_at": _memory_readside_text(envelope.get("last_referenced_at"), 80),
        "is_sensitive": _memory_readside_is_sensitive(envelope, adapted),
        "score": float(envelope.get("score") or 0),
    }


def _build_memory_fetch_item(envelope: dict, inner: dict) -> dict:
    adapted = _memory_inner_to_v1(inner, envelope)
    return {
        "id": envelope.get("id", ""),
        "summary": adapted.get("summary", ""),
        "content": adapted.get("content", ""),
        "bucket": adapted.get("bucket", ""),
        "threads": list(adapted.get("threads") or [])[:8],
        "importance": float(envelope.get("importance") or 0.5),
        "pulse": float(envelope.get("pulse") or 0.3),
        "status": _memory_readside_status(envelope, inner),
        "source": _memory_readside_text(envelope.get("source"), 160),
        "occurred_at": _memory_readside_text(envelope.get("occurred_at"), 80),
        "last_referenced_at": _memory_readside_text(envelope.get("last_referenced_at"), 80),
        "is_sensitive": _memory_readside_is_sensitive(envelope, adapted),
    }


def _memory_index_filter_items(items: list[dict], payload: dict) -> list[dict]:
    bucket = _memory_readside_text(payload.get("bucket"), 120)
    thread = _memory_readside_text(payload.get("thread"), 120)
    filtered = []
    for item in items:
        if bucket and item.get("bucket") != bucket:
            continue
        if thread and thread not in (item.get("threads") or []):
            continue
        filtered.append(item)
    return filtered


def _memory_readside_auth_context() -> tuple[str | None, str | None, object | None, tuple[dict, int] | None]:
    if not _state["ready"]:
        return None, None, None, ({"error": "not_ready", "detail": _state["error"]}, 503)
    api_key = _extract_api_key()
    if not api_key and not _caller_runtime_token():
        return None, None, None, ({"error": "missing api_key"}, 401)
    try:
        whoami = _whoami_cached(api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return None, None, None, ({"error": "unauthorized"}, 401)
        return None, None, None, ({"error": f"backend_error: {e}"}, 502)
    except httpx.HTTPError as e:
        return None, None, None, ({"error": f"backend_unreachable: {e}"}, 502)
    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return None, None, None, ({"error": "cannot resolve user_id"}, 401)
    try:
        content_sk = _get_or_derive_content_sk()
    except Exception as e:
        return None, None, None, ({"error": f"key_derivation_unavailable: {e}"}, 503)
    return api_key, authorized_user_id, content_sk, None


def _memory_readside_decrypt_items(
    moments: list,
    authorized_user_id: str,
    content_sk,
    *,
    item_builder,
) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    unavailable_ids: list[str] = []
    for moment in moments:
        if not isinstance(moment, dict):
            continue
        memory_id = str(moment.get("id") or "")
        if moment.get("visibility") == "local_only" or not moment.get("K_enclave"):
            if memory_id:
                unavailable_ids.append(memory_id)
            continue
        try:
            plaintext = _decrypt_envelope(moment, authorized_user_id, content_sk)
            inner = json.loads(plaintext.decode("utf-8"))
            if not isinstance(inner, dict):
                raise ValueError("memory plaintext is not an object")
        except (DecryptFailure, json.JSONDecodeError, ValueError):
            if memory_id:
                unavailable_ids.append(memory_id)
            continue
        items.append(item_builder(moment, inner))
    return items, unavailable_ids


@app.route("/v1/memory/index", methods=["POST"])
def v1_memory_index():
    _api_key, authorized_user_id, content_sk, error = _memory_readside_auth_context()
    if error is not None:
        body, status = error
        return jsonify(body), status
    payload = request.get_json(silent=True) or {}
    moments = payload.get("moments")
    if not isinstance(moments, list):
        return jsonify({"error": "moments must be a list"}), 400
    effective_limit = _memory_readside_effective_limit(payload.get("limit"))
    items, unavailable_ids = _memory_readside_decrypt_items(
        moments[:effective_limit],
        authorized_user_id or "",
        content_sk,
        item_builder=_build_memory_index_item,
    )
    items = _memory_index_filter_items(items, payload)
    if not bool(payload.get("include_sensitive", False)):
        items = [item for item in items if not item.get("is_sensitive")]
    return jsonify({
        "user_id": authorized_user_id,
        "items": items,
        "unavailable_ids": unavailable_ids,
    })


@app.route("/v1/memory/fetch", methods=["POST"])
def v1_memory_fetch():
    _api_key, authorized_user_id, content_sk, error = _memory_readside_auth_context()
    if error is not None:
        body, status = error
        return jsonify(body), status
    payload = request.get_json(silent=True) or {}
    moments = payload.get("moments")
    if not isinstance(moments, list):
        return jsonify({"error": "moments must be a list"}), 400
    effective_limit = _memory_readside_effective_limit(payload.get("limit"))
    items, unavailable_ids = _memory_readside_decrypt_items(
        moments[:effective_limit],
        authorized_user_id or "",
        content_sk,
        item_builder=_build_memory_fetch_item,
    )
    blocked_sensitive_ids: list[str] = []
    if not bool(payload.get("include_sensitive", False)):
        allowed = []
        for item in items:
            if item.get("is_sensitive"):
                blocked_sensitive_ids.append(str(item.get("id") or ""))
            else:
                allowed.append(item)
        items = allowed
    return jsonify({
        "user_id": authorized_user_id,
        "items": items,
        "unavailable_ids": unavailable_ids,
        "blocked_sensitive_ids": [mid for mid in blocked_sensitive_ids if mid],
    })


@app.route("/v1/chat/history", methods=["GET"])
def v1_chat_history():
    """Decrypt-and-serve chat history for the authenticated user.

    Query params:
      since (float, default 0): only return messages with ts > since
      limit (int,   default 200, max 200)

    The caller's api_key determines whose content gets decrypted. Items
    with visibility=local_only come back as placeholders (content = null)
    — the enclave doesn't have K_enclave for them and the agent never will.
    """
    if not _state["ready"]:
        return jsonify({"error": "not_ready", "detail": _state["error"]}), 503

    api_key = _extract_api_key()
    if not api_key and not _caller_runtime_token():
        # Stage D: decrypt-and-serve routes accept a runtime token too; identity
        # is then resolved from it by the token-aware _whoami_cached below.
        return jsonify({"error": "missing api_key"}), 401

    # Resolve whose content we're decrypting — returns the caller's usr_...
    # from the backend's per-user HMAC-peppered api_key lookup.
    try:
        whoami = _whoami_cached(api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        # Connect/timeout reaching the backend whoami (NOT an HTTP status) —
        # e.g. the reentrant whoami round-trip stalling under load. Map to a
        # retryable 502 instead of letting it bubble into a bare Flask 500.
        return jsonify({"error": f"backend_unreachable: {e}"}), 502
    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return jsonify({"error": "cannot resolve user_id"}), 401

    # Fetch the raw history (always v1 envelopes post-strip).
    since = request.args.get("since", "0")
    limit = request.args.get("limit", "200")
    try:
        hist = _flask_get(
            "/v1/chat/history",
            api_key,
            params={"since": since, "limit": limit},
        )
    except httpx.HTTPStatusError as e:
        # whoami may have been cached, so a key revoked since then surfaces here;
        # keep it a 401, not a generic 502.
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        return jsonify({"error": f"backend_error: {e}"}), 502

    # Reconstruct content_sk here — we cached only the pubkey on boot, the
    # privkey is always in-memory under _state but we didn't store it.
    # Fix: also cache the sk. For now, re-derive once on first call.
    try:
        content_sk = _get_or_derive_content_sk()
    except Exception as e:
        # The only runtime dstack round-trip. A socket hiccup deriving the
        # content key is a transient infra failure, not an enclave bug — return
        # a retryable 503 rather than a bare 500 the consumer can't interpret.
        return jsonify({"error": f"key_derivation_unavailable: {e}"}), 503

    decrypted = []
    errors = []
    for m in hist.get("messages", []):
        v = int(m.get("v", 0))
        # Default to "text" for legacy messages stored before the
        # content_type field was added.
        ctype = m.get("content_type", "text")
        # v1+ envelope (v0 plaintext paths were stripped post-migration).
        if m.get("visibility") == "local_only":
            decrypted.append({
                "id": m["id"],
                "role": m["role"],
                "ts": m["ts"],
                "source": m.get("source"),
                "content": None,
                "content_type": ctype,
                "v": v,
                "visibility": "local_only",
                "decrypt_status": "local_only_agent_cannot_read",
            })
            continue

        try:
            plaintext = _decrypt_envelope(m, authorized_user_id, content_sk)
            entry: dict = {
                "id": m["id"],
                "role": m["role"],
                "ts": m["ts"],
                "source": m.get("source"),
                "content_type": ctype,
                "v": v,
                "visibility": m.get("visibility", "shared"),
                "decrypt_status": "ok",
            }
            if ctype == "image":
                # Image plaintext is raw JPEG bytes — surface as base64 so
                # JSON callers (vision-capable agents, iOS clients with
                # local copies) can decode and render. `content` left empty.
                entry["content"] = ""
                entry["image_b64"] = base64.b64encode(plaintext).decode("ascii")
            else:
                entry["content"] = plaintext.decode("utf-8", errors="replace")
            decrypted.append(entry)
        except DecryptFailure as e:
            # Surface the failure per-item so the agent sees partial
            # progress rather than a blanket 500 on one bad blob.
            errors.append({"id": m.get("id"), "reason": e.reason})
            decrypted.append({
                "id": m["id"],
                "role": m["role"],
                "ts": m["ts"],
                "content": None,
                "content_type": ctype,
                "v": v,
                "decrypt_status": f"error: {e.reason}",
            })

    # Attach context_memories — up to 8 plaintext memory cards selected
    # for this conversation moment. Best-effort: if anything fails, return
    # the chat response without them rather than 500-ing.
    context_memories: list[dict] = []
    context_memory_trace: dict | None = None
    try:
        latest_user_text = ""
        for m in reversed(decrypted):
            if m.get("role") == "user" and m.get("content"):
                latest_user_text = m["content"]
                break
        context_mode = str(
            request.args.get("context_mode")
            or request.args.get("contextMode")
            or ""
        ).strip()
        if not context_mode and str(request.args.get("context_strict") or "").lower() in {"1", "true", "yes", "on"}:
            context_mode = "strict"
        want_trace = str(request.args.get("context_trace") or "").lower() in {"1", "true", "yes", "on"}
        use_readside = context_mode == "model_api" and _memory_readside_for_model_api_enabled()
        memory_limit = _memory_readside_model_api_limit() if use_readside else 200
        moments = _load_decrypted_moments(api_key, authorized_user_id, content_sk, limit=memory_limit)
        if use_readside:
            context_memories, context_memory_trace = _select_context_memories_via_readside(
                moments,
                latest_user_text,
                cap=8,
            )
            if not want_trace:
                context_memory_trace = None
        elif want_trace:
            context_memories, context_memory_trace = select_context_memories_with_trace(
                moments,
                latest_user_text,
                mode=context_mode,
            )
        else:
            context_memories = select_context_memories(moments, latest_user_text, mode=context_mode)
    except Exception as e:
        print(f"[chat/history:{authorized_user_id}] context_memories failed: {e}")

    payload = {
        "user_id": authorized_user_id,
        "messages": decrypted,
        "context_memories": context_memories,
        "total": hist.get("total", len(decrypted)),
        "decrypt_errors": errors,
    }
    if context_memory_trace is not None:
        payload["context_memory_trace"] = context_memory_trace
    return jsonify(payload)


@app.route("/v1/memory/list", methods=["GET"])
def v1_memory_list():
    """Decrypt-and-serve memory garden for the authenticated user.

    Query params:
      since (ISO string, optional): pass-through to /v1/memory/list
      limit (int, default 50, max 200)
    """
    if not _state["ready"]:
        return jsonify({"error": "not_ready", "detail": _state["error"]}), 503
    api_key = _extract_api_key()
    if not api_key and not _caller_runtime_token():
        # Stage D: decrypt-and-serve routes accept a runtime token too; identity
        # is then resolved from it by the token-aware _whoami_cached below.
        return jsonify({"error": "missing api_key"}), 401

    try:
        whoami = _whoami_cached(api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        # Connect/timeout reaching the backend whoami (NOT an HTTP status) —
        # e.g. the reentrant whoami round-trip stalling under load. Map to a
        # retryable 502 instead of letting it bubble into a bare Flask 500.
        return jsonify({"error": f"backend_unreachable: {e}"}), 502
    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return jsonify({"error": "cannot resolve user_id"}), 401

    limit = request.args.get("limit", "50")
    since = request.args.get("since", "")
    params = {"limit": limit}
    if since:
        params["since"] = since
    try:
        listing = _flask_get("/v1/memory/list", api_key, params=params)
    except httpx.HTTPStatusError as e:
        # whoami may have been cached, so a key revoked since then surfaces here;
        # keep it a 401, not a generic 502.
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        return jsonify({"error": f"backend_error: {e}"}), 502

    try:
        content_sk = _get_or_derive_content_sk()
    except Exception as e:
        # The only runtime dstack round-trip. A socket hiccup deriving the
        # content key is a transient infra failure, not an enclave bug — return
        # a retryable 503 rather than a bare 500 the consumer can't interpret.
        return jsonify({"error": f"key_derivation_unavailable: {e}"}), 503
    decrypted = []
    errors = []
    for m in listing.get("moments", []):
        v = int(m.get("v", 0))
        base = {
            "id": m["id"],
            "occurred_at": m.get("occurred_at"),
            "created_at": m.get("created_at"),
            "source": m.get("source"),
            "v": v,
        }
        if m.get("visibility") == "local_only":
            base.update({
                "title": None, "description": None, "type": None,
                "visibility": "local_only",
                "decrypt_status": "local_only_agent_cannot_read",
            })
            decrypted.append(base); continue
        try:
            plaintext = _decrypt_envelope(m, authorized_user_id, content_sk)
            inner = json.loads(plaintext.decode("utf-8"))
            base.update({
                "title": inner.get("title"),
                "description": inner.get("description"),
                "type": inner.get("type"),
                "visibility": m.get("visibility", "shared"),
                "decrypt_status": "ok",
            })
        except (DecryptFailure, json.JSONDecodeError) as e:
            reason = e.reason if isinstance(e, DecryptFailure) else f"json: {e}"
            errors.append({"id": m.get("id"), "reason": reason})
            base.update({
                "title": None, "description": None, "type": None,
                "decrypt_status": f"error: {reason}",
            })
        decrypted.append(base)

    return jsonify({
        "user_id": authorized_user_id,
        "moments": decrypted,
        "total": listing.get("total", len(decrypted)),
        "decrypt_errors": errors,
    })


@app.route("/v1/identity/get", methods=["GET"])
def v1_identity_get():
    """Decrypt-and-serve the identity card for the authenticated user.

    Returns the same shape as /v1/identity/get (agent_name, self_introduction,
    dimensions[]), assembled from decrypted ciphertext when stored as v1.
    """
    if not _state["ready"]:
        return jsonify({"error": "not_ready", "detail": _state["error"]}), 503
    api_key = _extract_api_key()
    if not api_key and not _caller_runtime_token():
        # Stage D: decrypt-and-serve routes accept a runtime token too; identity
        # is then resolved from it by the token-aware _whoami_cached below.
        return jsonify({"error": "missing api_key"}), 401

    try:
        whoami = _whoami_cached(api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        # Connect/timeout reaching the backend whoami (NOT an HTTP status) —
        # e.g. the reentrant whoami round-trip stalling under load. Map to a
        # retryable 502 instead of letting it bubble into a bare Flask 500.
        return jsonify({"error": f"backend_unreachable: {e}"}), 502
    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return jsonify({"error": "cannot resolve user_id"}), 401

    try:
        resp = _flask_get("/v1/identity/get", api_key)
    except httpx.HTTPStatusError as e:
        # whoami may have been cached, so a key revoked since then surfaces here;
        # keep it a 401, not a generic 502.
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        return jsonify({"error": f"backend_error: {e}"}), 502

    identity = resp.get("identity")
    if identity is None:
        return jsonify({"identity": None, "user_id": authorized_user_id})

    v = int(identity.get("v", 0))
    base = {
        "v": v,
        "created_at": identity.get("created_at"),
        "updated_at": identity.get("updated_at"),
    }
    if identity.get("visibility") == "local_only":
        base.update({
            "visibility": "local_only",
            "decrypt_status": "local_only_agent_cannot_read",
        })
        return jsonify({"identity": base, "user_id": authorized_user_id})

    try:
        content_sk = _get_or_derive_content_sk()
    except Exception as e:
        # The only runtime dstack round-trip. A socket hiccup deriving the
        # content key is a transient infra failure, not an enclave bug — return
        # a retryable 503 rather than a bare 500 the consumer can't interpret.
        return jsonify({"error": f"key_derivation_unavailable: {e}"}), 503
    try:
        plaintext = _decrypt_envelope(identity, authorized_user_id, content_sk)
        inner = json.loads(plaintext.decode("utf-8"))

        # days_with_user is computed live from the server-side anchor.
        # This makes the count auto-increment daily without the agent ever
        # writing it again (the old envelope-embedded value is ignored).
        # Legacy fallback: if no anchor on file, use the embedded value
        # so users that bootstrapped before this migration still see something.
        anchor = identity.get("relationship_started_at")
        if anchor:
            started = _parse_iso_calendar_date(anchor)
            live_days = (
                max(0, (_dt.datetime.now().date() - started).days)
                if started else inner.get("days_with_user", 0)
            )
        else:
            live_days = inner.get("days_with_user", 0)

        base.update({
            "agent_name": inner.get("agent_name"),
            "self_introduction": inner.get("self_introduction"),
            "dimensions": inner.get("dimensions", []),
            "days_with_user": live_days,
            "category": inner.get("category", ""),
            "signature": inner.get("signature", []),
            "visibility": identity.get("visibility", "shared"),
            "decrypt_status": "ok",
        })
        return jsonify({"identity": base, "user_id": authorized_user_id})
    except (DecryptFailure, json.JSONDecodeError) as e:
        reason = e.reason if isinstance(e, DecryptFailure) else f"json: {e}"
        base.update({"decrypt_status": f"error: {reason}"})
        return jsonify({"identity": base, "user_id": authorized_user_id,
                        "decrypt_errors": [{"reason": reason}]})


@app.route("/v1/screen/frames/<frame_id>/decrypt", methods=["GET"])
def v1_frame_decrypt(frame_id):
    """Decrypt a single v1 screen-frame envelope and return its plaintext.

    The iOS broadcast extension runs VNRecognizeText on each frame and
    packs both the base64 JPEG and the OCR text into one JSON payload
    before sealing it with ChaCha20-Poly1305. The backend never sees the
    plaintext; this route is the only way agents or API clients can
    read either the pixels or the OCR text.

    Query params:
      include_image (bool, default true): omit `image_b64` if false —
        helpful when the caller only wants OCR + metadata and wants to
        avoid pulling ~80-120 KB per frame.
    """
    if not _state["ready"]:
        return jsonify({"error": "not_ready", "detail": _state["error"]}), 503
    if not re.match(r"^[a-f0-9]{16,64}$", frame_id or ""):
        return jsonify({"error": "bad frame id"}), 400

    api_key = _extract_api_key()
    if not api_key and not _caller_runtime_token():
        # Stage D: decrypt-and-serve routes accept a runtime token too; identity
        # is then resolved from it by the token-aware _whoami_cached below.
        return jsonify({"error": "missing api_key"}), 401

    try:
        whoami = _whoami_cached(api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        # Connect/timeout reaching the backend whoami (NOT an HTTP status) —
        # e.g. the reentrant whoami round-trip stalling under load. Map to a
        # retryable 502 instead of letting it bubble into a bare Flask 500.
        return jsonify({"error": f"backend_unreachable: {e}"}), 502
    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return jsonify({"error": "cannot resolve user_id"}), 401

    try:
        env = _flask_get(f"/v1/screen/frames/{frame_id}/envelope", api_key)
    except httpx.HTTPStatusError as e:
        # whoami may have been cached, so a key revoked since then surfaces here;
        # keep it a 401, not a generic 502.
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        if e.response.status_code == 404:
            return jsonify({"error": "frame not found"}), 404
        return jsonify({"error": f"backend_error: {e}"}), 502

    include_image = request.args.get("include_image", "true").lower() != "false"
    try:
        content_sk = _get_or_derive_content_sk()
    except Exception as e:
        # The only runtime dstack round-trip. A socket hiccup deriving the
        # content key is a transient infra failure, not an enclave bug — return
        # a retryable 503 rather than a bare 500 the consumer can't interpret.
        return jsonify({"error": f"key_derivation_unavailable: {e}"}), 503

    try:
        plaintext = _decrypt_envelope(env, authorized_user_id, content_sk)
    except DecryptFailure as e:
        return jsonify({"error": f"decrypt_failed: {e.reason}"}), 502

    try:
        inner = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return jsonify({"error": f"plaintext_parse: {e}"}), 502

    result = {
        "id": frame_id,
        "ts": inner.get("ts") or env.get("ts"),
        "app": inner.get("app"),
        "bundle": inner.get("bundle"),
        "ocr_text": inner.get("ocr_text", ""),
        "urls": inner.get("urls", []),
        "w": inner.get("w", 0),
        "h": inner.get("h", 0),
        "tier_hint": inner.get("tier_hint"),
        "v": int(env.get("v", 1)),
        "owner_user_id": authorized_user_id,
        "decrypt_status": "ok",
    }
    if include_image:
        result["image_b64"] = inner.get("image", "")
        result["image_mime"] = "image/jpeg"
    else:
        result["image_b64"] = None
        result["image_bytes_omitted"] = True
    return jsonify(result)


@app.route("/v1/screen/frames/<frame_id>/caption", methods=["GET"])
def v1_frame_caption(frame_id):
    """Decrypt a frame IN-ENCLAVE and return a VLM caption — never pixels.

    This is the screen.read tool path. Unlike /decrypt, the plaintext image
    never leaves the enclave to the backend caller: only the derived caption
    text crosses back. The image is sent to the VLM (OpenRouter Qwen) from
    inside the enclave, which is the only place plaintext legitimately lives.

    Query params:
      mode (str, default "caption"): "caption" returns a 1-2 sentence
        description; "full" additionally echoes back the on-device OCR text.
    """
    if not _state["ready"]:
        return jsonify({"error": "not_ready", "detail": _state["error"]}), 503
    if not re.match(r"^[a-f0-9]{16,64}$", frame_id or ""):
        return jsonify({"error": "bad frame id"}), 400
    # Read live so tests / rotated secrets take effect without a restart.
    # VLM key: deliberately NO fallback to the SCREEN_VLM_API_KEY module constant
    # (fail-closed: an unset key must read as empty so the route returns
    # screen_caption_unconfigured). model/base_url may fall back to their defaults.
    vlm_key = os.environ.get("FEEDLING_SCREEN_VLM_API_KEY", "")
    if not vlm_key:
        return jsonify({"error": "screen_caption_unconfigured"}), 503

    api_key = _extract_api_key()
    if not api_key and not _caller_runtime_token():
        # Stage D: decrypt-and-serve routes accept a runtime token too; identity
        # is then resolved from it by the token-aware _whoami_cached below.
        return jsonify({"error": "missing api_key"}), 401
    try:
        whoami = _whoami_cached(api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        return jsonify({"error": f"backend_unreachable: {e}"}), 502
    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return jsonify({"error": "cannot resolve user_id"}), 401

    try:
        env = _flask_get(f"/v1/screen/frames/{frame_id}/envelope", api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        if e.response.status_code == 404:
            return jsonify({"error": "frame not found"}), 404
        return jsonify({"error": f"backend_error: {e}"}), 502
    try:
        content_sk = _get_or_derive_content_sk()
    except Exception as e:
        return jsonify({"error": f"key_derivation_unavailable: {e}"}), 503
    try:
        plaintext = _decrypt_envelope(env, authorized_user_id, content_sk)
        inner = json.loads(plaintext.decode("utf-8"))
    except DecryptFailure as e:
        return jsonify({"error": f"decrypt_failed: {e.reason}"}), 502
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return jsonify({"error": f"plaintext_parse: {e}"}), 502

    image_b64 = inner.get("image", "")
    ocr_text = str(inner.get("ocr_text") or "")
    mode = (request.args.get("mode") or "caption").lower()
    full = mode == "full"

    instruction = (
        "Describe what is on this phone screen in one or two sentences: what app "
        "or content it is, and what the user appears to be doing. Be concrete and "
        "neutral."
        + (" Then list the key visible text verbatim." if full else "")
    )
    user_content = [
        {"type": "text", "text": instruction},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
    ]
    if ocr_text:
        user_content.append(
            {"type": "text", "text": f"On-device OCR text:\n{ocr_text[:2000]}"}
        )

    try:
        model = os.environ.get("FEEDLING_SCREEN_VLM_MODEL", SCREEN_VLM_MODEL)
        base_url = os.environ.get("FEEDLING_SCREEN_VLM_BASE_URL", SCREEN_VLM_BASE_URL)
        result = provider_client.chat_completion(
            provider_client.ProviderConfig(
                provider="openrouter", model=model, api_key=vlm_key, base_url=base_url,
            ),
            [{"role": "user", "content": user_content}],
            max_tokens=400 if full else 160,
            temperature=0.2,
            timeout=45.0,
        )
    except provider_client.ProviderError as e:
        return jsonify({"error": f"screen_caption_failed: {e}"}), 502
    except httpx.HTTPError as e:
        return jsonify({"error": f"screen_caption_failed: {e}"}), 502

    out = {
        "frame_id": frame_id,
        "caption": str(result.get("reply") or "").strip(),
        "model": model,
        "decrypt_status": "ok",
    }
    if full:
        out["ocr_text"] = ocr_text[:4000]
    return jsonify(out)


@app.route("/v1/screen/frames/<frame_id>/image", methods=["GET"])
def v1_frame_image(frame_id):
    """Decrypt a v1 screen-frame envelope and return the raw JPEG bytes.

    Binary sibling of /decrypt. Returns Content-Type image/jpeg and
    supports HTTP Range requests, which lets a client fetch the image
    in N parallel chunks. dstack-gateway throttles each TCP connection
    to ~1 Mbps, so a 4-way parallel Range fetch on a ~175 KB JPEG can
    complete in ~1s rather than ~3-4s on a single stream.

    Why a separate endpoint rather than reusing /decrypt:
      - /decrypt returns JSON with base64-encoded image inside. Range
        on that is awkward (base64 boundaries, JSON framing).
      - Raw bytes with Range gives us 33% savings over base64 AND
        trivial multi-stream support.
      - Future server-side OCR still runs on these same bytes inside
        the enclave — this endpoint just exposes them to agents that
        want to view the pixels themselves.

    Metadata (OCR text, app, timestamp, dimensions) remains on
    /decrypt?include_image=false — callers wanting both should hit
    both endpoints; they can be fetched in parallel.
    """
    if not _state["ready"]:
        return jsonify({"error": "not_ready", "detail": _state["error"]}), 503
    if not re.match(r"^[a-f0-9]{16,64}$", frame_id or ""):
        return jsonify({"error": "bad frame id"}), 400

    api_key = _extract_api_key()
    if not api_key and not _caller_runtime_token():
        # Stage D: decrypt-and-serve routes accept a runtime token too; identity
        # is then resolved from it by the token-aware _whoami_cached below.
        return jsonify({"error": "missing api_key"}), 401

    try:
        whoami = _whoami_cached(api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"error": f"backend_error: {e}"}), 502
    except httpx.HTTPError as e:
        # Connect/timeout reaching the backend whoami (NOT an HTTP status) —
        # e.g. the reentrant whoami round-trip stalling under load. Map to a
        # retryable 502 instead of letting it bubble into a bare Flask 500.
        return jsonify({"error": f"backend_unreachable: {e}"}), 502
    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return jsonify({"error": "cannot resolve user_id"}), 401

    try:
        env = _flask_get(f"/v1/screen/frames/{frame_id}/envelope", api_key)
    except httpx.HTTPStatusError as e:
        # whoami may have been cached, so a key revoked since then surfaces here;
        # keep it a 401, not a generic 502.
        if e.response.status_code == 401:
            return jsonify({"error": "unauthorized"}), 401
        if e.response.status_code == 404:
            return jsonify({"error": "frame not found"}), 404
        return jsonify({"error": f"backend_error: {e}"}), 502

    try:
        content_sk = _get_or_derive_content_sk()
    except Exception as e:
        # The only runtime dstack round-trip. A socket hiccup deriving the
        # content key is a transient infra failure, not an enclave bug — return
        # a retryable 503 rather than a bare 500 the consumer can't interpret.
        return jsonify({"error": f"key_derivation_unavailable: {e}"}), 503
    try:
        plaintext = _decrypt_envelope(env, authorized_user_id, content_sk)
    except DecryptFailure as e:
        return jsonify({"error": f"decrypt_failed: {e.reason}"}), 502

    try:
        inner = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return jsonify({"error": f"plaintext_parse: {e}"}), 502

    image_b64 = inner.get("image", "")
    if not image_b64:
        return jsonify({"error": "no image in plaintext"}), 404
    try:
        jpeg_bytes = base64.b64decode(image_b64)
    except Exception as e:
        return jsonify({"error": f"image_b64_decode: {e}"}), 502

    # Flask's send_file with conditional=True honors HTTP Range + etag
    # out of the box — clients can split the JPEG into parallel chunks
    # to bypass the per-TCP-connection throttle on dstack-gateway.
    return send_file(
        BytesIO(jpeg_bytes),
        mimetype="image/jpeg",
        as_attachment=False,
        download_name=f"{frame_id}.jpg",
        conditional=True,
        max_age=0,
    )


def _get_or_derive_content_sk() -> nacl.public.PrivateKey:
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


# Number of worker threads in the single gunicorn worker. The enclave's
# concurrency profile is I/O-bound: every decrypt-and-serve request calls
# back into the backend over httpx and parks the thread on that round-trip,
# so a generously sized thread pool (not CPU count) is what keeps the pool
# from starving. The whoami short-TTL cache + singleflight (see top of file)
# already collapse the history-import auth storm, so 32 is ample headroom.
_ENCLAVE_THREADS = int(os.environ.get("FEEDLING_ENCLAVE_THREADS", 32))


def _materialize_tls_files() -> tuple[str, str] | None:
    """Write the in-memory TLS PEM to two tmpfs files for gunicorn.

    gunicorn loads its server cert/key from file paths (and flips SSL on iff
    a certfile/keyfile is configured), so we materialize the PEM that
    bootstrap() derived. The files are mode 0600 and unlinked atexit. In a
    TDX CVM /tmp is an in-memory tmpfs, so the key never touches persistent
    storage or the operator's disk; outside TDX (local dev) they are ordinary
    temp files cleaned up on exit.

    Returns (cert_path, key_path), or None when TLS is disabled — in which
    case gunicorn serves plain HTTP, matching the old app.run(ssl_context=None)
    behaviour.
    """
    if not _state["tls_enabled"]:
        return None
    cert_pem = _state["tls_cert_pem"]
    key_pem = _state["tls_key_pem"]
    if not cert_pem or not key_pem:
        return None

    paths: list[str] = []
    for pem in (cert_pem, key_pem):
        with tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False) as f:
            os.chmod(f.name, 0o600)
            f.write(pem)
            f.flush()
            paths.append(f.name)
    cert_path, key_path = paths

    # Guard cleanup to THIS (master) process. gunicorn forks its worker after
    # we register, so the worker inherits this atexit handler; a graceful
    # worker recycle (SIGHUP reload / max_requests) exits the child via
    # sys.exit, which runs atexit. Without the pid guard the dying worker would
    # unlink the cert/key while the master lives, and the respawned worker's
    # load_cert_chain would then FileNotFoundError into a boot crash-loop.
    owner_pid = os.getpid()

    def _cleanup() -> None:
        if os.getpid() != owner_pid:
            return
        for p in (cert_path, key_path):
            try: os.unlink(p)
            except OSError: pass
    atexit.register(_cleanup)
    return cert_path, key_path


def _enclave_ssl_context(conf, default_ssl_context_factory) -> ssl.SSLContext:
    """gunicorn `ssl_context` hook — reproduce the enclave's exact TLS posture.

    iOS pins sha256(cert.DER) of the served leaf against the fingerprint baked
    into REPORT_DATA (see docs/DESIGN_E2E.md §7 + the phala compose enclave
    note), so the handshake must serve precisely the cert bootstrap() derived
    and nothing else. We therefore build a bare PROTOCOL_TLS_SERVER context
    (no client-cert verification, no HTTP/2 ALPN) pinned to TLS 1.2+, exactly
    as the previous _build_ssl_context did, rather than letting gunicorn's
    default create_default_context() factory alter the chain or negotiation.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=conf.certfile, keyfile=conf.keyfile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _run_enclave_server(tls: tuple[str, str] | None) -> None:
    """Serve `app` under gunicorn's gthread worker (production WSGI).

    Replaces app.run() (the Werkzeug dev server), which is single-threaded
    and not production-grade under the TDX CVM. We embed gunicorn
    programmatically — via BaseApplication — instead of changing the compose
    entrypoint, so the command stays `python -u backend/enclave_app.py` and
    the published compose_hash is unaffected (CONTRIBUTING.md §7).

    Single worker mirrors the previous single-process model exactly (the
    process-local whoami/content-key caches stay coherent); gthread + 32
    threads gives the production-grade concurrency the old `threaded=True`
    provided, now on a real WSGI server with worker timeouts.
    """
    # Imported lazily so that `import enclave_app` in the test suite (which
    # never reaches this entrypoint) does not hard-require gunicorn.
    import gunicorn.app.base

    options: dict[str, Any] = {
        "bind": f"0.0.0.0:{ENCLAVE_PORT}",
        "workers": 1,
        "worker_class": "gthread",
        "threads": _ENCLAVE_THREADS,
        "timeout": 120,
        "graceful_timeout": 30,
    }
    if tls is not None:
        cert_path, key_path = tls
        # certfile/keyfile flip gunicorn's is_ssl on (and satisfy its path
        # validation); the actual context is built by the ssl_context hook.
        options["certfile"] = cert_path
        options["keyfile"] = key_path
        options["ssl_context"] = _enclave_ssl_context

    class _EnclaveApplication(gunicorn.app.base.BaseApplication):
        def load_config(self):
            for key, value in options.items():
                self.cfg.set(key, value)

        def load(self):
            return app

    _EnclaveApplication().run()


if __name__ == "__main__":
    bootstrap()
    tls = _materialize_tls_files()
    scheme = "https" if tls else "http"
    print(f"Feedling enclave service listening on {scheme}://0.0.0.0:{ENCLAVE_PORT}", flush=True)
    _run_enclave_server(tls)
