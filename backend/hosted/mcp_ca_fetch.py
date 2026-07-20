"""Auto-fetch a TLS trust anchor for a self-signed user MCP server (Runtime V2).

A self-signed MCP server can't be verified against the public roots, and we
refuse to skip verification. The manual path is "user pastes ``ca_pem`` in the
config"; this module is the AUTOMATIC fallback the V2 turn takes when no
``ca_pem`` was configured and the connection failed with a TLS error: fetch the
server's own chain, pin its anchor, and retry with verification still ON.

This mirrors ``tools/user_mcp_ca_fetch.py`` (the VPS-resident path) but is a
SEPARATE backend copy on purpose — backend never imports ``tools/`` (the two
sides duplicate the small CA helpers rather than cross the layer, exactly like
``MAX_CA_BYTES`` is duplicated between ``mcp_core`` and ``user_mcp_ca_fetch``).
The behavioural difference that forces a copy rather than reuse: the VPS path
connects to the raw hostname, but the multi-tenant backend MUST connect only to
the SSRF-validated public IP (``mcp_probe._validated_public_ips``) and never
re-resolve — otherwise a self-signed server's hostname could DNS-rebind to an
internal address between validation and the openssl fetch.

Why a subprocess: the anchor is the ROOT of the chain, and Python 3.11's ssl
exposes only the leaf (get_unverified_chain needs 3.13). The runner image ships
/usr/bin/openssl, so ``openssl s_client -showcerts`` is the chain source with no
new dependency. Every network call below targets the validated IP literal with
``-servername``/``server_hostname`` set to the configured host, so SNI and
certificate-hostname verification stay correct while DNS is never re-queried.
"""
from __future__ import annotations

import asyncio
import re
import socket
import ssl
import subprocess
from urllib.parse import urlparse

from hosted import mcp_probe

_PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)

# Mirrors backend/hosted/mcp_core.py MAX_CA_BYTES and tools/user_mcp_ca_fetch.py.
# Byte-count the UTF-8 encoding, not len(str) — keep the three in lockstep.
MAX_CA_BYTES = 32768


def _pick_trust_anchor(chain_pems: list[str]) -> str | None:
    """The LAST cert in the chain: the anchor in every shape we see (a lone
    self-signed cert is itself; a private CA + leaf gives the CA; a leaf +
    intermediate with the root withheld gives the intermediate — still a CA).
    A heuristic; ``_anchor_works`` is what makes the pick safe."""
    return chain_pems[-1] if chain_pems else None


def _fetch_chain(ip: str, port: int, server_hostname: str, timeout: float) -> list[str]:
    """The server's chain via ``openssl s_client -showcerts``, connecting to the
    VALIDATED IP literal with SNI set to the configured host (no re-resolution).
    Returns [] on any failure (unreachable / no openssl / timeout)."""
    try:
        proc = subprocess.run(
            ["openssl", "s_client", "-connect", f"{ip}:{port}",
             "-servername", server_hostname, "-showcerts"],
            input=b"", capture_output=True, timeout=timeout + 2)
    except Exception:  # noqa: BLE001 — unreachable / no openssl / timeout
        return []
    return _PEM_RE.findall(proc.stdout.decode("utf-8", "replace"))


def _anchor_works(pem: str, ip: str, port: int, server_hostname: str, timeout: float) -> bool:
    """Self-check: does pinning this anchor actually verify a real handshake to
    the validated IP (cert must chain to it AND match the configured hostname)?
    A pick that doesn't verify is worse than nothing."""
    try:
        ctx = ssl.create_default_context(cadata=pem)
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=server_hostname):
                return True
    except Exception:  # noqa: BLE001
        return False


def _is_well_formed_ca(pem: str) -> bool:
    """The same shape+size check the backend runs on a manually-pasted ca_pem
    (mcp_core._validate_ca_pem), INCLUDING the MAX_CA_BYTES ceiling. An oversized
    but valid CA still passes a real handshake and a bare parse, so the size gate
    matters on its own: an oversized bundle in the verify context is refused on
    the manual path and must be refused here too."""
    if len(pem.encode("utf-8")) > MAX_CA_BYTES:
        return False
    try:
        ssl.create_default_context().load_verify_locations(cadata=pem)
        return True
    except Exception:  # noqa: BLE001
        return False


def _fetch_anchor_pinned(ip: str, port: int, server_hostname: str, timeout: float) -> str | None:
    """Blocking anchor fetch against a pre-validated IP. Never raises."""
    chain = _fetch_chain(ip, port, server_hostname, timeout)
    pem = _pick_trust_anchor(chain)
    if not pem:
        return None
    if not _is_well_formed_ca(pem):
        return None
    if not _anchor_works(pem, ip, port, server_hostname, timeout):
        return None
    return pem


async def fetch_anchor_for_url(url: str, *, timeout: float = 3.0) -> str | None:
    """A PEM usable as a trust anchor for ``url``, or None. Never raises.

    SSRF-safe: resolves through ``mcp_probe._validated_public_ips`` (rejecting
    any non-public address) and fetches ONLY from that validated IP, with SNI
    and hostname verification bound to the configured host. The blocking socket
    /openssl work runs on a worker thread so it never stalls the event loop.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return None
        port = parsed.port or 443
    except ValueError:
        return None
    try:
        host, ips = mcp_probe._validated_public_ips(url)
    except mcp_probe.ProbeError:
        return None
    if not ips:
        return None
    return await asyncio.to_thread(_fetch_anchor_pinned, ips[0], port, host, timeout)
