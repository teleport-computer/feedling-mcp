"""Review follow-ups on the targeted `users` reload (see
docs/incidents/2026-07-25-users-reload-storm-resident-heartbeat.md).

The first cut of `reload_user` traded away three properties `load_users` had:

1. read-and-install atomicity — it read the row OUTSIDE `_users_lock` and
   installed it inside, so a concurrent same-process edit could be reverted in
   memory and then written back to the DB by the next whole-doc upsert;
2. normalization — it installed rows verbatim, while `load_users` normalizes AND
   CAS-persists, which admin data_track relies on (it snapshots `_users` raw);
3. a periodic full refresh — the untargeted heartbeat broadcast was, by
   accident, a ~1.5s full-registry repair for every process. Narrowing it left
   nothing to repair a worker that dropped a notify.

Plus a cost the narrowing did not actually remove: a one-row refresh still
cleared and rebuilt the WHOLE key cache.

Run:  python -m pytest tests/test_users_reload_review_fixes.py -q
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from core import wake_bus  # noqa: E402


@pytest.fixture()
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(wake_bus, "notify", lambda ch, uid="": None)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with registry._resolve_neg_lock:
        registry._resolve_neg_cache.clear()
    return None


def _register() -> tuple[str, str]:
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(os.urandom(32)).decode("ascii"),
              "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --- 1. read-and-install atomicity ----------------------------------------


def test_targeted_reload_reads_the_row_under_users_lock(clean, monkeypatch):
    """Reading outside the lock lets a stale snapshot overwrite a newer edit."""
    user_id, _api_key = _register()
    observed: list[bool] = []
    real_load_user = db.load_user

    def spy(uid):
        observed.append(registry._users_lock.locked())
        return real_load_user(uid)

    monkeypatch.setattr(db, "load_user", spy)
    registry.reload_user(user_id)

    assert observed == [True], (
        "db.load_user must run under _users_lock so the read and the install "
        "are atomic — load_users() reads inside the lock for this exact reason"
    )


# --- 2. normalization parity with load_users ------------------------------


def test_targeted_reload_normalizes_and_cas_persists_a_legacy_row(clean):
    """admin data_track snapshots _users raw and relies on reload normalizing."""
    user_id = "usr_000000000000ab01"
    legacy = {
        "user_id": user_id,
        "api_key_hash": "legacy-targeted-reload-hash",
        "created_at": "2026-01-01T00:00:00",
    }
    db.upsert_user(legacy)

    registry.reload_users_after_notify(user_id)

    cached = next(u for u in registry._users if u.get("user_id") == user_id)
    assert cached.get("principal_id"), "the installed row must be normalized"
    persisted = next(u for u in db.load_all_users() if u["user_id"] == user_id)
    assert persisted.get("principal_id") == cached["principal_id"], (
        "normalization must be CAS-persisted, not just applied in memory — "
        "otherwise every process serves its own generated principal_id"
    )


def test_targeted_reload_does_not_serve_ids_it_could_not_persist(clean, monkeypatch):
    """Mirrors load_users: a failed normalization write serves the row as read."""
    user_id = "usr_000000000000ab02"
    legacy = {
        "user_id": user_id,
        "api_key_hash": "legacy-cas-failure-hash",
        "created_at": "2026-01-01T00:00:00",
    }
    db.upsert_user(legacy)
    monkeypatch.setattr(db, "normalize_user_cas", lambda *_a, **_k: (False, None))

    registry.reload_users_after_notify(user_id)

    cached = next(u for u in registry._users if u.get("user_id") == user_id)
    assert cached == legacy
    assert registry._key_to_user["legacy-cas-failure-hash"] == user_id


# --- 3. periodic full-reload safety net -----------------------------------


def test_periodic_full_reload_repairs_a_process_that_dropped_a_notify(clean, monkeypatch):
    """The heartbeat storm used to be the de-facto repair; make it explicit."""
    user_id, api_key = _register()
    survivor_id, survivor_key = _register()  # keeps the read non-empty
    key_hash = registry._hash_api_key(api_key)
    # This process missed the delete notify entirely: the DB no longer has the
    # user, but the stale key still resolves from the in-memory cache.
    db.delete_user(user_id)
    assert registry._key_to_user.get(key_hash) == user_id

    registry._full_reload_tick()

    assert registry._key_to_user.get(key_hash) is None
    assert not any(u.get("user_id") == user_id for u in registry._users)
    assert registry._key_to_user[registry._hash_api_key(survivor_key)] == survivor_id


def test_full_reload_keeps_the_snapshot_on_a_db_read_error(clean, monkeypatch):
    """A DB blip must never blank the registry — an empty _users 401s everything.

    We tell a failed read from a genuinely empty table via raise_on_error, not a
    row-count heuristic: db.load_all_users(raise_on_error=True) propagates the
    error and load_users keeps the current snapshot.
    """
    user_id, api_key = _register()

    def boom(*_a, **_k):
        raise RuntimeError("transient DB read failure")

    monkeypatch.setattr(db, "load_all_users", boom)
    registry._full_reload_tick()

    assert any(u.get("user_id") == user_id for u in registry._users)
    assert registry._key_to_user.get(registry._hash_api_key(api_key)) == user_id


def test_untargeted_notify_reload_keeps_the_snapshot_on_a_db_error(clean, monkeypatch):
    """The wake-bus reconnect-replay path calls reload_users_after_notify('') →
    bare load_users(); a Postgres blip during replay must not blank _users."""
    user_id, api_key = _register()

    def boom(*_a, **_k):
        raise RuntimeError("blip during reconnect replay")

    monkeypatch.setattr(db, "load_all_users", boom)
    registry.reload_users_after_notify("")

    assert any(u.get("user_id") == user_id for u in registry._users)
    assert registry._key_to_user.get(registry._hash_api_key(api_key)) == user_id


def test_full_reload_clears_the_registry_on_a_genuinely_empty_table(clean, monkeypatch):
    """A real TRUNCATE (0 rows, no error) must still evict the wiped accounts —
    a guard that refuses to ever shrink to zero would let their keys live on."""
    user_id, api_key = _register()

    monkeypatch.setattr(db, "load_all_users", lambda *_a, **_k: [])
    registry._full_reload_tick()

    assert not any(u.get("user_id") == user_id for u in registry._users)
    assert registry._key_to_user.get(registry._hash_api_key(api_key)) is None


def test_periodic_full_reload_thread_starts_once(clean, monkeypatch):
    """Idempotence via a registry-local spawn seam — never by monkeypatching the
    stdlib threading module (that would neuter every other thread this test
    process spawns and hang the suite)."""
    monkeypatch.setattr(registry, "_full_reload_thread", None)
    spawned: list[int] = []

    monkeypatch.setattr(
        registry, "_spawn_full_reload_thread", lambda: spawned.append(1) or object()
    )
    registry.start_periodic_full_reload()
    registry.start_periodic_full_reload()

    assert len(spawned) == 1


# --- 4. key-cache narrowing ------------------------------------------------


def test_targeted_reload_reindexes_only_that_user_s_keys(clean, monkeypatch):
    """Clearing the whole cache 36x/min/process keeps the storm's lock cost."""
    user_a, key_a = _register()
    user_b, key_b = _register()
    hash_a, hash_b = registry._hash_api_key(key_a), registry._hash_api_key(key_b)

    def boom():
        raise AssertionError(
            "a targeted reload must not clear/rebuild the whole key cache"
        )

    monkeypatch.setattr(registry, "_rebuild_key_cache", boom)
    registry.reload_user(user_a)

    assert registry._key_to_user[hash_a] == user_a
    assert registry._key_to_user[hash_b] == user_b


