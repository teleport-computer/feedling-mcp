"""HTTP access to the enclave (attestation info, decrypt proxy).

The enclave is the only process that can produce plaintext from v1
envelopes; the backend only ever relays. Tests monkeypatch the functions
on THIS module — callers must invoke them as ``enclave.func()``.
"""

import base64
import os
import threading
import time

import httpx


def _enclave_get_json_for_gate(path: str, api_key: str | None, params: dict | None = None) -> tuple[dict | None, str]:
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return None, "enclave_unavailable"
    if not api_key:
        return None, "api_key_unavailable"
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.get(
                f"{enclave_url}{path}",
                headers={"X-API-Key": api_key},
                params=params or {},
            )
        if resp.status_code >= 400:
            return None, f"enclave_http_{resp.status_code}:{resp.text[:160]}"
        data = resp.json()
        if not isinstance(data, dict):
            return None, "enclave_non_object"
        return data, ""
    except Exception as e:
        return None, f"enclave_error:{type(e).__name__}:{str(e)[:120]}"


# Cached enclave attestation (for wrapping envelopes we can't decrypt
# ourselves). Refetched every _ENCLAVE_INFO_TTL seconds — short enough
# that a rotated enclave is reflected within the window, long enough
# that writes don't pay a round-trip to the CVM per call.
_ENCLAVE_INFO_TTL = 60.0
_enclave_info_cache: dict = {"ts": 0.0, "data": None}
_enclave_info_lock = threading.Lock()


def _get_enclave_info() -> dict | None:
    """Fetch the enclave's (content_pk_hex, compose_hash) with a short
    cache. Returns None if no enclave is configured or reachable — the
    caller should surface the failure rather than proceed without the
    enclave's pubkey (v1 writes require it for shared visibility)."""
    url = os.environ.get("FEEDLING_ENCLAVE_URL", "").strip()
    if not url:
        return None
    now = time.time()
    with _enclave_info_lock:
        if _enclave_info_cache["data"] and now - _enclave_info_cache["ts"] < _ENCLAVE_INFO_TTL:
            return _enclave_info_cache["data"]
    try:
        # verify=False because the in-cluster enclave presents a
        # self-signed cert whose trust comes from REPORT_DATA, not a CA.
        # We're not pinning here; just fetching public material. Any
        # MITM between backend and enclave would at worst substitute a
        # different pubkey, which would then fail AEAD verification on
        # the enclave side when the agent tries to decrypt.
        with httpx.Client(timeout=5, verify=False) as client:
            r = client.get(f"{url.rstrip('/')}/attestation")
            r.raise_for_status()
            b = r.json()
        data = {
            "content_pk_hex": b.get("enclave_content_pk_hex", ""),
            "compose_hash": b.get("compose_hash", ""),
        }
        if not data["content_pk_hex"]:
            return None
        with _enclave_info_lock:
            _enclave_info_cache["ts"] = now
            _enclave_info_cache["data"] = data
        return data
    except Exception as e:
        print(f"[enclave-info] fetch failed from {url}: {e}")
        return None


def _decrypt_envelope_via_enclave(envelope: dict, api_key: str | None, *, purpose: str) -> bytes:
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        raise RuntimeError("enclave_unavailable")
    if not api_key:
        raise RuntimeError("api_key_unavailable")
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.post(
                f"{enclave_url}/v1/envelope/decrypt",
                headers={"X-API-Key": api_key},
                json={"envelope": envelope, "purpose": purpose},
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"enclave_error:{type(e).__name__}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")
    body = resp.json()
    if not isinstance(body, dict) or not isinstance(body.get("plaintext_b64"), str):
        raise RuntimeError("enclave_invalid_decrypt_response")
    try:
        return base64.b64decode(body["plaintext_b64"])
    except Exception as e:
        raise RuntimeError(f"enclave_plaintext_decode:{type(e).__name__}") from e
