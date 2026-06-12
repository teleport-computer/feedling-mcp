"""Request authentication: api-key extraction and the per-request gate."""

from flask import abort, g, request

from accounts import registry
from core import store as core_store
from core.store import UserStore

def _extract_api_key() -> str | None:
    key = request.headers.get("X-API-Key", "").strip()
    if key:
        return key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    qkey = request.args.get("key", "").strip()
    if qkey:
        return qkey
    return None


def require_user() -> UserStore:
    """Return the UserStore for the current request. Aborts 401 on bad auth."""
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
