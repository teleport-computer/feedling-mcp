from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import safe_url_fetch as suf  # noqa: E402
from core import net_safety  # noqa: E402

PUBLIC_IP = "93.184.216.34"
URL = "https://images.example/generated.png?sig=secret-token-value"


def _resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(net_safety, "resolve_ips", lambda host: list(addresses))


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=(b"\x89PNG",)):
        self.status_code = status_code
        self.headers = headers if headers is not None else {"content-type": "image/png"}
        self._chunks = chunks
        self.closed = False

    async def aiter_raw(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk

    async def aiter_bytes(self):  # must stay unused: it would decompress
        raise AssertionError("the body must be read undecoded")

    async def aclose(self):
        self.closed = True


class _FakeClient:
    last_request: dict = {}
    kwargs: dict = {}

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def build_request(self, method, url, *, headers=None, extensions=None):
        _FakeClient.last_request = {
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "extensions": dict(extensions or {}),
        }
        return _FakeClient.last_request

    async def send(self, request, stream=False):
        return self._response


def _install_client(monkeypatch, response):
    def factory(**kwargs):
        _FakeClient.kwargs = dict(kwargs)
        return _FakeClient(response)

    monkeypatch.setattr(suf.httpx, "AsyncClient", factory)


def _fetch(url=URL, max_bytes=1_000_000, **kwargs):
    return _run(suf.fetch_image_bytes_async(url, max_bytes=max_bytes, **kwargs))


# --- where the request may point ------------------------------------------

@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",            # loopback
        "10.0.0.5",             # private
        "192.168.1.10",         # private
        "172.16.0.9",           # private
        "169.254.169.254",      # cloud instance metadata
        "100.64.0.1",           # carrier-grade NAT
        "0.0.0.0",              # unspecified
        "224.0.0.1",            # multicast
        "240.0.0.1",            # reserved
        "::1",                  # IPv6 loopback
        "fc00::1",              # IPv6 unique local
        "fe80::1",              # IPv6 link-local
        "::ffff:127.0.0.1",     # v4-mapped loopback
    ],
)
def test_refuses_hosts_that_resolve_inside_the_network(monkeypatch, address):
    _resolves_to(monkeypatch, address)
    monkeypatch.setattr(
        suf.httpx, "AsyncClient",
        lambda **kw: pytest.fail("a blocked host must not be contacted"),
    )
    with pytest.raises(suf.UnsafeURLError, match="image_url_blocked"):
        _fetch()


def test_refuses_when_any_answer_is_internal(monkeypatch):
    """One public and one private answer is a rebinding attempt, not a list to
    pick the good entry from."""
    _resolves_to(monkeypatch, PUBLIC_IP, "127.0.0.1")
    with pytest.raises(suf.UnsafeURLError, match="image_url_blocked"):
        _fetch()


@pytest.mark.parametrize(
    "url",
    [
        "http://images.example/x.png",          # plaintext
        "ftp://images.example/x.png",           # other scheme
        "file:///etc/passwd",                   # local file
        "https://user:pw@images.example/x.png",  # credentials in url
        "https://images.example:0/x.png",       # illegal port
        "https:///x.png",                       # no host
        "https://images.example/" + "a" * 4000,  # absurd length
        "",
    ],
)
def test_refuses_urls_that_are_not_plain_https(monkeypatch, url):
    _resolves_to(monkeypatch, PUBLIC_IP)
    monkeypatch.setattr(
        suf.httpx, "AsyncClient",
        lambda **kw: pytest.fail("a rejected url must not be contacted"),
    )
    with pytest.raises(suf.UnsafeURLError):
        _fetch(url)


def test_connects_to_the_validated_address_and_never_resolves_again(monkeypatch):
    calls: list[str] = []

    def resolve(host):
        calls.append(host)
        return [PUBLIC_IP]

    monkeypatch.setattr(net_safety, "resolve_ips", resolve)
    _install_client(monkeypatch, _FakeResponse())

    fetched = _fetch()

    assert fetched.mime_type == "image/png"
    # Exactly one lookup, and the connection uses its result — not the name.
    assert calls == ["images.example"]
    assert _FakeClient.last_request["url"].startswith(f"https://{PUBLIC_IP}/")
    # ...while TLS and routing still address the original hostname.
    assert _FakeClient.last_request["headers"]["Host"] == "images.example"
    assert _FakeClient.last_request["extensions"]["sni_hostname"] == "images.example"


def test_ignores_the_process_proxy_environment(monkeypatch):
    """A proxy would resolve the hostname itself, undoing the pinning."""
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(monkeypatch, _FakeResponse())
    _fetch()
    assert _FakeClient.kwargs["trust_env"] is False
    assert _FakeClient.kwargs["follow_redirects"] is False


