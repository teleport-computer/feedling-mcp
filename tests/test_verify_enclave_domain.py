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
COMPOSE_HASH = "12" * 32
CONTENT_PK = "34" * 32
INGRESS_MEASUREMENTS = {
    "mrtd": "10" * 48,
    "rtmr0": "11" * 48,
    "rtmr1": "12" * 48,
    "rtmr2": "13" * 48,
}
ENCLAVE_MEASUREMENTS = {
    "mrtd": "20" * 48,
    "rtmr0": "21" * 48,
    "rtmr1": "22" * 48,
    "rtmr2": "23" * 48,
}


def _reference_measurements(**overrides):
    reference = {
        "version": 1,
        "status": "APPROVED_REFERENCE",
        "approved_by": "security@example.com",
        "approved_at": "2026-08-03T00:00:00Z",
        "domain": DOMAIN,
        "expected_compose_hash": COMPOSE_HASH,
        "expected_content_pk_hex": CONTENT_PK,
        "ingress": dict(INGRESS_MEASUREMENTS),
        "enclave": dict(ENCLAVE_MEASUREMENTS),
    }
    reference.update(overrides)
    return reference


def _make_tdx_quote(
    *,
    report_data: bytes,
    measurements: dict[str, str],
    rtmr3: str = "24" * 48,
    mr_config_id: str | None = None,
) -> bytes:
    quote = bytearray(48 + 584 + 4)
    quote[0:2] = (4).to_bytes(2, "little")
    quote[4:8] = (0x81).to_bytes(4, "little")
    body = 48
    quote[body + 136 : body + 184] = bytes.fromhex(measurements["mrtd"])
    quote[body + 184 : body + 232] = bytes.fromhex(
        mr_config_id or "01" + COMPOSE_HASH + "00" * 15
    )
    quote[body + 328 : body + 376] = bytes.fromhex(measurements["rtmr0"])
    quote[body + 376 : body + 424] = bytes.fromhex(measurements["rtmr1"])
    quote[body + 424 : body + 472] = bytes.fromhex(measurements["rtmr2"])
    quote[body + 472 : body + 520] = bytes.fromhex(rtmr3)
    quote[body + 520 : body + 584] = report_data
    return bytes(quote)


def _enclave_report_data(content_pk_hex=CONTENT_PK, tls_fingerprint_hex="00" * 32):
    binding = hashlib.sha256(
        bytes.fromhex(content_pk_hex)
        + bytes.fromhex(tls_fingerprint_hex)
        + b"feedling-v1"
    ).digest()
    flag = b"\x01" if tls_fingerprint_hex == "00" * 32 else b"\x00"
    return binding + b"\x01" + flag + bytes(30)


def _attestation_bundle(
    *, measurements=None, quote_measurements=None, report_data=None, mr_config_id=None
):
    claimed = dict(measurements or ENCLAVE_MEASUREMENTS)
    claimed["rtmr3"] = "24" * 48
    claimed["mr_config_id"] = mr_config_id or "01" + COMPOSE_HASH + "00" * 15
    quote_claims = dict(quote_measurements or ENCLAVE_MEASUREMENTS)
    quote = _make_tdx_quote(
        report_data=report_data or _enclave_report_data(),
        measurements=quote_claims,
        rtmr3=claimed["rtmr3"],
        mr_config_id=claimed["mr_config_id"],
    )
    return {
        "tdx_quote_hex": quote.hex(),
        "measurements": claimed,
        "compose_hash": COMPOSE_HASH,
        "enclave_content_pk_hex": CONTENT_PK,
        "enclave_tls_cert_fingerprint_hex": "00" * 32,
        "report_data_version": 1,
        "transport_mode": "attested_ingress",
    }


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


def test_manifest_rejects_seventeenth_entry():
    manifest = "".join(f"{'00' * 32}  evidence-{index}.bin\n" for index in range(17))
    with pytest.raises(EvidenceError, match="more than 16 entries"):
        parse_sha256_manifest(manifest)


@pytest.mark.parametrize("separator", ["\r", "\v", "\f", "\x85", "\u2028"])
def test_manifest_rejects_noncanonical_line_separator(separator):
    manifest = f"{'00' * 32}  cert.pem{separator}{'11' * 32}  account.json\n"
    with pytest.raises(EvidenceError, match="canonical LF or CRLF"):
        parse_sha256_manifest(manifest)


