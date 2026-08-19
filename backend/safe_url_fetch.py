"""Fetch image bytes from a URL we did not choose ourselves.

An image-generation provider may answer with a link instead of inline bytes.
Following such a link turns this process into an HTTP client aimed by whoever
runs that provider, so the URL is treated as fully untrusted — not as "our
provider's link" — and every step below exists to bound where that aim can go
and how much it can cost:

* https only, no userinfo, bounded length;
* the address check and the connection cannot disagree: `core.net_safety`
  validates every resolved address and the request is then pinned to one of
  them, with Host and TLS SNI preserved (DNS rebinding closes here);
* the process proxy environment is ignored — a proxy would re-resolve the
  hostname and undo the pinning;
* redirects are never followed, and only `200` is accepted: a `3xx` is a second,
  unvalidated destination;
* the body is read undecoded with a hard byte ceiling, so a compressed response
  cannot expand past it, and a whole-operation deadline bounds a slow trickle
  that would never trip a per-read timeout;
* the content type must be one of the image formats the generated-image
  pipeline can actually decode — the header is a filter, never the proof;
* nothing of ours is attached: no Authorization, no cookies, no referer.

Errors carry a stable reason and never the URL: a signed CDN link is itself a
credential, so path and query stay out of exceptions and logs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from core import net_safety

# The formats generated_image can decode. `image/*` would also admit SVG, which
# is a document, not pixels.
ALLOWED_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")
MAX_URL_CHARS = 2048
DEFAULT_DEADLINE_SECONDS = 30.0
# Bounded so one hostile chunk cannot ask for an arbitrary allocation before
# the ceiling is consulted.
READ_CHUNK_BYTES = 64 * 1024


class UnsafeURLError(Exception):
    """The fetch was refused. `str()` is a stable slug, never the URL."""


@dataclass(frozen=True)
class FetchedBytes:
    data: bytes
    mime_type: str


async def _resolve_pinned_target(url: str) -> tuple[str, str, str]:
    """Validate the URL off the event loop and pin it to a checked address."""
    try:
        blocked, resolved_ip = await net_safety.run_on_dns_executor(
            net_safety.validated_pinned_ip,
            url,
            # Resolved through the module attribute on purpose: a default
            # argument binds at import, which would make the resolver
            # unswappable — and an unswappable resolver can only ever be tested
            # against whatever the machine's DNS happens to answer.
            resolve=net_safety.resolve_ips,
            allowed_schemes=("https",),
        )
    except net_safety.ResolverBusy as exc:
        raise UnsafeURLError("image_url_resolver_busy") from exc
    except net_safety.ResolverUnavailable as exc:
        raise UnsafeURLError("image_url_resolver_unavailable") from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - closes the slug contract
        # IDNA errors, a resolver that raises its own type, an authority
        # urlsplit accepts but ipaddress rejects: none of these may escape with
        # a message that could quote the URL.
        raise UnsafeURLError("image_url_blocked") from exc
    if blocked is not None or not resolved_ip:
        raise UnsafeURLError(
            "image_url_dns_failed" if blocked == "dns" else "image_url_blocked"
        )
    try:
        return net_safety.pinned_url(url, resolved_ip)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - same slug contract
        raise UnsafeURLError("image_url_blocked") from exc


def _check_url_shape(url: str) -> None:
    text = str(url or "").strip()
    if not text or len(text) > MAX_URL_CHARS:
        raise UnsafeURLError("image_url_blocked")
    try:
        parts = urlparse(text)
    except ValueError as exc:
        raise UnsafeURLError("image_url_blocked") from exc
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise UnsafeURLError("image_url_blocked")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise UnsafeURLError("image_url_blocked")


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    # aiter_raw, not aiter_bytes: the latter decompresses, so one small chunk
    # could inflate past the ceiling before the ceiling is consulted.
    buffer = bytearray()
    async for chunk in response.aiter_raw(chunk_size=READ_CHUNK_BYTES):
        # Checked *before* extending: appending first and measuring after would
        # copy an oversized chunk into memory in full, so the ceiling would
        # bound what is kept rather than what is allocated.
        if len(chunk) > max_bytes - len(buffer):
            raise UnsafeURLError("image_url_too_large")
        buffer.extend(chunk)
    if not buffer:
        raise UnsafeURLError("image_url_empty")
    return bytes(buffer)


async def _fetch(url: str, max_bytes: int, timeout: float) -> FetchedBytes:
    pinned, host_header, sni_hostname = await _resolve_pinned_target(url)
    async with httpx.AsyncClient(
        trust_env=False,          # a proxy would re-resolve and unpin the host
        follow_redirects=False,   # a redirect is an unvalidated second target
        timeout=timeout,
    ) as client:
        request = client.build_request(
            "GET",
            pinned,
            headers={
                "Host": host_header,
                "Accept": ", ".join(ALLOWED_IMAGE_MIME_TYPES),
                # Ask for no transfer coding so the cap counts real bytes.
                "Accept-Encoding": "identity",
            },
            extensions={"sni_hostname": sni_hostname},
        )
        response = await client.send(request, stream=True)
        try:
            if response.status_code != 200:
                raise UnsafeURLError("image_url_status")
            encoding = response.headers.get("content-encoding", "").strip().lower()
            if encoding and encoding != "identity":
                raise UnsafeURLError("image_url_encoded")
            mime = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if mime not in ALLOWED_IMAGE_MIME_TYPES:
                raise UnsafeURLError("image_url_not_an_image")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise UnsafeURLError("image_url_too_large")
            return FetchedBytes(await _read_capped(response, max_bytes), mime)
        finally:
            await response.aclose()


async def fetch_image_bytes_async(
    url: str,
    *,
    max_bytes: int,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> FetchedBytes:
    """Fetch one image under the rules in this module's docstring.

    `deadline_seconds` bounds the whole operation — DNS, connect, headers and
    body together. A per-read timeout alone does not: a sender that trickles a
    byte before every timeout can hold the connection open indefinitely.
    """
    _check_url_shape(url)
    try:
        return await asyncio.wait_for(
            _fetch(url, max_bytes, deadline_seconds),
            timeout=deadline_seconds,
        )
    except UnsafeURLError:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise UnsafeURLError("image_url_timeout") from exc
    except httpx.HTTPError as exc:
        # The exception type is safe to name; the URL inside it is not.
        raise UnsafeURLError(f"image_url_unreachable_{type(exc).__name__}") from exc
