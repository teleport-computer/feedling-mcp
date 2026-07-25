"""Multi-tenant user registry: accounts, api keys, access bindings.

``_users`` / ``_key_to_user`` are the in-memory truth — mutate them in place,
never rebind (tests and the wake-bus reload rely on the objects' identity).
Persistence is PostgreSQL (db.save_all_users / db.upsert_user).
"""

import copy
import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime

import db
from core import util as core_util
from core import wake_bus

_users_lock = threading.Lock()
_users: list[dict] = []                    # [{user_id, principal_id, api_keys, public_key, created_at}]
_key_to_user: dict[str, str] = {}          # api_key_hash -> user_id (in-memory cache)

# Negative cache for the DB miss-fallback in `_resolve_user`: api_key_hash ->
# monotonic expiry. A hash the DB has never heard of is remembered for a short
# TTL so a bogus/forged key cannot hit the DB on every request. Valid keys are
# NEVER negative-cached (they resolve on the first DB read), so a genuine new
# user is unaffected. Cleared on every `load_users()` so a real registration
# that lands between a negative probe and its TTL is picked up immediately.
_resolve_neg_lock = threading.Lock()
_resolve_neg_cache: dict[str, float] = {}

def _env_float(name: str, default: float) -> float:
    """Parse a float env var without letting a typo crash module import. A bad
    value (e.g. ``5m``) degrades to ``default`` with a log line instead of
    raising a ValueError out of ``import accounts.registry`` — which would kill
    every gunicorn worker and the V2 serve_worker at boot, before any health
    endpoint exists to report it."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(f"[users] invalid {name}={raw!r}, falling back to {default}")
        return default


# Periodic full-registry refresh — the explicit replacement for the repair that
# the resident heartbeat's untargeted broadcast used to provide by accident.
# See start_periodic_full_reload. Kept at the ~minute scale (not the old ~1.5s
# storm cadence, not a lazy 300s) so a dropped revoke/delete notify still self-
# heals within roughly a minute while costing one full reload per process per
# interval instead of ~271/min across the fleet.
_FULL_RELOAD_INTERVAL_SEC = max(
    10.0, _env_float("FEEDLING_REGISTRY_FULL_RELOAD_SEC", 60.0)
)
_full_reload_start_lock = threading.Lock()
_full_reload_thread: threading.Thread | None = None
_NEG_CACHE_TTL = max(1.0, float(os.environ.get("FEEDLING_REGISTRY_NEG_CACHE_TTL_SEC", "30") or "30"))
_NEG_CACHE_MAX = 10000

ACCESS_MODES = ("resident", "model_api", "official_import")
ACCESS_MODE_LABELS = {
    "resident": "Server",
    "model_api": "API",
    "official_import": "Official App Chat",
}
_ACCESS_MODE_ALIASES = {
    "server": "resident",
    "resident_agent": "resident",
    "modelapi": "model_api",
    "model_api_key": "model_api",
    "api": "model_api",
    "official": "official_import",
    "official_app": "official_import",
    "official_chat": "official_import",
    "app_chat": "official_import",
    "import_only": "official_import",
}

# API keys are 32 random bytes (high-entropy), so a fast collision-resistant
# hash is sufficient — bcrypt is designed for low-entropy passwords. Using
# SHA-256 over a per-server pepper keeps the hash table safe even if the file
# leaks, while avoiding per-request bcrypt cost (which would be dramatic given
# long-poll + screen-analyze are hit every few seconds).
def _server_pepper() -> bytes:
    """Stable secret for key hashing. Persisted in PostgreSQL (server_config).

    Bootstrap is race-safe: the first writer's pepper wins and every worker
    reads back the same value, so api_key_hashes stay stable. The migration
    script imports the pre-existing .pepper bytes so old api_keys keep working.
    """
    existing = db.get_config("pepper")
    if existing:
        return existing
    return db.set_config_if_absent("pepper", secrets.token_bytes(32))


_pepper_cache: bytes | None = None
_pepper_lock = threading.Lock()


def _pepper() -> bytes:
    """Lazy pepper: resolved on first hash instead of at import, so importing
    this module never requires a reachable database."""
    global _pepper_cache
    if _pepper_cache is None:
        with _pepper_lock:
            if _pepper_cache is None:
                _pepper_cache = _server_pepper()
    return _pepper_cache


def _hash_api_key(api_key: str) -> str:
    return hmac.new(_pepper(), api_key.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalize_access_mode(mode: str) -> str:
    raw = (mode or "").strip().lower().replace("-", "_")
    return _ACCESS_MODE_ALIASES.get(raw, raw)


def _new_principal_id() -> str:
    return f"prn_{secrets.token_hex(8)}"


def _new_key_id() -> str:
    return f"key_{secrets.token_hex(6)}"


def _new_binding_id() -> str:
    return f"bind_{secrets.token_hex(6)}"


def _normalize_api_key_entries(user_entry: dict) -> tuple[list[dict], bool]:
    changed = False
    created_at = str(user_entry.get("created_at") or datetime.now().isoformat())
    existing = user_entry.get("api_keys")
    keys: list[dict] = []
    seen_hashes: set[str] = set()
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                changed = True
                continue
            key_hash = str(item.get("api_key_hash") or "").strip()
            if not key_hash or key_hash in seen_hashes:
                changed = True
                continue
            mode = _normalize_access_mode(str(item.get("access_mode") or "official_import"))
            if mode not in ACCESS_MODES:
                mode = "official_import"
                changed = True
            normalized = {
                "key_id": str(item.get("key_id") or _new_key_id()),
                "api_key_hash": key_hash,
                "access_mode": mode,
                "label": str(item.get("label") or ACCESS_MODE_LABELS.get(mode, mode)),
                "created_at": str(item.get("created_at") or created_at),
                "revoked_at": str(item.get("revoked_at") or ""),
            }
            if normalized != item:
                changed = True
            keys.append(normalized)
            seen_hashes.add(key_hash)
    legacy_hash = str(user_entry.get("api_key_hash") or "").strip()
    if legacy_hash and legacy_hash not in seen_hashes:
        keys.insert(0, {
            "key_id": "key_primary",
            "api_key_hash": legacy_hash,
            "access_mode": "official_import",
            "label": "Primary",
            "created_at": created_at,
            "revoked_at": "",
        })
        changed = True
    return keys, changed


def _normalize_access_bindings(user_entry: dict, api_keys: list[dict]) -> tuple[list[dict], bool]:
    changed = False
    created_at = str(user_entry.get("created_at") or datetime.now().isoformat())
    existing = user_entry.get("access_bindings")
    bindings_by_mode: dict[str, dict] = {}
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                changed = True
                continue
            mode = _normalize_access_mode(str(item.get("access_mode") or item.get("route") or ""))
            if mode not in ACCESS_MODES:
                changed = True
                continue
            normalized = {
                "binding_id": str(item.get("binding_id") or _new_binding_id()),
                "access_mode": mode,
                "label": str(item.get("label") or ACCESS_MODE_LABELS.get(mode, mode)),
                "status": str(item.get("status") or "connected"),
                "created_at": str(item.get("created_at") or created_at),
                "updated_at": str(item.get("updated_at") or item.get("created_at") or created_at),
                "last_seen_at": str(item.get("last_seen_at") or ""),
                "last_key_id": str(item.get("last_key_id") or ""),
            }
            if normalized != item:
                changed = True
            current = bindings_by_mode.get(mode)
            if current is None or normalized["updated_at"] >= current.get("updated_at", ""):
                bindings_by_mode[mode] = normalized
    for key in api_keys:
        if key.get("revoked_at"):
            continue
        mode = _normalize_access_mode(str(key.get("access_mode") or "official_import"))
        if mode not in ACCESS_MODES:
            continue
        if mode not in bindings_by_mode:
            bindings_by_mode[mode] = {
                "binding_id": _new_binding_id(),
                "access_mode": mode,
                "label": ACCESS_MODE_LABELS.get(mode, mode),
                "status": "connected",
                "created_at": str(key.get("created_at") or created_at),
                "updated_at": str(key.get("created_at") or created_at),
                "last_seen_at": "",
                "last_key_id": str(key.get("key_id") or ""),
            }
            changed = True
    return list(bindings_by_mode.values()), changed


def _normalize_user_entry(user_entry: dict) -> bool:
    changed = False
    if not str(user_entry.get("principal_id") or "").strip():
        user_entry["principal_id"] = _new_principal_id()
        changed = True
    keys, key_changed = _normalize_api_key_entries(user_entry)
    if key_changed or user_entry.get("api_keys") != keys:
        user_entry["api_keys"] = keys
        changed = True
    bindings, binding_changed = _normalize_access_bindings(user_entry, keys)
    if binding_changed or user_entry.get("access_bindings") != bindings:
        user_entry["access_bindings"] = bindings
        changed = True
    return changed


def _normalize_all_users() -> bool:
    changed = False
    for user_entry in _users:
        if isinstance(user_entry, dict):
            changed = _normalize_user_entry(user_entry) or changed
    return changed


def _normalize_all_users_cas() -> bool:
    """Normalize loaded rows without ever rewriting a stale registry snapshot.

    ``load_users`` runs asynchronously in every process after ``users`` NOTIFY
    and LISTEN reconnects. Another process can register or edit an account
    after this process read its snapshot. Persist each changed row with a JSONB
    compare-and-swap so normalization can neither delete a missing new account
    nor overwrite a newer version of the same account.
    """
    originals = {
        str(user_entry.get("user_id")): copy.deepcopy(user_entry)
        for user_entry in _users
        if isinstance(user_entry, dict) and user_entry.get("user_id")
    }
    changed = _normalize_all_users()
    if not changed:
        return False
    authoritative_users: list[dict] = []
    for user_entry in _users:
        if not isinstance(user_entry, dict):
            continue
        user_id = str(user_entry.get("user_id") or "")
        expected = originals.get(user_id)
        if expected is not None and expected != user_entry:
            read_ok, authoritative = db.normalize_user_cas(
                user_id, expected, user_entry
            )
            if read_ok:
                if authoritative is not None:
                    authoritative_users.append(authoritative)
                continue
            # Do not serve generated IDs that failed to persist. Keep the exact
            # row this process read; a later reconnect/NOTIFY can retry safely.
            authoritative_users.append(expected)
            continue
        authoritative_users.append(user_entry)
    _users[:] = authoritative_users
    return True


def _index_user_keys_locked(user_entry: dict) -> None:
    """Point every LIVE key hash of one row at its user_id, unconditionally
    (last-writer-wins — the full rebuild walks rows in ``db.load_all_users``'
    deterministic order, so a shared hash resolves to the last row). Caller holds
    ``_users_lock``."""
    user_id = str(user_entry.get("user_id") or "")
    if not user_id:
        return
    for key_hash in _live_key_hashes(user_entry):
        _key_to_user[key_hash] = user_id


def _live_key_hashes(user_entry: dict) -> set[str]:
    """The LIVE api-key hashes of one row (legacy top-level + non-revoked
    ``api_keys``). Same liveness rule as ``_index_user_keys_locked`` so the two
    can't disagree on what resolves."""
    hashes: set[str] = set()
    if not isinstance(user_entry, dict) or not user_entry.get("user_id"):
        return hashes
    legacy_hash = str(user_entry.get("api_key_hash") or "").strip()
    if legacy_hash:
        hashes.add(legacy_hash)
    for key_entry in user_entry.get("api_keys") or []:
        if not isinstance(key_entry, dict) or key_entry.get("revoked_at"):
            continue
        key_hash = str(key_entry.get("api_key_hash") or "").strip()
        if key_hash:
            hashes.add(key_hash)
    return hashes


