"""Pure outbound-URL safety checks shared by backend-originated HTTP clients.

This is an application-layer SSRF guard. It rejects malformed URLs and hosts
that resolve to any non-global address. Callers must re-run it for every
redirect and disable automatic redirects. DNS can still change between this
check and connection; infrastructure egress policy remains the final boundary.
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlparse


def resolve_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


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
        return None if ip.is_global else "blocked_url"
    except ValueError:
        pass
    try:
        ips = list(resolve(host))
    except OSError:
        return "dns"
    if not ips:
        return "dns"
    try:
        return None if all(ipaddress.ip_address(raw).is_global for raw in ips) else "blocked_url"
    except ValueError:
        return "dns"
