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

import debug_trace


def _trace_store_from_user_id(user_id: str):
    user_id = str(user_id or "").strip()
    if not user_id:
        return None
    return type("_EnclaveTraceStore", (), {"user_id": user_id})()


def _trace_enclave(
    store,
    event_type: str,
    *,
    purpose: str = "",
    path: str = "",
    status: str = "ok",
    summary: str = "",
    detail: dict | None = None,
    dur_ms: float | None = None,
) -> None:
    if store is None:
        return
    try:
        debug_trace.trace_event(
            store,
            subsystem="enclave",
            type=event_type,
            actor="backend",
            status=status,
            summary=summary,
            explain="Backend called the enclave over HTTP; only metadata is recorded.",
            detail={
                "purpose": purpose,
                "path": path,
                **(detail or {}),
            },
            dur_ms=dur_ms,
        )
    except Exception:
        pass


def _enclave_get_json_for_gate(path: str, api_key: str | None, params: dict | None = None,
                               *, runtime_token: str = "") -> tuple[dict | None, str]:
    """Auth = api_key (``X-API-Key``) or a runtime token (``X-Feedling-Runtime-Token``,
    Stage-D zero-roster host-all). The enclave accepts either; mirrors
    agent_runtime.supervisor._auth_headers / _decrypt_envelope_via_enclave."""
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return None, "enclave_unavailable"
    if not api_key and not runtime_token:
        return None, "api_key_unavailable"
    headers = {"X-Feedling-Runtime-Token": runtime_token} if runtime_token else {"X-API-Key": api_key}
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.get(
                f"{enclave_url}{path}",
                headers=headers,
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


def _reencrypt_frame_via_enclave(envelope: dict, api_key: str | None, *,
                                 key_version: str = "v1",
                                 runtime_token: str = "") -> dict:
    """Storage-layer re-encryption (D4): hand a frame's v1 envelope (incl.
    ``body_ct``) to the enclave, which opens it, seals the PLAINTEXT under its
    KMS-derived storage key, and returns ``{body_ct_storage, key_version,
    sha256, size}`` — the plaintext never leaves the enclave. Auth mirrors
    _decrypt_envelope_via_enclave (api_key or runtime token). Raises
    RuntimeError on any transport/HTTP/shape failure (the tee_replicator maps
    HTTP-401/403-shaped errors to a token re-mint)."""
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        raise RuntimeError("enclave_unavailable")
    if not api_key and not runtime_token:
        raise RuntimeError("api_key_unavailable")
    headers = {"X-Feedling-Runtime-Token": runtime_token} if runtime_token else {"X-API-Key": api_key}
    path = "/v1/storage/reencrypt-frame"
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            resp = client.post(
                f"{enclave_url}{path}",
                headers=headers,
                json={"envelope": envelope, "key_version": key_version},
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"enclave_error:{type(e).__name__}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")
    body = resp.json()
    if not isinstance(body, dict) or not isinstance(body.get("body_ct_storage"), str):
        raise RuntimeError("enclave_invalid_reencrypt_response")
    return body


def _decrypt_envelope_via_enclave(envelope: dict, api_key: str | None, *, purpose: str,
                                  runtime_token: str = "") -> bytes:
    """Decrypt an envelope via the enclave. Auth = api_key (``X-API-Key``) or a
    runtime token (``X-Feedling-Runtime-Token``, Stage-D zero-roster host-all where
    the supervisor has no per-user api_key). The enclave accepts either; mirrors
    agent_runtime.supervisor._auth_headers."""
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        raise RuntimeError("enclave_unavailable")
    if not api_key and not runtime_token:
        raise RuntimeError("api_key_unavailable")
    headers = {"X-Feedling-Runtime-Token": runtime_token} if runtime_token else {"X-API-Key": api_key}
    path = "/v1/envelope/decrypt"
    store = _trace_store_from_user_id(str(envelope.get("owner_user_id") or envelope.get("user_id") or ""))
    started_at = time.time()
    _trace_enclave(
        store,
        "enclave.call.start",
        purpose=purpose,
        path=path,
        summary="enclave decrypt call started",
    )
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.post(
                f"{enclave_url}{path}",
                headers=headers,
                json={"envelope": envelope, "purpose": purpose},
            )
    except httpx.HTTPError as e:
        _trace_enclave(
            store,
            "enclave.call.timeout" if isinstance(e, httpx.TimeoutException) else "enclave.call.error",
            purpose=purpose,
            path=path,
            status="error",
            summary="enclave decrypt call failed",
            detail={"error_class": type(e).__name__},
            dur_ms=(time.time() - started_at) * 1000,
        )
        raise RuntimeError(f"enclave_error:{type(e).__name__}") from e
    if resp.status_code >= 400:
        _trace_enclave(
            store,
            "enclave.call.error",
            purpose=purpose,
            path=path,
            status="error",
            summary="enclave decrypt call returned error",
            detail={"status_code": resp.status_code},
            dur_ms=(time.time() - started_at) * 1000,
        )
        raise RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")
    body = resp.json()
    if not isinstance(body, dict) or not isinstance(body.get("plaintext_b64"), str):
        _trace_enclave(
            store,
            "enclave.call.error",
            purpose=purpose,
            path=path,
            status="error",
            summary="enclave decrypt call returned invalid body",
            dur_ms=(time.time() - started_at) * 1000,
        )
        raise RuntimeError("enclave_invalid_decrypt_response")
    try:
        out = base64.b64decode(body["plaintext_b64"])
        _trace_enclave(
            store,
            "enclave.call.done",
            purpose=purpose,
            path=path,
            summary="enclave decrypt call done",
            detail={"status_code": resp.status_code},
            dur_ms=(time.time() - started_at) * 1000,
        )
        return out
    except Exception as e:
        _trace_enclave(
            store,
            "enclave.call.error",
            purpose=purpose,
            path=path,
            status="error",
            summary="enclave decrypt plaintext decode failed",
            detail={"error_class": type(e).__name__},
            dur_ms=(time.time() - started_at) * 1000,
        )
        raise RuntimeError(f"enclave_plaintext_decode:{type(e).__name__}") from e