def _reindex_user_keys_locked(user_id: str, user_entry: dict | None) -> None:
    """Re-point ``_key_to_user`` at ONE user's current hashes — the targeted
    counterpart of ``_rebuild_key_cache``, and the reason a one-row reload does
    not clear the cache.

    Two things this must NOT do, both because ``_resolve_user`` reads
    ``_key_to_user`` WITHOUT the lock:

    * Never make a still-valid hash momentarily absent. A clear-then-rebuild (or
      delete-then-readd) opens a window where a live key misses, takes
      ``_users_lock`` and runs ``_normalize_all_users_cas`` (a deepcopy of every
      row) — the exact convoy the storm fix exists to remove. So ADD the new
      hashes first, then remove only the ones this user no longer has.
    * Never steal a hash currently owned by ANOTHER user. A single-row reload
      cannot see the global ``ORDER BY`` precedence that ``_rebuild_key_cache``
      resolves a shared/orphan hash by, so overwriting it here would cross-wire
      auth to the wrong account until the next full rebuild. Leave a conflicting
      hash to whoever owns it; the periodic full reload settles it deterministically.

    Caller holds ``_users_lock``."""
    new_hashes = _live_key_hashes(user_entry) if isinstance(user_entry, dict) else set()
    for key_hash in new_hashes:
        owner = _key_to_user.get(key_hash)
        if owner is None or owner == user_id:
            _key_to_user[key_hash] = user_id
        else:
            print(
                f"[users] key hash conflict: {key_hash[:12]}… claimed by "
                f"{owner}, not reassigning to {user_id} (full reload will settle)"
            )
    for key_hash in [
        h for h, uid in list(_key_to_user.items())
        if uid == user_id and h not in new_hashes
    ]:
        del _key_to_user[key_hash]


