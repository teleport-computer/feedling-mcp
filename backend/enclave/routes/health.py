"""GET /healthz + GET /attestation（旧 enclave_app L469-518 语义逐字）。"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter
from starlette.responses import JSONResponse, Response

from enclave import config, state

router = APIRouter()


def _health_body() -> dict:
    """Liveness + readiness snapshot, built purely from the in-memory ``_state``.

    The enclave is reentrancy-sensitive, so /healthz does
    NO crypto and NO backend round-trip — every field here is a plain read of
    the attestation/bootstrap state cached at process start. Backward compatible:
    ``ok`` / ``ready`` / ``error`` keep their old meaning; ``status`` /
    ``release`` / ``uptime_s`` / ``tls_enabled`` / ``phase`` are additive so an
    external heartbeat can see build + TLS posture without hitting /attestation.
    """
    st = state._state
    ready = bool(st["ready"])
    booted = st.get("booted_at")
    uptime = round(time.time() - booted, 1) if booted else None
    return {
        "ok": ready,
        "ready": ready,
        "status": "healthy" if ready else "unhealthy",
        # Just the identity triplet — the full RELEASE (build/compose URLs) stays
        # on /attestation. Keeps /healthz small and parallel to the backend.
        "release": {
            "git_commit": config.RELEASE.get("git_commit"),
            "image_digest": config.RELEASE.get("image_digest"),
            "built_at": config.RELEASE.get("built_at"),
        },
        "uptime_s": uptime,
        "booted_at": booted,
        "tls_enabled": st["tls_enabled"],
        "phase": 3 if st["tls_enabled"] else 1,
        "error": st["error"],
    }


@router.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    body = _health_body()
    return JSONResponse(body, status_code=200 if body["ready"] else 503)


@router.api_route("/attestation", methods=["GET", "HEAD"])
async def attestation():
    if not state._state["ready"]:
        return JSONResponse(
            {"error": "not_ready", "detail": state._state["error"]}, status_code=503
        )

    att = state._state["attestation"]
    bundle = {
        "tdx_quote_hex": att["tdx_quote_hex"],
        "event_log_json": att["event_log_json"],
        "measurements": att["measurements"],
        "compose_hash": att["compose_hash"],
        "app_id": att["app_id"],
        "instance_id": att["instance_id"],
        "enclave_content_pk_hex": state._state["content_pk_hex"],
        "enclave_signing_pk_hex": state._state["signing_pk_hex"],
        "enclave_tls_cert_fingerprint_hex": state._state["tls_cert_fingerprint_hex"],
        # Phase C.2: sha256(SubjectPublicKeyInfo DER) of the MCP port's cert key.
        # Derived independently from dstack-KMS so it's pre-computable without
        # talking to the MCP service. Stable across LE cert renewals because the
        # key doesn't change — only the CA-signed certificate wrapper does.
        "mcp_tls_cert_pubkey_fingerprint_hex": state._state["mcp_tls_cert_pubkey_fingerprint_hex"],
        "enclave_release": config.RELEASE,
        "app_auth": config.APP_AUTH,
        "report_data_version": 1,
        "phase": 3 if state._state["tls_enabled"] else 1,
        "tls_in_enclave": state._state["tls_enabled"],
        "notes": (
            "phase-3: TLS terminated inside the enclave."
            " enclave_tls_cert_fingerprint_hex = sha256(cert.DER) of the"
            " cert the TLS handshake presents. Clients must compare the"
            " live cert's DER hash to this value; do not trust the"
            " self-signed chain on its own."
            if state._state["tls_enabled"] else
            "phase-1 skeleton — TLS cert binding is a placeholder (all"
            " zeros). Operator-controlled infrastructure terminates TLS."
            " Until in-enclave TLS is enabled, clients must trust the"
            " dstack-gateway operator to forward traffic unmodified."
        ),
        "booted_at": state._state["booted_at"],
    }
    return Response(
        json.dumps(bundle, indent=2),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=60"},
    )
