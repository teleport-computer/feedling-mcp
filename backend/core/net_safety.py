"""Pure outbound-URL safety checks shared by backend-originated HTTP clients.

This is an application-layer SSRF guard. It rejects malformed URLs and hosts
that resolve to any non-global address. Callers must re-run it for every
redirect and disable automatic redirects. DNS can still change between this
check and connection; infrastructure egress policy remains the final boundary.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import socket
import threading
from collections.abc import Callable, Iterable
from typing import Any, TypeVar
from urllib.parse import urlparse, urlunparse

_T = TypeVar("_T")


def resolve_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


class ResolverBusy(Exception):
    """The bounded resolver is saturated; the caller must fail closed."""


class ResolverUnavailable(Exception):
    """The resolver pool could not accept work at all (e.g. shut down).

    Distinct from `ResolverBusy` on purpose: saturation is a transient load
    signal, while this is the pool being unusable. Callers map them to their own
    stable errors, and neither may surface as a raw executor exception.
    """


# Cancelling `getaddrinfo` cannot stop its native worker thread, so hostile DNS
# on one caller's URL must not run on asyncio's shared default executor, where
# it would starve unrelated provider and enclave offload work. A dedicated pool
# bounds how many such calls can be in flight; the submission semaphore bounds
# the queue behind it so a degraded resolver fails closed instead of growing an
# unbounded backlog.
_DNS_WORKERS = 8
_DNS_MAX_PENDING = 32
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_DNS_WORKERS,
    thread_name_prefix="feedling-dns",
)
_DNS_SUBMISSION_SLOTS = threading.BoundedSemaphore(_DNS_MAX_PENDING)


async def run_on_dns_executor(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a blocking resolution off the default executor, bounded and fail-closed."""
    if not _DNS_SUBMISSION_SLOTS.acquire(blocking=False):
        raise ResolverBusy("resolver capacity exhausted")
    try:
        future = _DNS_EXECUTOR.submit(fn, *args, **kwargs)
    except Exception as exc:
        _DNS_SUBMISSION_SLOTS.release()
        raise ResolverUnavailable("DNS resolver unavailable") from exc
    future.add_done_callback(lambda _future: _DNS_SUBMISSION_SLOTS.release())
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        # Cancels queued work where possible; a call already running stays
        # bounded by the worker count and releases its slot when it finishes.
        future.cancel()
        raise


def validated_pinned_ip(
    url: str,
    *,
    resolve: Callable[[str], Iterable[str]] = resolve_ips,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> tuple[str | None, str | None]:
    """Validate one URL hop and return the exact global address to connect to.

    Resolving during validation and then handing the hostname back to an HTTP
    client leaves a DNS-rebinding window: the client's own lookup can land on
    loopback or internal infrastructure. Capture what the guard checked and pin
    the request to one of those addresses. Redirects must be revalidated and
    repinned by the caller.
    """
    resolved: list[str] = []

    def _recording_resolve(host: str) -> list[str]:
        ips = [str(value) for value in resolve(host)]
        resolved.extend(ips)
        return ips

    blocked = blocked_url_kind(
        url, resolve=_recording_resolve, allowed_schemes=allowed_schemes
    )
    if blocked is not None:
        return blocked, None
    host = urlparse(url).hostname or ""
    try:
        # Literal global IP: blocked_url_kind validated it without DNS.
        return None, ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    if not resolved:  # defensive; blocked_url_kind normally returns "dns"
        return "dns", None
    return None, ipaddress.ip_address(resolved[0]).compressed


def ascii_authority(host: str, port: int | None) -> str:
    try:
        parsed_ip = ipaddress.ip_address(host)
        rendered = (
            f"[{parsed_ip.compressed}]"
            if parsed_ip.version == 6
            else parsed_ip.compressed
        )
    except ValueError:
        rendered = host.encode("idna").decode("ascii")
    return rendered + (f":{port}" if port is not None else "")


def pinned_url(url: str, resolved_ip: str) -> tuple[str, str, str]:
    """Return (url aimed at the pinned IP, Host header, TLS SNI hostname)."""
    parsed = urlparse(url)
    original_host = parsed.hostname or ""
    pinned_authority = ascii_authority(resolved_ip, parsed.port)
    host_header = ascii_authority(original_host, parsed.port)
    try:
        sni_hostname = ipaddress.ip_address(original_host).compressed
    except ValueError:
        sni_hostname = original_host.encode("idna").decode("ascii")
    return (
        urlunparse(parsed._replace(netloc=pinned_authority)),
        host_header,
        sni_hostname,
    )


def _address_is_reachable_publicly(ip: ipaddress._BaseAddress) -> bool:
    """`is_global` alone is not enough: it answers True for multicast.

    224.0.0.0/4 and ff00::/8 are neither private nor reserved by that property,
    so a hostname resolving to 224.0.0.1 or ff02::1 would pass an is_global-only
    check and let a caller aim this process at a local network segment.
    """
    return bool(getattr(ip, "is_global", False)) and not ip.is_multicast


def blocked_url_kind(
    url: str,
    *,
    resolve: Callable[[str], Iterable[str]] = resolve_ips,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> str | None:
    """Return ``blocked_url``, ``dns``, or ``None`` for an outbound URL."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "blocked_url"
    if (
        parsed.scheme.lower() not in allowed_schemes
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None and not (1 <= port <= 65535)
    ):
        return "blocked_url"
    try:
        ip = ipaddress.ip_address(host)
        return None if _address_is_reachable_publicly(ip) else "blocked_url"
    except ValueError:
        pass
    try:
        ips = list(resolve(host))
    except OSError:
        return "dns"
    if not ips:
        return "dns"
    try:
        return (
            None
            if all(
                _address_is_reachable_publicly(ipaddress.ip_address(raw))
                for raw in ips
            )
            else "blocked_url"
        )
    except ValueError:
        return "dns"