def _rebuild_key_cache() -> None:
    _key_to_user.clear()
    for user_entry in _users:
        if isinstance(user_entry, dict):
            _index_user_keys_locked(user_entry)


def load_users():
    """(Re)load the registry from PostgreSQL IN PLACE — ``_users`` keeps its
    object identity because tests operate on ``accounts.registry._users``
    directly (see tests/conftest.py: ``registry._users[:] = []`` /
    ``registry._users.append``); rebinding would silently fork the registry.

    Holds ``_users_lock`` for the whole reload: under -w N this also runs on the
    wake-bus listener thread (the ``users`` channel handler), concurrently with
    request threads that mutate the registry under the same lock — without it the
    listener could replace ``_users`` / ``_key_to_user`` mid-edit and lose a
    write or expose a half-rebuilt key cache. Callers must NOT already hold the
    lock (the two callers — startup assembly and the listener — don't).

    A DB READ FAILURE keeps the current snapshot instead of blanking it. Every
    caller of this — startup, the wake-bus ``users`` handler AND its
    reconnect-replay, the periodic refresh — could otherwise land on a Postgres
    blip and leave ``_users`` empty, which 401s every request until the next
    successful reload (see the lifespan-missing-load_users incident). We tell a
    failed read from a genuinely empty table via ``raise_on_error`` rather than a
    row-count heuristic, so a real ``TRUNCATE users`` (0 rows, no error) still
    correctly clears the registry."""
    with _users_lock:
        try:
            rows = db.load_all_users(raise_on_error=True)
        except Exception as e:
            print(f"[users] reload read failed, keeping current snapshot: {e}")
            return
        _users[:] = rows
        _normalize_all_users_cas()
        _rebuild_key_cache()
    # Drop the negative cache AFTER releasing _users_lock (no nested lock order):
    # a fresh full snapshot invalidates any prior "DB has never heard of this
    # hash" verdict, so a key negative-cached moments before a registration lands
    # resolves on the next lookup instead of waiting out its TTL.
    with _resolve_neg_lock:
        _resolve_neg_cache.clear()
    print(f"[users] loaded {len(_users)} user(s)")


