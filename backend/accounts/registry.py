"""Multi-tenant user registry: accounts, api keys, access bindings.

``_users`` / ``_key_to_user`` are the in-memory truth — mutate them in place,
never rebind (tests and the wake-bus reload rely on the objects' identity).
Persistence is PostgreSQL (db.save_all_users / db.upsert_user).
"""

import copy
import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
import time
from datetime import datetime

import db
from core import util as core_util
from core import wake_bus

_users_lock = threading.Lock()
_users: list[dict] = []                    # [{user_id, principal_id, api_keys, public_key, created_at}]
_key_to_user: dict[str, str] = {}          # api_key_hash -> user_id (in-memory cache)

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


def _rebuild_key_cache() -> None:
    _key_to_user.clear()
    for user_entry in _users:
        if not isinstance(user_entry, dict):
            continue
        user_id = str(user_entry.get("user_id") or "")
        if not user_id:
            continue
        legacy_hash = str(user_entry.get("api_key_hash") or "").strip()
        if legacy_hash:
            _key_to_user[legacy_hash] = user_id
        for key_entry in user_entry.get("api_keys") or []:
            if not isinstance(key_entry, dict) or key_entry.get("revoked_at"):
                continue
            key_hash = str(key_entry.get("api_key_hash") or "").strip()
            if key_hash:
                _key_to_user[key_hash] = user_id


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
    lock (the two callers — startup assembly and the listener — don't)."""
    with _users_lock:
        _users[:] = db.load_all_users()
        changed = _normalize_all_users()
        _rebuild_key_cache()
        if changed:
            _save_users()
    print(f"[users] loaded {len(_users)} user(s)")


def _save_users():
    """Persist the WHOLE in-memory user registry to PostgreSQL (full rewrite via
    db.save_all_users). Now used only for normalization-on-read and test resets —
    paths that rewrite this worker's own freshly-read/owned snapshot. Genuine,
    user-initiated single-user edits must NOT use this under -w N (a stale
    snapshot's full rewrite would wipe another worker's concurrent edit); they go
    through ``persist_user`` (per-row upsert + cross-worker ``users`` broadcast).
    This path deliberately does NOT broadcast — a normalization reload firing a
    NOTIFY would ping-pong with the load_users handler."""
    db.save_all_users(_users)


def notify_users_changed() -> None:
    """Broadcast a cross-worker ``users`` reload so other workers don't keep
    serving a stale ``_users`` / ``_key_to_user`` snapshot. Pair it with the
    persist for any genuine registry edit; ``persist_user`` already does."""
    wake_bus.notify("users")


def persist_user(entry: dict) -> None:
    """Persist ONE edited user row (non-destructive per-row upsert) and broadcast
    a cross-worker reload. This is the multi-worker-safe way to persist a genuine
    single-user edit (registration, api-key add, access-binding flip, public-key
    / preference change). It replaces ``_save_users(broadcast=True)``, whose
    ``db.save_all_users`` does a DELETE-all + reinsert from THIS worker's
    in-memory snapshot — under ``-w N`` two workers editing DIFFERENT users
    concurrently could each wipe the other's user before the NOTIFY reload lands.
    A per-row upsert only touches the edited row. Caller holds ``_users_lock``."""
    db.upsert_user(entry)
    notify_users_changed()


def _resolve_user(api_key: str) -> str | None:
    if not api_key:
        return None
    h = _hash_api_key(api_key)
    uid = _key_to_user.get(h)
    if uid:
        return uid
    with _users_lock:
        changed = _normalize_all_users()
        for u in _users:
            if u.get("api_key_hash") == h:
                _key_to_user[h] = u["user_id"]
                if changed:
                    _save_users()
                return u["user_id"]
            for key_entry in u.get("api_keys") or []:
                if not isinstance(key_entry, dict) or key_entry.get("revoked_at"):
                    continue
                if key_entry.get("api_key_hash") == h:
                    _key_to_user[h] = u["user_id"]
                    if changed:
                        _save_users()
                    return u["user_id"]
        if changed:
            _save_users()
    return None


def _register_user(public_key: str | None = None,
                   archive_language: str | None = None,
                   access_mode: str = "official_import",
                   label: str | None = None,
                   *,
                   _qa_synthetic_metadata_builder: Callable[[dict], dict] | None = None) -> dict:
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
    # Private assembly seam used only by the admin-authenticated QA registration
    # route. Public /v1/users/register never supplies a builder, so a user-chosen
    # ``agent-e2e-*`` label cannot gain reaper metadata. The builder runs only
    # after the authoritative user/key identities exist and before the first
    # append/upsert, allowing the lease signature to bind those identities with
    # no post-registration marking window.
    if _qa_synthetic_metadata_builder is not None:
        metadata = _qa_synthetic_metadata_builder(entry)
        if not isinstance(metadata, dict):
            raise TypeError("QA synthetic metadata builder must return a dict")
        entry["qa_synthetic_account"] = dict(metadata)
    with _users_lock:
        _users.append(entry)
        try:
            persist_user(entry)  # per-row upsert (multi-worker-safe) + users broadcast
        except Exception:
            if _qa_synthetic_metadata_builder is None:
                # Preserve the ordinary registration path's existing failure
                # semantics. The rollback below exists only to make the
                # short-lived, server-marked QA registration all-or-nothing.
                raise
            # ``persist_user`` can fail after the primary upsert (for example,
            # during mirror replication or the cross-worker notification).
            # Roll back both representations before withholding the API key so
            # a failed registration cannot leave an inaccessible account row.
            _users.remove(entry)
            try:
                db.delete_user(user_id)
                notify_users_changed()
            except Exception:
                # A signed, expiring QA lease remains a final cleanup backstop
                # if the database itself is unavailable during rollback.
                pass
            raise
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

    The single-row upsert stays under ``_users_lock`` because the in-memory user
    dict is the registry's source of truth; releasing the lock before persisting
    a snapshot could let a concurrent edit land first and then be overwritten by
    this stale heartbeat snapshot. At most one call per user per interval reaches
    the DB. A failed persist rolls the binding back so the next poll retries.
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
        resident = next(
            (
                binding
                for binding in user_entry.get("access_bindings") or []
                if isinstance(binding, dict)
                and _normalize_access_mode(str(binding.get("access_mode") or "")) == "resident"
            ),
            None,
        )
        last_seen_epoch = core_util._to_epoch((resident or {}).get("last_seen_at"))
        if last_seen_epoch > 0 and now - last_seen_epoch < interval:
            return False

        bindings_before = copy.deepcopy(user_entry.get("access_bindings"))
        binding = _upsert_access_binding_locked(user_entry, "resident")
        binding["last_seen_at"] = now_iso
        binding["updated_at"] = now_iso
        try:
            persist_user(user_entry)
        except Exception as e:
            user_entry["access_bindings"] = bindings_before
            print(
                f"[resident-seen] persist failed for {user_id}, "
                f"rolled back for retry: {e}"
            )
            return False
    return True


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
