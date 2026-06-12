"""Access-mode payload + access link tokens (cross-device key issuance)."""

import copy
import os
import threading
import time

import db
from accounts import onboarding, registry
from core.store import UserStore

ACCESS_LINK_TOKEN_TTL_SEC = int(os.environ.get("FEEDLING_ACCESS_LINK_TOKEN_TTL_SEC", "900"))
_access_link_tokens_lock = threading.Lock()

def _access_modes_payload(store: UserStore) -> dict:
    active_route = onboarding._load_onboarding_route(store)
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(store.user_id)
        if not user_entry:
            return {"error": "user not found"}
        # Treat the selected onboarding route as a connected access mode, but
        # do not move any content: all Memory/Chat/Identity files remain under
        # the same user_id.
        #
        # whoami hits this on every request, so persistence here must be cheap
        # and rare. It used to call `_save_users()` — a full `DELETE FROM users`
        # + re-INSERT of every row — under the global registry._users_lock on EVERY
        # whoami. That made the hottest endpoint a serialized full-table
        # rewrite; raising gunicorn --threads only widened the lock convoy
        # (prod whoami p50 ~100s, max ~247s). Now we persist ONLY when the
        # binding is genuinely new or flips to connected, and only the single
        # affected user row (db.upsert_user) — steady-state re-polls touch no DB.
        mode = registry._normalize_access_mode(active_route)
        prior = next(
            (b for b in user_entry.get("access_bindings") or []
             if isinstance(b, dict)
             and registry._normalize_access_mode(str(b.get("access_mode") or "")) == mode),
            None,
        )
        was_connected = bool(prior) and str(prior.get("status") or "") == "connected"
        if was_connected:
            registry._upsert_access_binding_locked(user_entry, active_route)
        else:
            # First connect / status flip: persist the single affected row.
            # Snapshot the bindings first and roll back if the write fails, so a
            # transient DB blip doesn't leave the binding marked "connected" in
            # memory but unpersisted — otherwise the next whoami sees
            # was_connected and skips the write forever, losing it on restart.
            # We swallow (don't 500) to match the old _save_users behavior, but
            # the rollback makes the next whoami retry instead of giving up.
            binding_snapshot = copy.deepcopy(user_entry.get("access_bindings"))
            registry._upsert_access_binding_locked(user_entry, active_route)
            try:
                db.upsert_user(user_entry)
            except Exception as e:
                user_entry["access_bindings"] = binding_snapshot
                print(f"[access-modes] binding persist failed for {store.user_id}, "
                      f"rolled back for retry: {e}")
        key_count = sum(
            1
            for key_entry in user_entry.get("api_keys") or []
            if isinstance(key_entry, dict) and not key_entry.get("revoked_at")
        )
        return {
            "user_id": store.user_id,
            "principal_id": user_entry.get("principal_id", ""),
            "active_route": active_route,
            "access_modes": registry._public_access_mode_state(user_entry, active_route),
            "api_keys_count": key_count,
            "link_token_ttl_seconds": ACCESS_LINK_TOKEN_TTL_SEC,
        }


def _load_access_link_tokens() -> list[dict]:
    data = db.get_global_blob("access_link_tokens")
    return data if isinstance(data, list) else []


def _save_access_link_tokens(rows: list[dict]) -> None:
    db.set_global_blob("access_link_tokens", rows)


def _trim_access_link_tokens(rows: list[dict]) -> list[dict]:
    cutoff = time.time() - 86400
    trimmed = []
    for row in rows:
        try:
            expires_at = float(row.get("expires_at_epoch") or 0)
        except Exception:
            expires_at = 0
        used_at = str(row.get("used_at") or "")
        if expires_at >= cutoff or not used_at:
            trimmed.append(row)
    return trimmed[-500:]