def test_targeted_reload_drops_only_the_deleted_user_s_keys(clean, monkeypatch):
    user_a, key_a = _register()
    user_b, key_b = _register()
    db.delete_user(user_a)

    registry.reload_user(user_a)

    assert registry._key_to_user.get(registry._hash_api_key(key_a)) is None
    assert registry._key_to_user[registry._hash_api_key(key_b)] == user_b


def test_targeted_reload_drops_a_key_revoked_on_the_reloaded_row(clean):
    """The narrow reindex must still evict hashes the new row no longer holds."""
    user_id, api_key = _register()
    key_hash = registry._hash_api_key(api_key)
    persisted = next(u for u in db.load_all_users() if u["user_id"] == user_id)
    persisted["api_key_hash"] = ""
    for key_entry in persisted.get("api_keys") or []:
        key_entry["revoked_at"] = "2026-07-25T00:00:00"
    db.upsert_user(persisted)

    registry.reload_user(user_id)

    assert registry._key_to_user.get(key_hash) is None


def test_targeted_reload_does_not_steal_a_hash_owned_by_another_user(clean):
    """A single-row reload can't see global ORDER-BY precedence, so it must not
    yank a shared/orphan hash away from the user who currently owns it."""
    user_a, key_a = _register()
    user_b, _key_b = _register()
    shared = registry._hash_api_key(key_a)  # currently resolves to user_a
    # Orphan-merge residue: user_b's DB doc also carries user_a's hash.
    pb = next(u for u in db.load_all_users() if u["user_id"] == user_b)
    pb["api_key_hash"] = shared
    db.upsert_user(pb)

    registry.reload_user(user_b)

    assert registry._key_to_user[shared] == user_a, (
        "targeted reload cross-wired a shared hash to the wrong account"
    )