def _save_users():
    """Persist the WHOLE in-memory user registry to PostgreSQL (full rewrite via
    db.save_all_users). This destructive helper remains for explicit test resets
    and offline whole-snapshot tooling only. Production normalization uses
    per-row JSONB compare-and-swap; genuine user edits use ``persist_user``.
    Never call this from a request, startup, or wake-bus reload path."""
    db.save_all_users(_users)


def notify_users_changed(user_id: str = "") -> None:
    """Broadcast a cross-worker ``users`` reload so other workers don't keep
    serving a stale ``_users`` / ``_key_to_user`` snapshot. Pair it with the
    persist for any genuine registry edit; ``persist_user`` already does.

    Passing ``user_id`` narrows the reload to that ONE row (see
    ``reload_users_after_notify``); the empty default reloads the whole
    registry, which is what an edit with cross-row effects (a merge, a delete)
    needs."""
    wake_bus.notify("users", user_id)


def _normalize_row_cas_locked(row: dict) -> dict | None:
    """Single-row twin of ``_normalize_all_users_cas`` — same contract, same
    reasons. Returns the row to install, or None when the CAS read found the row
    concurrently deleted. Caller holds ``_users_lock``."""
    expected = copy.deepcopy(row)
    if not _normalize_user_entry(row):
        return row
    read_ok, authoritative = db.normalize_user_cas(
        str(row.get("user_id") or ""), expected, row
    )
    if not read_ok:
        # Never serve process-local generated ids that failed to persist; keep
        # the exact row we read and let a later reload retry.
        return expected
    return authoritative


def _install_user_row_locked(user_id: str, row: dict | None) -> bool:
    """Replace / insert / remove ONE row in ``_users`` and reindex its keys.
    ``row=None`` removes the user. Returns True when the row was newly inserted
    (a user this process had not seen). Caller holds ``_users_lock``.

    Appends a new row at the end rather than re-sorting ``_users``: the DB read
    order is not a load-bearing invariant (``_resolve_user_via_db`` already
    appends unsorted), and a Python sort key over possibly-non-dict entries is
    one more place to raise mid-critical-section."""
    index = next(
        (
            i
            for i, entry in enumerate(_users)
            if isinstance(entry, dict) and entry.get("user_id") == user_id
        ),
        None,
    )
    inserted = False
    if row is None:
        if index is not None:
            _users.pop(index)
    elif index is None:
        _users.append(row)
        inserted = True
    else:
        _users[index] = row
    _reindex_user_keys_locked(user_id, row)
    return inserted