def test_manifest_accepts_canonical_lf_and_crlf():
    assert parse_sha256_manifest(
        f"{'00' * 32}  cert.pem\r\n{'11' * 32}  account.json\n"
    ) == {"cert.pem": "00" * 32, "account.json": "11" * 32}


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


def test_evidence_accepts_live_dstack_ingress_raw_prehash_contract(
    evidence_fixture,
):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json.update(hash_algorithm="raw", prefix="")
    files["quote.json"] = json.dumps(quote_json).encode()

    checks = verify_ingress_evidence(
        DOMAIN,
        evidence_fixture.peer_der,
        files,
        evidence_fixture.quote_parser,
    )

    assert "quote report_data binds sha256sum.txt" in checks


@pytest.mark.parametrize(
    ("hash_algorithm", "prefix"),
    [
        ("raw", "attacker-controlled"),
        ("sha256", "attacker-controlled"),
        ("raw", None),
        ("sha256", 1),
    ],
)
def test_evidence_rejects_unsafe_ingress_quote_prefix(
    evidence_fixture, hash_algorithm, prefix
):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json.update(hash_algorithm=hash_algorithm, prefix=prefix)
    files["quote.json"] = json.dumps(quote_json).encode()

    with pytest.raises(EvidenceError, match="prefix"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


def test_evidence_rejects_unknown_ingress_quote_hash_algorithm(evidence_fixture):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json.update(hash_algorithm="sha512", prefix="")
    files["quote.json"] = json.dumps(quote_json).encode()

    with pytest.raises(EvidenceError, match="unsupported hash_algorithm"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quote", None, "quote"),
        ("quote", "not-hex", "quote"),
        ("quote", "a", "quote"),
        ("hash_algorithm", None, "hash_algorithm"),
    ],
)
def test_evidence_rejects_malformed_ingress_quote_fields(
    evidence_fixture, field, value, message
):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json[field] = value
    files["quote.json"] = json.dumps(quote_json).encode()

    with pytest.raises(EvidenceError, match=message):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


