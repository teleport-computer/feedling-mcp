#!/usr/bin/env python3
"""Verify dstack-ingress evidence served by an enclave custom domain.

This tool verifies evidence-file hashes, binds the live WebPKI TLS peer
certificate to the domain-specific evidence certificate, structurally parses
the TDX quote, and checks the enclave attestation values supplied by the
operator. It does not validate the Intel DCAP signature chain.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import re
import socket
import ssl
import sys
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import urljoin, urlsplit, urlunsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization

try:
    from tools.dcap.dcap_parse import TDXQuote, parse_quote
except ModuleNotFoundError:  # Direct execution: python tools/verify_enclave_domain.py
    from dcap.dcap_parse import TDXQuote, parse_quote


CONNECT_TIMEOUT_SECONDS = 10
RESPONSE_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
_READ_CHUNK_BYTES = 64 * 1024
_MANIFEST_LINE = re.compile(r"([0-9A-Fa-f]{64})  ([^\r\n]+)")
_SAFE_EVIDENCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_HEX_32_BYTES = re.compile(r"[0-9A-Fa-f]{64}")


class EvidenceError(Exception):
    """Raised when ingress or enclave evidence fails closed."""


def _validate_domain(domain: str) -> str:
    """Return a normalized bare DNS hostname, rejecting URL components."""
    if not isinstance(domain, str) or not domain or len(domain) > 253:
        raise EvidenceError("domain must be a bare DNS hostname")
    if any(not _DNS_LABEL.fullmatch(label) for label in domain.split(".")):
        raise EvidenceError(
            "domain must be a bare DNS hostname without scheme, path, port, or userinfo"
        )
    return domain.lower()


def _is_safe_evidence_filename(name: str) -> bool:
    return bool(_SAFE_EVIDENCE_NAME.fullmatch(name)) and name not in {".", ".."}


def parse_sha256_manifest(text: str) -> dict[str, str]:
    """Parse strict ``sha256sum`` output containing safe flat filenames."""
    if not isinstance(text, str):
        raise EvidenceError("sha256 manifest must be UTF-8 text")
    lines = text.splitlines()
    if not lines:
        raise EvidenceError("malformed sha256 manifest: empty manifest")

    manifest: dict[str, str] = {}
    seen_names: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        match = _MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise EvidenceError(f"malformed sha256 manifest line {line_number}")
        digest, name = match.groups()
        if not _is_safe_evidence_filename(name):
            raise EvidenceError(f"unsafe evidence filename: {name!r}")
        folded_name = name.casefold()
        if folded_name in seen_names:
            raise EvidenceError(f"duplicate evidence filename: {name}")
        seen_names.add(folded_name)
        manifest[name] = digest.lower()
    return manifest


def _required_file(files: Mapping[str, bytes], name: str) -> bytes:
    try:
        value = files[name]
    except KeyError as exc:
        raise EvidenceError(f"missing referenced evidence file: {name}") from exc
    if not isinstance(value, bytes):
        raise EvidenceError(f"evidence file is not bytes: {name}")
    return value


def verify_ingress_evidence(
    domain: str,
    peer_der: bytes,
    files: Mapping[str, bytes],
    quote_parser: Callable[[bytes], TDXQuote] = parse_quote,
) -> list[str]:
    """Verify the TLS peer, manifest files, and quote report-data binding."""
    normalized_domain = _validate_domain(domain)
    cert_name = f"cert-{normalized_domain}.pem"
    manifest_bytes = _required_file(files, "sha256sum.txt")
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("sha256sum.txt is not valid UTF-8") from exc
    manifest = parse_sha256_manifest(manifest_text)
    if cert_name not in manifest:
        raise EvidenceError(f"manifest does not reference {cert_name}")

    for name, expected in manifest.items():
        actual = hashlib.sha256(_required_file(files, name)).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise EvidenceError(f"evidence hash mismatch: {name}")

    cert_pem = _required_file(files, cert_name)
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"invalid evidence certificate: {cert_name}") from exc
    evidence_der = cert.public_bytes(serialization.Encoding.DER)
    if not isinstance(peer_der, bytes) or not hmac.compare_digest(
        hashlib.sha256(peer_der).digest(),
        hashlib.sha256(evidence_der).digest(),
    ):
        raise EvidenceError("TLS peer certificate does not match ingress evidence")

    quote_json_bytes = _required_file(files, "quote.json")
    try:
        quote_json = json.loads(quote_json_bytes.decode("utf-8"))
        if not isinstance(quote_json, dict):
            raise TypeError("quote.json root is not an object")
        quote_hex = quote_json["quote"]
        if not isinstance(quote_hex, str):
            raise TypeError("quote is not a string")
        if quote_json.get("hash_algorithm") != "sha256":
            raise ValueError("hash_algorithm is not sha256")
        if "event_log" not in quote_json or "prefix" not in quote_json:
            raise KeyError("missing live ingress quote fields")
        quote_bytes = bytes.fromhex(quote_hex)
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise EvidenceError(f"invalid quote.json: {exc}") from exc
    try:
        parsed_quote = quote_parser(quote_bytes)
        report_data = parsed_quote.body.report_data
    except Exception as exc:
        raise EvidenceError(f"cannot parse ingress quote: {exc}") from exc
    if not isinstance(report_data, bytes) or len(report_data) < 32:
        raise EvidenceError("ingress quote report_data is shorter than 32 bytes")
    manifest_digest = hashlib.sha256(manifest_bytes).digest()
    if not hmac.compare_digest(report_data[:32], manifest_digest):
        raise EvidenceError("quote report_data does not bind sha256sum.txt")

    return [
        f"peer certificate matches {cert_name}",
        "manifest hashes match all referenced evidence files",
        "quote report_data binds sha256sum.txt",
    ]


def _url_port(parts) -> int | None:
    try:
        return parts.port
    except ValueError as exc:
        raise EvidenceError("redirect must stay on the same HTTPS origin") from exc


def _resolve_same_origin_redirect(current_url: str, location: str, domain: str) -> str:
    """Resolve a redirect and reject every scheme/host/port origin change."""
    if not isinstance(location, str) or not location:
        raise EvidenceError("redirect missing Location header")
    target = urljoin(current_url, location)
    parts = urlsplit(target)
    if (
        parts.scheme != "https"
        or parts.hostname != domain
        or _url_port(parts) not in (None, 443)
        or parts.username is not None
        or parts.password is not None
    ):
        raise EvidenceError("redirect must stay on the same HTTPS origin")
    return urlunsplit(("https", domain, parts.path or "/", parts.query, ""))


def _read_bounded_response(response: Any, description: str) -> bytes:
    """Read a response body while rejecting the byte immediately over limit."""
    body = bytearray()
    while True:
        remaining_with_overflow_byte = MAX_RESPONSE_BYTES + 1 - len(body)
        chunk = response.read(min(_READ_CHUNK_BYTES, remaining_with_overflow_byte))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise EvidenceError(
                f"{description} exceeds {MAX_RESPONSE_BYTES} byte limit"
            )


def _https_get(url: str, domain: str) -> bytes:
    """Fetch a bounded HTTPS response, following only same-origin redirects."""
    normalized_domain = _validate_domain(domain)
    current_url = url
    context = ssl.create_default_context()
    for redirect_count in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current_url)
        if (
            parts.scheme != "https"
            or parts.hostname != normalized_domain
            or _url_port(parts) not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            raise EvidenceError("fetch URL must stay on the same HTTPS origin")
        request_target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection = http.client.HTTPSConnection(
            normalized_domain,
            443,
            timeout=CONNECT_TIMEOUT_SECONDS,
            context=context,
        )
        try:
            connection.connect()
            if connection.sock is None:
                raise EvidenceError("TLS connection did not create a socket")
            connection.sock.settimeout(RESPONSE_TIMEOUT_SECONDS)
            connection.request(
                "GET",
                request_target,
                headers={
                    "Accept": "application/json, text/plain, application/octet-stream"
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                if redirect_count == MAX_REDIRECTS:
                    raise EvidenceError(f"too many redirects fetching {url}")
                current_url = _resolve_same_origin_redirect(
                    current_url,
                    response.getheader("Location"),
                    normalized_domain,
                )
                continue
            if not 200 <= response.status < 300:
                raise EvidenceError(
                    f"HTTPS fetch failed for {current_url}: HTTP {response.status}"
                )
            return _read_bounded_response(response, current_url)
        except EvidenceError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise EvidenceError(f"HTTPS fetch failed for {current_url}: {exc}") from exc
        finally:
            connection.close()
    raise EvidenceError(f"too many redirects fetching {url}")


def _fetch_tls_peer_certificate(domain: str) -> bytes:
    """Capture the WebPKI-validated DER peer certificate for ``domain``."""
    normalized_domain = _validate_domain(domain)
    context = ssl.create_default_context()
    try:
        with socket.create_connection(
            (normalized_domain, 443), timeout=CONNECT_TIMEOUT_SECONDS
        ) as raw_socket:
            raw_socket.settimeout(CONNECT_TIMEOUT_SECONDS)
            with context.wrap_socket(
                raw_socket, server_hostname=normalized_domain
            ) as tls_socket:
                tls_socket.settimeout(RESPONSE_TIMEOUT_SECONDS)
                peer_der = tls_socket.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise EvidenceError(f"TLS peer certificate fetch failed: {exc}") from exc
    if not peer_der:
        raise EvidenceError("TLS peer did not provide a certificate")
    return peer_der


def _normalize_32_byte_hex(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceError(f"{field} must be a 32-byte hexadecimal value")
    unprefixed = value[2:] if value.startswith(("0x", "0X")) else value
    if _HEX_32_BYTES.fullmatch(unprefixed) is None:
        raise EvidenceError(f"{field} must be a 32-byte hexadecimal value")
    return unprefixed.lower()


def _verify_attestation(
    attestation: Mapping[str, Any],
    expected_compose_hash: str,
    expected_content_pk: str,
) -> list[str]:
    """Compare the live enclave attestation to operator-supplied expectations."""
    expected_compose = _normalize_32_byte_hex(
        expected_compose_hash, "expected compose_hash"
    )
    expected_pk = _normalize_32_byte_hex(
        expected_content_pk, "expected enclave_content_pk_hex"
    )
    actual_compose = _normalize_32_byte_hex(
        attestation.get("compose_hash"), "attestation compose_hash"
    )
    actual_pk = _normalize_32_byte_hex(
        attestation.get("enclave_content_pk_hex"),
        "attestation enclave_content_pk_hex",
    )
    if not hmac.compare_digest(actual_compose, expected_compose):
        raise EvidenceError("attestation compose_hash does not match expected value")
    if not hmac.compare_digest(actual_pk, expected_pk):
        raise EvidenceError(
            "attestation enclave_content_pk_hex does not match expected value"
        )
    if attestation.get("transport_mode") != "attested_ingress":
        raise EvidenceError("attestation transport_mode is not attested_ingress")
    return [
        "attestation compose_hash matches expected value",
        "attestation enclave_content_pk_hex matches expected value",
        "attestation transport_mode is attested_ingress",
    ]


def _load_json_object(data: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{description} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{description} JSON root is not an object")
    return value


def _run(
    domain: str,
    expected_compose_hash: str,
    expected_content_pk: str,
) -> list[str]:
    normalized_domain = _validate_domain(domain)
    evidence_base = f"https://{normalized_domain}/evidences/"
    files = {
        "sha256sum.txt": _https_get(evidence_base + "sha256sum.txt", normalized_domain),
        "quote.json": _https_get(evidence_base + "quote.json", normalized_domain),
    }
    try:
        manifest_text = files["sha256sum.txt"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("sha256sum.txt is not valid UTF-8") from exc
    for name in parse_sha256_manifest(manifest_text):
        if name not in files:
            files[name] = _https_get(
                evidence_base + urlquote(name, safe=""), normalized_domain
            )

    checks = verify_ingress_evidence(
        normalized_domain,
        _fetch_tls_peer_certificate(normalized_domain),
        files,
    )
    attestation_data = _https_get(
        f"https://{normalized_domain}/attestation", normalized_domain
    )
    attestation = _load_json_object(attestation_data, "attestation")
    checks.extend(
        _verify_attestation(
            attestation,
            expected_compose_hash,
            expected_content_pk,
        )
    )
    return checks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify dstack-ingress evidence for an enclave custom domain."
    )
    parser.add_argument("--domain", required=True, help="bare enclave DNS hostname")
    parser.add_argument("--expected-compose-hash", required=True)
    parser.add_argument("--expected-content-pk", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        checks = _run(
            args.domain,
            args.expected_compose_hash,
            args.expected_content_pk,
        )
    except EvidenceError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    for check in checks:
        print(f"PASS: {check}")
    print(
        "LIMITATION: structural quote parsing only; this tool does not validate "
        "the Intel DCAP signature chain. The client release gate must verify it separately."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
