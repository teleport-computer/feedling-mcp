"""TLS cert acquisition (ACME DNS-01 or dstack-KMS fallback)."""

import base64
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastmcp import FastMCP, Context
from fastmcp.server.dependencies import get_http_request
from fastmcp.utilities.types import Image
from fastmcp.server.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from content_encryption import build_envelope



# Fingerprint of the currently-active MCP TLS cert public key (set at boot).
# acme_dns01: sha256(SubjectPublicKeyInfo DER) — stable across LE renewals.
# dstack-KMS fallback: sha256(cert.DER) of the self-signed cert.
_mcp_cert_pubkey_fingerprint_hex: str = ""


def _acquire_tls_cert() -> tuple[str | None, str | None]:
    """Acquire TLS cert for MCP.

    Priority:
      1. FEEDLING_ACME_DOMAIN set → ACME-DNS-01 via Cloudflare; cert from
         Let's Encrypt for the given domain. Cert key derived from dstack-KMS
         at 'feedling-mcp-tls-v1' (stable; fingerprint can be pre-computed
         by enclave_app for the attestation bundle).
      2. FEEDLING_MCP_TLS=true, no ACME → dstack-KMS self-signed cert (Phase C.1
         fallback; same cert as attestation port, fingerprint in bundle).
      3. Neither → HTTP only (local dev).
    """
    global _mcp_cert_pubkey_fingerprint_hex

    if os.environ.get("DSTACK_SIMULATOR_ENDPOINT", "") == "":
        os.environ.pop("DSTACK_SIMULATOR_ENDPOINT", None)

    acme_domain = os.environ.get("FEEDLING_ACME_DOMAIN", "").strip()

    if acme_domain:
        try:
            from dstack_sdk import DstackClient
            from dstack_tls import derive_key_only, MCP_TLS_KEY_PATH, ACME_ACCOUNT_KEY_PATH
            import acme_dns01

            dstack = DstackClient()
            account_key = derive_key_only(dstack, ACME_ACCOUNT_KEY_PATH)
            cert_key = derive_key_only(dstack, MCP_TLS_KEY_PATH)

            result = acme_dns01.get_or_renew(
                domain=acme_domain,
                email=os.environ.get("FEEDLING_ACME_EMAIL", "sxysun9@gmail.com"),
                cf_token=os.environ["FEEDLING_CF_API_TOKEN"],
                cf_zone_id=os.environ["FEEDLING_CF_ZONE_ID"],
                account_key=account_key,
                cert_key=cert_key,
                cache_dir=os.environ.get("FEEDLING_TLS_CACHE_DIR", "/tls"),
                staging=os.environ.get("FEEDLING_ACME_STAGING", "false").lower() == "true",
            )

            _mcp_cert_pubkey_fingerprint_hex = result["pubkey_fingerprint_hex"]
            print(
                f"[mcp] ACME cert acquired for {acme_domain}: "
                f"pubkey_fp={_mcp_cert_pubkey_fingerprint_hex[:32]}…",
                flush=True,
            )

            acme_dns01.start_renewal_watchdog(
                domain=acme_domain,
                email=os.environ.get("FEEDLING_ACME_EMAIL", "sxysun9@gmail.com"),
                cf_token=os.environ["FEEDLING_CF_API_TOKEN"],
                cf_zone_id=os.environ["FEEDLING_CF_ZONE_ID"],
                account_key=account_key,
                cert_key=cert_key,
                cache_dir=os.environ.get("FEEDLING_TLS_CACHE_DIR", "/tls"),
                staging=os.environ.get("FEEDLING_ACME_STAGING", "false").lower() == "true",
            )

            cert_f = tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False)
            key_f = tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False)
            cert_f.write(result["cert_pem"]); cert_f.flush(); cert_f.close()
            key_f.write(result["key_pem"]); key_f.flush(); key_f.close()
            return (cert_f.name, key_f.name)

        except Exception as e:
            print(f"[mcp] ACME failed: {e} — falling back to dstack-KMS cert", flush=True)

    if os.environ.get("FEEDLING_MCP_TLS", "false").lower() != "true":
        return (None, None)

    from dstack_sdk import DstackClient
    from dstack_tls import derive_tls_cert_and_key
    import hashlib as _hl

    dstack = DstackClient()
    tls = derive_tls_cert_and_key(dstack)
    _mcp_cert_pubkey_fingerprint_hex = _hl.sha256(tls["cert_der"]).hexdigest()

    cert_f = tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False)
    key_f = tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False)
    cert_f.write(tls["cert_pem"]); cert_f.flush(); cert_f.close()
    key_f.write(tls["key_pem"]); key_f.flush(); key_f.close()
    return (cert_f.name, key_f.name)