@pytest.mark.parametrize("event_log", [None, 1, {}, []])
def test_evidence_rejects_malformed_ingress_quote_event_log(
    evidence_fixture, event_log
):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json["event_log"] = event_log
    files["quote.json"] = json.dumps(quote_json).encode()

    with pytest.raises(EvidenceError, match="event_log"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


def test_evidence_rejects_ingress_event_log_with_invalid_json_text(evidence_fixture):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json["event_log"] = "not-json"
    files["quote.json"] = json.dumps(quote_json).encode()

    with pytest.raises(EvidenceError, match="event_log.*valid JSON"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


def test_evidence_rejects_ingress_event_log_with_non_list_json_root(
    evidence_fixture,
):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json["event_log"] = '{"event": "not-a-list"}'
    files["quote.json"] = json.dumps(quote_json).encode()

    with pytest.raises(EvidenceError, match="event_log.*list"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            evidence_fixture.quote_parser,
        )


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
    "report_data",
    [
        hashlib.sha256(b"different manifest").digest() + bytes(32),
        bytes(64),
    ],
)
def test_evidence_rejects_malformed_report_data_digest(evidence_fixture, report_data):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json.update(hash_algorithm="raw", prefix="")
    files["quote.json"] = json.dumps(quote_json).encode()

    def quote_parser(_raw: bytes):
        return SimpleNamespace(body=SimpleNamespace(report_data=report_data))

    with pytest.raises(EvidenceError, match="does not bind sha256sum.txt"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            quote_parser,
        )


@pytest.mark.parametrize(
    "report_data",
    [
        hashlib.sha256(b"placeholder").digest(),
        hashlib.sha256(b"placeholder").digest() + bytes(31),
        hashlib.sha256(b"placeholder").digest() + bytes(33),
    ],
)
def test_evidence_requires_exact_64_byte_ingress_report_data(
    evidence_fixture, report_data
):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json.update(hash_algorithm="raw", prefix="")
    files["quote.json"] = json.dumps(quote_json).encode()
    manifest_digest = hashlib.sha256(files["sha256sum.txt"]).digest()
    shaped_report_data = manifest_digest + report_data[32:]

    def quote_parser(_raw: bytes):
        return SimpleNamespace(body=SimpleNamespace(report_data=shaped_report_data))

    with pytest.raises(EvidenceError, match="exactly 64 bytes"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            quote_parser,
        )


def test_evidence_rejects_nonzero_ingress_report_data_padding(evidence_fixture):
    files = dict(evidence_fixture.files)
    quote_json = json.loads(files["quote.json"])
    quote_json.update(hash_algorithm="raw", prefix="")
    files["quote.json"] = json.dumps(quote_json).encode()
    manifest_digest = hashlib.sha256(files["sha256sum.txt"]).digest()

    def quote_parser(_raw: bytes):
        return SimpleNamespace(
            body=SimpleNamespace(report_data=manifest_digest + bytes(31) + b"\x01")
        )

    with pytest.raises(EvidenceError, match="zero padding"):
        verify_ingress_evidence(
            DOMAIN,
            evidence_fixture.peer_der,
            files,
            quote_parser,
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


def test_redirect_resolution_normalizes_malformed_url_error():
    current = f"https://{DOMAIN}/evidences/sha256sum.txt"
    with pytest.raises(EvidenceError, match="malformed redirect URL"):
        _resolve_same_origin_redirect(current, "https://[invalid", DOMAIN)


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


def test_https_fetch_rejects_headers_after_absolute_response_deadline(monkeypatch):
    class FakeClock:
        now = 0.0

        def __call__(self):
            return self.now

    class FakeSocket:
        def settimeout(self, _timeout):
            pass

    class FakeResponse:
        status = 200

        def read(self, _amount):
            return b""

    clock = FakeClock()

    class FakeConnection:
        sock = FakeSocket()

        def __init__(self, *_args, **_kwargs):
            pass

        def connect(self):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            clock.now = RESPONSE_TIMEOUT_SECONDS + 0.1
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(verifier, "_monotonic", clock, raising=False)
    monkeypatch.setattr(verifier.ssl, "create_default_context", object)
    monkeypatch.setattr(verifier.http.client, "HTTPSConnection", FakeConnection)

    with pytest.raises(EvidenceError, match="absolute response deadline exceeded"):
        _https_get(f"https://{DOMAIN}/attestation", DOMAIN)


def test_https_fetch_rejects_drip_fed_body_after_absolute_deadline(monkeypatch):
    class FakeClock:
        now = 0.0

        def __call__(self):
            return self.now

    class FakeSocket:
        def settimeout(self, _timeout):
            pass

    clock = FakeClock()

    class DripResponse:
        status = 200
        chunks = 0

        def read(self, _amount):
            self.chunks += 1
            clock.now += 4.0
            return b"x" if self.chunks <= 3 else b""

    class FakeConnection:
        sock = FakeSocket()

        def __init__(self, *_args, **_kwargs):
            pass

        def connect(self):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return DripResponse()

        def close(self):
            pass

    monkeypatch.setattr(verifier, "_monotonic", clock, raising=False)
    monkeypatch.setattr(verifier.ssl, "create_default_context", object)
    monkeypatch.setattr(verifier.http.client, "HTTPSConnection", FakeConnection)

    with pytest.raises(EvidenceError, match="absolute response deadline exceeded"):
        _https_get(f"https://{DOMAIN}/attestation", DOMAIN)


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
    assert _verify_attestation(
        _attestation_bundle(),
        COMPOSE_HASH,
        CONTENT_PK,
        ENCLAVE_MEASUREMENTS,
    ) == [
        "attestation compose_hash matches expected value",
        "attestation enclave_content_pk_hex matches expected value",
        "attestation transport_mode is attested_ingress",
        "enclave quote matches attestation measurements",
        "enclave quote matches approved reference measurements",
        "enclave report_data binds content key and listener TLS mode",
        "enclave mr_config_id binds expected compose_hash",
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
    attestation = _attestation_bundle()
    attestation[field] = value
    with pytest.raises(EvidenceError, match=message):
        _verify_attestation(
            attestation,
            COMPOSE_HASH,
            CONTENT_PK,
            ENCLAVE_MEASUREMENTS,
        )


def test_attestation_rejects_quote_measurement_mismatch():
    attestation = _attestation_bundle(
        quote_measurements={**ENCLAVE_MEASUREMENTS, "mrtd": "ff" * 48}
    )
    with pytest.raises(EvidenceError, match="quote measurement mismatch: mrtd"):
        _verify_attestation(
            attestation,
            COMPOSE_HASH,
            CONTENT_PK,
            ENCLAVE_MEASUREMENTS,
        )


def test_attestation_rejects_unapproved_base_measurement():
    approved = {**ENCLAVE_MEASUREMENTS, "rtmr1": "ff" * 48}
    with pytest.raises(
        EvidenceError, match="approved enclave measurement mismatch: rtmr1"
    ):
        _verify_attestation(
            _attestation_bundle(),
            COMPOSE_HASH,
            CONTENT_PK,
            approved,
        )


def test_attestation_rejects_report_data_content_key_mismatch():
    attestation = _attestation_bundle(report_data=bytes(64))
    with pytest.raises(EvidenceError, match="report_data does not bind content key"):
        _verify_attestation(
            attestation,
            COMPOSE_HASH,
            CONTENT_PK,
            ENCLAVE_MEASUREMENTS,
        )


def test_attestation_rejects_mr_config_id_compose_mismatch():
    attestation = _attestation_bundle(mr_config_id="01" + "ff" * 32 + "00" * 15)
    with pytest.raises(
        EvidenceError, match="mr_config_id does not bind expected compose_hash"
    ):
        _verify_attestation(
            attestation,
            COMPOSE_HASH,
            CONTENT_PK,
            ENCLAVE_MEASUREMENTS,
        )


def test_run_accepts_approved_reference_and_fetches_all_evidence(
    monkeypatch, evidence_fixture
):
    files = dict(evidence_fixture.files)
    quote = _make_tdx_quote(
        report_data=hashlib.sha256(files["sha256sum.txt"]).digest() + bytes(32),
        measurements=INGRESS_MEASUREMENTS,
    )
    files["quote.json"] = json.dumps(
        {
            "quote": quote.hex(),
            "event_log": "[]",
            "hash_algorithm": "sha256",
            "prefix": "dstack-ingress",
        }
    ).encode()
    attestation = json.dumps(_attestation_bundle()).encode()
    responses = {
        f"https://{DOMAIN}/evidences/{name}": content for name, content in files.items()
    }
    responses[f"https://{DOMAIN}/attestation"] = attestation
    requested = []

    def fake_https_get(url, domain, **_kwargs):
        assert domain == DOMAIN
        requested.append(url)
        return responses[url]

    monkeypatch.setattr(verifier, "_https_get", fake_https_get)
    monkeypatch.setattr(
        verifier,
        "_fetch_tls_peer_certificate",
        lambda domain, **_kwargs: evidence_fixture.peer_der,
    )

    assert _run(
        DOMAIN,
        COMPOSE_HASH,
        CONTENT_PK,
        _reference_measurements(),
    ) == [
        f"peer certificate matches {CERT_NAME}",
        "manifest hashes match all referenced evidence files",
        "quote report_data binds sha256sum.txt",
        "ingress quote matches approved reference measurements",
        "ingress mr_config_id binds expected compose_hash",
        "attestation compose_hash matches expected value",
        "attestation enclave_content_pk_hex matches expected value",
        "attestation transport_mode is attested_ingress",
        "enclave quote matches attestation measurements",
        "enclave quote matches approved reference measurements",
        "enclave report_data binds content key and listener TLS mode",
        "enclave mr_config_id binds expected compose_hash",
    ]
    assert requested == [
        f"https://{DOMAIN}/evidences/sha256sum.txt",
        f"https://{DOMAIN}/evidences/quote.json",
        f"https://{DOMAIN}/evidences/{CERT_NAME}",
        f"https://{DOMAIN}/evidences/account.json",
        f"https://{DOMAIN}/attestation",
    ]


def test_run_rejects_reference_metadata_mismatch(monkeypatch):
    monkeypatch.setattr(
        verifier,
        "_https_get",
        lambda *_args, **_kwargs: pytest.fail(
            "network must not run before reference validation"
        ),
    )
    with pytest.raises(EvidenceError, match="reference domain"):
        _run(
            DOMAIN,
            COMPOSE_HASH,
            CONTENT_PK,
            _reference_measurements(domain="pre-enclave.feedling.app"),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", None, "status"),
        ("status", "UNAPPROVED_CANDIDATE", "status"),
        ("approved_by", None, "approved_by"),
        ("approved_by", "   ", "approved_by"),
        ("approved_by", 7, "approved_by"),
        ("approved_at", None, "approved_at"),
        ("approved_at", "not-a-timestamp", "approved_at"),
        ("approved_at", "2026-02-30T00:00:00Z", "approved_at"),
        ("approved_at", "2026-08-03T00:00:00+00:00", "approved_at"),
    ],
)
def test_run_rejects_unapproved_reference_before_network(
    monkeypatch, field, value, message
):
    network_calls = []

    def unexpected_network(*_args, **_kwargs):
        network_calls.append(True)
        raise EvidenceError("network reached before reference approval validation")

    monkeypatch.setattr(verifier, "_https_get", unexpected_network)
    reference = _reference_measurements()
    if value is None:
        reference.pop(field)
    else:
        reference[field] = value

    with pytest.raises(EvidenceError, match=message):
        _run(DOMAIN, COMPOSE_HASH, CONTENT_PK, reference)
    assert network_calls == []


def test_run_rejects_more_than_four_mebibytes_of_aggregate_evidence(
    monkeypatch, evidence_fixture
):
    files = dict(evidence_fixture.files)
    for index in range(4):
        files[f"large-{index}.bin"] = bytes(800 * 1024)
    files["sha256sum.txt"] = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in files.items()
        if name not in {"sha256sum.txt", "quote.json"}
    ).encode()
    files["quote.json"] = json.dumps(
        {
            "quote": _make_tdx_quote(
                report_data=hashlib.sha256(files["sha256sum.txt"]).digest() + bytes(32),
                measurements=INGRESS_MEASUREMENTS,
            ).hex(),
            "event_log": "[]",
            "hash_algorithm": "sha256",
            "prefix": "dstack-ingress",
        }
    ).encode()
    responses = {
        f"https://{DOMAIN}/evidences/{name}": content for name, content in files.items()
    }
    oversized_aggregate_attestation = _attestation_bundle()
    oversized_aggregate_attestation["padding"] = "x" * (900 * 1024)
    responses[f"https://{DOMAIN}/attestation"] = json.dumps(
        oversized_aggregate_attestation
    ).encode()

    monkeypatch.setattr(
        verifier, "_https_get", lambda url, _domain, **_kwargs: responses[url]
    )
    monkeypatch.setattr(
        verifier,
        "_fetch_tls_peer_certificate",
        lambda *_args, **_kwargs: evidence_fixture.peer_der,
    )

    with pytest.raises(EvidenceError, match="aggregate evidence exceeds 4194304 bytes"):
        _run(DOMAIN, COMPOSE_HASH, CONTENT_PK, _reference_measurements())


def test_run_rejects_cumulative_network_duration_over_sixty_seconds(
    monkeypatch, evidence_fixture
):
    class FakeClock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = FakeClock()
    cert_pem = evidence_fixture.files[CERT_NAME]
    manifest = f"{hashlib.sha256(cert_pem).hexdigest()}  {CERT_NAME}\n".encode()
    ingress_quote = _make_tdx_quote(
        report_data=hashlib.sha256(manifest).digest() + bytes(32),
        measurements=INGRESS_MEASUREMENTS,
    )
    responses = {
        f"https://{DOMAIN}/evidences/sha256sum.txt": manifest,
        f"https://{DOMAIN}/evidences/quote.json": json.dumps(
            {
                "quote": ingress_quote.hex(),
                "event_log": "[]",
                "hash_algorithm": "sha256",
                "prefix": "dstack-ingress",
            }
        ).encode(),
        f"https://{DOMAIN}/evidences/{CERT_NAME}": cert_pem,
        f"https://{DOMAIN}/attestation": json.dumps(_attestation_bundle()).encode(),
    }

    def slow_fetch(url, _domain, **_kwargs):
        clock.now += 20.0
        return responses[url]

    monkeypatch.setattr(verifier, "_monotonic", clock)
    monkeypatch.setattr(verifier, "_https_get", slow_fetch)
    monkeypatch.setattr(
        verifier,
        "_fetch_tls_peer_certificate",
        lambda *_args, **_kwargs: evidence_fixture.peer_der,
    )

    with pytest.raises(EvidenceError, match="overall verification deadline exceeded"):
        _run(DOMAIN, COMPOSE_HASH, CONTENT_PK, _reference_measurements())


def test_run_rejects_ingress_measurement_outside_approved_reference(
    monkeypatch, evidence_fixture
):
    files = dict(evidence_fixture.files)
    files["quote.json"] = json.dumps(
        {
            "quote": _make_tdx_quote(
                report_data=hashlib.sha256(files["sha256sum.txt"]).digest() + bytes(32),
                measurements=INGRESS_MEASUREMENTS,
            ).hex(),
            "event_log": "[]",
            "hash_algorithm": "sha256",
            "prefix": "dstack-ingress",
        }
    ).encode()
    responses = {
        f"https://{DOMAIN}/evidences/{name}": content for name, content in files.items()
    }
    responses[f"https://{DOMAIN}/attestation"] = json.dumps(
        _attestation_bundle()
    ).encode()
    monkeypatch.setattr(
        verifier, "_https_get", lambda url, _domain, **_kwargs: responses[url]
    )
    monkeypatch.setattr(
        verifier,
        "_fetch_tls_peer_certificate",
        lambda *_args, **_kwargs: evidence_fixture.peer_der,
    )
    reference = _reference_measurements()
    reference["ingress"]["mrtd"] = "ff" * 48

    with pytest.raises(
        EvidenceError, match="approved ingress measurement mismatch: mrtd"
    ):
        _run(DOMAIN, COMPOSE_HASH, CONTENT_PK, reference)


def test_run_rejects_ingress_mr_config_id_compose_mismatch(
    monkeypatch, evidence_fixture
):
    files = dict(evidence_fixture.files)
    files["quote.json"] = json.dumps(
        {
            "quote": _make_tdx_quote(
                report_data=hashlib.sha256(files["sha256sum.txt"]).digest() + bytes(32),
                measurements=INGRESS_MEASUREMENTS,
                mr_config_id="01" + "ff" * 32 + "00" * 15,
            ).hex(),
            "event_log": "[]",
            "hash_algorithm": "raw",
            "prefix": "",
        }
    ).encode()
    responses = {
        f"https://{DOMAIN}/evidences/{name}": content for name, content in files.items()
    }
    responses[f"https://{DOMAIN}/attestation"] = json.dumps(
        _attestation_bundle()
    ).encode()
    monkeypatch.setattr(
        verifier, "_https_get", lambda url, _domain, **_kwargs: responses[url]
    )
    monkeypatch.setattr(
        verifier,
        "_fetch_tls_peer_certificate",
        lambda *_args, **_kwargs: evidence_fixture.peer_der,
    )

    with pytest.raises(
        EvidenceError, match="ingress mr_config_id does not bind expected compose_hash"
    ):
        _run(DOMAIN, COMPOSE_HASH, CONTENT_PK, _reference_measurements())


def test_cli_requires_reference_measurements_file(monkeypatch):
    monkeypatch.setattr(verifier, "_run", lambda *_args: ["unexpected"])
    with pytest.raises(SystemExit) as exc_info:
        verifier.main(
            [
                "--domain",
                DOMAIN,
                "--expected-compose-hash",
                COMPOSE_HASH,
                "--expected-content-pk",
                CONTENT_PK,
            ]
        )
    assert exc_info.value.code == 2


def test_cli_prints_structural_dcap_limitation(monkeypatch, capsys, tmp_path):
    reference_path = tmp_path / "references.json"
    reference_path.write_text(json.dumps(_reference_measurements()))
    monkeypatch.setattr(verifier, "_run", lambda *_args: ["evidence verified"])
    result = verifier.main(
        [
            "--domain",
            DOMAIN,
            "--expected-compose-hash",
            "12" * 32,
            "--expected-content-pk",
            CONTENT_PK,
            "--reference-measurements",
            str(reference_path),
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "PASS: evidence verified" in output
    assert "does not validate the Intel DCAP signature chain" in output
    assert "client release gate must verify it separately" in output


def test_cli_discloses_structural_dcap_limitation_on_failure(
    monkeypatch, capsys, tmp_path
):
    reference_path = tmp_path / "references.json"
    reference_path.write_text(json.dumps(_reference_measurements()))

    def fail(*_args):
        raise EvidenceError("synthetic failure")

    monkeypatch.setattr(verifier, "_run", fail)
    result = verifier.main(
        [
            "--domain",
            DOMAIN,
            "--expected-compose-hash",
            COMPOSE_HASH,
            "--expected-content-pk",
            CONTENT_PK,
            "--reference-measurements",
            str(reference_path),
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert "FAIL: synthetic failure" in captured.err
    assert "does not validate the Intel DCAP signature chain" in captured.out