def reload_user(user_id: str) -> None:
    """Refresh ONE user row in place from PostgreSQL — the targeted counterpart
    of ``load_users``, and it must keep that function's three guarantees:

    * The read happens INSIDE ``_users_lock``, so the read and the install are
      atomic. Reading first and installing later lets a same-process edit that
      lands in between be reverted in memory — and the next whole-document
      ``persist_user`` then writes the reverted row back, permanently dropping a
      key that edit had just issued.
    * The row is normalized and CAS-persisted, not installed verbatim: admin
      data_track snapshots ``_users`` raw and relies on reload-time
      normalization, and lazy normalization in ``_find_user_entry_locked`` only
      applies in memory — it never writes the generated ids back.
    * A read failure leaves the current row untouched: the only other option is
      treating a transient error as "row deleted" and evicting a live user,
      which 401s every request that key makes until the next full reload.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return
    inserted = False
    with _users_lock:
        try:
            row = db.load_user(uid)
        except Exception as e:
            print(f"[users] targeted reload failed for {uid}, keeping current row: {e}")
            return
        if row is not None:
            row = _normalize_row_cas_locked(row)
        inserted = _install_user_row_locked(uid, row)
    if inserted:
        # Same reason load_users() clears it, and with the same lock order (never
        # under _users_lock): a row this process had never seen may hold a key
        # some earlier lookup negative-cached. An UPDATE needs no clear — a key
        # set only changes on issue/revoke, and those broadcast untargeted.
        with _resolve_neg_lock:
            _resolve_neg_cache.clear()


def _full_reload_tick() -> None:
    """One periodic full-registry refresh. Best-effort: a failure here must
    never kill the thread that is the last line of defence. ``load_users`` itself
    keeps the current snapshot on a DB read error, so a blip is a no-op here."""
    try:
        load_users()
    except Exception as e:
        print(f"[users] periodic full reload failed: {e}")


def _full_reload_loop() -> None:
    while True:
        time.sleep(_FULL_RELOAD_INTERVAL_SEC)
        _full_reload_tick()


def _spawn_full_reload_thread() -> "threading.Thread":
    """Create+start the daemon thread. A module-level seam (not an inline
    ``threading.Thread(...)``) so a test can stub THIS instead of monkeypatching
    the stdlib ``threading.Thread`` process-wide — which would silently neuter
    every other thread spawned during that test (pool workers, the wake-bus
    listener) and hang the suite."""
    thread = threading.Thread(
        target=_full_reload_loop, name="users-full-reload", daemon=True
    )
    thread.start()
    return thread


def start_periodic_full_reload() -> None:
    """Start this process's periodic full-registry refresh (idempotent).

    A cross-worker ``users`` notify is best-effort — ``wake_bus._dispatch``
    swallows a handler exception, and a notify emitted while this process's
    LISTEN is down is simply lost. Until 2026-07-25 that never mattered: the
    resident heartbeat broadcast untargeted ~38.7 times/min, so every process
    re-ran the full ``load_users`` every ~1.5s and any state it had wrong (a
    stale ``_key_to_user`` entry pointing at a revoked key, a row deleted
    elsewhere) was repaired within seconds. Narrowing that broadcast to a single
    row removed the accidental repair channel, so this makes it explicit and
    cheap: one full reload per process per interval instead of ~271/min across
    the fleet.

    Wired from the process's runtime entrypoint only (lifespan's background gate,
    serve_worker ``main``), never from the test-facing ``wire_assembly`` — an
    unstoppable daemon that rewrites ``registry._users`` mid-suite would flake
    unrelated tests."""
    global _full_reload_thread
    with _full_reload_start_lock:
        if _full_reload_thread is not None:
            return
        _full_reload_thread = _spawn_full_reload_thread()


def reload_users_after_notify(user_id: str = "") -> None:
    """Wake-bus ``users`` handler: refresh what the notify says changed.

    An untargeted notify still reloads the whole registry. A targeted one
    reloads a single row, which is what keeps the resident poll heartbeat from
    turning ~36 per-row writes/min into a full-table re-read in every
    subscribing process (prod 2026-07-24: 38.7 users-NOTIFY/min × 667 rows × 7
    processes, scaling linearly with the number of online residents)."""
    if str(user_id or "").strip():
        reload_user(user_id)
        return
    load_users()


def persist_user(entry: dict) -> None:
    """Persist ONE edited user row (non-destructive per-row upsert) and broadcast
    an UNTARGETED cross-worker reload. This is the multi-worker-safe way to
    persist a genuine single-user edit (registration, api-key add, access-binding
    flip, public-key / preference change). It replaces ``_save_users(broadcast=
    True)``, whose ``db.save_all_users`` does a DELETE-all + reinsert from THIS
    worker's in-memory snapshot — under ``-w N`` two workers editing DIFFERENT
    users concurrently could each wipe the other's user before the NOTIFY reload
    lands. A per-row upsert only touches the edited row. Caller holds
    ``_users_lock``.

    The broadcast is deliberately untargeted: every caller is a sparse edit that
    can affect a key set, so other processes take a full reload (which also
    clears their negative cache — ``reload_user`` only does so on insert). The
    one high-frequency, row-confined writer, the resident heartbeat, does NOT go
    through here — it persists via ``_persist_resident_seen_cas`` and emits its
    own targeted ``notify_users_changed(user_id)``."""
    db.upsert_user(entry)
    notify_users_changed()


def _neg_cached(h: str) -> bool:
    """True while ``h`` is within its negative-cache TTL (the DB has already
    answered 'no such user'). Checked before the in-memory scan so a bogus key
    skips both the DB round-trip AND the ``_users_lock`` normalize+scan."""
    now = time.monotonic()
    with _resolve_neg_lock:
        exp = _resolve_neg_cache.get(h)
        return exp is not None and exp > now


def _resolve_user(api_key: str) -> str | None:
    if not api_key:
        return None
    h = _hash_api_key(api_key)
    uid = _key_to_user.get(h)
    if uid:
        return uid
    if _neg_cached(h):
        return None
    with _users_lock:
        _normalize_all_users_cas()
        for u in _users:
            if u.get("api_key_hash") == h:
                _key_to_user[h] = u["user_id"]
                return u["user_id"]
            for key_entry in u.get("api_keys") or []:
                if not isinstance(key_entry, dict) or key_entry.get("revoked_at"):
                    continue
                if key_entry.get("api_key_hash") == h:
                    _key_to_user[h] = u["user_id"]
                    return u["user_id"]
    # In-memory miss. Under -w N a just-registered user may not yet be in THIS
    # worker's snapshot (the cross-worker "users" reload can be missed when a
    # worker's wake-bus LISTEN is down). The DB is the shared source of truth —
    # consult it before rejecting, so a valid key never 401s on a stale worker.
    return _resolve_user_via_db(h)


def _resolve_user_via_db(h: str) -> str | None:
    """DB miss-fallback for `_resolve_user`. Resolves ``h`` against the DB and
    lazy-loads the row into this worker's registry on a hit; negative-caches ONLY
    a definitive miss so a bogus key cannot hammer the DB.

    Critically distinguishes a DB error from a genuine miss: on a transient DB
    failure (`find_user_by_api_key_hash` raises) it degrades to None WITHOUT
    negative-caching, so the next request re-queries once the DB recovers. Were
    it to cache the error as a miss, one pool-timeout hiccup would pin a VALID
    key to 401 for the whole TTL — and on the stale workers this fallback exists
    for, `load_users()` (the only other cache-clear) never fires."""
    try:
        doc = db.find_user_by_api_key_hash(h)
    except Exception:
        return None  # transient DB error — retry next request, do NOT neg-cache
    if not isinstance(doc, dict) or not doc.get("user_id"):
        # The query ran and definitively found nothing → safe to negative-cache.
        with _resolve_neg_lock:
            if len(_resolve_neg_cache) >= _NEG_CACHE_MAX:
                _resolve_neg_cache.clear()
            _resolve_neg_cache[h] = time.monotonic() + _NEG_CACHE_TTL
        return None
    uid = str(doc["user_id"])
    with _users_lock:
        if _find_user_entry_locked(uid) is None:
            _users.append(doc)
        _key_to_user[h] = uid
    return uid


def _register_user(public_key: str | None = None,
                   archive_language: str | None = None,
                   access_mode: str = "official_import",
                   label: str | None = None) -> dict:
    user_id = f"usr_{secrets.token_hex(8)}"
    principal_id = _new_principal_id()
    api_key = secrets.token_hex(32)
    api_key_hash = _hash_api_key(api_key)
    mode = _normalize_access_mode(access_mode)
    if mode not in ACCESS_MODES:
        mode = "official_import"
    key_id = "key_primary"
    now_iso = datetime.now().isoformat()
    entry = {
        "user_id": user_id,
        "principal_id": principal_id,
        "api_key_hash": api_key_hash,
        "api_keys": [{
            "key_id": key_id,
            "api_key_hash": api_key_hash,
            "access_mode": mode,
            "label": (label or "Primary").strip() or "Primary",
            "created_at": now_iso,
            "revoked_at": "",
        }],
        "access_bindings": [{
            "binding_id": _new_binding_id(),
            "access_mode": mode,
            "label": ACCESS_MODE_LABELS.get(mode, mode),
            "status": "connected",
            "created_at": now_iso,
            "updated_at": now_iso,
            "last_seen_at": "",
            "last_key_id": key_id,
        }],
        "public_key": (public_key or "").strip(),
        "created_at": now_iso,
    }
    # archive_language: the BCP-47-ish locale code the iOS app picked up
    # from Locale.preferredLanguages on the registering device (e.g. "en",
    # "zh-Hans", "ja"). Drives the second defense layer against agent
    # archive-language drift — see /v1/users/preferences for migration
    # path and the skill's "Lock the Memory Garden language" rule for
    # how the agent consumes it.
    if archive_language:
        entry["archive_language"] = archive_language.strip()
    with _users_lock:
        _users.append(entry)
        persist_user(entry)  # per-row upsert (multi-worker-safe) + users broadcast
        _key_to_user[api_key_hash] = user_id
    print(f"[users] registered {user_id} archive_language={entry.get('archive_language', 'unset')}")
    return {"user_id": user_id, "principal_id": principal_id, "api_key": api_key}


def _get_user_archive_language(user_id: str) -> str | None:
    """Return the user's stored archive_language, or None if unset.
    Caller is the source of truth for fallback behavior; this is a thin
    read helper used by /v1/bootstrap, /v1/memory/verify, /v1/users/whoami.
    """
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                val = u.get("archive_language")
                return val if val else None
    return None


def _get_user_timezone(user_id: str) -> str | None:
    """Return the user's stored IANA timezone, or None if unset. Thin read
    mirroring _get_user_archive_language; used by /v1/users/whoami and any
    subsystem needing the user's device timezone."""
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                val = u.get("timezone")
                return val if val else None
    return None


