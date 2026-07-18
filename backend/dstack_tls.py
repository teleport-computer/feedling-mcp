"""Shared dstack-KMS-bound TLS cert derivation.

Used by both `enclave_app.py` (port 5003 / attestation) and `mcp_server.py`
(port 5002 / MCP SSE in Phase C). The derivation is deterministic per
(kms_root, app_id, path) — so both services derive the same cert, and
`sha256(cert.DER)` stays identical across rotations of the same
compose_hash or rotation-independent of compose updates (Phala's
dstack-KMS keys are bound to app_id, not compose_hash).

This means the attestation bundle's `enclave_tls_cert_fingerprint_hex`
applies to BOTH ports — the fingerprint an iOS client sees when it pins
`-5003s.` is the same one it would see when pinning `-5002s.`.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives import serialization


TLS_KEY_PATH = "feedling-tls-v1"


def derive_tls_cert_and_key(dstack) -> dict[str, Any]:
    """Return {'cert_pem', 'key_pem', 'cert_der', 'fingerprint'}.

    `dstack` is a `DstackClient` instance; the caller is responsible for
    wiring it up (simulator endpoint vs /var/run/dstack.sock). Output is
    byte-identical across calls within a given deploy because the
    cert body is deterministic and the ECDSA signature uses RFC-6979.
    """
    seed_resp = dstack.get_key(TLS_KEY_PATH, "")
    seed = bytes.fromhex(seed_resp.key) if isinstance(seed_resp.key, str) else seed_resp.key
    scalar_bytes = hashlib.sha256(b"feedling-tls-v1|" + seed[:32]).digest()
    scalar = int.from_bytes(scalar_bytes, "big")
    curve = ec.SECP256R1()
    priv_key = ec.derive_private_key(scalar, curve)

    subject_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "feedling-enclave"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Feedling (TDX CVM)"),
    ])
    not_before = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    not_after = _dt.datetime(2036, 1, 1, tzinfo=_dt.timezone.utc)
    pub_der = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    serial = int.from_bytes(hashlib.sha256(pub_der).digest()[:8], "big") | 1

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(subject_name)
        .public_key(priv_key.public_key())
        .serial_number(serial)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("feedling-enclave"),
                x509.DNSName("*.dstack-pha-prod5.phala.network"),
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                key_agreement=False, content_commitment=False,
                data_encipherment=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    )
    cert = builder.sign(
        private_key=priv_key,
        algorithm=_hashes.SHA256(),
        ecdsa_deterministic=True,
    )

    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = priv_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {
        "cert_pem": cert_pem,
        "key_pem": key_pem,
        "cert_der": cert_der,
        "fingerprint": hashlib.sha256(cert_der).digest(),
    }
