"""Shared test helper: build a real self-signed CA in memory.

Not named test_* on purpose — conftest.py's collect_ignore only filters
test_*.py, so this module is importable from both the DB-backed suites and
the _PURE_UNIT ones. Depends only on `cryptography` (an existing backend dep),
never on the DB layer.
"""


def self_signed_ca_pem() -> str:
    """真实自签名 CA（内存生成），用于喂 ssl.load_verify_locations。"""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "feedling-test-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()
