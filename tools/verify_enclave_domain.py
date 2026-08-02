#!/usr/bin/env python3
"""Verify dstack-ingress evidence served by an enclave custom domain.

This tool verifies evidence-file hashes, binds the live WebPKI TLS peer
certificate to the domain-specific evidence certificate, structurally parses
the TDX quote, and checks the enclave attestation values supplied by the
operator. It does not validate the Intel DCAP signature chain.

dstack-ingress 2.2 uses ``hash_algorithm=raw`` with an empty prefix because it
passes an already-computed 32-byte SHA-256 manifest digest to the quote API.
``raw`` does not mean that unhashed evidence is placed in REPORT_DATA.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import queue
import re
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
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
MAX_MANIFEST_ENTRIES = 16
MAX_AGGREGATE_EVIDENCE_BYTES = 4 * 1024 * 1024
OVERALL_RUN_TIMEOUT_SECONDS = 60
_READ_CHUNK_BYTES = 64 * 1024
_MANIFEST_LINE = re.compile(r"([0-9A-Fa-f]{64})  ([^\r\n]+)")
_SAFE_EVIDENCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_HEX_32_BYTES = re.compile(r"[0-9A-Fa-f]{64}")
_HEX_48_BYTES = re.compile(r"[0-9A-Fa-f]{96}")
_MEASUREMENT_FIELDS = ("mrtd", "rtmr0", "rtmr1", "rtmr2")
_CLAIMED_MEASUREMENT_FIELDS = _MEASUREMENT_FIELDS + ("rtmr3", "mr_config_id")
_INGRESS_QUOTE_PREFIX_BY_ALGORITHM = {
    "raw": "",
    "sha256": "dstack-ingress",
}
_monotonic = time.monotonic
_DCAP_LIMITATION = (
    "LIMITATION: structural quote parsing only; this tool does not validate "
    "the Intel DCAP signature chain. The client release gate must verify it separately."
)


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
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or any(
        separator in normalized
        for separator in (
            "\v",
            "\f",
            "\x1c",
            "\x1d",
            "\x1e",
            "\x85",
            "\u2028",
            "\u2029",
        )
    ):
        raise EvidenceError(
            "sha256 manifest must use canonical LF or CRLF line endings"
        )
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise EvidenceError("malformed sha256 manifest: empty manifest")
    if len(lines) > MAX_MANIFEST_ENTRIES:
        raise EvidenceError(
            f"sha256 manifest contains more than {MAX_MANIFEST_ENTRIES} entries"
        )

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


def _parse_ingress_quote(
    files: Mapping[str, bytes],
    quote_parser: Callable[[bytes], TDXQuote] = parse_quote,
) -> TDXQuote:
    quote_json_bytes = _required_file(files, "quote.json")
    try:
        quote_json = json.loads(quote_json_bytes.decode("utf-8"))
        if not isinstance(quote_json, dict):
            raise TypeError("quote.json root is not an object")
        quote_hex = quote_json["quote"]
        if (
            not isinstance(quote_hex, str)
            or not quote_hex
            or len(quote_hex) % 2
            or re.fullmatch(r"[0-9A-Fa-f]+", quote_hex) is None
        ):
            raise TypeError("quote must be non-empty, even-length hexadecimal text")
        hash_algorithm = quote_json["hash_algorithm"]
        if not isinstance(hash_algorithm, str):
            raise TypeError("hash_algorithm must be text")
        try:
            expected_prefix = _INGRESS_QUOTE_PREFIX_BY_ALGORITHM[hash_algorithm]
        except KeyError as exc:
            raise ValueError(f"unsupported hash_algorithm: {hash_algorithm!r}") from exc
        prefix = quote_json["prefix"]
        if not isinstance(prefix, str):
            raise TypeError("prefix must be text")
        if prefix != expected_prefix:
            raise ValueError(
                f"unsupported prefix for hash_algorithm {hash_algorithm!r}"
            )
        event_log_text = quote_json["event_log"]
        if not isinstance(event_log_text, str):
            raise TypeError("event_log must be text")
        try:
            event_log = json.loads(event_log_text)
        except json.JSONDecodeError as exc:
            raise ValueError("event_log must be valid JSON") from exc
        if not isinstance(event_log, list):
            raise TypeError("event_log JSON root must be a list")
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
        return quote_parser(quote_bytes)
    except Exception as exc:
        raise EvidenceError(f"cannot parse ingress quote: {exc}") from exc


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

    parsed_quote = _parse_ingress_quote(files, quote_parser)
    report_data = parsed_quote.body.report_data
    if not isinstance(report_data, bytes) or len(report_data) != 64:
        raise EvidenceError("ingress quote report_data must be exactly 64 bytes")
    manifest_digest = hashlib.sha256(manifest_bytes).digest()
    if not hmac.compare_digest(report_data[:32], manifest_digest):
        raise EvidenceError("quote report_data does not bind sha256sum.txt")
    if not hmac.compare_digest(report_data[32:], bytes(32)):
        raise EvidenceError("ingress quote report_data must use zero padding")

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
    try:
        target = urljoin(current_url, location)
        parts = urlsplit(target)
        hostname = parts.hostname
        port = _url_port(parts)
        username = parts.username
        password = parts.password
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError("malformed redirect URL") from exc
    if (
        parts.scheme != "https"
        or hostname != domain
        or port not in (None, 443)
        or username is not None
        or password is not None
    ):
        raise EvidenceError("redirect must stay on the same HTTPS origin")
    return urlunsplit(("https", domain, parts.path or "/", parts.query, ""))


def _raise_if_response_deadline_expired(
    deadline: float | None, description: str
) -> None:
    if deadline is not None and _monotonic() >= deadline:
        raise EvidenceError(
            "absolute response deadline exceeded after "
            f"{RESPONSE_TIMEOUT_SECONDS} seconds: {description}"
        )


def _read_bounded_response(
    response: Any,
    description: str,
    *,
    deadline: float | None = None,
) -> bytes:
    """Read a response body while rejecting the byte immediately over limit."""
    body = bytearray()
    while True:
        _raise_if_response_deadline_expired(deadline, description)
        remaining_with_overflow_byte = MAX_RESPONSE_BYTES + 1 - len(body)
        chunk = response.read(min(_READ_CHUNK_BYTES, remaining_with_overflow_byte))
        _raise_if_response_deadline_expired(deadline, description)
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise EvidenceError(
                f"{description} exceeds {MAX_RESPONSE_BYTES} byte limit"
            )


def _receive_response(
    connection: http.client.HTTPSConnection,
    description: str,
    deadline: float,
) -> tuple[int, str | None, bytes | None]:
    response = connection.getresponse()
    _raise_if_response_deadline_expired(deadline, description)
    if response.status in {301, 302, 303, 307, 308}:
        return response.status, response.getheader("Location"), None
    if not 200 <= response.status < 300:
        return response.status, None, None
    return (
        response.status,
        None,
        _read_bounded_response(response, description, deadline=deadline),
    )


def _receive_response_before_deadline(
    connection: http.client.HTTPSConnection,
    description: str,
    deadline: float,
) -> tuple[int, str | None, bytes | None]:
    """Interrupt a header/body drip at one absolute monotonic deadline."""
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def receive() -> None:
        try:
            result_queue.put(
                (True, _receive_response(connection, description, deadline))
            )
        except Exception as exc:  # noqa: BLE001 - propagate worker failure verbatim
            result_queue.put((False, exc))

    worker = threading.Thread(target=receive, daemon=True)
    worker.start()
    remaining = deadline - _monotonic()
    if remaining <= 0:
        _raise_if_response_deadline_expired(deadline, description)
    try:
        succeeded, result = result_queue.get(timeout=remaining)
    except queue.Empty as exc:
        raise EvidenceError(
            "absolute response deadline exceeded after "
            f"{RESPONSE_TIMEOUT_SECONDS} seconds: {description}"
        ) from exc
    _raise_if_response_deadline_expired(deadline, description)
    if not succeeded:
        raise result
    return result


def _raise_if_overall_deadline_expired(deadline: float, description: str) -> None:
    if _monotonic() >= deadline:
        raise EvidenceError(
            "overall verification deadline exceeded after "
            f"{OVERALL_RUN_TIMEOUT_SECONDS} seconds: {description}"
        )


def _remaining_timeout(deadline: float | None, maximum: float) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - _monotonic()
    if remaining <= 0:
        _raise_if_overall_deadline_expired(deadline, "network operation")
    return min(maximum, remaining)


def _call_before_overall_deadline(
    callback: Callable[[], Any], deadline: float, description: str
) -> Any:
    """Bound a complete blocking network operation by the run deadline."""
    _raise_if_overall_deadline_expired(deadline, description)
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def call() -> None:
        try:
            result = callback()
            _raise_if_overall_deadline_expired(deadline, description)
            result_queue.put((True, result))
        except Exception as exc:  # noqa: BLE001 - propagate worker failure verbatim
            result_queue.put((False, exc))

    threading.Thread(target=call, daemon=True).start()
    try:
        succeeded, result = result_queue.get(timeout=deadline - _monotonic())
    except queue.Empty as exc:
        raise EvidenceError(
            "overall verification deadline exceeded after "
            f"{OVERALL_RUN_TIMEOUT_SECONDS} seconds: {description}"
        ) from exc
    _raise_if_overall_deadline_expired(deadline, description)
    if not succeeded:
        raise result
    return result


def _https_get(
    url: str, domain: str, *, overall_deadline: float | None = None
) -> bytes:
    """Fetch a bounded HTTPS response, following only same-origin redirects."""
    normalized_domain = _validate_domain(domain)
    current_url = url
    context = ssl.create_default_context()
    for redirect_count in range(MAX_REDIRECTS + 1):
        if overall_deadline is not None:
            _raise_if_overall_deadline_expired(overall_deadline, current_url)
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
            timeout=_remaining_timeout(overall_deadline, CONNECT_TIMEOUT_SECONDS),
            context=context,
        )
        try:
            connection.connect()
            if overall_deadline is not None:
                _raise_if_overall_deadline_expired(overall_deadline, current_url)
            if connection.sock is None:
                raise EvidenceError("TLS connection did not create a socket")
            connection.sock.settimeout(
                _remaining_timeout(overall_deadline, RESPONSE_TIMEOUT_SECONDS)
            )
            connection.request(
                "GET",
                request_target,
                headers={
                    "Accept": "application/json, text/plain, application/octet-stream"
                },
            )
            if overall_deadline is not None:
                _raise_if_overall_deadline_expired(overall_deadline, current_url)
            deadline = _monotonic() + RESPONSE_TIMEOUT_SECONDS
            if overall_deadline is not None:
                deadline = min(deadline, overall_deadline)
            status, location, body = _receive_response_before_deadline(
                connection, current_url, deadline
            )
            if overall_deadline is not None:
                _raise_if_overall_deadline_expired(overall_deadline, current_url)
            if status in {301, 302, 303, 307, 308}:
                if redirect_count == MAX_REDIRECTS:
                    raise EvidenceError(f"too many redirects fetching {url}")
                current_url = _resolve_same_origin_redirect(
                    current_url,
                    location,
                    normalized_domain,
                )
                continue
            if not 200 <= status < 300:
                raise EvidenceError(
                    f"HTTPS fetch failed for {current_url}: HTTP {status}"
                )
            if body is None:
                raise EvidenceError(f"HTTPS fetch returned no body for {current_url}")
            return body
        except EvidenceError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise EvidenceError(f"HTTPS fetch failed for {current_url}: {exc}") from exc
        finally:
            connection.close()
    raise EvidenceError(f"too many redirects fetching {url}")


def _fetch_tls_peer_certificate(
    domain: str, *, overall_deadline: float | None = None
) -> bytes:
    """Capture the WebPKI-validated DER peer certificate for ``domain``."""
    normalized_domain = _validate_domain(domain)
    context = ssl.create_default_context()
    if overall_deadline is not None:
        _raise_if_overall_deadline_expired(overall_deadline, "TLS peer certificate")
    try:
        with socket.create_connection(
            (normalized_domain, 443),
            timeout=_remaining_timeout(overall_deadline, CONNECT_TIMEOUT_SECONDS),
        ) as raw_socket:
            if overall_deadline is not None:
                _raise_if_overall_deadline_expired(
                    overall_deadline, "TLS peer certificate"
                )
            raw_socket.settimeout(
                _remaining_timeout(overall_deadline, CONNECT_TIMEOUT_SECONDS)
            )
            with context.wrap_socket(
                raw_socket, server_hostname=normalized_domain
            ) as tls_socket:
                if overall_deadline is not None:
                    _raise_if_overall_deadline_expired(
                        overall_deadline, "TLS peer certificate"
                    )
                tls_socket.settimeout(
                    _remaining_timeout(overall_deadline, RESPONSE_TIMEOUT_SECONDS)
                )
                peer_der = tls_socket.getpeercert(binary_form=True)
                if overall_deadline is not None:
                    _raise_if_overall_deadline_expired(
                        overall_deadline, "TLS peer certificate"
                    )
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


def _normalize_48_byte_hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HEX_48_BYTES.fullmatch(value) is None:
        raise EvidenceError(f"{field} must be a 48-byte hexadecimal value")
    return value.lower()


def _validate_reference_measurements(
    reference: Mapping[str, Any],
    domain: str,
    expected_compose_hash: str,
    expected_content_pk: str,
) -> dict[str, dict[str, str]]:
    if not isinstance(reference, Mapping):
        raise EvidenceError("reference measurements root must be an object")
    if type(reference.get("version")) is not int or reference.get("version") != 1:
        raise EvidenceError("reference measurements version must be 1")
    reference_domain = _validate_domain(reference.get("domain"))
    if reference_domain != domain:
        raise EvidenceError("reference domain does not match CLI domain")
    reference_compose = _normalize_32_byte_hex(
        reference.get("expected_compose_hash"),
        "reference expected_compose_hash",
    )
    reference_pk = _normalize_32_byte_hex(
        reference.get("expected_content_pk_hex"),
        "reference expected_content_pk_hex",
    )
    if not hmac.compare_digest(
        reference_compose,
        _normalize_32_byte_hex(expected_compose_hash, "expected compose_hash"),
    ):
        raise EvidenceError("reference expected_compose_hash does not match CLI value")
    if not hmac.compare_digest(
        reference_pk,
        _normalize_32_byte_hex(expected_content_pk, "expected enclave_content_pk_hex"),
    ):
        raise EvidenceError(
            "reference expected_content_pk_hex does not match CLI value"
        )

    approved: dict[str, dict[str, str]] = {}
    for workload in ("ingress", "enclave"):
        values = reference.get(workload)
        if not isinstance(values, Mapping):
            raise EvidenceError(f"reference {workload} measurements must be an object")
        approved[workload] = {
            field: _normalize_48_byte_hex(
                values.get(field), f"reference {workload}.{field}"
            )
            for field in _MEASUREMENT_FIELDS
        }
    return approved


def _verify_approved_quote_measurements(
    quote: TDXQuote, expected: Mapping[str, str], workload: str
) -> None:
    for field in _MEASUREMENT_FIELDS:
        actual = getattr(quote.body, field).hex()
        if not hmac.compare_digest(actual, expected[field]):
            raise EvidenceError(f"approved {workload} measurement mismatch: {field}")


def _parse_enclave_quote(attestation: Mapping[str, Any]) -> TDXQuote:
    quote_hex = attestation.get("tdx_quote_hex")
    if not isinstance(quote_hex, str):
        raise EvidenceError("attestation tdx_quote_hex must be hexadecimal text")
    try:
        return parse_quote(bytes.fromhex(quote_hex))
    except Exception as exc:
        raise EvidenceError(f"cannot parse enclave quote: {exc}") from exc


def _verify_attestation(
    attestation: Mapping[str, Any],
    expected_compose_hash: str,
    expected_content_pk: str,
    approved_measurements: Mapping[str, str],
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

    quote = _parse_enclave_quote(attestation)
    claimed_measurements = attestation.get("measurements")
    if not isinstance(claimed_measurements, Mapping):
        raise EvidenceError("attestation measurements must be an object")
    for field in _CLAIMED_MEASUREMENT_FIELDS:
        claimed = _normalize_48_byte_hex(
            claimed_measurements.get(field), f"attestation measurements.{field}"
        )
        quote_field = "mrconfig_id" if field == "mr_config_id" else field
        actual = getattr(quote.body, quote_field).hex()
        if not hmac.compare_digest(actual, claimed):
            raise EvidenceError(f"quote measurement mismatch: {field}")
    _verify_approved_quote_measurements(quote, approved_measurements, "enclave")

    tls_fingerprint = _normalize_32_byte_hex(
        attestation.get("enclave_tls_cert_fingerprint_hex"),
        "attestation enclave_tls_cert_fingerprint_hex",
    )
    if attestation.get("report_data_version") != 1:
        raise EvidenceError("attestation report_data_version is not 1")
    tls_fingerprint_bytes = bytes.fromhex(tls_fingerprint)
    expected_report_data = (
        hashlib.sha256(
            bytes.fromhex(actual_pk) + tls_fingerprint_bytes + b"feedling-v1"
        ).digest()
        + b"\x01"
        + (b"\x01" if tls_fingerprint_bytes == bytes(32) else b"\x00")
        + bytes(30)
    )
    if not hmac.compare_digest(quote.body.report_data, expected_report_data):
        raise EvidenceError(
            "enclave quote report_data does not bind content key and listener TLS mode"
        )

    expected_mr_config_id = b"\x01" + bytes.fromhex(expected_compose) + bytes(15)
    if not hmac.compare_digest(quote.body.mrconfig_id, expected_mr_config_id):
        raise EvidenceError("enclave mr_config_id does not bind expected compose_hash")
    return [
        "attestation compose_hash matches expected value",
        "attestation enclave_content_pk_hex matches expected value",
        "attestation transport_mode is attested_ingress",
        "enclave quote matches attestation measurements",
        "enclave quote matches approved reference measurements",
        "enclave report_data binds content key and listener TLS mode",
        "enclave mr_config_id binds expected compose_hash",
    ]


def _load_json_object(data: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{description} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{description} JSON root is not an object")
    return value


def _load_reference_measurements_file(path: str) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as reference_file:
            data = reference_file.read(MAX_RESPONSE_BYTES + 1)
    except OSError as exc:
        raise EvidenceError(f"cannot read reference measurements file: {exc}") from exc
    if len(data) > MAX_RESPONSE_BYTES:
        raise EvidenceError(
            f"reference measurements file exceeds {MAX_RESPONSE_BYTES} byte limit"
        )
    return _load_json_object(data, "reference measurements")


def _run(
    domain: str,
    expected_compose_hash: str,
    expected_content_pk: str,
    reference_measurements: Mapping[str, Any],
) -> list[str]:
    normalized_domain = _validate_domain(domain)
    approved = _validate_reference_measurements(
        reference_measurements,
        normalized_domain,
        expected_compose_hash,
        expected_content_pk,
    )
    overall_deadline = _monotonic() + OVERALL_RUN_TIMEOUT_SECONDS
    evidence_base = f"https://{normalized_domain}/evidences/"
    aggregate_bytes = 0
    files: dict[str, bytes] = {}

    def fetch(url: str, description: str) -> bytes:
        return _call_before_overall_deadline(
            lambda: _https_get(
                url,
                normalized_domain,
                overall_deadline=overall_deadline,
            ),
            overall_deadline,
            description,
        )

    def retain(name: str, data: bytes) -> None:
        nonlocal aggregate_bytes
        aggregate_bytes += len(data)
        if aggregate_bytes > MAX_AGGREGATE_EVIDENCE_BYTES:
            raise EvidenceError(
                "aggregate evidence exceeds "
                f"{MAX_AGGREGATE_EVIDENCE_BYTES} bytes while retaining {name}"
            )
        files[name] = data

    retain(
        "sha256sum.txt",
        fetch(evidence_base + "sha256sum.txt", "sha256sum.txt fetch"),
    )
    retain("quote.json", fetch(evidence_base + "quote.json", "quote.json fetch"))
    try:
        manifest_text = files["sha256sum.txt"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("sha256sum.txt is not valid UTF-8") from exc
    for name in parse_sha256_manifest(manifest_text):
        if name not in files:
            retain(
                name,
                fetch(
                    evidence_base + urlquote(name, safe=""),
                    f"evidence fetch: {name}",
                ),
            )

    peer_der = _call_before_overall_deadline(
        lambda: _fetch_tls_peer_certificate(
            normalized_domain, overall_deadline=overall_deadline
        ),
        overall_deadline,
        "TLS peer certificate fetch",
    )
    aggregate_bytes += len(peer_der)
    if aggregate_bytes > MAX_AGGREGATE_EVIDENCE_BYTES:
        raise EvidenceError(
            f"aggregate evidence exceeds {MAX_AGGREGATE_EVIDENCE_BYTES} bytes "
            "while retaining TLS peer certificate"
        )
    checks = verify_ingress_evidence(
        normalized_domain,
        peer_der,
        files,
    )
    ingress_quote = _parse_ingress_quote(files)
    _verify_approved_quote_measurements(ingress_quote, approved["ingress"], "ingress")
    checks.append("ingress quote matches approved reference measurements")

    attestation_data = fetch(
        f"https://{normalized_domain}/attestation", "attestation fetch"
    )
    aggregate_bytes += len(attestation_data)
    if aggregate_bytes > MAX_AGGREGATE_EVIDENCE_BYTES:
        raise EvidenceError(
            f"aggregate evidence exceeds {MAX_AGGREGATE_EVIDENCE_BYTES} bytes "
            "while retaining attestation"
        )
    attestation = _load_json_object(attestation_data, "attestation")
    checks.extend(
        _verify_attestation(
            attestation,
            expected_compose_hash,
            expected_content_pk,
            approved["enclave"],
        )
    )
    _raise_if_overall_deadline_expired(overall_deadline, "verification completion")
    return checks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify dstack-ingress evidence for an enclave custom domain."
    )
    parser.add_argument("--domain", required=True, help="bare enclave DNS hostname")
    parser.add_argument("--expected-compose-hash", required=True)
    parser.add_argument("--expected-content-pk", required=True)
    parser.add_argument(
        "--reference-measurements",
        required=True,
        help="operator-approved versioned JSON measurement policy",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    print(_DCAP_LIMITATION)
    args = _build_parser().parse_args(argv)
    try:
        reference_measurements = _load_reference_measurements_file(
            args.reference_measurements
        )
        checks = _run(
            args.domain,
            args.expected_compose_hash,
            args.expected_content_pk,
            reference_measurements,
        )
    except EvidenceError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    for check in checks:
        print(f"PASS: {check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
