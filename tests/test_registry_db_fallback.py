"""Regression: `_resolve_user` must fall back to the DB when this worker's
in-memory registry is stale.

Root cause (prod, 2026-07-24): under many gunicorn workers, a newly registered
user is only in the worker that handled the register; the cross-worker
``wake_bus "users"`` reload does not always land on every worker (worker count
can exceed the number of live LISTEN connections). A worker that missed the
reload has the user in the DB but not in its ``_users`` snapshot, and
``_resolve_user`` returned ``None`` on the in-memory miss → ~50% of requests for
any new user got 401 until a backend restart.

Fix: on an in-memory miss, resolve against the DB (the shared source of truth)
and lazy-load the row into this worker's registry. Invalid keys are
negative-cached so a bogus key cannot hammer the DB on every request.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from accounts import registry  # noqa: E402


def _mk_user_entry(uid: str, api_key: str, *, revoked: bool = False) -> dict:
    h = registry._hash_api_key(api_key)
    return {
        "user_id": uid,
        "principal_id": f"prin_{uid}",
        "api_key_hash": h,
        "api_keys": [{
            "key_id": "key_primary",
            "api_key_hash": h,
            "access_mode": "model_api",
            "label": "Primary",
            "created_at": "2026-01-01T00:00:00",
            "revoked_at": "revoked" if revoked else "",
        }],
        "access_bindings": [],
        "public_key": "",
        "created_at": "2026-01-01T00:00:00",
    }


def _fresh_neg_cache():
    with registry._resolve_neg_lock:
        registry._resolve_neg_cache.clear()


def test_resolve_falls_back_to_db_when_registry_stale(backend_env):
    """User in DB but absent from this worker's in-memory registry → resolve
    must still succeed by consulting the DB (the stale-worker 401 fix)."""
    _fresh_neg_cache()
    api_key = "k_" + secrets.token_hex(16)
    uid = "usr_stale" + secrets.token_hex(4)
    db.upsert_user(_mk_user_entry(uid, api_key))

    # backend_env leaves _users empty → guaranteed in-memory miss.
    assert registry._resolve_user(api_key) == uid, "must fall back to the DB"

    # And the row is now cached in this worker's registry (no repeat DB hit).
    with registry._users_lock:
        assert registry._key_to_user.get(registry._hash_api_key(api_key)) == uid
        assert any(u.get("user_id") == uid for u in registry._users)


def test_resolve_falls_back_via_api_keys_array_entry(backend_env):
    """Match must work through the ``api_keys[]`` entry, not just legacy
    ``api_key_hash`` — that is the path modern accounts use."""
    _fresh_neg_cache()
    api_key = "k_" + secrets.token_hex(16)
    uid = "usr_arr" + secrets.token_hex(4)
    entry = _mk_user_entry(uid, api_key)
    entry.pop("api_key_hash")  # only the api_keys[] entry carries the hash
    db.upsert_user(entry)

    assert registry._resolve_user(api_key) == uid


def test_revoked_key_does_not_resolve(backend_env):
    """A revoked api key must never resolve, even via the DB fallback."""
    _fresh_neg_cache()
    api_key = "k_" + secrets.token_hex(16)
    uid = "usr_rev" + secrets.token_hex(4)
    entry = _mk_user_entry(uid, api_key, revoked=True)
    entry.pop("api_key_hash")  # legacy primary absent; only a revoked keys[] entry
    db.upsert_user(entry)

    assert registry._resolve_user(api_key) is None


def test_unknown_key_returns_none_and_is_negative_cached(backend_env, monkeypatch):
    """An invalid key returns None and is negative-cached so a second lookup
    does NOT hit the DB again (protects the DB from bogus-key floods)."""
    _fresh_neg_cache()
    calls: list[str] = []
    real = db.find_user_by_api_key_hash

    def _counting(h):
        calls.append(h)
        return real(h)

    monkeypatch.setattr(db, "find_user_by_api_key_hash", _counting)

    bogus = "k_" + secrets.token_hex(16)
    assert registry._resolve_user(bogus) is None
    assert registry._resolve_user(bogus) is None
    assert len(calls) == 1, "second lookup must be served by the negative cache"


def test_transient_db_error_does_not_poison_negative_cache(backend_env, monkeypatch):
    """A transient DB failure must NOT be negative-cached: it degrades to None
    for that one attempt, and the next request (DB recovered) resolves. Caching
    the error as a miss would pin a valid key to 401 for the whole TTL — the
    exact failure mode the review flagged."""
    _fresh_neg_cache()
    api_key = "k_" + secrets.token_hex(16)
    uid = "usr_dberr" + secrets.token_hex(4)
    db.upsert_user(_mk_user_entry(uid, api_key))

    real = db.find_user_by_api_key_hash
    n = {"calls": 0}

    def flaky(h):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("transient pool timeout")
        return real(h)

    monkeypatch.setattr(db, "find_user_by_api_key_hash", flaky)

    assert registry._resolve_user(api_key) is None  # transient error → unauth
    # DB recovered: the key must resolve now, NOT be blocked by a poisoned cache.
    assert registry._resolve_user(api_key) == uid
    assert n["calls"] == 2, "second attempt must re-query the DB (no neg-cache)"


def test_find_user_by_api_key_hash_sql(backend_env):
    """The DB primitive itself: matches legacy + keys[] hashes, excludes revoked,
    returns None for an unknown hash."""
    _fresh_neg_cache()
    api_key = "k_" + secrets.token_hex(16)
    uid = "usr_sql" + secrets.token_hex(4)
    db.upsert_user(_mk_user_entry(uid, api_key))
    h = registry._hash_api_key(api_key)

    doc = db.find_user_by_api_key_hash(h)
    assert doc is not None and doc.get("user_id") == uid
    assert db.find_user_by_api_key_hash("deadbeef" * 8) is None
