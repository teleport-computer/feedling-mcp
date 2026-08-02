from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tools import verify_enclave_domain as verifier
from tools.verify_enclave_domain import (
    CONNECT_TIMEOUT_SECONDS,
    EvidenceError,
    MAX_RESPONSE_BYTES,
    RESPONSE_TIMEOUT_SECONDS,
    _fetch_tls_peer_certificate,
    _https_get,
    _read_bounded_response,
    _resolve_same_origin_redirect,
    _run,
    _validate_domain,
    _verify_attestation,
    parse_sha256_manifest,
    verify_ingress_evidence,
)


DOMAIN = "test-enclave.feedling.app"
CERT_NAME = f"cert-{DOMAIN}.pem"


@dataclass(frozen=True)
class EvidenceFixture:
    peer_der: bytes
    files: dict[str, bytes]
    quote_parser: object


@pytest.fixture
def evidence_fixture() -> EvidenceFixture:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DOMAIN)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(DOMAIN)]), False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    peer_der = cert.public_bytes(serialization.Encoding.DER)
    account = b"acme-account-evidence\n"
    manifest_bytes = (
        f"{hashlib.sha256(cert_pem).hexdigest()}  {CERT_NAME}\n"
        f"{hashlib.sha256(account).hexdigest()}  account.json\n"
    ).encode()
    quote_bytes = b"synthetic-quote"
    quote_json = json.dumps(
        {
            "quote": quote_bytes.hex(),
            "event_log": "[]",
            "hash_algorithm": "sha256",
            "prefix": "dstack-ingress",
        }
    ).encode()
    report_data = hashlib.sha256(manifest_bytes).digest() + bytes(32)

    def quote_parser(raw: bytes):
        if raw != quote_bytes:
            raise AssertionError("unexpected synthetic quote bytes")
        return SimpleNamespace(body=SimpleNamespace(report_data=report_data))

    return EvidenceFixture(
        peer_der=peer_der,
        files={
            "sha256sum.txt": manifest_bytes,
            "quote.json": quote_json,
            CERT_NAME: cert_pem,
            "account.json": account,
        },
        quote_parser=quote_parser,
    )


def test_manifest_rejects_path_traversal():
    with pytest.raises(EvidenceError, match="unsafe evidence filename"):
        parse_sha256_manifest("00" * 32 + "  ../cert.pem\n")


@pytest.mark.parametrize(
    "filename",
    [
        "/tmp/cert.pem",
        r"C:\\tmp\\cert.pem",
        "nested/cert.pem",
        r"nested\\cert.pem",
        ".",
        "..",
    ],
)
def test_manifest_rejects_unsafe_filenames(filename):
    with pytest.raises(EvidenceError, match="unsafe evidence filename"):
        parse_sha256_manifest("00" * 32 + f"  {filename}\n")


def test_manifest_rejects_duplicate_filename():
    text = "11" * 32 + "  cert.pem\n" + "22" * 32 + "  cert.pem\n"
    with pytest.raises(EvidenceError, match="duplicate evidence filename"):
        parse_sha256_manifest(text)


@pytest.mark.parametrize(
    "line",
    [
        "0" * 63 + "  cert.pem\n",
        "g" * 64 + "  cert.pem\n",
        "0" * 64 + " cert.pem\n",
        "0" * 64 + " *cert.pem\n",
        "\n",
    ],
)
def test_manifest_rejects_malformed_lines(line):
    with pytest.raises(EvidenceError, match="malformed sha256 manifest"):
        parse_sha256_manifest(line)


def test_evidence_rejects_peer_certificate_mismatch(evidence_fixture):
    with pytest.raises(EvidenceError, match="TLS peer certificate"):
        verify_ingress_evidence(
            domain="test-enclave.feedling.app",
            peer_der=b"different",
            files=evidence_fixture.files,
            quote_parser=evidence_fixture.quote_parser,
        )


def test_evidence_accepts_peer_manifest_and_report_data(evidence_fixture):
    checks = verify_ingress_evidence(
        domain="test-enclave.feedling.app",
        peer_der=evidence_fixture.peer_der,
        files=evidence_fixture.files,
        quote_parser=evidence_fixture.quote_parser,
    )
    assert checks == [
        "peer certificate matches cert-test-enclave.feedling.app.pem",
        "manifest hashes match all referenced evidence files",
        "quote report_data binds sha256sum.txt",
    ]


def test_evidence_rejects_missing_manifest_file(evidence_fixture):
    files = dict(evidence_fixture.files)
    del files["account.json"]
    with pytest.raises(
        EvidenceError, match="missing referenced evidence file: account.json"
    ):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


