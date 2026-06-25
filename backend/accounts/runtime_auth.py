"""Runtime-token authentication (Stage D).

Lets a hosted consumer authenticate with a short-lived, user-scoped runtime
token (minted by the trusted agent-runner supervisor) instead of the user's
long-term Feedling API key, so a compromised consumer can't leak a long-lived
credential. Verified against the shared HMAC secret
``FEEDLING_RUNTIME_TOKEN_SECRET``; the WHOLE feature is OFF unless that secret is
set, so existing API-key callers are unaffected.

The primitive lives in ``core.runtime_token`` (stdlib-only) so this — and the
supervisor's minting side — can both use it without a dependency-direction
violation.
"""

from __future__ import annotations

import os

from flask import abort, g, request

from core import runtime_token

RUNTIME_TOKEN_HEADER = "X-Feedling-Runtime-Token"


def _secret() -> bytes | None:
    s = (os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET") or "").strip()
    return s.encode("utf-8") if s else None


def is_enabled() -> bool:
    return _secret() is not None


def extract_runtime_token() -> str | None:
    return (request.headers.get(RUNTIME_TOKEN_HEADER) or "").strip() or None


def resolve_claims() -> dict | None:
    """Verified claims when a runtime token is present AND the feature is enabled.

    Returns None when no token is sent or the feature is disabled (caller falls
    back to the API key). Raises ``core.runtime_token.TokenError`` when a token IS
    present but invalid/expired, so the caller fails closed (401) instead of
    silently falling back to another credential.
    """
    token = extract_runtime_token()
    if not token:
        return None
    secret = _secret()
    if secret is None:
        return None  # feature disabled — ignore the header entirely
    return runtime_token.verify(secret, token)


def authorize_scope(scope: str) -> None:
    """Enforce that a runtime-token request carries ``scope`` (slice 4).

    No-op for api-key auth (the long-term key is full-access) and when the
    feature is off. Aborts 403 when the request authenticated with a runtime
    token whose ``scope`` list does not include ``scope``. Routes call this right
    after ``require_user`` with the scope they need (e.g. "memory", "identity")."""
    claims = getattr(g, "runtime_token_claims", None)
    if not claims:
        return
    try:
        runtime_token.authorize(claims, user_id=getattr(g, "user_id", ""), scope=scope)
    except runtime_token.TokenError:
        abort(403)
