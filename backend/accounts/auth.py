"""Request authentication: api-key extraction and the per-request gate."""

from flask import abort, g, request

from accounts import registry
from accounts import runtime_auth
from core import store as core_store
from core.runtime_token import TokenError
from core.store import UserStore

def _extract_api_key() -> str | None:
    key = request.headers.get("X-API-Key", "").strip()
    if key:
        return key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # LEGACY / compat only: `?key=` carries the key in the URL, where it leaks
    # into ingress access logs and client history. Kept working for old
    # integrations; new callers must use the X-API-Key or Bearer header above.
    # Do not promote this in docs — the access log redacts it (see app.py).
    qkey = request.args.get("key", "").strip()
    if qkey:
        return qkey
    return None


def require_user() -> UserStore:
    """Return the UserStore for the current request. Aborts 401 on bad auth.

    A runtime token (Stage D) is tried first when present: it carries its own
    user identity, so the consumer never needs the long-term API key. A token
    that is present but invalid/expired fails closed (no fall-back). When no
    token is sent — or the feature is disabled — the API-key path is unchanged.
    """
    try:
        claims = runtime_auth.resolve_claims()
    except TokenError:
        abort(401)
    if claims is not None:
        user_id = str(claims.get("user_id") or "")
        if not user_id or registry._user_entry_snapshot(user_id) is None:
            abort(401)
        g.user_id = user_id
        g.runtime_token_claims = claims
        return core_store.get_store(user_id)

    key = _extract_api_key()
    if not key:
        abort(401)
    user_id = registry._resolve_user(key)
    if not user_id:
        abort(401)
    g.user_id = user_id
    store = core_store.get_store(user_id)
    store.last_seen_api_key = key
    return store
