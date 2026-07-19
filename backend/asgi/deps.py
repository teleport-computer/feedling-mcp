"""FastAPI auth dependencies (ASGI-migration plan §7.1 / §5.9).

The ASGI counterpart of the Flask ``auth.require_user()`` wrapper: resolve the
caller via the framework-neutral ``accounts.auth_core``, wire the identity into
the request-scoped ``current_user_id`` contextvar (so the access-log middleware
and downstream code see it — the ASGI equivalent of ``g.user_id``), and hand the
route the resolved store. ``AuthError`` raised in here propagates to the
registered exception handler, which renders the fixed 401/403 body.

Resolution runs on the bounded threadpool because ``auth_core`` performs
registry / store lookups that may touch sync ``db.py`` — a red-line if called
directly on the event loop (plan §5.0/§7.2).
"""

from __future__ import annotations

from fastapi import Depends, Request

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.context import current_runtime_claims, current_user_id


async def require_auth(request: Request) -> AuthResult:
    # Plain dicts (not the live request objects) cross into the thread; auth_core
    # does case-insensitive header lookup so this is safe.
    headers = dict(request.headers)
    query = dict(request.query_params)
    result = await threadpool.run_db(auth_core.resolve_user, headers, query)
    current_user_id.set(result.user_id)
    current_runtime_claims.set(result.runtime_token_claims)
    if result.api_key is not None:
        # Preserve the Flask side effect (api-key path records last_seen).
        result.store.last_seen_api_key = result.api_key
    return result


def require_scope(scope: str):
    """Dependency factory enforcing a runtime-token scope (plan slice 4).

    No-op for api-key auth (full access). Use on scoped routes:
    ``auth: AuthResult = Depends(require_scope("memory"))``.
    """

    async def _dep(auth: AuthResult = Depends(require_auth)) -> AuthResult:
        auth_core.authorize_scope(auth.runtime_token_claims, auth.user_id, scope)
        return auth

    return _dep


async def require_api_key(auth: AuthResult = Depends(require_auth)) -> AuthResult:
    """Restrict a route to long-term api-key callers only (reject runtime tokens).

    Management endpoints (e.g. the user-MCP server config surface) are the iOS
    control plane: only a human holding the long-term Feedling api key may change
    config. A hosted consumer authenticates with a short-lived runtime token
    (``runtime_token_claims`` set) and has no business mutating config — it is a
    read-only信封 consumer — so it is refused here with 403.

    Unlike ``require_scope`` this is a hard api-key gate: no scope on a runtime
    token can satisfy it.
    """
    if auth.runtime_token_claims is not None:
        raise auth_core.AuthError(403, "forbidden", "api_key_required")
    return auth
