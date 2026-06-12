"""Token entry helpers (pure functions over the per-user token list)."""

from datetime import datetime

from core import store as core_store
from core.store import UserStore

def _is_live_activity_token(entry: dict) -> bool:
    return entry.get("type") in ("live-activity", "live_activity")


def _is_push_to_start_token(entry: dict) -> bool:
    return entry.get("type") == "push_to_start"


def _is_device_token(entry: dict) -> bool:
    return entry.get("type") in ("device", "apns")


def _entry_is_active(entry: dict) -> bool:
    return (entry.get("status") or "active") == "active"


def _select_token(store: UserStore, predicate, activity_id: str | None = None, active_only: bool = True):
    candidates = _select_tokens(store, predicate, activity_id=activity_id, active_only=active_only)
    return candidates[0] if candidates else None


def _select_tokens(store: UserStore, predicate, activity_id: str | None = None, active_only: bool = True) -> list[dict]:
    candidates = []
    for raw in store.tokens:
        entry = core_store._normalize_token_entry(raw)
        if not predicate(entry):
            continue
        if activity_id and entry.get("activity_id") != activity_id:
            continue
        if active_only and not _entry_is_active(entry):
            continue
        if not entry.get("token"):
            continue
        candidates.append(entry)

    candidates.sort(key=lambda x: x.get("registered_at", ""), reverse=True)
    return candidates


def _update_token_lifecycle(
    store: UserStore,
    entry: dict,
    *,
    status: str | None = None,
    last_error: str | None = None,
    success: bool = False,
    apns_env: str | None = None,
):
    token = entry.get("token")
    token_type = entry.get("type")
    activity_id = entry.get("activity_id")
    now_iso = datetime.now().isoformat()

    changed = False
    for idx, raw in enumerate(store.tokens):
        cur = core_store._normalize_token_entry(raw)
        if cur.get("token") != token or cur.get("type") != token_type or cur.get("activity_id") != activity_id:
            continue
        if status is not None:
            cur["status"] = status
            if status == "expired":
                cur["expired_at"] = now_iso
        if last_error is not None:
            cur["last_error"] = last_error
            cur["last_error_at"] = now_iso
        if success:
            cur["last_success_at"] = now_iso
            cur["status"] = "active"
            cur["expired_at"] = ""
            cur["last_error"] = ""
            cur["last_error_at"] = ""
        if apns_env:
            cur["apns_env"] = apns_env
        cur["updated_at"] = now_iso
        store.tokens[idx] = cur
        changed = True
        break

    if changed:
        store._save_tokens()


def _mark_expired_token(store: UserStore, entry: dict, reason: str):
    _update_token_lifecycle(store, entry, status="expired", last_error=reason)


def _mark_active_token_success(store: UserStore, entry: dict, apns_env: str | None = None):
    _update_token_lifecycle(store, entry, success=True, apns_env=apns_env)