def test_evidence_requires_domain_certificate_in_manifest(evidence_fixture):
    files = dict(evidence_fixture.files)
    files["sha256sum.txt"] = (
        f"{hashlib.sha256(files['account.json']).hexdigest()}  account.json\n"
    ).encode()
    with pytest.raises(EvidenceError, match=f"manifest does not reference {CERT_NAME}"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


def test_evidence_rejects_manifest_hash_mismatch(evidence_fixture):
    files = dict(evidence_fixture.files)
    files["account.json"] = b"tampered"
    with pytest.raises(EvidenceError, match="evidence hash mismatch: account.json"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


def test_evidence_rejects_wrong_report_data_binding(evidence_fixture):
    def wrong_quote_parser(_raw: bytes):
        return SimpleNamespace(body=SimpleNamespace(report_data=bytes(64)))

    with pytest.raises(EvidenceError, match="quote report_data"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            evidence_fixture.files,
            wrong_quote_parser,
        )


@pytest.mark.parametrize(
    "domain",
    [
        "https://test-enclave.feedling.app",
        "test-enclave.feedling.app/path",
        "test-enclave.feedling.app:443",
        "user@test-enclave.feedling.app",
        "../test-enclave.feedling.app",
    ],
)
def test_domain_validation_rejects_non_bare_hostnames(domain):
    with pytest.raises(EvidenceError, match="bare DNS hostname"):
        _validate_domain(domain)


def test_redirect_resolution_accepts_only_same_https_origin():
    current = f"https://{DOMAIN}/evidences/sha256sum.txt"
    assert _resolve_same_origin_redirect(current, "/evidences/next", DOMAIN) == (
        f"https://{DOMAIN}/evidences/next"
    )
    with pytest.raises(EvidenceError, match="same HTTPS origin"):
        _resolve_same_origin_redirect(current, "https://evil.example/next", DOMAIN)
    with pytest.raises(EvidenceError, match="same HTTPS origin"):
        _resolve_same_origin_redirect(current, f"http://{DOMAIN}/next", DOMAIN)
    with pytest.raises(EvidenceError, match="same HTTPS origin"):
        _resolve_same_origin_redirect(current, f"https://{DOMAIN}:444/next", DOMAIN)


def test_response_body_rejects_more_than_one_mebibyte():
    class OversizedResponse:
        def __init__(self):
            self.remaining = MAX_RESPONSE_BYTES + 1

        def read(self, amount):
            size = min(amount, self.remaining)
            self.remaining -= size
            return b"x" * size

    with pytest.raises(EvidenceError, match="exceeds 1048576 byte limit"):
        _read_bounded_response(OversizedResponse(), "test response")


def test_https_fetch_applies_connect_and_response_timeouts(monkeypatch):
    observed = {}

    class FakeSocket:
        def settimeout(self, timeout):
            observed["response_timeout"] = timeout

    class FakeResponse:
        status = 200

        def read(self, _amount):
            return b""

    class FakeConnection:
        def __init__(self, host, port, timeout, context):
            observed.update(
                host=host, port=port, connect_timeout=timeout, context=context
            )
            self.sock = FakeSocket()

        def connect(self):
            observed["connected"] = True

        def request(self, method, target, headers):
            observed.update(method=method, target=target, headers=headers)

        def getresponse(self):
            return FakeResponse()

        def close(self):
            observed["closed"] = True

    tls_context = object()
    monkeypatch.setattr(verifier.ssl, "create_default_context", lambda: tls_context)
    monkeypatch.setattr(verifier.http.client, "HTTPSConnection", FakeConnection)

    assert _https_get(f"https://{DOMAIN}/attestation", DOMAIN) == b""
    assert observed["connect_timeout"] == CONNECT_TIMEOUT_SECONDS == 10
    assert observed["response_timeout"] == RESPONSE_TIMEOUT_SECONDS == 10
    assert observed["context"] is tls_context
    assert observed["target"] == "/attestation"
    assert observed["connected"] is True
    assert observed["closed"] is True


def test_tls_peer_capture_uses_default_context_and_ten_second_timeouts(monkeypatch):
    observed = {"raw_timeouts": [], "tls_timeouts": []}

    class FakeRawSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, timeout):
            observed["raw_timeouts"].append(timeout)

    class FakeTLSSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, timeout):
            observed["tls_timeouts"].append(timeout)

        def getpeercert(self, binary_form):
            assert binary_form is True
            return b"peer-der"

    class FakeContext:
        def wrap_socket(self, raw_socket, server_hostname):
            observed.update(raw_socket=raw_socket, server_hostname=server_hostname)
            return FakeTLSSocket()

    raw_socket = FakeRawSocket()

    def fake_create_connection(address, timeout):
        observed.update(address=address, socket_connect_timeout=timeout)
        return raw_socket

    context = FakeContext()
    monkeypatch.setattr(verifier.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(verifier.socket, "create_connection", fake_create_connection)

    assert _fetch_tls_peer_certificate(DOMAIN) == b"peer-der"
    assert observed["address"] == (DOMAIN, 443)
    assert observed["socket_connect_timeout"] == CONNECT_TIMEOUT_SECONDS == 10
    assert observed["raw_timeouts"] == [CONNECT_TIMEOUT_SECONDS]
    assert observed["tls_timeouts"] == [RESPONSE_TIMEOUT_SECONDS]
    assert observed["raw_socket"] is raw_socket
    assert observed["server_hostname"] == DOMAIN


def test_attestation_must_match_expected_environment():
    attestation = {
        "compose_hash": "12" * 32,
        "enclave_content_pk_hex": "34" * 32,
        "transport_mode": "attested_ingress",
    }
    assert _verify_attestation(attestation, "12" * 32, "34" * 32) == [
        "attestation compose_hash matches expected value",
        "attestation enclave_content_pk_hex matches expected value",
        "attestation transport_mode is attested_ingress",
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("compose_hash", "56" * 32, "compose_hash"),
        ("enclave_content_pk_hex", "78" * 32, "enclave_content_pk_hex"),
        ("transport_mode", "direct_tls", "transport_mode"),
    ],
)
def test_attestation_rejects_mismatches(field, value, message):
    attestation = {
        "compose_hash": "12" * 32,
        "enclave_content_pk_hex": "34" * 32,
        "transport_mode": "attested_ingress",
    }
    attestation[field] = value
    with pytest.raises(EvidenceError, match=message):
        _verify_attestation(attestation, "12" * 32, "34" * 32)