def _is_valid_iana_timezone(tz: str) -> bool:
    """True if tz is a resolvable IANA zone. Empty string is NOT valid here
    (callers treat empty/None as 'clear', handled separately)."""
    if not tz:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def _set_user_timezone(user_id: str, tz: str | None) -> bool:
    """Set (or clear, when tz is falsy) the user's first-class timezone field.
    Validates IANA before writing — never poisons the record with a junk zone.
    Returns True when the record was found and updated, False when the user is
    unknown or tz is a non-empty invalid zone."""
    value = str(tz or "").strip()
    if value and not _is_valid_iana_timezone(value):
        return False
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                # Unchanged value is a pure no-op. iOS re-reports the same zone
                # on every app-presence device event (~1/min/device), and each
                # persist here is a users-row upsert + TEE mirror + a cross-worker
                # broadcast that makes EVERY worker reload the whole registry.
                if value == str(u.get("timezone") or ""):
                    return True
                if value:
                    u["timezone"] = value
                else:
                    u.pop("timezone", None)
                persist_user(u)  # per-row upsert + cross-worker users broadcast
                return True
    return False


def _find_user_entry_locked(user_id: str) -> dict | None:
    for user_entry in _users:
        if user_entry.get("user_id") == user_id:
            _normalize_user_entry(user_entry)
            return user_entry
    return None


