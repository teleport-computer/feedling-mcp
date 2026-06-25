"""Short-lived, user-scoped runtime tokens (shared primitive).

A runtime token lets a hosted agent call Feedling routes for exactly ONE user
without the agent ever holding that user's long-term Feedling API key. The
agent-runner supervisor (trusted infra, same TDX domain) mints a token after
acquiring a user's runtime lease; the backend verifies it on each request.

Lives in ``core`` (stdlib-only, no business deps) so both the minting side
(``agent_runtime``) and the verifying side (``accounts.auth``) can import it
without violating dependency direction.

Self-contained HMAC token (no JWT dependency): ``base64url(payload).hex(sig)``
where ``sig = HMAC-SHA256(secret, payload_b64)``. Constant-time compared.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class TokenError(Exception):
    """Raised when a runtime token is invalid, expired, or out of scope."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(secret: bytes, payload_b64: str) -> str:
    return hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def mint(
    secret: bytes,
    *,
    user_id: str,
    runtime_instance_id: str,
    scope: list[str],
    now: float | None = None,
    ttl: float = 900.0,
) -> str:
    """Mint a token binding (user_id, runtime_instance_id, scope, exp)."""
    issued = time.time() if now is None else now
    claims = {
        "sub": runtime_instance_id,
        "user_id": user_id,
        "scope": list(scope),
        "iat": issued,
        "exp": issued + ttl,
    }
    payload_b64 = _b64e(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(secret, payload_b64)}"


def verify(secret: bytes, token: str, *, now: float | None = None) -> dict:
    """Verify signature + expiry; return claims or raise ``TokenError``."""
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError as e:
        raise TokenError("malformed_token") from e
    expected = _sign(secret, payload_b64)
    if not hmac.compare_digest(expected, sig):
        raise TokenError("bad_signature")
    try:
        claims = json.loads(_b64d(payload_b64))
    except Exception as e:  # noqa: BLE001
        raise TokenError("bad_payload") from e
    clock = time.time() if now is None else now
    if clock >= float(claims.get("exp") or 0):
        raise TokenError("token_expired")
    return claims


def authorize(claims: dict, *, user_id: str, scope: str) -> None:
    """Assert the token may act on ``user_id`` with ``scope``; else raise.

    The single most important runtime-token invariant: a token minted for one
    user must never be usable against another user's data.
    """
    if claims.get("user_id") != user_id:
        raise TokenError("user_mismatch")
    if scope not in (claims.get("scope") or []):
        raise TokenError("scope_denied")
