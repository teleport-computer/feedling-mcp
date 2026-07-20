"""Fetch a usable TLS trust anchor for a user-configured MCP server.

Self-signed MCP servers can't be verified against the public roots, and we
refuse to skip verification (spec 2026-07-16 §2.1: claude can only disable it
process-wide, and codex offers no switch at all). Instead the consumer fetches
the server's own chain and pins its anchor — verification stays ON, the server
still has to prove it holds the matching key.

Why a subprocess: the anchor we need is the ROOT of the chain (empirically, a
pinned leaf fails with CERTIFICATE_VERIFY_FAILED while the root verifies), but
``ssl.get_server_certificate()`` returns only the leaf and
``SSLSocket.get_unverified_chain()`` needs Python 3.13 — the runner image is
python:3.11 (deploy/Dockerfile.agent-runner:16). It does ship
/usr/bin/openssl 3.0.20, so `openssl s_client -showcerts` is the chain source
that costs no new dependency.

I/O lives here and NOT in user_mcp_materialize.py, whose contract is
"pure functions only — no I/O, no env".
"""

from __future__ import annotations

import re
import socket
import ssl
import subprocess
from urllib.parse import urlparse

_PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)

# Mirrors backend/hosted/mcp_core.py's MAX_CA_BYTES. Duplicated, not imported:
# this module is deliberately dependency-light (see module docstring), and
# mcp_core pulls in the DB/store stack. Keep the two numbers and the
# byte-counting rule (len(pem.encode("utf-8")), not len(pem)) in lockstep —
# see _is_well_formed_ca.
MAX_CA_BYTES = 32768


def _pick_trust_anchor(chain_pems: list[str]) -> str | None:
    """The LAST cert in the chain, which is the anchor in every shape we see:
    a lone self-signed cert (itself), a private CA + leaf (the CA), or a leaf +
    intermediate with the root withheld (the intermediate — still a CA).
    Callers MUST still verify the pick actually works; this is a heuristic.
    """
    return chain_pems[-1] if chain_pems else None


def _verifies_against_public_roots(host: str, port: int, timeout: float) -> bool:
    """True when the server already verifies against the public roots.

    This is the gate (spec §3.1): a server with a real certificate must NOT be
    pinned. NOT because a pinned anchor would break on rotation — it wouldn't:
    both delivery paths are additive (NODE_EXTRA_CA_CERTS is Node's ADD-scoped
    env var; SSL_CERT_FILE's content is certifi's public roots PLUS this
    bundle), so the public roots stay in the trust store regardless and a
    rotated intermediate still verifies through them. The real reason is
    attack-surface minimization: TOFU-pinning is this feature's own new risk
    (spec §8 — a pin made at the wrong moment anchors an interceptor's CA into
    the agent's GLOBAL trust store, not just for this server). Pinning a
    server that already has a real certificate buys nothing and only opens
    another one of those windows for free, so it's skipped.
    """
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host):
                return True
    except Exception:  # noqa: BLE001 — any failure means "not publicly trusted"
        return False


def _fetch_chain(host: str, port: int, timeout: float) -> list[str]:
    """The server's chain, via `openssl s_client -showcerts`.

    Subprocess rather than stdlib because we need the ROOT and 3.11's ssl only
    exposes the leaf — see the module docstring.
    """
    try:
        proc = subprocess.run(
            ["openssl", "s_client", "-connect", f"{host}:{port}",
             "-servername", host, "-showcerts"],
            input=b"", capture_output=True, timeout=timeout + 2)
    except Exception:  # noqa: BLE001 — unreachable / no openssl / timeout
        return []
    return _PEM_RE.findall(proc.stdout.decode("utf-8", "replace"))


def _anchor_works(pem: str, host: str, port: int, timeout: float) -> bool:
    """Self-check #1 (spec §6.1): does pinning this actually verify?

    "Last cert in the chain" is a heuristic; this is what makes it safe. A pick
    that doesn't verify is worse than nothing.
    """
    try:
        ctx = ssl.create_default_context(cadata=pem)
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host):
                return True
    except Exception:  # noqa: BLE001
        return False


def _is_well_formed_ca(pem: str) -> bool:
    """Self-check #2 (spec §6.1): the same shape check the backend runs on a
    manually-pasted ca_pem (mcp_core._validate_ca_pem) — INCLUDING its
    MAX_CA_BYTES size ceiling, not just the parse check. Auto-fetched anchors
    never pass through that endpoint, so we run both halves here.

    The size check matters on its own: a well-formed, self-signed CA padded
    with huge SAN/extension blocks still verifies in _anchor_works (a real
    handshake) and still parses cleanly here, so without a size ceiling it
    would sail through both self-checks. The manually-pasted path has never
    allowed an oversized ca_pem through; this path shouldn't either, because a
    malformed OR oversized bundle in codex's SSL_CERT_FILE (REPLACE semantics)
    kills all of its outbound TLS, including calls back to OpenAI.
    """
    if len(pem.encode("utf-8")) > MAX_CA_BYTES:
        return False
    try:
        ssl.create_default_context().load_verify_locations(cadata=pem)
        return True
    except Exception:  # noqa: BLE001
        return False


def _leaf_is_ca_pem(pem: str) -> bool | None:
    """One cert: is its basicConstraints CA:TRUE? None on parse failure."""
    try:
        from cryptography import x509  # noqa: PLC0415 — backend dep, lazy
        cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed → "don't know", never raise
        return None
    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        return bool(bc.ca)
    except x509.ExtensionNotFound:
        return False  # no basicConstraints → not a CA → fine as an end-entity


def leaf_is_ca(chain_pems: list[str]) -> bool | None:
    """The server's LEAF (chain_pems[0]) presented as end-entity: is it a CA
    cert? True ⇒ rustls-based agents (codex) reject it as CaUsedAsEndEntity;
    claude/pi (Node) accept it. None ⇒ empty chain or unparseable (don't warn)."""
    if not chain_pems:
        return None
    return _leaf_is_ca_pem(chain_pems[0])


def fetch_anchor_and_leaf_ca(url: str, *, timeout: float = 3.0) -> tuple[str | None, bool | None]:
    """Like fetch_trust_anchor but ALSO reports whether the server's leaf is a
    CA cert (the codex/rustls incompatibility signal). Never raises."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return None, None
        host = parsed.hostname
        if not host:
            return None, None
        port = parsed.port or 443
    except ValueError:
        return None, None

    if _verifies_against_public_roots(host, port, timeout):
        return None, None  # real cert — not pinned (see _verifies_against_public_roots)

    chain = _fetch_chain(host, port, timeout)
    leaf_ca = leaf_is_ca(chain)
    pem = _pick_trust_anchor(chain)
    if not pem:
        return None, leaf_ca
    if not _is_well_formed_ca(pem):
        return None, leaf_ca
    if not _anchor_works(pem, host, port, timeout):
        return None, leaf_ca
    return pem, leaf_ca


def fetch_trust_anchor(url: str, *, timeout: float = 3.0) -> str | None:
    """A PEM usable as a trust anchor for ``url``, or None. Never raises.
    Thin wrapper over fetch_anchor_and_leaf_ca (which also reports leaf_is_ca)."""
    return fetch_anchor_and_leaf_ca(url, timeout=timeout)[0]
