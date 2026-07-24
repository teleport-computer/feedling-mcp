"""Cached attestation state + process-startup bootstrap.

Verbatim extraction of enclave_app.py's "Cached attestation state" section:
the module-level `_state` dict every route reads from, and `bootstrap()`
which derives keys + assembles the attestation bundle once at process start.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

from dstack_sdk import DstackClient

from dstack_tls import derive_tls_cert_and_key

from enclave import attestation, config, keys

# Fixed plaintext sealed to content_pk at boot for the not-bound-to-any-user
# decrypt self-check probe (GET /v1/decrypt/selfcheck, stage 2 of the
# runner-shared decrypt-health work). The endpoint opens the stored sealed blob
# with content_sk and asserts it restores to this — proving the SAME content
# key derived at boot still decrypts (catches mid-life key drift), stronger than
# the old "HTTP 200 on a limit=1 read" reachability signal. See
# docs/proposals/shared-decrypt-health-probe.md.
SELFCHECK_PLAINTEXT = b"feedling-decrypt-selfcheck-v1"

_state: dict[str, Any] = {
    "ready": False,
    "error": None,
    "content_pk_hex": None,
    "signing_pk_hex": None,
    "tls_cert_fingerprint_hex": attestation.PHASE1_TLS_FINGERPRINT.hex(),
    # Always empty since the MCP user line was removed (2026-06-12) —
    # kept in the payload so existing iOS audit-card parsers fall through
    # to the "Pre-Phase-C.2 deployment" disclosure row.
    "mcp_tls_cert_pubkey_fingerprint_hex": "",
    "tls_enabled": False,
    "tls_cert_pem": None,  # bytes; only kept for the SSLContext load path
    "tls_key_pem": None,   # bytes; only kept for the SSLContext load path
    "attestation": None,
    "booted_at": None,
    # box_seal(SELFCHECK_PLAINTEXT, content_pk) filled at boot; None means the
    # self-check endpoint can't verify decrypt (reports "fail" rather than
    # crashing). Never carries user data — the plaintext is a fixed constant.
    "decrypt_selfcheck_sealed": None,
}


def bootstrap():
    """Derive keys + generate attestation once at startup. Cached thereafter.

    When ENCLAVE_TLS is true we also derive an ECDSA P-256 cert bound to
    compose_hash and bake its sha256(DER) into REPORT_DATA so iOS can
    pin the TLS cert against the quote. Off → the old zero placeholder
    stays, and iOS will surface the amber "operator-terminated TLS" row.
    """
    try:
        dev_seed = os.environ.get("FEEDLING_DEV_DSTACK_SEED", "").strip()
        dstack = None
        if dev_seed:
            derived = keys.derive_keys_from_dev_seed()
        else:
            dstack = DstackClient()
            derived = keys.derive_keys(dstack)

        tls_fingerprint = attestation.PHASE1_TLS_FINGERPRINT
        if config.ENCLAVE_TLS:
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

        report_data = attestation.build_report_data(
            content_pk_bytes=derived["content_pk_bytes"],
            tls_cert_fingerprint=tls_fingerprint,
            version_tag=b"feedling-v1",
        )
        att = (
            attestation.dev_attestation(report_data)
            if dstack is None
            else attestation.fetch_quote_and_measurements(dstack, report_data)
        )

        _state["content_pk_hex"] = derived["content_pk_bytes"].hex()
        _state["signing_pk_hex"] = derived["signing_pk_bytes"].hex()
        keys.set_cached_content_sk(derived["content_sk"])
        # Seal the self-check probe to the content pubkey now that the key is
        # derived. Best-effort: a failure here only degrades /v1/decrypt/selfcheck
        # to reporting "fail", it must never block the enclave from booting.
        try:
            import content_encryption
            _state["decrypt_selfcheck_sealed"] = content_encryption.box_seal(
                SELFCHECK_PLAINTEXT, derived["content_pk_bytes"]
            )
        except Exception as e:
            _state["decrypt_selfcheck_sealed"] = None
            print(f"[enclave] decrypt self-check seal skipped: {e}",
                  file=sys.stderr, flush=True)
        _state["tls_cert_fingerprint_hex"] = tls_fingerprint.hex()
        _state["attestation"] = att
        _state["booted_at"] = time.time()
        _state["ready"] = True
        print(
            f"[enclave] ready: content_pk={_state['content_pk_hex'][:16]}… "
            f"compose_hash={att['compose_hash'][:16]}… "
            f"tls={'yes' if _state['tls_enabled'] else 'no'}",
            flush=True,
        )
    except Exception as e:
        _state["error"] = repr(e)
        print(f"[enclave] bootstrap failed: {e}", file=sys.stderr, flush=True)
