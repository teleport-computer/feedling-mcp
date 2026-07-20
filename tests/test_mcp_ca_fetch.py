"""Backend auto-CA fetch for self-signed user MCP servers (Runtime V2).

The security-critical property: the fetch resolves through
``mcp_probe._validated_public_ips`` (SSRF gate) and connects ONLY to the
returned public IP with SNI bound to the configured host — it never re-resolves
the hostname, so a self-signed server cannot DNS-rebind to an internal address
between validation and the openssl fetch.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import asyncio  # noqa: E402

from hosted import mcp_ca_fetch  # noqa: E402
from hosted import mcp_probe  # noqa: E402


def test_non_https_url_is_not_fetched(monkeypatch):
    def _must_not_resolve(url):
        raise AssertionError("non-https must be refused before any resolution")

    monkeypatch.setattr(mcp_probe, "_validated_public_ips", _must_not_resolve)
    assert asyncio.run(mcp_ca_fetch.fetch_anchor_for_url("http://x.example.com")) is None


def test_ssrf_non_public_host_returns_none(monkeypatch):
    """A host that resolves to a non-public address is refused by
    _validated_public_ips (ProbeError) and yields no anchor — never fetched."""
    fetched = {"n": 0}

    def _blocked(url):
        raise mcp_probe.ProbeError("unreachable_from_backend", "non-public address")

    monkeypatch.setattr(mcp_probe, "_validated_public_ips", _blocked)
    monkeypatch.setattr(
        mcp_ca_fetch, "_fetch_anchor_pinned",
        lambda *a, **k: fetched.__setitem__("n", fetched["n"] + 1) or "X")
    assert asyncio.run(
        mcp_ca_fetch.fetch_anchor_for_url("https://internal.example.com")) is None
    assert fetched["n"] == 0


def test_fetches_only_from_validated_ip_with_configured_sni(monkeypatch):
    """The blocking fetch is handed the VALIDATED IP (not the hostname) and the
    configured host as SNI/verification name."""
    seen = {}

    monkeypatch.setattr(
        mcp_probe, "_validated_public_ips",
        lambda url: ("maps.example.com", ["203.0.113.9"]))

    def _fake_pinned(ip, port, server_hostname, timeout):
        seen.update(ip=ip, port=port, host=server_hostname)
        return "ANCHOR_PEM"

    monkeypatch.setattr(mcp_ca_fetch, "_fetch_anchor_pinned", _fake_pinned)
    out = asyncio.run(
        mcp_ca_fetch.fetch_anchor_for_url("https://maps.example.com:8443/sse"))
    assert out == "ANCHOR_PEM"
    assert seen == {"ip": "203.0.113.9", "port": 8443, "host": "maps.example.com"}


def test_default_https_port_used_when_absent(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        mcp_probe, "_validated_public_ips", lambda url: ("h.example.com", ["203.0.113.1"]))
    monkeypatch.setattr(
        mcp_ca_fetch, "_fetch_anchor_pinned",
        lambda ip, port, host, timeout: seen.update(port=port) or "PEM")
    asyncio.run(mcp_ca_fetch.fetch_anchor_for_url("https://h.example.com/sse"))
    assert seen["port"] == 443


def test_pinned_fetch_rejects_oversized_anchor(monkeypatch):
    """_is_well_formed_ca enforces the MAX_CA_BYTES ceiling even on a PEM that
    would otherwise parse — mirrors the manually-pasted ca_pem path."""
    huge = "-----BEGIN CERTIFICATE-----\n" + ("A" * (mcp_ca_fetch.MAX_CA_BYTES + 10)) \
        + "\n-----END CERTIFICATE-----"
    monkeypatch.setattr(mcp_ca_fetch, "_fetch_chain", lambda *a, **k: [huge])
    # _anchor_works must never be reached for an oversized pick.
    monkeypatch.setattr(
        mcp_ca_fetch, "_anchor_works",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("size gate bypassed")))
    assert mcp_ca_fetch._fetch_anchor_pinned("203.0.113.1", 443, "h", 1.0) is None


def test_pinned_fetch_rejects_anchor_that_does_not_verify(monkeypatch):
    """A well-formed but non-verifying pick is dropped (worse than nothing)."""
    monkeypatch.setattr(mcp_ca_fetch, "_fetch_chain", lambda *a, **k: ["pem"])
    monkeypatch.setattr(mcp_ca_fetch, "_is_well_formed_ca", lambda pem: True)
    monkeypatch.setattr(mcp_ca_fetch, "_anchor_works", lambda *a, **k: False)
    assert mcp_ca_fetch._fetch_anchor_pinned("203.0.113.1", 443, "h", 1.0) is None


def test_pinned_fetch_returns_verified_anchor(monkeypatch):
    monkeypatch.setattr(mcp_ca_fetch, "_fetch_chain", lambda *a, **k: ["leaf", "ANCHOR"])
    monkeypatch.setattr(mcp_ca_fetch, "_is_well_formed_ca", lambda pem: True)
    monkeypatch.setattr(mcp_ca_fetch, "_anchor_works", lambda *a, **k: True)
    # _pick_trust_anchor takes the LAST cert in the chain.
    assert mcp_ca_fetch._fetch_anchor_pinned("203.0.113.1", 443, "h", 1.0) == "ANCHOR"


def test_empty_chain_returns_none(monkeypatch):
    monkeypatch.setattr(mcp_ca_fetch, "_fetch_chain", lambda *a, **k: [])
    assert mcp_ca_fetch._fetch_anchor_pinned("203.0.113.1", 443, "h", 1.0) is None
