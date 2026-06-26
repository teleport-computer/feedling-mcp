"""Resident-consumer liveness state + validation gate input."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

import db
from core.store import UserStore



_OFFICIAL_CONSUMER_NAME = "feedling-chat-resident"
_CONSUMER_RECENT_SEC = int(os.environ.get("FEEDLING_CONSUMER_RECENT_SEC", "180"))


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


def _consumer_headers_from_request() -> dict:
    name = (request.headers.get("X-Feedling-Consumer") or "").strip()
    if not name:
        return {}
    return {
        "consumer_name": name,
        "consumer_id": (request.headers.get("X-Feedling-Consumer-Id") or "").strip(),
        "consumer_version": (request.headers.get("X-Feedling-Consumer-Version") or "").strip(),
        "consumer_commit": (request.headers.get("X-Feedling-Consumer-Commit") or "").strip(),
        "official": name == _OFFICIAL_CONSUMER_NAME,
        "remote_addr": request.remote_addr or "",
        "user_agent": request.headers.get("User-Agent", ""),
    }


def _record_consumer_event(store: UserStore, event_type: str) -> None:
    info = _consumer_headers_from_request()
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

