"""HTTP access to the enclave (attestation info, decrypt proxy).

The enclave is the only process that can produce plaintext from v1
envelopes; the backend only ever relays. Tests monkeypatch the functions
on THIS module — callers must invoke them as ``enclave.func()``.
"""

import base64
import contextlib
import os
import threading
import time

import httpx

import debug_trace


_QUIET_SUCCESS_PURPOSE_PREFIXES = ("tee_replicate:",)
_SUCCESS_TRACE_EVENT_TYPES = frozenset({"enclave.call.start", "enclave.call.done"})

# —— Bulk-decrypt trace coalescing ——
# `_decrypt_chat_rows` decrypts the prompt window one row at a time. Even the
# former 60-row window produced two success events per row;
# that is ~120 trace events for a single chat turn, against a ring that holds
# 500 — so ONE turn evicts everything else and the debug panel shows a window
# measured in seconds. Measured on usr_7001b1df80e2024d 2026-08-10: 182 of 200
# retained events were purpose=v2_chat_read, and the whole trace spanned 1s.
#
# Per-row success events carry no diagnostic signal that the batch total does
# not (the interesting per-row cases — error/timeout — are separate event types
# and are NEVER coalesced). So a bulk scope collapses the successes into one
# `enclave.call.batch` event carrying the count and the elapsed total.
#
# Thread-local: the scope must not leak across concurrently served users, and
# the decrypt loop is synchronous within one thread.
_bulk_scope = threading.local()


class _BulkTrace:
    __slots__ = (
        "purpose",
        "prefix",
        "store",
        "count",
        "by_purpose",
        "started_at",
        "path",
        "explain",
    )

    def __init__(self, purpose: str, *, prefix: bool = False) -> None:
        self.purpose = purpose
        self.prefix = prefix
        self.store = None
        self.count = 0
        # 前缀批次里,每个具体 purpose 各自的次数。折叠掉的是「一条条铺开」,
        # **不是**「这批里有哪些信号、各几次」—— 后者留在这里,否则查问题
        # 会真的变瞎(感知上报一批七个字段,分不清哪个来了才是问题)。
        self.by_purpose: dict[str, int] = {}
        self.started_at = time.time()
        self.path: str | None = None
        self.explain: str | None = None


@contextlib.contextmanager
def coalesced_success_trace(purpose: str, *, prefix: bool = False):
    """Collapse a bulk decrypt loop's per-call success events into one event.

    Errors and timeouts still emit individually — a failure inside a batch is
    exactly what someone reading the trace is looking for. Nested scopes of the
    same purpose join the outer batch; different purposes are tracked
    independently so a chat-row loop can fold both body and caption decrypts.

    ``prefix=True`` folds every purpose starting with ``purpose``. Perception
    ingestion needs it: each field decrypts under its own ``perception:<field>``
    purpose, so exact matching folds nothing. Measured 2026-08-12 on a live V2
    user — after the earlier folds shipped, 100% of the retained ring was still
    per-field ``perception:*`` pairs from the backend process (~14 events per
    report), which is what kept the window at hours instead of the 48h the TTL
    promises.

    ⚠️ The per-field purpose stays untouched on the decrypt call itself — seven
    tests pin those exact strings, and the field name is what tells you WHICH
    signal was decrypted. Folding must not cost that, so the batch event carries
    ``by_purpose`` counts.
    """
    scopes = getattr(_bulk_scope, "active_by_purpose", None)
    if scopes is None:
        scopes = {}
        _bulk_scope.active_by_purpose = scopes
    if purpose in scopes:
        yield
        return
    scope = _BulkTrace(purpose, prefix=prefix)
    scopes[purpose] = scope
    try:
        yield
    finally:
        scopes.pop(purpose, None)
        if not scopes:
            try:
                del _bulk_scope.active_by_purpose
            except AttributeError:
                pass
        if scope.store is not None and scope.count:
            detail: dict = {"calls": scope.count}
            if scope.prefix and scope.by_purpose:
                detail["by_purpose"] = dict(sorted(scope.by_purpose.items()))
            _trace_enclave(
                scope.store,
                "enclave.call.batch",
                purpose=purpose,
                path=scope.path or "",
                summary=f"enclave decrypt x{scope.count}",
                detail=detail,
                dur_ms=(time.time() - scope.started_at) * 1000,
                explain=scope.explain
                or ("Backend called the enclave over HTTP; only metadata is recorded."),
            )


