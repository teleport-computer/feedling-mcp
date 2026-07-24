"""Runtime-token authentication (Stage D) — scope re-check helper.

Lets a hosted worker authenticate with a short-lived, user-scoped runtime
token (minted by the trusted Runtime V2 worker) instead of the user's
long-term Feedling API key, so a compromised consumer can't leak a long-lived
credential. Verified against the shared HMAC secret
``FEEDLING_RUNTIME_TOKEN_SECRET``; the WHOLE feature is OFF unless that secret is
set, so independent API-key callers are unaffected.

The framework-neutral logic (secret, token extraction, scope authorization)
lives in ``accounts.auth_core`` so the ASGI routes share one source of truth
(plan §7.1). The scoped ASGI routes enforce scope at the route boundary via
``asgi.deps.require_scope``; ``authorize_scope`` here is the historical
tool-executor-level re-check, now reading the resolved claims from the ASGI
request context (``asgi.context``) instead of ``flask.g``.
"""

from __future__ import annotations

from accounts import auth_core

RUNTIME_TOKEN_HEADER = auth_core.RUNTIME_TOKEN_HEADER


def _secret() -> bytes | None:
    return auth_core.runtime_secret()


def is_enabled() -> bool:
    return auth_core.is_runtime_enabled()


def authorize_scope(scope: str) -> None:
    """Enforce that a runtime-token request carries ``scope`` (slice 4).

    No-op for api-key auth (the long-term key is full-access) and when the
    feature is off. Aborts (raises ``auth_core.AuthError``) when the request
    authenticated with a runtime token whose ``scope`` list does not include
    ``scope``. Reads the resolved claims / user id from the ASGI request context
    — the flask-free successor to the old ``flask.g`` read.
    """
    from asgi.context import current_runtime_claims, current_user_id

    auth_core.authorize_scope(
        current_runtime_claims.get(None), current_user_id.get("-"), scope
    )
