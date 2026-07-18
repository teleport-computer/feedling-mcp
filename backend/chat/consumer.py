"""Resident-consumer liveness state + validation gate input."""

import os
import time
from datetime import datetime

import db
from core.store import UserStore



_OFFICIAL_CONSUMER_NAME = "feedling-chat-resident"
_CONSUMER_RECENT_SEC = int(os.environ.get("FEEDLING_CONSUMER_RECENT_SEC", "180"))
_RESIDENT_BINDING_SEEN_INTERVAL_SEC = max(
    1, int(os.environ.get("FEEDLING_RESIDENT_BINDING_SEEN_INTERVAL_SEC", "60"))
)


def expected_consumer_commit() -> str:
    """The git commit a self-hosted resident consumer should be running.

    Advertised to consumers (see chat poll response) so they can self-update to
    the commit this backend deploys — keeping client and server in lockstep.
    Operators may pin an explicit value; otherwise we fall back to this
    backend's own deployed commit (the same ``FEEDLING_GIT_COMMIT`` used by the
    enclave RELEASE block). Read at call time so it is unit-testable."""
    return (
        os.environ.get("FEEDLING_EXPECTED_CONSUMER_COMMIT")
        or os.environ.get("FEEDLING_GIT_COMMIT")
        or ""
    ).strip()


def _load_consumer_state(store: UserStore) -> dict:
    try:
        data = db.get_blob(store.user_id, "consumer_state")
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[{store.user_id}/consumer_state] failed to load: {e}")
    return {}


def _save_consumer_state(store: UserStore, state: dict) -> None:
    db.set_blob(store.user_id, "consumer_state", state)


def _consumer_headers_from_map(headers, remote_addr: str = "") -> dict:
    """Framework-neutral: consumer identity from a headers mapping + remote addr.

    Both Flask (request.headers) and ASGI (request.headers) expose a
    case-insensitive ``.get``, so the ASGI poll route reuses this (plan §9.1)."""
    name = (headers.get("X-Feedling-Consumer") or "").strip()
    if not name:
        return {}
    return {
        "consumer_name": name,
        "consumer_id": (headers.get("X-Feedling-Consumer-Id") or "").strip(),
        "consumer_version": (headers.get("X-Feedling-Consumer-Version") or "").strip(),
        "consumer_commit": (headers.get("X-Feedling-Consumer-Commit") or "").strip(),
        "official": name == _OFFICIAL_CONSUMER_NAME,
        "remote_addr": remote_addr or "",
        "user_agent": headers.get("User-Agent", ""),
    }


def _record_consumer_event(store: UserStore, event_type: str, *, info: dict | None = None) -> None:
    # ASGI callers always pass ``info`` (computed from the ASGI request off the
    # loop). Missing/empty info is a no-op.
    if not info:
        return
    now_epoch = time.time()
    now_iso = datetime.now().isoformat()
    with store.consumer_state_lock:
        state = _load_consumer_state(store)
        state.update(info)
        state["last_event"] = event_type
        state["last_seen_at"] = now_iso
        state["last_seen_epoch"] = now_epoch
        if event_type == "poll":
            state["last_poll_at"] = now_iso
            state["last_poll_epoch"] = now_epoch
        elif event_type == "response":
            state["last_response_at"] = now_iso
            state["last_response_epoch"] = now_epoch
        _save_consumer_state(store, state)
    if event_type == "poll":
        _touch_resident_binding_seen(store, info=info, now_epoch=now_epoch)


def _touch_resident_binding_seen(
    store: UserStore,
    *,
    info: dict | None,
    now_epoch: float | None = None,
) -> bool:
    """Refresh resident access liveness for a real resident-consumer poll.

    The consumer identity header prevents ordinary app/API polling from claiming
    the resident is online. The active-route check excludes hosted model_api and
    official_import users even though the hosted runner uses the same consumer
    binary and identity header. Best-effort: liveness telemetry must never break
    chat polling when its route read or registry persist is unavailable.
    """
    if not isinstance(info, dict) or not info.get("official"):
        return False
    try:
        from accounts import onboarding, registry

        if onboarding._load_onboarding_route(store) != "resident":
            return False
        return registry._touch_resident_binding_seen(
            store.user_id,
            min_interval_sec=_RESIDENT_BINDING_SEEN_INTERVAL_SEC,
            now_epoch=now_epoch,
        )
    except Exception as e:
        print(f"[{store.user_id}/resident-seen] heartbeat update failed: {e}")
        return False


def _consumer_validation_state(store: UserStore) -> dict:
    with store.consumer_state_lock:
        state = _load_consumer_state(store)
    last_poll_epoch = 0.0
    try:
        last_poll_epoch = float(state.get("last_poll_epoch") or 0)
    except Exception:
        last_poll_epoch = 0.0
    age_sec = time.time() - last_poll_epoch if last_poll_epoch > 0 else None
    official = bool(state.get("official"))
    recent = age_sec is not None and age_sec <= _CONSUMER_RECENT_SEC
    passing = official and recent
    return {
        "passing": passing,
        "official": official,
        "consumer_name": state.get("consumer_name", ""),
        "consumer_id": state.get("consumer_id", ""),
        "consumer_version": state.get("consumer_version", ""),
        "consumer_commit": state.get("consumer_commit", ""),
        "last_poll_at": state.get("last_poll_at", ""),
        "last_response_at": state.get("last_response_at", ""),
        "age_sec": age_sec,
        "recent_window_sec": _CONSUMER_RECENT_SEC,
        "required": (
            "Run the standard independent feedling-chat-resident / IO resident "
            "consumer with the current FEEDLING_API_KEY. It must poll "
            "FEEDLING_API_URL/v1/chat/poll and identify itself with the "
            "X-Feedling-Consumer headers."
        ),
    }