# --- 5. heartbeat must not clobber another worker's write ------------------


def test_resident_heartbeat_does_not_clobber_a_key_issued_elsewhere(clean):
    """The heartbeat persists via CAS on a FRESH db row, so a stale in-memory
    snapshot can never write another field (a key issued on another worker) back.
    """
    user_id, _api_key = _register()
    # Another worker issued key K2 for this user and committed it to the DB.
    k2_hash = "k2hash_issued_by_another_worker_" + "0" * 24
    persisted = next(u for u in db.load_all_users() if u["user_id"] == user_id)
    persisted.setdefault("api_keys", []).append({
        "key_id": "key_second", "api_key_hash": k2_hash, "access_mode": "model_api",
        "label": "API", "created_at": "2026-07-25T00:00:00", "revoked_at": "",
    })
    db.upsert_user(persisted)

    # THIS process's snapshot is stale: it never saw K2, and its resident binding
    # is old enough for the heartbeat throttle to fire.
    with registry._users_lock:
        entry = registry._find_user_entry_locked(user_id)
        binding = registry._upsert_access_binding_locked(entry, "resident")
        binding["last_seen_at"] = "2020-01-01T00:00:00"
        binding["updated_at"] = "2020-01-01T00:00:00"

    assert registry._touch_resident_binding_seen(user_id, min_interval_sec=60) is True

    after = next(u for u in db.load_all_users() if u["user_id"] == user_id)
    hashes = {k.get("api_key_hash") for k in after.get("api_keys") or []}
    assert k2_hash in hashes, "heartbeat overwrote a key issued by another worker"
    resident = next(
        b for b in after["access_bindings"] if b.get("access_mode") == "resident"
    )
    assert resident["last_seen_at"] > "2020-01-01", "heartbeat did not bump last_seen"


def test_resident_heartbeat_retries_when_it_loses_the_cas(clean, monkeypatch):
    """A concurrent writer landing between our read and CAS must not lose the
    heartbeat: it syncs the winner into memory and retries on top."""
    user_id, _api_key = _register()
    with registry._users_lock:
        entry = registry._find_user_entry_locked(user_id)
        binding = registry._upsert_access_binding_locked(entry, "resident")
        binding["last_seen_at"] = "2020-01-01T00:00:00"
        binding["updated_at"] = "2020-01-01T00:00:00"
        registry.persist_user(entry)

    real_cas = db.compare_and_set_user
    calls = {"n": 0}

    def flaky_cas(uid, expected, new):
        calls["n"] += 1
        if calls["n"] == 1:
            # Pretend someone else wrote first: report not-applied, hand back the
            # current DB row as the winner.
            _ok, _applied, current = real_cas(uid, expected, expected)
            return True, False, current
        return real_cas(uid, expected, new)

    monkeypatch.setattr(db, "compare_and_set_user", flaky_cas)
    assert registry._touch_resident_binding_seen(user_id, min_interval_sec=60) is True
    assert calls["n"] == 2

    after = next(u for u in db.load_all_users() if u["user_id"] == user_id)
    resident = next(
        b for b in after["access_bindings"] if b.get("access_mode") == "resident"
    )
    assert resident["last_seen_at"] > "2020-01-01"


# --- 6. env parsing must not crash import ----------------------------------


def test_invalid_full_reload_interval_env_falls_back_instead_of_crashing():
    assert registry._env_float("FEEDLING_REGISTRY_FULL_RELOAD_SEC", 60.0) == 60.0

    import os as _os

    prev = _os.environ.get("FEEDLING_REGISTRY_FULL_RELOAD_SEC")
    _os.environ["FEEDLING_REGISTRY_FULL_RELOAD_SEC"] = "5m"
    try:
        assert registry._env_float("FEEDLING_REGISTRY_FULL_RELOAD_SEC", 60.0) == 60.0
    finally:
        if prev is None:
            _os.environ.pop("FEEDLING_REGISTRY_FULL_RELOAD_SEC", None)
        else:
            _os.environ["FEEDLING_REGISTRY_FULL_RELOAD_SEC"] = prev