def _user_entry_snapshot(user_id: str) -> dict | None:
    with _users_lock:
        user_entry = _find_user_entry_locked(user_id)
        return dict(user_entry) if user_entry else None


def _upsert_access_binding_locked(
    user_entry: dict,
    access_mode: str,
    *,
    status: str = "connected",
    key_id: str = "",
    label: str = "",
    touch_seen: bool = False,
) -> dict:
    mode = _normalize_access_mode(access_mode)
    if mode not in ACCESS_MODES:
        raise ValueError("access_mode must be resident, model_api, or official_import")
    now_iso = datetime.now().isoformat()
    bindings = user_entry.setdefault("access_bindings", [])
    if not isinstance(bindings, list):
        bindings = []
        user_entry["access_bindings"] = bindings
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        if _normalize_access_mode(str(binding.get("access_mode") or "")) != mode:
            continue
        binding["access_mode"] = mode
        binding["label"] = label or binding.get("label") or ACCESS_MODE_LABELS.get(mode, mode)
        binding["status"] = status
        binding["updated_at"] = now_iso
        if touch_seen:
            binding["last_seen_at"] = now_iso
        if key_id:
            binding["last_key_id"] = key_id
        if not binding.get("binding_id"):
            binding["binding_id"] = _new_binding_id()
        if not binding.get("created_at"):
            binding["created_at"] = now_iso
        return binding
    binding = {
        "binding_id": _new_binding_id(),
        "access_mode": mode,
        "label": label or ACCESS_MODE_LABELS.get(mode, mode),
        "status": status,
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_seen_at": now_iso if touch_seen else "",
        "last_key_id": key_id,
    }
    bindings.append(binding)
    return binding


def _resident_binding(user_entry: dict) -> dict | None:
    return next(
        (
            binding
            for binding in user_entry.get("access_bindings") or []
            if isinstance(binding, dict)
            and _normalize_access_mode(str(binding.get("access_mode") or "")) == "resident"
        ),
        None,
    )


def _persist_resident_seen_cas(user_id: str, now_iso: str, *, attempts: int = 2) -> bool:
    """Bump ONLY the resident binding's ``last_seen_at`` / ``updated_at``, via a
    CAS on a FRESH DB document, then sync the winning row into memory.

    Why CAS on a fresh read instead of the in-memory snapshot: the heartbeat
    fires once a minute for every online resident, and its snapshot can be up to
    a full-reload interval stale. A whole-document upsert from that snapshot
    would write every OTHER field back too — permanently deleting an api_key a
    different worker issued in the meantime. Reading the current row and CAS-ing
    our binding change on top of it touches nothing the heartbeat didn't intend.

    No ``_users_lock`` is held across the DB round-trips — they must not convoy
    the global registry lock. Returns True only when it persisted an update."""
    for _ in range(max(1, attempts)):
        try:
            current = db.load_user(user_id)
        except Exception as e:
            print(f"[resident-seen] read failed for {user_id}: {e}")
            return False
        if current is None:
            with _users_lock:
                _install_user_row_locked(user_id, None)
            return False
        expected = copy.deepcopy(current)
        # Pure dict edit on our local copy — despite the _locked name this touches
        # no module state, so it is safe without the lock (see its body).
        binding = _upsert_access_binding_locked(current, "resident")
        binding["last_seen_at"] = now_iso
        binding["updated_at"] = now_iso
        if current == expected:
            return True  # already current — nothing to write
        read_ok, applied, authoritative = db.compare_and_set_user(
            user_id, expected, current
        )
        if not read_ok:
            print(f"[resident-seen] persist failed for {user_id}")
            return False
        if authoritative is None:  # row deleted concurrently
            with _users_lock:
                _install_user_row_locked(user_id, None)
            return False
        with _users_lock:
            _install_user_row_locked(user_id, authoritative)
        if applied:
            notify_users_changed(user_id)  # targeted: only this row changed
            return True
        # CAS lost to a concurrent writer; we just synced their row into memory —
        # retry applying our binding bump on top of it.
    return False