def test_sends_nothing_of_ours(monkeypatch):
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(monkeypatch, _FakeResponse())
    _fetch()
    sent = {k.lower() for k in _FakeClient.last_request["headers"]}
    assert not sent & {"authorization", "x-api-key", "cookie", "referer"}
    assert _FakeClient.last_request["headers"]["Accept-Encoding"] == "identity"


# --- what the answer may be -----------------------------------------------

@pytest.mark.parametrize(
    ("label", "response"),
    [
        ("301", _FakeResponse(status_code=301, headers={"location": "https://x"})),
        ("302", _FakeResponse(status_code=302, headers={"location": "https://x"})),
        ("307", _FakeResponse(status_code=307, headers={"location": "https://x"})),
        ("404", _FakeResponse(status_code=404)),
        ("204", _FakeResponse(status_code=204)),
    ],
)
def test_only_a_200_is_an_answer(monkeypatch, label, response):
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(monkeypatch, response)
    with pytest.raises(suf.UnsafeURLError, match="image_url_status"):
        _fetch()


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "application/json",
        "image/svg+xml",   # a document, not pixels
        "image/gif",       # generated_image cannot normalize it
        "",
    ],
)
def test_refuses_content_types_the_image_pipeline_cannot_decode(
    monkeypatch, content_type
):
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(
        monkeypatch,
        _FakeResponse(headers={"content-type": content_type}, chunks=(b"<html>",)),
    )
    with pytest.raises(suf.UnsafeURLError, match="image_url_not_an_image"):
        _fetch()


def test_refuses_a_compressed_body(monkeypatch):
    """A single compressed chunk can inflate past the ceiling once decoded, so
    a transfer coding we did not ask for is refused outright."""
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(
        monkeypatch,
        _FakeResponse(
            headers={"content-type": "image/png", "content-encoding": "gzip"}
        ),
    )
    with pytest.raises(suf.UnsafeURLError, match="image_url_encoded"):
        _fetch()


def test_refuses_an_oversized_declared_length(monkeypatch):
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(
        monkeypatch,
        _FakeResponse(
            headers={"content-type": "image/png", "content-length": "999999"},
            chunks=(b"tiny",),
        ),
    )
    with pytest.raises(suf.UnsafeURLError, match="image_url_too_large"):
        _fetch(max_bytes=1000)


def test_stops_at_the_ceiling_when_the_length_is_absent_or_a_lie(monkeypatch):
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(
        monkeypatch,
        _FakeResponse(
            headers={"content-type": "image/png", "content-length": "4"},
            chunks=(b"x" * 400,) * 10,
        ),
    )
    with pytest.raises(suf.UnsafeURLError, match="image_url_too_large"):
        _fetch(max_bytes=1000)


def test_accepts_exactly_the_ceiling_and_refuses_one_byte_more(monkeypatch):
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(monkeypatch, _FakeResponse(chunks=(b"x" * 1000,)))
    assert len(_fetch(max_bytes=1000).data) == 1000

    _install_client(monkeypatch, _FakeResponse(chunks=(b"x" * 1001,)))
    with pytest.raises(suf.UnsafeURLError, match="image_url_too_large"):
        _fetch(max_bytes=1000)


def test_refuses_an_empty_body(monkeypatch):
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(monkeypatch, _FakeResponse(chunks=()))
    with pytest.raises(suf.UnsafeURLError, match="image_url_empty"):
        _fetch()


def test_a_slow_trickle_hits_the_whole_operation_deadline(monkeypatch):
    """Per-read timeouts never fire against a sender that drips one byte before
    each of them; the deadline covers DNS, connect, headers and body together."""
    _resolves_to(monkeypatch, PUBLIC_IP)

    class _Trickle(_FakeResponse):
        # Finite on purpose. An endless trickle would make this test *hang*
        # rather than fail when the deadline is removed, and a hang reads as
        # "still running" in CI instead of "this guard is gone".
        async def aiter_raw(self, chunk_size=None):
            for _ in range(40):
                await asyncio.sleep(0.05)
                yield b"x"

    _install_client(monkeypatch, _Trickle())
    with pytest.raises(suf.UnsafeURLError, match="image_url_timeout"):
        _fetch(max_bytes=10_000_000, deadline_seconds=0.1)


# --- what the failure may reveal ------------------------------------------

