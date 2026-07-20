"""Native ASGI /v1/notify-relay/* routes — the self-hosted push relay.

Deliberately OUTSIDE the account auth system (no ``Depends(require_auth)``):

- ``POST /v1/notify-relay/register`` is anonymous. Self-hosted users often have
  no official account (they registered against their own backend), so the app
  enrolls its APNs device token bare and receives a relay auth token. Abuse is
  damped by a per-IP rate limit; the minted token only authorizes pushing to
  the enrolled device.
- ``POST /v1/notify-relay/push`` authenticates with that relay token via the
  ``X-Relay-Token`` header (not ``X-API-Key``/``Authorization`` — it is not an
  account credential and must never be confused with one). Per-token rate
  limited, plus a per-IP limit on failed auth to slow token enumeration.

Neither route sets the ``current_user_id`` contextvar, so access-log lines show
an empty user — expected. Blocking core work runs via ``threadpool.run_db``.
"""

from __future__ import annotations

import math
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from asgi import http as asgi_http
from asgi import threadpool
from asgi.responses import api_error
from notify_relay import ratelimit, relay_core

router = APIRouter()

# Per-worker limiters (per-process memory; ceiling ≈ workers × limit — see
# ratelimit.py). Separate instances so a register flood can't starve push keys.
_register_limiter = ratelimit.SlidingWindowLimiter()
_push_limiter = ratelimit.SlidingWindowLimiter()
_bad_auth_limiter = ratelimit.SlidingWindowLimiter()

_REGISTER_RATE_DEFAULT = (10, 3600.0)
_PUSH_RATE_DEFAULT = (120, 60.0)
_BAD_AUTH_RATE_DEFAULT = (30, 60.0)


def _xff_trusted_hops() -> int:
    """How many trusted reverse proxies sit in front of the app (default 1:
    the CVM ingress). 0 = ignore X-Forwarded-For entirely and use the socket
    peer — the right setting for a deployment exposed directly to clients."""
    try:
        return max(0, int(os.environ.get("NOTIFY_RELAY_XFF_HOPS", "") or 1))
    except (TypeError, ValueError):
        return 1


def _client_ip(request: Request) -> str:
    # Rate-limit identity. The FIRST X-Forwarded-For hop is client-controlled
    # (a flood can mint a fresh fake IP per request and evade every per-IP
    # limit), so count from the END: with N trusted proxies the last N entries
    # were appended by them, and entry -N is the address the outermost trusted
    # proxy actually saw. Entries before that are untrusted noise and ignored.
    hops = _xff_trusted_hops()
    if hops > 0:
        parts = [p.strip() for p in
                 request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if parts:
            # Fewer entries than trusted hops ⇒ every entry was proxy-appended.
            return parts[-hops] if len(parts) >= hops else parts[0]
    return request.client.host if request.client else "unknown"


def _rate_limited(retry_after: float) -> JSONResponse:
    resp = api_error(429, "rate_limited")
    resp.headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return resp


@router.post("/v1/notify-relay/register")
async def register(request: Request):
    limit, window = ratelimit.rate_from_env(
        "NOTIFY_RELAY_REGISTER_RATE", _REGISTER_RATE_DEFAULT)
    allowed, retry_after = _register_limiter.allow(_client_ip(request), limit, window)
    if not allowed:
        return _rate_limited(retry_after)
    body = await asgi_http.read_json_silent(request)
    if not isinstance(body, dict):
        return api_error(400, "invalid_payload", detail="body must be a JSON object")
    status, payload = await threadpool.run_db(relay_core.register, body)
    return JSONResponse(payload, status_code=status)


@router.post("/v1/notify-relay/push")
async def push(request: Request):
    token = (request.headers.get("x-relay-token") or "").strip()
    ip = _client_ip(request)
    bad_limit, bad_window = ratelimit.rate_from_env(
        "NOTIFY_RELAY_BAD_AUTH_RATE", _BAD_AUTH_RATE_DEFAULT)
    push_limit, push_window = ratelimit.rate_from_env(
        "NOTIFY_RELAY_PUSH_RATE", _PUSH_RATE_DEFAULT)

    config = None
    if token.startswith(relay_core.AUTH_TOKEN_PREFIX):
        # Per-token DB-lookup throttle BEFORE the lookup: keyed on the token
        # string, it caps a KNOWN token's get_config hits (valid or disabled —
        # a disabled token could otherwise force one uncapped lookup per
        # request). It's keyed on the token, NOT the IP, so it never penalizes a
        # different caller that happens to share an egress IP.
        #
        # The per-IP bad-auth budget is charged ONLY on the failure path below,
        # never as a pre-lookup gate on this path: gating here would 429 a valid,
        # correctly-authenticated push just because an unrelated stale-token
        # burst from the same NAT/egress IP had exhausted the IP's bad-auth
        # budget (review R4 [3]). The unknown-token DB hit that remains is a
        # single indexed PK lookup, and the nrt_+48-hex space isn't enumerable.
        allowed, retry_after = _push_limiter.peek(token, push_limit, push_window)
        if not allowed:
            return _rate_limited(retry_after)
        config = await threadpool.run_db(relay_core.get_config, token)
    if config is None:
        # Missing/malformed/unknown token are indistinguishable on the wire (no
        # probe signal); failed lookups burn the per-IP bad-auth budget so
        # token enumeration stalls fast.
        allowed, retry_after = _bad_auth_limiter.allow(ip, bad_limit, bad_window)
        if not allowed:
            return _rate_limited(retry_after)
        return api_error(401, "unauthorized")
    if config.get("disabled"):
        # Charge the per-token budget so a revoked-but-hostile token starts
        # getting peek-blocked before its next DB lookup, instead of driving
        # unbounded get_config load after being disabled.
        _push_limiter.allow(config["auth_token"], push_limit, push_window)
        return api_error(403, "forbidden")

    allowed, retry_after = _push_limiter.allow(config["auth_token"], push_limit, push_window)
    if not allowed:
        return _rate_limited(retry_after)

    body = await asgi_http.read_json_silent(request)
    if not isinstance(body, dict):
        return api_error(400, "invalid_payload", detail="body must be a JSON object")
    status, payload = await threadpool.run_db(relay_core.push, config, body)
    return JSONResponse(payload, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
