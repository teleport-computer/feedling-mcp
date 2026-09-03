"""Framework-neutral request authentication (ASGI-migration plan §7.1).

This is the single source of truth for "who is this request" — pure
header/query → user resolution with **no Flask/FastAPI request object and no
framework `abort()`**. Both the Flask `require_user()` wrapper
(`accounts.auth`) and the forthcoming FastAPI routes call in here; each maps the
typed `AuthError` to its own HTTP response.

Auth precedence is unchanged from the old Flask code (do not widen it here — a
runtime token must never gain more reach than the api-key path granted):

1. A runtime token (`X-Feedling-Runtime-Token`) is tried first when present and
   the feature is enabled (`FEEDLING_RUNTIME_TOKEN_SECRET` set). It carries its
   own user identity. A token that is present but invalid/expired fails **closed**
   (`AuthError(401)`) — never silently falls back to another credential.
2. Otherwise the api key: `X-API-Key`, then `Authorization: Bearer`, then the
   legacy `?key=` query param (kept working for old integrations; the access log
   redacts it).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from accounts import registry
from core import runtime_token
from core import store as core_store
from core.store import UserStore

RUNTIME_TOKEN_HEADER = "X-Feedling-Runtime-Token"


class AuthError(Exception):
    """Typed auth failure. Frameworks map ``status_code`` to their response.

    ``code`` matches the fixed error bodies the backend already returns
    (``unauthorized`` / ``forbidden``); ``detail`` is a machine-readable reason
    (e.g. ``token_expired``) for logs, never required by clients.
    """

    def __init__(self, status_code: int, code: str, detail: Optional[str] = None):
        super().__init__(detail or code)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass
class AuthResult:
    """Resolved identity. ``api_key`` is set only on the api-key path (so the
    caller can record ``store.last_seen_api_key``); ``runtime_token_claims`` is
    set only on the runtime-token path."""

    store: UserStore
    user_id: str
    runtime_token_claims: Optional[dict]
    api_key: Optional[str]


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive single-header read tolerant of plain dicts.

    Flask ``request.headers`` and Starlette ``Headers`` are already
    case-insensitive; a plain ``dict`` passed by a test may not be, so fall back
    to a lowercased scan.
    """
    val = None
    getter = getattr(headers, "get", None)
    if getter is not None:
        val = getter(name)
    if val is None:
        lname = name.lower()
        try:
            items = headers.items()
        except AttributeError:
            items = []
        for k, v in items:
            if k.lower() == lname:
                val = v
                break
    return (val or "").strip()


def runtime_secret() -> Optional[bytes]:
    s = (os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET") or "").strip()
    return s.encode("utf-8") if s else None


def is_runtime_enabled() -> bool:
    return runtime_secret() is not None


def extract_runtime_token(headers: Mapping[str, str]) -> Optional[str]:
    return _header(headers, RUNTIME_TOKEN_HEADER) or None


def extract_api_key(
    headers: Mapping[str, str], query: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    key = _header(headers, "X-API-Key")
    if key:
        return key
    auth = _header(headers, "Authorization")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    # LEGACY / compat only: `?key=` carries the key in the URL. Kept working for
    # old integrations; the access log redacts it. Do not promote in docs.
    if query is not None:
        qget = getattr(query, "get", None)
        qkey = (qget("key") if qget is not None else None) or ""
        qkey = qkey.strip()
        if qkey:
            return qkey
    return None


def resolve_runtime_claims(headers: Mapping[str, str]) -> Optional[dict]:
    """Verified claims when a runtime token is present AND enabled, else None.

    Raises ``AuthError(401)`` when a token IS present but invalid/expired, so the
    caller fails closed instead of falling back to another credential.
    """
    token = extract_runtime_token(headers)
    if not token:
        return None
    secret = runtime_secret()
    if secret is None:
        return None  # feature disabled — ignore the header entirely
    try:
        return runtime_token.verify(secret, token)
    except runtime_token.TokenError as e:
        raise AuthError(401, "unauthorized", str(e)) from e


def authorize_scope(claims: Optional[dict], user_id: str, scope: str) -> None:
    """Enforce a runtime-token request carries ``scope`` (plan slice 4).

    No-op for api-key auth (``claims`` falsy — the long-term key is full access).
    Raises ``AuthError(403)`` when a runtime-token request's scope list does not
    include ``scope`` or the token was minted for a different user.
    """
    if not claims:
        return
    try:
        runtime_token.authorize(claims, user_id=user_id or "", scope=scope)
    except runtime_token.TokenError as e:
        raise AuthError(403, "forbidden", str(e)) from e


def resolve_user(
    headers: Mapping[str, str], query: Optional[Mapping[str, str]] = None
) -> AuthResult:
    """Resolve the authenticated user from headers (+ optional query for ?key=).

    Raises ``AuthError(401)`` on any auth failure. Never touches request-scoped
    globals — the caller wires identity into ``g`` / ``request.state``.
    """
    claims = resolve_runtime_claims(headers)  # raises AuthError(401) on bad token
    if claims is not None:
        user_id = str(claims.get("user_id") or "")
        if not user_id or registry._user_entry_snapshot(user_id) is None:
            raise AuthError(401, "unauthorized", "unknown_user")
        return AuthResult(
            store=core_store.get_store_shell_only(
                user_id, reason="authentication returns identity, locks, and waiters"
            ),
            user_id=user_id,
            runtime_token_claims=claims,
            api_key=None,
        )

    key = extract_api_key(headers, query)
    if not key:
        raise AuthError(401, "unauthorized", "no_credential")
    user_id = registry._resolve_user(key)
    if not user_id:
        raise AuthError(401, "unauthorized", "bad_api_key")
    return AuthResult(
        store=core_store.get_store_shell_only(
            user_id, reason="authentication returns identity, locks, and waiters"
        ),
        user_id=user_id,
        runtime_token_claims=None,
        api_key=key,
    )