def test_run_fetches_every_manifest_file_and_attestation(monkeypatch, evidence_fixture):
    files = dict(evidence_fixture.files)
    quote = bytearray(48 + 584 + 4)
    quote[0:2] = (4).to_bytes(2, "little")
    quote[4:8] = (0x81).to_bytes(4, "little")
    quote[48 + 520 : 48 + 552] = hashlib.sha256(files["sha256sum.txt"]).digest()
    files["quote.json"] = json.dumps(
        {
            "quote": bytes(quote).hex(),
            "event_log": "[]",
            "hash_algorithm": "sha256",
            "prefix": "dstack-ingress",
        }
    ).encode()
    attestation = json.dumps(
        {
            "compose_hash": "12" * 32,
            "enclave_content_pk_hex": "34" * 32,
            "transport_mode": "attested_ingress",
        }
    ).encode()
    responses = {
        f"https://{DOMAIN}/evidences/{name}": content for name, content in files.items()
    }
    responses[f"https://{DOMAIN}/attestation"] = attestation
    requested = []

    def fake_https_get(url, domain):
        assert domain == DOMAIN
        requested.append(url)
        return responses[url]

    monkeypatch.setattr(verifier, "_https_get", fake_https_get)
    monkeypatch.setattr(
        verifier,
        "_fetch_tls_peer_certificate",
        lambda domain: evidence_fixture.peer_der,
    )

    assert _run(DOMAIN, "12" * 32, "34" * 32) == [
        f"peer certificate matches {CERT_NAME}",
        "manifest hashes match all referenced evidence files",
        "quote report_data binds sha256sum.txt",
        "attestation compose_hash matches expected value",
        "attestation enclave_content_pk_hex matches expected value",
        "attestation transport_mode is attested_ingress",
    ]
    assert requested == [
        f"https://{DOMAIN}/evidences/sha256sum.txt",
        f"https://{DOMAIN}/evidences/quote.json",
        f"https://{DOMAIN}/evidences/{CERT_NAME}",
        f"https://{DOMAIN}/evidences/account.json",
        f"https://{DOMAIN}/attestation",
    ]


def test_cli_prints_structural_dcap_limitation(monkeypatch, capsys):
    monkeypatch.setattr(verifier, "_run", lambda *_args: ["evidence verified"])
    result = verifier.main(
        [
            "--domain",
            DOMAIN,
            "--expected-compose-hash",
            "12" * 32,
            "--expected-content-pk",
            "34" * 32,
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "PASS: evidence verified" in output
    assert "does not validate the Intel DCAP signature chain" in output
    assert "client release gate must verify it separately" in output