def _touch_resident_binding_seen(
    user_id: str,
    *,
    min_interval_sec: float = 60.0,
    now_epoch: float | None = None,
) -> bool:
    """Persist a throttled heartbeat on one user's resident binding.

    ``/v1/chat/poll`` is a 30-second hot path, so the binding row must not be
    rewritten on every poll. The persisted ``last_seen_at`` is the throttle
    source, which also keeps the behavior stable after a worker restart. Returns
    True only when this call actually persisted an update.

    The throttle check reads the in-memory snapshot under ``_users_lock``; the
    persist itself is a CAS on a fresh DB row done WITHOUT the lock (see
    ``_persist_resident_seen_cas``) so DB I/O never convoys the registry lock and
    a stale snapshot can never write another field back. The throttle is
    best-effort across the lock release — a rare duplicate CAS write is harmless.
    """
    try:
        interval = max(0.0, float(min_interval_sec))
    except (TypeError, ValueError):
        interval = 60.0
    try:
        now = float(time.time() if now_epoch is None else now_epoch)
    except (TypeError, ValueError):
        now = time.time()
    now_iso = datetime.fromtimestamp(now).isoformat()

    with _users_lock:
        user_entry = _find_user_entry_locked(user_id)
        if not user_entry:
            return False
        resident = _resident_binding(user_entry)
        last_seen_epoch = core_util._to_epoch((resident or {}).get("last_seen_at"))
        if last_seen_epoch > 0 and now - last_seen_epoch < interval:
            return False

    return _persist_resident_seen_cas(user_id, now_iso)


def _issue_api_key_for_user_locked(
    user_entry: dict,
    *,
    access_mode: str,
    label: str = "",
) -> dict:
    mode = _normalize_access_mode(access_mode)
    if mode not in ACCESS_MODES:
        raise ValueError("access_mode must be resident, model_api, or official_import")
    raw_key = secrets.token_hex(32)
    key_hash = _hash_api_key(raw_key)
    key_id = _new_key_id()
    now_iso = datetime.now().isoformat()
    key_entry = {
        "key_id": key_id,
        "api_key_hash": key_hash,
        "access_mode": mode,
        "label": (label or ACCESS_MODE_LABELS.get(mode, mode)).strip(),
        "created_at": now_iso,
        "revoked_at": "",
    }
    keys = user_entry.setdefault("api_keys", [])
    if not isinstance(keys, list):
        keys = []
        user_entry["api_keys"] = keys
    keys.append(key_entry)
    _upsert_access_binding_locked(
        user_entry,
        mode,
        key_id=key_id,
        label=ACCESS_MODE_LABELS.get(mode, mode),
        touch_seen=True,
    )
    _key_to_user[key_hash] = user_entry["user_id"]
    return {"api_key": raw_key, "key_entry": key_entry}


def _public_access_mode_state(user_entry: dict, active_route: str) -> list[dict]:
    _normalize_user_entry(user_entry)
    bindings_by_mode = {
        _normalize_access_mode(str(binding.get("access_mode") or "")): binding
        for binding in user_entry.get("access_bindings") or []
        if isinstance(binding, dict)
    }
    key_counts: dict[str, int] = {mode: 0 for mode in ACCESS_MODES}
    for key_entry in user_entry.get("api_keys") or []:
        if not isinstance(key_entry, dict) or key_entry.get("revoked_at"):
            continue
        mode = _normalize_access_mode(str(key_entry.get("access_mode") or "official_import"))
        if mode in key_counts:
            key_counts[mode] += 1
    out = []
    for mode in ACCESS_MODES:
        binding = bindings_by_mode.get(mode) or {}
        out.append({
            "access_mode": mode,
            "route": mode,
            "label": ACCESS_MODE_LABELS.get(mode, mode),
            "connected": bool(binding),
            "active": active_route == mode,
            "status": binding.get("status", "not_connected") if binding else "not_connected",
            "binding_id": binding.get("binding_id", ""),
            "created_at": binding.get("created_at", ""),
            "updated_at": binding.get("updated_at", ""),
            "last_seen_at": binding.get("last_seen_at", ""),
            "api_keys": key_counts.get(mode, 0),
        })
    return out


def _recover_account_rank(entry: dict) -> tuple:
    live = len([k for k in (entry.get("api_keys") or [])
                if isinstance(k, dict) and not k.get("revoked_at")])
    if live == 0 and entry.get("api_key_hash"):
        live = 1
    return (1 if live > 0 else 0, str(entry.get("created_at") or ""))


def _canonical_account_for_pubkey(public_key: str) -> dict | None:
    """The account a recovering device should land on for this public_key: the
    most recently registered one that still has a live api_key (matches the
    survivor chosen by tools/recover_orphan_accounts.py)."""
    pk = (public_key or "").strip()
    if not pk:
        return None
    with _users_lock:
        matches = [dict(u) for u in _users if (u.get("public_key") or "").strip() == pk]
    if not matches:
        return None
    return max(matches, key=_recover_account_rank)


def _get_user_public_key(user_id: str) -> str:
    """Return the caller's base64 X25519 content pubkey from users.json,
    or empty string if the user predates v1 registration."""
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                return (u.get("public_key") or "").strip()
    return ""


def _set_user_public_key(user_id: str, public_key: str) -> bool:
    updated = False
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                u["public_key"] = public_key.strip()
                updated = True
                break
        if updated:
            persist_user(u)  # per-row upsert + cross-worker users broadcast
    return updated