# —— Pooled HTTP client ——
# Every enclave call used to build its own ``httpx.Client``, which meant a fresh
# TCP connect + TLS handshake per request. That is invisible for one-shot calls
# but brutal on the V2 prompt path: `_decrypt_chat_rows` decrypts the tail one
# row at a time, so the former 60-row window paid 60
# handshakes — measured at ~82ms each on test, ~4.9s of every chat turn.
#
# One pooled client per process instead. It is lazily built, guarded by a lock,
# and tagged with the pid that built it: a client inherited across ``fork``
# holds sockets the parent owns, so the child rebuilds rather than reusing a
# poisoned pool (it also must NOT close the inherited one — that would send a
# FIN on the parent's live connections). ``verify=False`` keeps the pre-existing
# contract: the in-cluster enclave presents a self-signed cert whose trust comes
# from REPORT_DATA, not a CA. Per-call timeouts stay per-call.
_HTTP_LIMITS = httpx.Limits(max_keepalive_connections=32, max_connections=64)
_http_client: "httpx.Client | None" = None
_http_client_pid: int | None = None
_http_client_lock = threading.Lock()


def _client() -> "httpx.Client":
    """Return this process's pooled enclave client, building it if needed."""
    global _http_client, _http_client_pid
    pid = os.getpid()
    client = _http_client
    if client is not None and _http_client_pid == pid:
        return client
    with _http_client_lock:
        if _http_client is not None and _http_client_pid == pid:
            return _http_client
        # Inherited across fork: drop the reference without closing it.
        _http_client = httpx.Client(verify=False, limits=_HTTP_LIMITS)
        _http_client_pid = pid
        return _http_client


def reset_http_client() -> None:
    """Close and forget the pooled client (shutdown paths and tests)."""
    global _http_client, _http_client_pid
    with _http_client_lock:
        client, _http_client, _http_client_pid = _http_client, None, None
    if client is None:
        return
    try:
        client.close()
    except Exception:  # noqa: BLE001 — best-effort fd cleanup
        pass


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
    explain: str = "Backend called the enclave over HTTP; only metadata is recorded.",
) -> None:
    if store is None:
        return
    if (
        status == "ok"
        and event_type in _SUCCESS_TRACE_EVENT_TYPES
        and purpose.startswith(_QUIET_SUCCESS_PURPOSE_PREFIXES)
    ):
        return
    scopes = getattr(_bulk_scope, "active_by_purpose", None) or {}
    scope = scopes.get(purpose)
    if scope is None:
        # 没有精确作用域时才看前缀作用域。顺序不能反:精确的更具体,
        # 前缀的是兜底;反过来会让一个宽前缀把本该独立成批的 purpose 吸走。
        # 同时命中多个前缀时取最长的那个(同样是「更具体者优先」)。
        candidates = [
            s for s in scopes.values()
            if s.prefix and purpose.startswith(s.purpose)
        ]
        if candidates:
            scope = max(candidates, key=lambda s: len(s.purpose))
    if (
        scope is not None
        and status == "ok"
        and event_type in _SUCCESS_TRACE_EVENT_TYPES
        and (purpose == scope.purpose
             or (scope.prefix and purpose.startswith(scope.purpose)))
    ):
        # Count the pair once, on `.done`, so the rollup reports calls not events.
        if event_type == "enclave.call.done":
            scope.count += 1
            if scope.prefix:
                scope.by_purpose[purpose] = scope.by_purpose.get(purpose, 0) + 1
        if scope.store is None:
            scope.store = store
        if scope.path is None:
            scope.path = path
        elif scope.path != path:
            # A multi-resource proxy batch (screen frames) has no single exact
            # route. Omit it instead of reporting the first frame's path as if
            # every call used that resource.
            scope.path = ""
        if scope.explain is None:
            scope.explain = explain
        return
    try:
        debug_trace.trace_event(
            store,
            subsystem="enclave",
            type=event_type,
            actor="backend",
            status=status,
            summary=summary,
            explain=explain,
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
    used by pooled V2 and other trusted background workers)."""
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return None, "enclave_unavailable"
    if not api_key and not runtime_token:
        return None, "api_key_unavailable"
    headers = {"X-Feedling-Runtime-Token": runtime_token} if runtime_token else {"X-API-Key": api_key}
    try:
        resp = _client().get(
            f"{enclave_url}{path}",
            headers=headers,
            params=params or {},
            timeout=20,
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
        # The pooled client runs verify=False because the in-cluster enclave
        # presents a self-signed cert whose trust comes from REPORT_DATA, not a
        # CA. We're not pinning here; just fetching public material. Any MITM
        # between backend and enclave would at worst substitute a different
        # pubkey, which would then fail AEAD verification on the enclave side
        # when the agent tries to decrypt.
        r = _client().get(f"{url.rstrip('/')}/attestation", timeout=5)
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
        resp = _client().post(
            f"{enclave_url}{path}",
            headers=headers,
            json={"envelope": envelope, "key_version": key_version},
            timeout=30,
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
    runtime token (``X-Feedling-Runtime-Token``) when a trusted background worker
    has no per-user api_key."""
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
        resp = _client().post(
            f"{enclave_url}{path}",
            headers=headers,
            json={"envelope": envelope, "purpose": purpose},
            timeout=20,
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