def test_no_failure_ever_names_the_url(monkeypatch, caplog):
    """A signed CDN link is itself a credential: its path and query must not
    reach an exception message or a log line."""
    secret = "secret-token-value"
    cases = [
        ("127.0.0.1", _FakeResponse()),
        (PUBLIC_IP, _FakeResponse(status_code=404)),
        (PUBLIC_IP, _FakeResponse(headers={"content-type": "text/html"})),
        (PUBLIC_IP, _FakeResponse(chunks=(b"x" * 5000,))),
    ]
    for address, response in cases:
        _resolves_to(monkeypatch, address)
        _install_client(monkeypatch, response)
        with caplog.at_level("DEBUG"):
            with pytest.raises(suf.UnsafeURLError) as raised:
                _fetch(max_bytes=100)
        rendered = str(raised.value) + repr(raised.value)
        assert secret not in rendered
        assert "generated.png" not in rendered
        assert secret not in caplog.text


def test_an_oversized_chunk_never_enters_the_accumulator(monkeypatch):
    """The ceiling must bound what is *allocated*, not merely what is kept.
    Appending first and measuring after would copy the whole chunk in."""
    _resolves_to(monkeypatch, PUBLIC_IP)

    class _Guarded(bytearray):
        def extend(self, chunk):  # noqa: D102
            if len(self) + len(chunk) > 1000:
                raise AssertionError(
                    f"oversized chunk was allocated: {len(self) + len(chunk)} bytes"
                )
            super().extend(chunk)

    monkeypatch.setattr(suf, "bytearray", _Guarded, raising=False)
    _install_client(monkeypatch, _FakeResponse(chunks=(b"x" * 50_000,)))

    with pytest.raises(suf.UnsafeURLError, match="image_url_too_large"):
        _fetch(max_bytes=1000)


def test_the_resolver_runs_off_the_default_executor(monkeypatch):
    """getaddrinfo cannot be cancelled, so hostile DNS must not be able to
    occupy the pool that unrelated provider and enclave work share."""
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(monkeypatch, _FakeResponse())

    def forbidden(*args, **kwargs):
        raise AssertionError("resolution must not use the default executor")

    monkeypatch.setattr(suf.asyncio, "to_thread", forbidden)
    assert _fetch().mime_type == "image/png"


def test_a_saturated_resolver_fails_closed(monkeypatch):
    _resolves_to(monkeypatch, PUBLIC_IP)
    _install_client(monkeypatch, _FakeResponse())

    async def busy(*args, **kwargs):
        raise net_safety.ResolverBusy("resolver capacity exhausted")

    monkeypatch.setattr(net_safety, "run_on_dns_executor", busy)
    with pytest.raises(suf.UnsafeURLError, match="image_url_resolver_busy"):
        _fetch()


def test_the_resolver_slot_is_released_after_every_outcome():
    """A leaked submission slot would degrade into a permanent dns_busy."""
    import asyncio as _asyncio

    def boom():
        raise OSError("resolver exploded")

    async def scenario():
        for _ in range(net_safety._DNS_MAX_PENDING + 5):
            with pytest.raises(OSError):
                await net_safety.run_on_dns_executor(boom)
        return await net_safety.run_on_dns_executor(lambda: "still works")

    assert _asyncio.run(scenario()) == "still works"


@pytest.mark.parametrize(
    "failure",
    [
        UnicodeError("resolver rejected hostname"),
        ValueError("bad authority"),
        RuntimeError("resolver blew up"),
    ],
)
def test_every_resolution_failure_becomes_a_slug(monkeypatch, failure, caplog):
    """`str(UnsafeURLError)` is the module's whole error contract: an exception
    from IDNA or a custom resolver must not escape carrying the URL."""

    def raising(host):
        raise failure

    monkeypatch.setattr(net_safety, "resolve_ips", raising)
    monkeypatch.setattr(
        suf.httpx, "AsyncClient",
        lambda **kw: pytest.fail("a failed resolution must not connect"),
    )

    with caplog.at_level("DEBUG"):
        with pytest.raises(suf.UnsafeURLError) as raised:
            _fetch()

    assert str(raised.value).startswith("image_url_")
    assert "secret-token-value" not in str(raised.value) + caplog.text


def test_a_resolver_that_cannot_accept_work_is_a_stable_slug(monkeypatch):
    """Distinct from saturation: the pool being unusable must not surface as a
    raw executor exception either."""
    _resolves_to(monkeypatch, PUBLIC_IP)
    monkeypatch.setattr(
        suf.httpx, "AsyncClient",
        lambda **kw: pytest.fail("an unusable resolver must not connect"),
    )

    class _Broken:
        def submit(self, *args, **kwargs):
            raise RuntimeError("executor shut down")

    monkeypatch.setattr(net_safety, "_DNS_EXECUTOR", _Broken())
    before = net_safety._DNS_SUBMISSION_SLOTS._value

    with pytest.raises(suf.UnsafeURLError, match="image_url_resolver_unavailable"):
        _fetch()

    assert net_safety._DNS_SUBMISSION_SLOTS._value == before
