import asyncio
import base64
import copy
import errno
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import jwt
import websockets
from flask import Flask, abort, g, jsonify, request, Response, send_file
from flask_compress import Compress

from content_encryption import build_envelope
from provider_client import (
    ProviderConfig,
    ProviderError,
    chat_completion,
    mask_api_key,
    public_config as public_provider_config,
    test_provider_key,
    validate_config as validate_provider_config,
)
from context_memory_selection import memory_relevance_details
from hosted_runtime import (
    ACTION_RESPONSE_FORMAT as HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
    ACTION_METHOD as HOSTED_RUNTIME_ACTION_METHOD,
    BACKGROUND_METHOD as HOSTED_RUNTIME_BACKGROUND_METHOD,
    BACKGROUND_NOT_STARTED_METHOD as HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
    NOOP_METHOD as HOSTED_RUNTIME_NOOP_METHOD,
    PENDING_CONFIRM_METHOD as HOSTED_RUNTIME_PENDING_CONFIRM_METHOD,
    PENDING_REJECT_METHOD as HOSTED_RUNTIME_PENDING_REJECT_METHOD,
    RUNTIME_ENGINE_NATIVE as HOSTED_RUNTIME_ENGINE_NATIVE,
    build_background_execution_messages as build_hosted_runtime_background_execution_messages,
    background_execution_trace as hosted_runtime_background_trace,
    companion_turn_contract_message as hosted_runtime_companion_turn_contract_message,
    coerce_pending_decision as coerce_hosted_runtime_pending_decision,
    coerce_runtime_action as coerce_hosted_runtime_action,
)
from model_api_runtime.prompts import (
    build_foreground_chat_messages as build_model_api_foreground_chat_messages,
    build_memory_capture_messages as build_model_api_memory_capture_messages,
    build_pending_confirmation_messages as build_model_api_pending_confirmation_messages,
    build_web_search_results_message as build_model_api_web_search_results_message,
    web_search_followup_message as model_api_web_search_followup_message,
)
from model_api_runtime.tools import (
    extract_web_search_requests as extract_model_api_web_search_requests,
    run_web_searches as run_model_api_web_searches,
    web_search_trace as model_api_web_search_trace,
)
from model_api_runtime.wake import (
    wake_turn_contract_message as model_api_wake_turn_contract_message,
    build_wake_event_message as build_model_api_wake_event_message,
    hosted_tick_trigger as model_api_hosted_tick_trigger,
    parse_wake_actions as parse_model_api_wake_actions,
)

import db

# ---------------------------------------------------------------------------
# Root directory + deployment mode
# ---------------------------------------------------------------------------

# FEEDLING_DIR is no longer the source of truth for user data (that lives in
# PostgreSQL now — see db.py). It is still used for non-user-data files that
# ride in the data volume, e.g. the APNs .p8 push key. Kept for compatibility.
FEEDLING_DIR = Path(os.environ.get("FEEDLING_DATA_DIR", str(Path.home() / "feedling-data"))).expanduser()
FEEDLING_DIR.mkdir(parents=True, exist_ok=True)

# Create the PostgreSQL schema (idempotent) before any load/save runs at import
# time. Fails fast if DATABASE_URL is unset / the DB is unreachable — the
# backend now requires Postgres and must not silently fall back to files.
db.init_schema()

# ---------------------------------------------------------------------------
# Users registry (multi-tenant). Every request is auth'd by api_key.
# ---------------------------------------------------------------------------

_users_lock = threading.Lock()
_users: list[dict] = []                    # [{user_id, principal_id, api_keys, public_key, created_at}]
_key_to_user: dict[str, str] = {}          # api_key_hash -> user_id (in-memory cache)
_access_link_tokens_lock = threading.Lock()

ACCESS_MODES = ("resident", "model_api", "official_import")
ACCESS_MODE_LABELS = {
    "resident": "Server",
    "model_api": "API",
    "official_import": "Official App Chat",
}
ACCESS_LINK_TOKEN_TTL_SEC = int(os.environ.get("FEEDLING_ACCESS_LINK_TOKEN_TTL_SEC", "900"))
CHAT_HISTORY_INLINE_BODY_CT_MAX = int(os.environ.get("FEEDLING_CHAT_HISTORY_INLINE_BODY_CT_MAX", "262144"))
CHAT_POLL_CLAIM_TTL_SEC = int(os.environ.get("FEEDLING_CHAT_POLL_CLAIM_TTL_SEC", "120"))
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


_PEPPER = _server_pepper()


def _hash_api_key(api_key: str) -> str:
    return hmac.new(_PEPPER, api_key.encode("utf-8"), hashlib.sha256).hexdigest()


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


def _load_users():
    global _users, _key_to_user
    _users = db.load_all_users()
    changed = _normalize_all_users()
    _rebuild_key_cache()
    if changed:
        _save_users()
    print(f"[users] loaded {len(_users)} user(s)")


def _save_users():
    """Persist the whole in-memory user registry to PostgreSQL. Called wherever
    the registry changes (registration, api-key add/revoke, normalization,
    preference / public-key edits). The file era wrote users.json here."""
    db.save_all_users(_users)


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


_USER_ID_RE = re.compile(r"^usr_[a-f0-9]{16}$")


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
        _save_users()
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


_load_users()

# ---------------------------------------------------------------------------
# Per-user state store
# ---------------------------------------------------------------------------

MAX_FRAMES = 200
# Chat history ring buffer per user. Bumped from 500 → 5000 on 2026-05-11
# to give users meaningful scroll-back across months of normal use without
# silently losing their oldest conversations. Chat now persists row-per-message
# in PostgreSQL (see db.chat_append), so an append is a single-row INSERT plus
# a bounded trim — O(1) regardless of history depth — rather than rewriting the
# whole history. The cap still bounds storage and the in-memory list size.
MAX_CHAT_MESSAGES = 5000
PUSH_COOLDOWN_SECONDS = int(os.environ.get("FEEDLING_PUSH_COOLDOWN_SEC", 300))
LIVE_ACTIVITY_DEDUPE_SEC = int(os.environ.get("FEEDLING_LIVE_ACTIVITY_DEDUPE_SEC", 900))
LIVE_ACTIVITY_START_COOLDOWN_SEC = int(os.environ.get("FEEDLING_LIVE_ACTIVITY_START_COOLDOWN_SEC", 1800))
APP_FOREGROUND_FRESH_SEC = int(os.environ.get("FEEDLING_APP_FOREGROUND_FRESH_SEC", 90))
DEVICE_EVENT_RETENTION_DAYS = int(os.environ.get("FEEDLING_DEVICE_EVENT_RETENTION_DAYS", 30))
TRACK_EVENT_RETENTION_DAYS = int(os.environ.get("FEEDLING_TRACK_EVENT_RETENTION_DAYS", 90))
TRACK_EVENT_MAX = int(os.environ.get("FEEDLING_TRACK_EVENT_MAX", 2000))
PROACTIVE_JOB_SOURCE = "agent_initiated_proactive"
PROACTIVE_JOB_MAX = int(os.environ.get("FEEDLING_PROACTIVE_JOB_MAX", 500))
PROACTIVE_V2_WAKE_TTL_SEC = float(os.environ.get("FEEDLING_PROACTIVE_V2_WAKE_TTL_SEC", "180"))
PROACTIVE_USER_STATES = {"default", "focused", "social", "resting", "away"}
PROACTIVE_AI_STATES = {"present", "watching", "thinking", "curious", "waiting"}
PROACTIVE_BROADCAST_STATES = {"unknown", "on", "off", "paused"}
PROACTIVE_DEBUG_DECISION_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_DECISION_READ_MAX", 1000))
PROACTIVE_DEBUG_JOB_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_JOB_READ_MAX", PROACTIVE_JOB_MAX))
PROACTIVE_DEBUG_EVENT_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_EVENT_READ_MAX", 500))
PROACTIVE_DEBUG_REVIEW_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_REVIEW_READ_MAX", 500))
PROACTIVE_DEBUG_MESSAGE_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_MESSAGE_READ_MAX", 500))
PROACTIVE_DEBUG_FRAME_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_FRAME_READ_MAX", MAX_FRAMES))
PROACTIVE_WAKE_MAX_FRAMES = int(os.environ.get("FEEDLING_PROACTIVE_WAKE_MAX_FRAMES", "5"))
PROACTIVE_WAKE_FRAME_CANDIDATE_MAX = int(os.environ.get("FEEDLING_PROACTIVE_WAKE_FRAME_CANDIDATE_MAX", "60"))
PROACTIVE_DEFAULT_TIMEZONE = os.environ.get("FEEDLING_DEFAULT_TIMEZONE", "Asia/Shanghai").strip() or "UTC"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
PROACTIVE_DEBUG_TRANSLATION_MODEL = os.environ.get(
    "FEEDLING_PROACTIVE_DEBUG_TRANSLATION_MODEL",
    "google/gemini-3.1-flash-lite",
).strip()
PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC = float(
    os.environ.get("FEEDLING_PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC", "10")
)
_debug_translation_cache: dict[str, str] = {}
_debug_translation_lock = threading.Lock()


# Used from inside UserStore._load_tokens on boot; must be defined before
# the class that calls it. Other token helpers (_select_token,
# _update_token_lifecycle, etc.) stay below since they only run at request
# time, after the full module has loaded.
def _normalize_token_entry(entry: dict) -> dict:
    normalized = dict(entry)
    normalized.setdefault("status", "active")
    normalized.setdefault("last_error", "")
    normalized.setdefault("last_success_at", "")
    normalized.setdefault("expired_at", "")
    normalized.setdefault("apns_env", normalized.get("environment", ""))
    normalized.setdefault("updated_at", normalized.get("registered_at", datetime.now().isoformat()))
    return normalized


class UserStore:
    """All per-user state + locks. One instance per user_id. Persistence is in
    PostgreSQL (see db.py); state below is the in-memory working copy."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        # Legacy on-disk dir for this user. No longer written to — kept only so
        # account_reset can sweep any pre-migration residual files if present.
        self.dir = FEEDLING_DIR / user_id

        # frames
        self.frames_meta: list[dict] = []
        self.frames_lock = threading.Lock()

        # chat
        self.chat_messages: list[dict] = []
        self.chat_lock = threading.Lock()
        self.chat_waiters: list[threading.Event] = []
        self.chat_waiters_lock = threading.Lock()

        # tokens
        self.tokens: list[dict] = []

        # push cooldown
        self.last_push_epoch: float = 0.0
        self.last_push_mono: float = 0.0
        self.push_lock = threading.Lock()

        # live activity dedupe
        self.live_activity_state = {
            "last_message": "",
            "last_top_app": "",
            "last_sent_epoch": 0.0,
            "last_start_epoch": 0.0,
        }
        self.live_activity_state_lock = threading.Lock()

        # identity / memory locks
        self.identity_lock = threading.Lock()
        self.memory_lock = threading.Lock()
        self.consumer_state_lock = threading.Lock()

        # proactive presence state
        self.proactive_lock = threading.Lock()
        self.proactive_job_waiters: list[threading.Event] = []
        self.proactive_job_waiters_lock = threading.Lock()

        # Plaintext api_key last seen on an authenticated request. IN-MEMORY
        # ONLY (never persisted — the DB stores peppered hashes). Lets
        # background hosted-wake consumers call the enclave decrypt paths,
        # which require the user's real key. Single gunicorn worker, so this
        # cache is process-wide. Empty until the user's first request after
        # a process restart.
        self.last_seen_api_key: str = ""

        # load persistent state
        self._load_tokens()
        self._load_push_state()
        self._load_live_activity_state()
        self._load_chat()
        self._load_frames_meta()

    # ------- frames index -------
    def _load_frames_meta(self):
        # Fast path: index blob already persisted.
        data = db.get_blob(self.user_id, "frames_meta")
        if isinstance(data, list):
            self.frames_meta = data
            print(f"[{self.user_id}/frames] loaded index n={len(self.frames_meta)}")
            return

        # Rebuild path: no index blob yet (first boot post-migration, or the
        # blob was lost). Reconstruct the lightweight index from the stored
        # frame envelope rows, prune to MAX_FRAMES, and re-persist the index.
        try:
            recovered = db.frame_list_meta(self.user_id)  # already sorted by ts
            if len(recovered) > MAX_FRAMES:
                drop = recovered[:-MAX_FRAMES]
                recovered = recovered[-MAX_FRAMES:]
                for m in drop:
                    db.frame_delete(self.user_id, m["id"])
            self.frames_meta = recovered
            self._persist_frames_meta()
            print(f"[{self.user_id}/frames] rebuilt index from db n={len(recovered)}")
        except Exception as e:
            print(f"[{self.user_id}/frames] rebuild failed: {e}")
            self.frames_meta = []

    def _persist_frames_meta(self):
        db.set_blob(self.user_id, "frames_meta", self.frames_meta)

    # ------- tokens -------
    def _load_tokens(self):
        data = db.get_blob(self.user_id, "tokens")
        self.tokens = data if isinstance(data, list) else []
        self.tokens[:] = [_normalize_token_entry(t) for t in self.tokens]
        self._save_tokens()

    def _save_tokens(self):
        db.set_blob(self.user_id, "tokens", self.tokens)

    # ------- push cooldown -------
    def _load_push_state(self):
        try:
            data = db.get_blob(self.user_id, "push_state")
            if isinstance(data, dict):
                epoch = float(data.get("last_push_epoch", 0.0))
                elapsed = time.time() - epoch
                if 0 <= elapsed < PUSH_COOLDOWN_SECONDS:
                    self.last_push_epoch = epoch
                    self.last_push_mono = time.monotonic() - elapsed
        except Exception as e:
            print(f"[{self.user_id}/push_state] load failed: {e}")

    def record_successful_push(self):
        with self.push_lock:
            self.last_push_epoch = time.time()
            self.last_push_mono = time.monotonic()
        db.set_blob(self.user_id, "push_state", {"last_push_epoch": self.last_push_epoch})

    def cooldown_remaining_seconds(self) -> float:
        with self.push_lock:
            elapsed = time.monotonic() - self.last_push_mono
        return max(0.0, PUSH_COOLDOWN_SECONDS - elapsed)

    # ------- live activity dedupe -------
    def _load_live_activity_state(self):
        try:
            data = db.get_blob(self.user_id, "live_activity_state")
            if isinstance(data, dict):
                self.live_activity_state = {
                    "last_message": str(data.get("last_message", "")),
                    "last_top_app": str(data.get("last_top_app", "")),
                    "last_sent_epoch": float(data.get("last_sent_epoch", 0.0)),
                    "last_start_epoch": float(data.get("last_start_epoch", 0.0)),
                }
        except Exception as e:
            print(f"[{self.user_id}/live-activity] load failed: {e}")

    def _save_live_activity_state(self):
        db.set_blob(self.user_id, "live_activity_state", self.live_activity_state)

    def should_suppress_live_activity(self, message: str, top_app: str) -> tuple[bool, str]:
        normalized_message = " ".join((message or "").strip().split())
        normalized_app = (top_app or "").strip().lower()
        if not normalized_message:
            return True, "empty_message"

        with self.live_activity_state_lock:
            last_message = " ".join((self.live_activity_state.get("last_message") or "").strip().split())
            last_app = (self.live_activity_state.get("last_top_app") or "").strip().lower()
            last_sent = float(self.live_activity_state.get("last_sent_epoch", 0.0))

        elapsed = max(0.0, time.time() - last_sent)

        if normalized_message == last_message and elapsed < 1800:
            return True, f"duplicate_message_within_30m:{int(1800 - elapsed)}s"

        if (
            normalized_message == last_message
            and normalized_app == last_app
            and elapsed < LIVE_ACTIVITY_DEDUPE_SEC
        ):
            return True, f"same_app_duplicate:{int(LIVE_ACTIVITY_DEDUPE_SEC - elapsed)}s"

        return False, "ok"

    def record_live_activity_sent(self, message: str, top_app: str):
        with self.live_activity_state_lock:
            self.live_activity_state["last_message"] = " ".join((message or "").strip().split())
            self.live_activity_state["last_top_app"] = (top_app or "").strip().lower()
            self.live_activity_state["last_sent_epoch"] = time.time()
        self._save_live_activity_state()

    def live_activity_start_cooldown_remaining_seconds(self) -> float:
        with self.live_activity_state_lock:
            last_start = float(self.live_activity_state.get("last_start_epoch", 0.0))
        if last_start <= 0:
            return 0.0
        elapsed = max(0.0, time.time() - last_start)
        return max(0.0, LIVE_ACTIVITY_START_COOLDOWN_SEC - elapsed)

    def should_start_live_activity(self) -> tuple[bool, str]:
        remaining = self.live_activity_start_cooldown_remaining_seconds()
        if remaining <= 0:
            return True, "start_window_open"
        return False, f"start_cooldown:{int(remaining)}s"

    def record_live_activity_started(self, message: str, top_app: str):
        with self.live_activity_state_lock:
            self.live_activity_state["last_start_epoch"] = time.time()
        self.record_live_activity_sent(message=message, top_app=top_app)

    # ------- chat -------
    def _load_chat(self):
        self.chat_messages = db.chat_load(self.user_id)

    def reload(self):
        """Re-read this store's cached state from PostgreSQL IN PLACE, keeping
        the same object identity (and the same waiter lists). Used by the cache
        TTL / admin eviction so out-of-band DB writes surface without a swap.

        Each collection is reassigned under its own lock. chat_load + a
        concurrent append() are both serialized on chat_lock, so no append is
        lost: either reload reads it from the DB, or append re-adds it to the
        freshly-loaded list."""
        with self.chat_lock:
            self.chat_messages = db.chat_load(self.user_id)
        with self.frames_lock:
            self._load_frames_meta()
        self._load_tokens()
        self._load_live_activity_state()
        self._load_push_state()

    def append_chat(
        self,
        role: str,
        source: str,
        envelope: dict,
        content_type: str = "text",
        extra: dict | None = None,
    ) -> dict:
        """Append a v1 ciphertext chat message. `envelope` holds the AEAD
        payload. See docs/DESIGN_E2E.md §3.2 for field definitions. Server
        never decrypts — the envelope is stored verbatim.

        The client supplies the envelope's `id`, which becomes the stored
        message id so the AEAD additional-data the client baked in
        (owner||v||id) stays verifiable by the enclave on read-back.

        `content_type` is plaintext metadata: "text" (default) or "image".
        Used by clients/enclave to render the decrypted bytes correctly —
        the envelope itself only carries opaque bytes; the type tag tells
        the renderer to show a string vs decode JPEG.
        """
        msg_id = envelope.get("id") if isinstance(envelope.get("id"), str) and envelope["id"] else uuid.uuid4().hex
        ct = content_type if content_type in ("text", "image") else "text"

        msg: dict = {
            "id": msg_id,
            "role": role,
            "ts": time.time(),
            "source": source,
            "v": envelope.get("v", 1),
            "body_ct": envelope["body_ct"],
            "nonce": envelope["nonce"],
            "K_user": envelope["K_user"],
            "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
            "visibility": envelope.get("visibility", "shared"),
            "owner_user_id": envelope.get("owner_user_id", self.user_id),
            "content_type": ct,
        }
        # Synthetic verify pings are not real user content and are removed
        # after /v1/chat/verify_loop completes. They still need plaintext
        # while resident consumers are polling, because local_only synthetic
        # envelopes intentionally do not carry K_enclave and therefore cannot
        # be decrypted through the normal enclave/MCP history path.
        if source == "verify_ping" and envelope.get("synthetic_marker"):
            msg["content"] = envelope["synthetic_marker"]
        if envelope.get("K_enclave") is not None:
            msg["K_enclave"] = envelope["K_enclave"]
        if extra:
            for key in (
                "gate_decision_id",
                "proactive_job_id",
                "alert_preview",
                "push_body_preview",
                "push_live_activity_requested",
                "live_activity_status",
                "live_activity_reason",
                "live_activity_activity_id",
                "live_activity_mode",
                "alert_status",
                "alert_reason",
                "push_decision",
                "push_reason",
                "app_presence_phase",
                "app_presence_age_sec",
                "model_api_kind",
                "thinking_v",
                "thinking_id",
                "thinking_body_ct",
                "thinking_nonce",
                "thinking_K_user",
                "thinking_K_enclave",
                "thinking_enclave_pk_fpr",
                "thinking_visibility",
                "thinking_owner_user_id",
                "thinking_kind",
                "thinking_source",
                "thinking_model",
                "thinking_native",
                "reply_claimed_by",
                "reply_claimed_at",
                "reply_claim_expires_at",
                "reply_status",
                "reply_message_id",
                "replied_by",
                "replied_at",
            ):
                value = extra.get(key)
                if isinstance(value, str) and value.strip():
                    msg[key] = value.strip()
                elif isinstance(value, bool):
                    msg[key] = value

        with self.chat_lock:
            self.chat_messages.append(msg)
            if len(self.chat_messages) > MAX_CHAT_MESSAGES:
                self.chat_messages[:] = self.chat_messages[-MAX_CHAT_MESSAGES:]
            db.chat_append(self.user_id, msg_id, msg["ts"], msg, MAX_CHAT_MESSAGES)
        return msg

    def update_chat_message_metadata(self, msg_id: str, fields: dict) -> dict | None:
        allowed = {
            "live_activity_status",
            "live_activity_reason",
            "live_activity_activity_id",
            "live_activity_mode",
            "alert_status",
            "alert_reason",
            "push_decision",
            "push_reason",
            "app_presence_phase",
            "app_presence_age_sec",
            "reply_claimed_by",
            "reply_claimed_at",
            "reply_claim_expires_at",
            "reply_status",
            "reply_message_id",
            "replied_by",
            "replied_at",
        }
        clean: dict = {}
        for key, value in (fields or {}).items():
            if key not in allowed:
                continue
            if value is None:
                continue
            clean[key] = str(value)[:500]
        if not clean:
            return None
        with self.chat_lock:
            for msg in self.chat_messages:
                if msg.get("id") == msg_id:
                    msg.update(clean)
                    db.chat_update_metadata(self.user_id, msg_id, clean)
                    return msg
        return None

    def notify_chat_waiters(self):
        with self.chat_waiters_lock:
            for ev in self.chat_waiters:
                ev.set()
            self.chat_waiters.clear()

    # ------- proactive presence -------
    def load_proactive_settings(self) -> dict:
        default = {
            "version": 2,
            "enabled": True,
            "dnd": False,
            "timezone": PROACTIVE_DEFAULT_TIMEZONE,
            "permission_states": {},
            "user_state": "default",
            "manual_user_state": "default",
            "ai_state": "present",
            "broadcast_state": "unknown",
            "updated_at": datetime.now().isoformat(),
        }
        try:
            data = db.get_blob(self.user_id, "proactive_settings")
            if isinstance(data, dict):
                merged = dict(default)
                merged.update(data)
                if not isinstance(merged.get("permission_states"), dict):
                    merged["permission_states"] = {}
                if str(merged.get("user_state") or "") not in PROACTIVE_USER_STATES:
                    merged["user_state"] = "default"
                if str(merged.get("manual_user_state") or "") not in PROACTIVE_USER_STATES:
                    merged["manual_user_state"] = str(merged.get("user_state") or "default")
                if str(merged.get("ai_state") or "") not in PROACTIVE_AI_STATES:
                    merged["ai_state"] = "present"
                if str(merged.get("broadcast_state") or "") not in PROACTIVE_BROADCAST_STATES:
                    merged["broadcast_state"] = "unknown"
                return merged
        except Exception as e:
            print(f"[{self.user_id}/proactive] settings load failed: {e}")
        return default

    def save_proactive_settings(self, patch: dict) -> dict:
        allowed = {
            "enabled",
            "dnd",
            "timezone",
            "permission_states",
            "user_state",
            "manual_user_state",
            "ai_state",
            "broadcast_state",
        }
        cur = self.load_proactive_settings()
        for key, value in (patch or {}).items():
            if key not in allowed:
                continue
            if key in {"enabled", "dnd"}:
                cur[key] = bool(value)
            elif key == "timezone":
                tz_name = str(value or "").strip()
                try:
                    ZoneInfo(tz_name)
                except ZoneInfoNotFoundError:
                    continue
                cur[key] = tz_name
            elif key == "permission_states" and isinstance(value, dict):
                states = dict(cur.get("permission_states") or {})
                for pname, pstate in value.items():
                    states[str(pname)] = str(pstate)
                cur["permission_states"] = states
            elif key in {"user_state", "manual_user_state"}:
                state = str(value or "").strip().lower()
                if state in PROACTIVE_USER_STATES:
                    cur[key] = state
            elif key == "ai_state":
                state = str(value or "").strip().lower()
                if state in PROACTIVE_AI_STATES:
                    cur[key] = state
            elif key == "broadcast_state":
                state = str(value or "").strip().lower()
                if state in PROACTIVE_BROADCAST_STATES:
                    cur[key] = state
        cur["version"] = 2
        cur["updated_at"] = datetime.now().isoformat()
        with self.proactive_lock:
            db.set_blob(self.user_id, "proactive_settings", cur)
        return cur

    # ------- append-only logs (PostgreSQL-backed; see db.user_logs) -------
    @staticmethod
    def _entry_epoch(entry: dict) -> float | None:
        """Extract the epoch ts an entry carries (``ts`` or ``ts_epoch``) for
        the indexed ts column. Returns None when the entry has no epoch ts
        (e.g. ISO-timestamped streams) — such rows are then ts-filter-exempt."""
        raw = entry.get("ts", entry.get("ts_epoch"))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def append_device_event(self, event: dict) -> dict:
        db.log_append(self.user_id, "device_events", event, ts=self._entry_epoch(event))
        cutoff = time.time() - DEVICE_EVENT_RETENTION_DAYS * 86400
        db.log_prune_older_than(self.user_id, "device_events", cutoff)
        return event

    def list_device_events(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return db.log_read(self.user_id, "device_events", limit=limit, since_epoch=since_epoch)

    def append_gate_decision(self, decision: dict) -> dict:
        db.log_append(self.user_id, "gate_decisions", decision, ts=self._entry_epoch(decision))
        return decision

    def list_gate_decisions(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return db.log_read(self.user_id, "gate_decisions", limit=limit, since_epoch=since_epoch)

    def append_gate_review(self, review: dict) -> dict:
        db.log_append(self.user_id, "gate_reviews", review, ts=self._entry_epoch(review))
        return review

    def list_gate_reviews(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return db.log_read(self.user_id, "gate_reviews", limit=limit, since_epoch=since_epoch)

    def append_tracking_event(self, event: dict) -> dict:
        db.log_append(self.user_id, "tracking_events", event, ts=self._entry_epoch(event))
        db.log_prune_older_than(
            self.user_id, "tracking_events", time.time() - TRACK_EVENT_RETENTION_DAYS * 86400
        )
        db.log_trim(self.user_id, "tracking_events", TRACK_EVENT_MAX)
        return event

    def list_tracking_events(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return db.log_read(self.user_id, "tracking_events", limit=limit, since_epoch=since_epoch)

    def append_proactive_job(self, job: dict) -> dict:
        db.log_append(
            self.user_id, "proactive_jobs", job,
            ts=self._entry_epoch(job),
            item_key=(str(job.get("job_id") or "") or None),
        )
        db.log_trim(self.user_id, "proactive_jobs", PROACTIVE_JOB_MAX)
        self.notify_proactive_job_waiters()
        # Hosted Model API users have no external resident consumer; consume
        # in-process. No-op for resident users (route gate inside).
        _maybe_start_hosted_wake_consumer(self, job)
        return job

    def list_proactive_jobs(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return db.log_read(self.user_id, "proactive_jobs", limit=limit, since_epoch=since_epoch)

    def update_proactive_job(
        self,
        job_id: str,
        fields: dict,
        *,
        only_if_status: str | None = None,
    ) -> dict | None:
        """Patch one hidden proactive job in-place. Status has a real lifecycle
        so the debug dashboard can distinguish "not consumed" from "agent
        failed" from "chat write delivered". The patch is an atomic single-row
        JSONB merge; ``only_if_status`` is enforced in SQL (no-op if it doesn't
        match the row's current status)."""
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        allowed = {
            "status",
            "status_reason",
            "consumer_id",
            "claimed_at",
            "realizing_at",
            "posted_at",
            "completed_at",
            "failed_at",
            "updated_at",
            "chat_message_id",
            "agent_action",
            "agent_action_status",
            "agent_actions",
            "ai_state",
            "broadcast_state",
            "request_broadcast",
            "wake_result",
        }
        patch = {k: v for k, v in (fields or {}).items() if k in allowed}
        if not patch:
            return None
        patch["updated_at"] = datetime.now().isoformat()
        changed = db.log_patch_item(
            self.user_id, "proactive_jobs", job_id, patch, only_if_status=only_if_status
        )
        if changed is not None:
            self.notify_proactive_job_waiters()
        return changed

    def notify_proactive_job_waiters(self):
        with self.proactive_job_waiters_lock:
            for ev in self.proactive_job_waiters:
                ev.set()
            self.proactive_job_waiters.clear()


# Registry of per-user stores
# In-process per-user store cache. gunicorn runs a single worker, so this is
# the one shared cache for the whole backend. A UserStore is a write-through
# cache over PostgreSQL (every mutation persists immediately), so dropping and
# rebuilding from the DB is always safe. The TTL bounds staleness from
# out-of-band DB writes (e.g. admin data surgery / the orphan-account recovery
# tool) so they surface without a backend redeploy; `_evict_store` is the
# targeted, immediate counterpart.
STORE_CACHE_TTL_SECONDS = 900  # 15 min

_stores: dict[str, UserStore] = {}
_stores_lock = threading.Lock()


def _wake_store_waiters(store: "UserStore") -> None:
    """Release threads parked on a store's long-poll waiters (chat / proactive)
    so they return promptly and re-evaluate against the refreshed state."""
    try:
        store.notify_chat_waiters()
    except Exception:
        pass
    try:
        with store.proactive_job_waiters_lock:
            for ev in store.proactive_job_waiters:
                ev.set()
    except Exception:
        pass


def _evict_store(user_id: str) -> bool:
    """Force a refresh of a user's cached store from PostgreSQL. Refreshes the
    state IN PLACE (the same instance is kept) rather than swapping in a new
    object, so a concurrent request that already holds the store and writes
    through it can't be lost. Returns whether a cached store was present."""
    with _stores_lock:
        store = _stores.get(user_id)
    if store is None:
        return False
    store.reload()
    store.loaded_at = time.monotonic()
    _wake_store_waiters(store)
    return True


def get_store(user_id: str) -> UserStore:
    now = time.monotonic()
    do_reload = False
    with _stores_lock:
        store = _stores.get(user_id)
        if store is None:
            store = UserStore(user_id)
            store.loaded_at = time.monotonic()
            _stores[user_id] = store
            return store
        if (now - getattr(store, "loaded_at", now)) >= STORE_CACHE_TTL_SECONDS:
            # Expired. Claim the reload by stamping loaded_at now (under the
            # lock) so concurrent callers don't stampede, then refresh the SAME
            # instance in place outside the lock. In-place refresh keeps object
            # identity stable: a request that grabbed this store and writes
            # through it (write-through to the DB + the same in-memory list)
            # is never shadowed by a freshly-swapped instance.
            store.loaded_at = time.monotonic()
            do_reload = True
    if do_reload:
        store.reload()
        _wake_store_waiters(store)
    return store


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


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
    user_id = _resolve_user(key)
    if not user_id:
        abort(401)
    g.user_id = user_id
    store = get_store(user_id)
    store.last_seen_api_key = key
    return store


# ---------------------------------------------------------------------------
# Frames helpers
# ---------------------------------------------------------------------------


def _frame_url(store: UserStore, filename: str) -> str:
    base = os.environ.get("FEEDLING_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        try:
            base = request.host_url.rstrip("/")
        except RuntimeError:
            base = ""
    return f"{base}/v1/screen/frames/{filename}?user={store.user_id}"


def _save_frame(store: UserStore, payload: dict):
    """Save a v1 frame envelope. See docs/DESIGN_E2E.md §3.2.

    Wire format:
      {"type":"frame","ts":..., "envelope":{
          "v":1,"id":...,"body_ct":...,"nonce":...,
          "K_user":...,"K_enclave":...,
          "visibility":"shared","owner_user_id":...}}

    The JPEG + OCR are inside `body_ct` (ChaCha20-Poly1305 AEAD bound to
    owner|v|id). Server never decrypts — it stores the envelope in a
    frame_envelopes row and appends the item to frames_meta with
    `encrypted=True` so the UI + enclave path can find it.
    """
    env = payload.get("envelope")
    if not (isinstance(env, dict) and env.get("v") and env.get("body_ct")):
        print(f"[ingest:{store.user_id}] rejecting frame without v1 envelope")
        return
    _save_frame_envelope(store, payload, env)


def _save_frame_envelope(store: UserStore, payload: dict, env: dict):
    """Persist a v1 frame envelope. The ciphertext blob is big (>150KB for
    typical screen frames) so it lives in its own frame_envelopes row instead
    of being inlined into the frames_meta index blob. frames_meta gets a
    lightweight index entry with `encrypted=True`.
    """
    item_id = env.get("id") or uuid.uuid4().hex
    ts = payload.get("ts") or time.time()
    db.frame_upsert(store.user_id, item_id, ts, env)

    meta = {
        "filename": f"{item_id}.env.json",
        "ts": ts,
        "app": None,         # unknown — inside ciphertext
        "ocr_text": "",      # unknown — inside ciphertext
        "w": payload.get("w", 0),
        "h": payload.get("h", 0),
        "encrypted": True,
        "id": item_id,
        "v": env.get("v", 1),
        "owner_user_id": env.get("owner_user_id"),
    }

    with store.frames_lock:
        store.frames_meta.append(meta)
        if len(store.frames_meta) > MAX_FRAMES:
            removed = store.frames_meta.pop(0)
            db.frame_delete(store.user_id, removed.get("id") or removed["filename"].split(".")[0])
        store._persist_frames_meta()

    body_len = len(env.get("body_ct") or "")
    print(f"[ingest:{store.user_id}] saved v1 frame id={item_id} body_ct_len={body_len}")


# ---------------------------------------------------------------------------
# Token entry helpers (pure functions over the per-user list)
# ---------------------------------------------------------------------------


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
        entry = _normalize_token_entry(raw)
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
        cur = _normalize_token_entry(raw)
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


# ---------------------------------------------------------------------------
# Semantic screen classifier — imported from a portable module so the iOS
# port can translate 1:1. See backend/semantic_analysis.py and
# docs/DESIGN_E2E.md §4 for the "classification on iOS" plan.
# ---------------------------------------------------------------------------

from semantic_analysis import analyze as _semantic_analysis  # noqa: E402


# ---------------------------------------------------------------------------
# Proactive wake helpers
# ---------------------------------------------------------------------------

_DEVICE_EVENT_ALLOWED_KEYS = {
    "permission",
    "status",
    "source",
    "type",
    "category",
    "place_type",
    "workout_type",
    "duration_min",
    "distance_bucket",
    "starts_in_min",
    "ended_min_ago",
    "is_busy",
    "has_location",
    "motion",
    "time_of_day",
    "scene_tags",
    "scene_phase",
    "phase",
    "app_state",
    "broadcast_state",
    "user_state",
    "ai_state",
    "wake_trigger",
    "is_foreground",
    "selected_tab",
    "tab",
    "is_chat_visible",
    "is_in_detail",
    "reason",
    "app_version",
    "build",
}

_DEVICE_EVENT_DROP_RE = re.compile(
    r"(raw|text|content|title|name|address|photo|image|lat|lng|lon|coordinate|phone|email)",
    re.IGNORECASE,
)


def _new_public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _redact_device_payload(payload: dict) -> dict:
    """Persist only coarse device-event facts.

    Raw location/photo/calendar text belongs in the user device or encrypted
    frame store, not in server-side proactive logs. The wake scheduler only
    needs coarse state; the real agent can ask for richer context later through
    the normal encrypted path.
    """
    if not isinstance(payload, dict):
        return {}

    redacted: dict = {}
    for key, value in payload.items():
        skey = str(key)
        if _DEVICE_EVENT_DROP_RE.search(skey):
            continue
        if skey not in _DEVICE_EVENT_ALLOWED_KEYS and not skey.startswith("safe_"):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            redacted[skey] = value
        elif isinstance(value, str):
            redacted[skey] = value[:120]
        elif isinstance(value, list):
            safe_items = []
            for item in value[:12]:
                if isinstance(item, (str, int, float, bool)):
                    safe_items.append(item if not isinstance(item, str) else item[:80])
            redacted[skey] = safe_items
    return redacted


def _make_device_event(source: str, event_type: str, payload: dict) -> dict:
    now = time.time()
    return {
        "event_id": _new_public_id("evt"),
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "source": (source or "ios").strip()[:80],
        "type": (event_type or "unknown").strip()[:80],
        "payload": _redact_device_payload(payload),
    }


def _latest_app_presence(store: UserStore, now: float | None = None) -> dict | None:
    now = now or time.time()
    for event in reversed(store.list_device_events(since_epoch=max(0.0, now - 86400), limit=300)):
        event_type = str(event.get("type") or "").strip().lower()
        if event_type not in {"app_presence", "app_state", "app_lifecycle"}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        phase = str(
            payload.get("scene_phase")
            or payload.get("phase")
            or payload.get("app_state")
            or ""
        ).strip().lower()
        try:
            ts = float(event.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        age = max(0.0, now - ts) if ts > 0 else float("inf")
        is_foreground = payload.get("is_foreground")
        if isinstance(is_foreground, str):
            is_foreground = is_foreground.lower() in {"1", "true", "yes", "active", "foreground"}
        elif not isinstance(is_foreground, bool):
            is_foreground = phase in {"active", "foreground"}
        is_chat_visible = payload.get("is_chat_visible")
        if isinstance(is_chat_visible, str):
            is_chat_visible = is_chat_visible.lower() in {"1", "true", "yes"}
        elif not isinstance(is_chat_visible, bool):
            is_chat_visible = False
        return {
            "event_id": event.get("event_id", ""),
            "phase": phase or "unknown",
            "age_sec": age,
            "is_foreground": bool(is_foreground),
            "is_chat_visible": bool(is_chat_visible),
            "selected_tab": str(payload.get("selected_tab") or payload.get("tab") or "")[:80],
        }
    return None


def _ai_push_decision(store: UserStore) -> dict:
    now = time.time()
    presence = _latest_app_presence(store, now)
    if presence is None:
        return {
            "should_push": True,
            "reason": "no_app_presence",
            "phase": "unknown",
            "age_sec": "",
        }

    age = float(presence.get("age_sec") or 0.0)
    phase = str(presence.get("phase") or "unknown")
    is_fresh = age <= APP_FOREGROUND_FRESH_SEC
    if presence.get("is_foreground") and is_fresh:
        reason = "app_foreground_chat_visible" if presence.get("is_chat_visible") else "app_foreground"
        return {
            "should_push": False,
            "reason": reason,
            "phase": phase,
            "age_sec": str(int(age)),
        }

    if presence.get("is_foreground") and not is_fresh:
        reason = "foreground_presence_stale"
    elif phase in {"background", "inactive"}:
        reason = f"app_{phase}"
    else:
        reason = "app_not_foreground"
    return {
        "should_push": True,
        "reason": reason,
        "phase": phase,
        "age_sec": str(int(age)) if age != float("inf") else "",
    }


def _recent_user_chat_active(store: UserStore, now: float, window_sec: float = 600.0) -> bool:
    with store.chat_lock:
        for msg in reversed(store.chat_messages):
            ts = float(msg.get("ts", 0) or 0)
            if now - ts > window_sec:
                return False
            if msg.get("role") == "user":
                return True
    return False


def _recent_frame_meta(store: UserStore, now: float, window_sec: float) -> list[dict]:
    with store.frames_lock:
        frames = [f for f in store.frames_meta if now - float(f.get("ts", 0) or 0) <= window_sec]
    return frames[-PROACTIVE_WAKE_FRAME_CANDIDATE_MAX:]


def _sample_frames_for_wake(frames: list[dict], max_frames: int = 5) -> list[dict]:
    """Metadata sampler for proactive wake frames."""
    clean = [f for f in frames if isinstance(f, dict) and str(f.get("id") or "").strip()]
    clean.sort(key=lambda f: float(f.get("ts", 0) or 0))
    if len(clean) <= max_frames:
        return clean
    if max_frames <= 2:
        return [clean[-1]]
    picks = [clean[0], clean[-1]]
    middle = clean[1:-1]
    slots = max_frames - len(picks)
    if middle and slots > 0:
        if slots == 1:
            picks.append(middle[len(middle) // 2])
        else:
            for i in range(slots):
                idx = round(i * (len(middle) - 1) / max(1, slots - 1))
                picks.append(middle[idx])
    by_id: dict[str, dict] = {}
    for frame in picks:
        by_id[str(frame.get("id"))] = frame
    return sorted(by_id.values(), key=lambda f: float(f.get("ts", 0) or 0))


def _frame_ids(frames: list[dict]) -> list[str]:
    out: list[str] = []
    for frame in frames:
        frame_id = str(frame.get("id") or frame.get("frame_id") or "").strip()
        if frame_id:
            out.append(frame_id)
    return out


def _base64_payload(data_url_or_b64: str) -> str:
    raw = str(data_url_or_b64 or "").strip()
    if "," in raw and raw.lower().startswith("data:"):
        return raw.split(",", 1)[1]
    return raw


def _decrypt_frame_metadata_for_gate(
    store: UserStore,
    frame_id: str,
    api_key: str | None,
    include_image: bool = False,
) -> dict:
    """Best-effort decrypt of a frame for request-scoped routes.

    The Flask backend does not store raw API keys, so only request-scoped
    callers (iOS / resident consumer / manual curl) can run this path.
    """
    fid = str(frame_id or "").strip()
    if not fid:
        return {"frame_id": "", "error": "missing_frame_id"}
    if not _frame_exists(store, fid):
        return {"frame_id": fid, "error": "frame_not_found"}
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return {"frame_id": fid, "error": "enclave_unavailable"}
    if not api_key:
        return {"frame_id": fid, "error": "api_key_unavailable"}
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.get(
                f"{enclave_url}/v1/screen/frames/{fid}/decrypt",
                headers={"X-API-Key": api_key},
                params={"include_image": "true" if include_image else "false"},
            )
        if resp.status_code >= 400:
            return {
                "frame_id": fid,
                "error": f"decrypt_http_{resp.status_code}",
                "body": resp.text[:240],
            }
        data = resp.json()
        if not isinstance(data, dict):
            return {"frame_id": fid, "error": "decrypt_non_object"}
        result = {
            "frame_id": fid,
            "id": fid,
            "ts": data.get("ts"),
            "app": data.get("app") or "unknown",
            "ocr_text": str(data.get("ocr_text") or "")[:1200],
            "w": data.get("w"),
            "h": data.get("h"),
            "image_mime": data.get("image_mime") or "image/jpeg",
        }
        if include_image and data.get("image_b64"):
            result["image_b64"] = str(data.get("image_b64") or "")
        return result
    except Exception as e:
        return {"frame_id": fid, "error": f"decrypt_error:{type(e).__name__}:{str(e)[:160]}"}


def _current_app_from_frames(frame_contexts: list[dict], fallback_frames: list[dict]) -> str:
    for frame in reversed(frame_contexts):
        app_name = str(frame.get("app") or "").strip()
        if app_name and app_name != "unknown":
            return app_name[:120]
    for frame in reversed(fallback_frames):
        app_name = str(frame.get("app") or "").strip()
        if app_name:
            return app_name[:120]
    return "unknown"


def _safe_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _enclave_get_json_for_gate(path: str, api_key: str | None, params: dict | None = None) -> tuple[dict | None, str]:
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return None, "enclave_unavailable"
    if not api_key:
        return None, "api_key_unavailable"
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.get(
                f"{enclave_url}{path}",
                headers={"X-API-Key": api_key},
                params=params or {},
            )
        if resp.status_code >= 400:
            return None, f"enclave_http_{resp.status_code}:{resp.text[:160]}"
        data = resp.json()
        if not isinstance(data, dict):
            return None, "enclave_non_object"
        return data, ""
    except Exception as e:
        return None, f"enclave_error:{type(e).__name__}:{str(e)[:120]}"


def _strip_json_code_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _ocr_summary(frames: list[dict]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for frame in reversed(frames):
        text = (frame.get("ocr_text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text[:240])
        if len(parts) >= 3:
            break
    return " | ".join(reversed(parts))[:700]


def _recent_device_events_for_wake(store: UserStore, now: float, window_sec: float) -> list[dict]:
    since = max(0.0, now - window_sec)
    return store.list_device_events(since_epoch=since, limit=25)


def _payload_float(payload: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(payload.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _normalize_proactive_state(value: Any, allowed: set[str], default: str) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in allowed else default


def _proactive_bool(payload: dict, *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if isinstance(value, (int, float)) and value:
            return True
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y", "on"}:
            return True
    return False


def _proactive_trigger(payload: dict, *, manual: bool, frames: list[dict]) -> str:
    raw = (
        payload.get("trigger")
        or payload.get("wake_trigger")
        or payload.get("event_type")
        or payload.get("type")
        or ""
    )
    trigger = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(raw or "").strip().lower()).strip("_.:-")
    if trigger:
        return trigger[:120]
    if manual:
        return "manual_wake"
    return "screen_tick" if frames else "heartbeat_no_frame"


def _proactive_v2_auto_wake_block_reason(trigger: str, *, broadcast_state: str, frame_ids: list[str]) -> str:
    """Mechanical suppression for automatic V2 wakes that lack a current signal."""
    normalized_trigger = str(trigger or "").strip().lower()
    normalized_broadcast = str(broadcast_state or "").strip().lower()
    has_frames = bool(frame_ids)

    if normalized_trigger in {"heartbeat_unknown", "heartbeat_no_frame"}:
        return "no_recent_frames"
    if normalized_trigger in {"heartbeat_broadcast_off", "heartbeat_broadcast_paused"}:
        return ""
    if normalized_trigger == "heartbeat_broadcast_on" and not has_frames:
        return "no_recent_frames"
    if normalized_trigger == "broadcast_opened" and not has_frames:
        return "no_recent_frames"
    if (
        normalized_broadcast in {"off", "paused"}
        and normalized_trigger.startswith("heartbeat")
        and normalized_trigger not in {"heartbeat_broadcast_off", "heartbeat_broadcast_paused"}
    ):
        return f"broadcast_{normalized_broadcast}"
    return ""


def _proactive_v2_wake_kind(trigger: str, *, frame_ids: list[str]) -> str:
    if frame_ids:
        return "screen"
    normalized_trigger = str(trigger or "").strip().lower()
    if normalized_trigger in {"broadcast_opened", "heartbeat_broadcast_on", "screen_tick"}:
        return "screen"
    return "presence"


def _latest_payload_state_from_events(store: UserStore, key: str, allowed: set[str]) -> str:
    for event in reversed(store.list_device_events(since_epoch=max(0.0, time.time() - 86400), limit=200)):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        state = str(payload.get(key) or "").strip().lower()
        if state in allowed:
            return state
    return ""


def _effective_broadcast_state(store: UserStore, settings: dict) -> str:
    """Broadcast state as the wake decision sees it: the latest device event
    (24h window) wins; the persisted settings value is only the fallback.
    Tick-trigger derivation MUST use this same source — deriving the trigger
    from stale settings while the decision uses the live event state lets a
    heartbeat_broadcast_off trigger bypass the no-frame suppression."""
    return (
        _latest_payload_state_from_events(store, "broadcast_state", PROACTIVE_BROADCAST_STATES)
        or _normalize_proactive_state(settings.get("broadcast_state"), PROACTIVE_BROADCAST_STATES, "unknown")
    )


def _build_proactive_v2_wake_decision(store: UserStore, payload: dict, api_key: str | None = None) -> dict:
    """Create a V2 wake event without doing platform-side semantic judgment.

    The platform may decide whether a wake is mechanically allowed, but it does
    not decrypt frames, require a memory connection, call a platform model, or infer
    whether the agent should speak. That judgment belongs to the authorized
    companion agent when the resident realizes this job.
    """
    now = time.time()
    payload = payload if isinstance(payload, dict) else {}
    settings = store.load_proactive_settings()
    force = _proactive_bool(payload, "force", "force_response")
    manual = force or _proactive_bool(payload, "manual", "manual_wake", "user_initiated") or bool(
        str(payload.get("context_hint") or "").strip()
    )

    payload_frames = payload.get("frames")
    if isinstance(payload_frames, list) and payload_frames:
        frames = [
            dict(f) for f in payload_frames
            if isinstance(f, dict) and str(f.get("id") or f.get("frame_id") or "").strip()
        ]
        for f in frames:
            if not f.get("id") and f.get("frame_id"):
                f["id"] = f.get("frame_id")
    else:
        frames = _recent_frame_meta(
            store,
            now,
            _payload_float(payload, "frame_window_sec", 300.0, 30.0, 3600.0),
        )
    selected_frames = _sample_frames_for_wake(frames, max_frames=PROACTIVE_WAKE_MAX_FRAMES)
    frame_ids = _frame_ids(selected_frames)
    device_events = _recent_device_events_for_wake(
        store,
        now,
        _payload_float(payload, "device_event_window_sec", 900.0, 30.0, 86400.0),
    )

    user_state = _normalize_proactive_state(
        payload.get("user_state"),
        PROACTIVE_USER_STATES,
        _normalize_proactive_state(settings.get("user_state"), PROACTIVE_USER_STATES, "default"),
    )
    ai_state = _normalize_proactive_state(
        payload.get("ai_state"),
        PROACTIVE_AI_STATES,
        _normalize_proactive_state(settings.get("ai_state"), PROACTIVE_AI_STATES, "present"),
    )
    broadcast_state = _normalize_proactive_state(
        payload.get("broadcast_state"),
        PROACTIVE_BROADCAST_STATES,
        _effective_broadcast_state(store, settings),
    )
    trigger = _proactive_trigger(payload, manual=manual, frames=selected_frames)

    block_reason = ""
    if not settings.get("enabled", True) and not force:
        block_reason = "proactive_disabled"
    elif settings.get("dnd", False) and not manual:
        block_reason = "dnd_enabled"
    elif user_state == "away" and not manual:
        block_reason = "user_away"
    elif not manual:
        block_reason = _proactive_v2_auto_wake_block_reason(
            trigger,
            broadcast_state=broadcast_state,
            frame_ids=frame_ids,
        )

    current_app = str(payload.get("current_app") or "").strip()
    if not current_app:
        current_app = _current_app_from_frames([], selected_frames)
    ocr = str(payload.get("ocr_summary") or "").strip() or _ocr_summary(selected_frames)
    should_wake_agent = not bool(block_reason)
    wake_kind = _proactive_v2_wake_kind(trigger, frame_ids=frame_ids)
    decision_id = _new_public_id("gd")
    wake_id = _new_public_id("wake")
    reason = "wake_created" if should_wake_agent else block_reason
    expires_at = datetime.fromtimestamp(now + PROACTIVE_V2_WAKE_TTL_SEC).isoformat()

    return {
        "decision_id": decision_id,
        "wake_id": wake_id,
        "schema_version": 2,
        "decision_type": "wake_event",
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "expires_at": expires_at,
        "gate_model": "proactive_v2:wake",
        "should_reach_out": should_wake_agent,
        "should_wake_agent": should_wake_agent,
        "should_garden_passive": False,
        "abstention_reason": "" if should_wake_agent else reason,
        "reason": reason,
        "intent_label": "",
        "context_hint": "",
        "connections": [],
        "connection": {},
        "frame_ids": frame_ids,
        "device_event_ids": [str(e.get("event_id")) for e in device_events if e.get("event_id")][:10],
        "current_app": current_app,
        "trigger": trigger,
        "wake_kind": wake_kind,
        "screen_context_available": bool(frame_ids),
        "manual": manual,
        "forced": force,
        "user_state": user_state,
        "ai_state": ai_state,
        "broadcast_state": broadcast_state,
        "semantic": {
            "reference": "agent_owned_v2",
            "llm_confidence": None,
            "llm_usage": {},
        },
        "gate_input": {
            "v2": True,
            "judgment": "agent_owned",
            "ocr_chars": len(ocr),
            "sampled_frame_count": len(selected_frames),
            "decrypt_ok": False,
            "image_count": 0,
            "decrypt_errors": [],
            "llm_called": False,
            "llm_error": "",
            "mechanical_block": block_reason,
            "memory_context": {
                "identity_loaded": False,
                "memory_count": 0,
                "passive_observation_count": 0,
                "recent_fire_count": 0,
                "connection_candidate_count": 0,
                "context_errors": {},
            },
        },
        "api_key_present": bool(api_key),
    }


def _proactive_job_from_decision(decision: dict) -> dict:
    now = time.time()
    return {
        "job_id": _new_public_id("pj"),
        "schema_version": int(decision.get("schema_version") or 1),
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "expires_at": decision.get("expires_at", ""),
        "source": PROACTIVE_JOB_SOURCE,
        "gate_decision_id": decision.get("decision_id", ""),
        "wake_id": decision.get("wake_id", decision.get("decision_id", "")),
        "status": "pending",
        "intent_label": decision.get("intent_label", ""),
        "context_hint": decision.get("context_hint", ""),
        "connections": decision.get("connections", []),
        "connection": decision.get("connection", {}),
        "frame_ids": decision.get("frame_ids", []),
        "device_event_ids": decision.get("device_event_ids", []),
        "current_app": decision.get("current_app", ""),
        "trigger": decision.get("trigger", ""),
        "manual": bool(decision.get("manual", False)),
        "forced": bool(decision.get("forced", False)),
        "user_state": decision.get("user_state", ""),
        "ai_state": decision.get("ai_state", ""),
        "broadcast_state": decision.get("broadcast_state", ""),
        "wake_kind": decision.get("wake_kind", ""),
        "screen_context_available": bool(decision.get("screen_context_available", False)),
        "agent_action": "",
        "agent_action_status": "",
    }


# ---------------------------------------------------------------------------
# Hosted wake consumer (model_api route only)
# ---------------------------------------------------------------------------

HOSTED_WAKE_CONSUMER_ID = "hosted_runtime"
# Thinking/reasoning models share this budget between reasoning and output
# tokens (same note as the chat path) — too small and the visible reply gets
# truncated into an unparseable_wake_reply failure.
HOSTED_WAKE_MAX_TOKENS = 2048


def _hosted_wake_base_eligible(store: UserStore) -> tuple[bool, str]:
    """Static preconditions for consuming ANY hosted wake job: hosted route,
    tested provider config, and a usable plaintext api_key in the process
    cache. The enabled/dnd gate is deliberately NOT here — it is per-job
    (_hosted_wake_job_block) because manual and forced summons bypass it."""
    if _load_onboarding_route(store) != "model_api":
        return False, "route_not_model_api"
    config = _load_model_api_config(store)
    if not config or config.get("test_status") != "ok":
        return False, "model_api_not_ready"
    if not store.last_seen_api_key:
        return False, "api_key_unavailable"
    return True, ""


def _hosted_wake_job_block(store: UserStore, job: dict) -> str:
    """Per-job mechanical gate, mirroring _build_proactive_v2_wake_decision:
    forced wakes bypass the enabled switch, manual wakes (incl. forced —
    the decision builder folds force into manual) bypass dnd. A blocked job
    is terminally skipped, not deferred — a wake describes a moment, and
    running it later when the user re-enables proactive would be wrong."""
    settings = store.load_proactive_settings()
    if not settings.get("enabled", True) and not job.get("forced"):
        return "proactive_disabled"
    if settings.get("dnd", False) and not job.get("manual"):
        return "dnd_enabled"
    return ""


# Concurrency caps for the per-job consumer threads. A capped-out job simply
# stays pending — the reconcile pass in the tick loop retries it — so the cap
# bounds resource use without dropping work.
HOSTED_WAKE_MAX_PER_USER = 2
HOSTED_WAKE_MAX_TOTAL = 16
_hosted_wake_slots_lock = threading.Lock()
_hosted_wake_slots: dict[str, int] = {}  # user_id -> active consumer threads


def _hosted_wake_slot_acquire(user_id: str) -> bool:
    with _hosted_wake_slots_lock:
        if _hosted_wake_slots.get(user_id, 0) >= HOSTED_WAKE_MAX_PER_USER:
            return False
        if sum(_hosted_wake_slots.values()) >= HOSTED_WAKE_MAX_TOTAL:
            return False
        _hosted_wake_slots[user_id] = _hosted_wake_slots.get(user_id, 0) + 1
        return True


def _hosted_wake_slot_release(user_id: str) -> None:
    with _hosted_wake_slots_lock:
        n = _hosted_wake_slots.get(user_id, 0) - 1
        if n > 0:
            _hosted_wake_slots[user_id] = n
        else:
            _hosted_wake_slots.pop(user_id, None)


def _spawn_hosted_wake_thread(store: UserStore, job: dict) -> bool:
    """Start one consumer thread under the concurrency caps. Returns False
    when at capacity — the job stays pending and the reconcile pass retries."""
    if not _hosted_wake_slot_acquire(store.user_id):
        return False
    try:
        threading.Thread(
            target=_run_model_api_wake_job,
            args=(store, store.last_seen_api_key, dict(job)),
            daemon=True,
        ).start()
        return True
    except Exception:
        _hosted_wake_slot_release(store.user_id)
        raise


def _maybe_start_hosted_wake_consumer(store: UserStore, job: dict) -> None:
    """append_proactive_job hook: hosted users get an in-backend consumer
    thread per job; resident users are untouched (their consumer polls).
    Only the base gate runs here — the runner applies the per-job enabled/dnd
    gate (with manual/forced bypasses) after claiming, so blocked jobs get a
    terminal skipped status instead of stranding pending."""
    try:
        eligible, _reason = _hosted_wake_base_eligible(store)
        if not eligible:
            return  # stays pending; the reconcile pass retries when base recovers
        if not _spawn_hosted_wake_thread(store, job):
            print(f"[hosted-wake:{store.user_id}] concurrency cap hit, job stays pending")
    except Exception as e:
        print(f"[hosted-wake:{store.user_id}] consumer start failed: {e}")


def _run_model_api_wake_job(store: UserStore, api_key: str, job: dict) -> None:
    """Thread entry: the push path (jsonify deep inside the Live Activity /
    APNs helpers) needs a Flask app context, which a bare daemon thread
    doesn't have. Always releases its concurrency slot."""
    try:
        with app.app_context():
            _run_model_api_wake_job_inner(store, api_key, job)
    finally:
        _hosted_wake_slot_release(store.user_id)


def _run_model_api_wake_job_inner(store: UserStore, api_key: str, job: dict) -> None:
    """Realize one V2 wake job through the user's own hosted model.

    Claim is atomic (only_if_status=pending) so a duplicate consumer or a
    stray resident poller can never double-realize. Every exit path writes a
    terminal job status — the wake dashboard must never show a silently
    half-consumed job."""
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return
    claimed = store.update_proactive_job(
        job_id,
        {"status": "claimed", "consumer_id": HOSTED_WAKE_CONSUMER_ID},
        only_if_status="pending",
    )
    if claimed is None:
        return  # already taken / not pending
    try:
        eligible, reason = _hosted_wake_base_eligible(store)
        if not eligible:
            store.update_proactive_job(job_id, {"status": "skipped", "status_reason": reason})
            return
        block = _hosted_wake_job_block(store, job)
        if block:
            store.update_proactive_job(job_id, {"status": "skipped", "status_reason": block})
            return
        runtime = _load_runtime_provider_config(store, api_key)
        if isinstance(runtime, tuple):
            _, err = runtime
            store.update_proactive_job(job_id, {
                "status": "failed",
                "status_reason": str(err.get("error") or "runtime_load_failed")[:500],
            })
            return

        wake_event_msg = build_model_api_wake_event_message(job)
        provider_messages, _context_payload, _screen_images = _model_api_context_messages(
            store,
            api_key,
            wake_event_msg["content"],
            include_screen_context=False,
        )
        provider_messages.insert(2, model_api_wake_turn_contract_message())
        store.update_proactive_job(job_id, {"status": "realizing"})
        result = chat_completion(
            runtime,
            provider_messages,
            max_tokens=HOSTED_WAKE_MAX_TOKENS,
            temperature=0.7,
            timeout=90.0,
            include_reasoning=MODEL_API_PROVIDER_REASONING_ENABLED,
        )
        actions = parse_model_api_wake_actions(str(result.get("reply") or ""))
        if actions is None:
            # 不把原始回包写进 chat —— 防内部 JSON/推理泄给用户。
            store.update_proactive_job(job_id, {
                "status": "failed", "status_reason": "unparseable_wake_reply",
            })
            return

        executed: list[dict] = []
        sent_message_ids: list[str] = []
        message_attempts = 0
        for action in actions:
            if action["type"] == "send_message":
                message_attempts += 1
                env, env_err = _build_shared_envelope_for_store(
                    store, action["text"].encode("utf-8"))
                if env is None:
                    executed.append({"type": "send_message",
                                     "status": f"envelope_failed:{str(env_err)[:120]}"})
                    continue
                row = store.append_chat(
                    "openclaw", "model_api", env,
                    extra={"proactive_job_id": job_id},
                )
                store.notify_chat_waiters()
                delivery_fields = _deliver_ai_message_push_if_background(
                    store,
                    body=action["text"],
                    title="IO",
                    data={"source": "model_api_proactive"},
                    visual_state="reply",
                )
                store.update_chat_message_metadata(row["id"], delivery_fields)
                sent_message_ids.append(row["id"])
                executed.append({"type": "send_message", "status": "posted",
                                 "chat_message_id": row["id"]})
            elif action["type"] == "set_ai_state":
                store.save_proactive_settings({"ai_state": action["state"]})
                executed.append({"type": "set_ai_state", "status": "ok",
                                 "state": action["state"]})
            else:  # sleep
                executed.append({"type": "sleep", "status": "ok"})

        if message_attempts and not sent_message_ids:
            # 模型想说话但一条都没写成（如 enclave 故障导致信封创建全部
            # 失败）——这是投递失败，不是 sleep。标 failed 让 dashboard 和
            # 后续重试看到真相，不能让"成功 sleep"吞掉主动消息。
            store.update_proactive_job(job_id, {
                "status": "failed",
                "status_reason": "message_envelope_failed",
                "agent_action": "send_message_failed",
                "agent_actions": executed[:10],
            })
            return
        if sent_message_ids:
            wake_result = "message_sent"
        elif job.get("manual"):
            # 用户主动召唤却没有任何可见回应 —— V2 评审词表里的
            # ignored_manual，单独标出供 dashboard 复盘（契约要求 manual
            # 至少给最小回应，模型违约时这里如实记录）。
            wake_result = "ignored_manual"
        elif any(a.get("type") == "set_ai_state" for a in executed):
            wake_result = "state_updated"
        else:
            wake_result = "sleep"
        final_patch = {
            "status": "completed",
            "wake_result": wake_result,
            "agent_action": wake_result,
            "agent_actions": executed[:10],
        }
        if sent_message_ids:
            final_patch["chat_message_id"] = sent_message_ids[0]
            final_patch["posted_at"] = datetime.now().isoformat()
        store.update_proactive_job(job_id, final_patch)
        print(f"[hosted-wake:{store.user_id}] job={job_id} result={wake_result}")
    except ProviderError as e:
        store.update_proactive_job(job_id, {
            "status": "failed",
            "status_reason": f"provider_chat_failed:{str(e)[:200]}",
        })
    except Exception as e:
        store.update_proactive_job(job_id, {
            "status": "failed",
            "status_reason": f"wake_runner_error:{type(e).__name__}:{str(e)[:200]}",
        })


HOSTED_TICK_INTERVAL_SEC = float(os.environ.get("FEEDLING_HOSTED_TICK_INTERVAL_SEC", "1800"))
HOSTED_TICK_LOOP_SEC = 60.0
# Reconcile leases: a claimed/realizing hosted job older than this means the
# process died mid-run (provider timeout is 90s, so 10 min is generous).
HOSTED_WAKE_LEASE_SEC = 600.0
# Pending jobs without an expires_at (perception wakes) go stale after this —
# the context_hint describes a moment, not a standing request.
HOSTED_WAKE_PENDING_MAX_AGE_SEC = 3600.0


def _job_age_ref_epoch(job: dict) -> float:
    """Best timestamp for staleness checks: updated_at (ISO) else ts."""
    raw = str(job.get("updated_at") or "")
    if raw:
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            pass
    try:
        return float(job.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _reconcile_hosted_jobs(store: UserStore, now: float) -> None:
    """Self-heal the hosted consumer across restarts/crashes. The append hook
    is the only normal consumer, so anything it missed (process died between
    append and thread completion, or the concurrency cap deferred a spawn)
    would otherwise strand forever:
      - pending: expired -> skipped; fresh -> re-spawn a consumer (the atomic
        claim makes a duplicate spawn harmless).
      - claimed/realizing owned by hosted_runtime past the lease -> failed.
        NOT re-run: the crash may have landed after the chat write, so a
        re-run could double-send.
    Resident jobs are untouched: this runs only for hosted-eligible users and
    skips claims owned by other consumer_ids."""
    for job in store.list_proactive_jobs(limit=50):
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        status = str(job.get("status") or "")
        if status == "pending":
            expired = False
            raw_exp = str(job.get("expires_at") or "")
            if raw_exp:
                try:
                    expired = datetime.fromisoformat(raw_exp).timestamp() <= now
                except ValueError:
                    pass
            elif now - _job_age_ref_epoch(job) > HOSTED_WAKE_PENDING_MAX_AGE_SEC:
                expired = True
            if expired:
                store.update_proactive_job(job_id, {
                    "status": "skipped", "status_reason": "expired_unconsumed",
                }, only_if_status="pending")
            else:
                _spawn_hosted_wake_thread(store, job)
        elif status in ("claimed", "realizing"):
            if str(job.get("consumer_id") or "") != HOSTED_WAKE_CONSUMER_ID:
                continue
            if now - _job_age_ref_epoch(job) > HOSTED_WAKE_LEASE_SEC:
                store.update_proactive_job(job_id, {
                    "status": "failed",
                    "status_reason": "stale_claim_reaped",
                }, only_if_status=status)
                print(f"[hosted-wake:{store.user_id}] reaped stale {status} job={job_id}")


def _run_hosted_tick_once(now: float | None = None) -> int:
    """One scheduler pass: create heartbeat wakes for due hosted users.
    Returns the number of jobs enqueued (the append hook consumes them).
    The trigger name comes from the user's broadcast state so the V2
    mechanical suppression treats hosted heartbeats exactly like resident
    ones."""
    now = now or time.time()
    with _users_lock:
        user_ids = [str(u.get("user_id") or "") for u in _users if u.get("user_id")]
    created = 0
    for user_id in user_ids:
        try:
            store = get_store(user_id)
            eligible, _reason = _hosted_wake_base_eligible(store)
            if not eligible:
                continue
            # Every pass (60s), not just on heartbeat: rescue stranded jobs
            # (restart/crash/concurrency-cap deferrals) before the dedup gate.
            # Runs on the BASE gate so a pending manual summon still gets
            # consumed (with its dnd bypass) while proactive is dnd'd off.
            _reconcile_hosted_jobs(store, now)
            settings = store.load_proactive_settings()
            # 心跳是自动 wake，不享受 manual/forced 豁免：开关关/勿扰时
            # 不创建（也不浪费 stamp），等用户打开后下一轮恢复。
            if not settings.get("enabled", True) or settings.get("dnd", False):
                continue
            last = db.get_blob(user_id, "hosted_tick") or {}
            if now - float(last.get("ts") or 0) < HOSTED_TICK_INTERVAL_SEC:
                continue
            # 先记 ts 再判定：判定被 suppress 也算一次 tick，避免对被
            # suppress 的用户每 60s 重复跑判定。
            db.set_blob(user_id, "hosted_tick", {
                "ts": now, "at": datetime.fromtimestamp(now).isoformat(),
            })
            # Same effective source as the decision builder (device events
            # win, settings fall back) — see _effective_broadcast_state.
            decision = _build_proactive_v2_wake_decision(
                store,
                {"trigger": model_api_hosted_tick_trigger(
                    _effective_broadcast_state(store, settings))},
                api_key=store.last_seen_api_key or None,
            )
            store.append_gate_decision(decision)
            if decision.get("should_wake_agent"):
                store.append_proactive_job(_proactive_job_from_decision(decision))
                created += 1
        except Exception as e:
            print(f"[hosted-tick:{user_id}] failed: {e}")
    return created


def _hosted_tick_loop() -> None:
    while True:
        time.sleep(HOSTED_TICK_LOOP_SEC)
        try:
            created = _run_hosted_tick_once()
            if created:
                print(f"[hosted-tick] created={created}")
        except Exception as e:
            print(f"[hosted-tick] loop error: {e}")


def _proactive_debug_snapshot(store: UserStore) -> dict:
    # The debug dashboard is used as an investigation surface, not a tiny
    # status widget. Read enough rows to cover a normal day of proactive
    # activity; the renderer still lets callers cap visible sections via
    # query params, but the backing snapshot should not hide history first.
    decisions = store.list_gate_decisions(limit=PROACTIVE_DEBUG_DECISION_READ_MAX)
    jobs = store.list_proactive_jobs(limit=PROACTIVE_DEBUG_JOB_READ_MAX)
    events = store.list_device_events(limit=PROACTIVE_DEBUG_EVENT_READ_MAX)
    reviews = store.list_gate_reviews(limit=PROACTIVE_DEBUG_REVIEW_READ_MAX)
    latest_review_by_decision: dict[str, dict] = {}
    for review in reviews:
        did = str(review.get("decision_id") or "")
        if did:
            latest_review_by_decision[did] = review
    with store.chat_lock:
        proactive_messages = [
            {
                "id": m.get("id"),
                "ts": m.get("ts"),
                "source": m.get("source"),
                "gate_decision_id": m.get("gate_decision_id", ""),
                "proactive_job_id": m.get("proactive_job_id", ""),
                "content_type": m.get("content_type", "text"),
                "alert_preview": m.get("alert_preview", ""),
                "push_body_preview": m.get("push_body_preview", ""),
                "push_live_activity_requested": bool(m.get("push_live_activity_requested")),
                "live_activity_status": m.get("live_activity_status", ""),
                "live_activity_reason": m.get("live_activity_reason", ""),
                "live_activity_activity_id": m.get("live_activity_activity_id", ""),
                "live_activity_mode": m.get("live_activity_mode", ""),
                "alert_status": m.get("alert_status", ""),
                "alert_reason": m.get("alert_reason", ""),
                "push_decision": m.get("push_decision", ""),
                "push_reason": m.get("push_reason", ""),
                "app_presence_phase": m.get("app_presence_phase", ""),
                "app_presence_age_sec": m.get("app_presence_age_sec", ""),
            }
            for m in store.chat_messages
            if m.get("source") == PROACTIVE_JOB_SOURCE or str(m.get("proactive_job_id") or "")
        ][-PROACTIVE_DEBUG_MESSAGE_READ_MAX:]
    messages_by_job = {
        str(m.get("proactive_job_id") or ""): m
        for m in proactive_messages
        if m.get("proactive_job_id")
    }
    enriched_jobs: list[dict] = []
    for job in jobs:
        row = dict(job)
        msg = messages_by_job.get(str(job.get("job_id") or ""))
        if msg:
            live_status = str(msg.get("live_activity_status") or "")
            alert_status = str(msg.get("alert_status") or "")
            if live_status == "delivered" and alert_status in {"", "delivered", "logged_only"}:
                row["derived_status"] = "delivered"
            elif live_status:
                row["derived_status"] = f"chat_written_live_activity_{live_status}"
            else:
                row["derived_status"] = "chat_written"
            row["chat_message_id"] = msg.get("id", "")
            row["chat_ts"] = msg.get("ts")
            row["alert_status"] = alert_status
            row["alert_reason"] = msg.get("alert_reason", "")
            row["live_activity_status"] = live_status
            row["live_activity_reason"] = msg.get("live_activity_reason", "")
            row["live_activity_mode"] = msg.get("live_activity_mode", "")
            row["push_decision"] = msg.get("push_decision", "")
            row["push_reason"] = msg.get("push_reason", "")
            row["preview"] = msg.get("alert_preview") or msg.get("push_body_preview") or ""
        else:
            row["derived_status"] = row.get("status", "pending")
            row.setdefault("preview", "")
        enriched_jobs.append(row)
    with store.frames_lock:
        frames = [
            {
                "id": f.get("id"),
                "ts": f.get("ts"),
                "app": f.get("app") or "unknown",
                "ocr_len": len((f.get("ocr_text") or "").strip()),
                "encrypted": bool(f.get("encrypted")),
            }
            for f in store.frames_meta[-PROACTIVE_DEBUG_FRAME_READ_MAX:]
        ]
    return {
        "user_id": store.user_id,
        "generated_at": datetime.now().isoformat(),
        "settings": store.load_proactive_settings(),
        "decisions": decisions,
        "reviews": reviews,
        "latest_review_by_decision": latest_review_by_decision,
        "jobs": enriched_jobs,
        "device_events": events,
        "proactive_messages": proactive_messages,
        "recent_frames": frames,
        "counts": {
            "decisions": len(decisions),
            "reviews": len(reviews),
            "jobs": len(jobs),
            "device_events": len(events),
            "proactive_messages": len(proactive_messages),
            "recent_frames": len(frames),
        },
    }


def _gate_input_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _gate_decision_has_frame_context(decision: dict) -> bool:
    try:
        schema_version = int(decision.get("schema_version") or 1)
    except (TypeError, ValueError):
        schema_version = 1
    if schema_version >= 2 or decision.get("decision_type") == "wake_event":
        return True
    frame_ids = decision.get("frame_ids")
    if isinstance(frame_ids, list) and any(str(fid).strip() for fid in frame_ids):
        return True
    gate_input = _gate_input_dict(decision.get("gate_input"))
    for key in ("sampled_frame_count", "image_count", "ocr_chars"):
        try:
            if int(gate_input.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return bool(gate_input.get("decrypt_ok"))


def _debug_translation_candidate(text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 8:
        return False
    # Machine enum values are handled by the fixed label dictionary. The
    # model translator is only for prose fields such as reason/context_hint.
    if re.fullmatch(r"[a-zA-Z0-9_:\-./ ]+", raw) and len(raw.split()) <= 4:
        return False
    return bool(re.search(r"[A-Za-z]", raw))


def _translate_debug_texts_to_zh(texts: list[str]) -> dict[str, str]:
    """Best-effort display-only translation for the debug dashboard.

    This never mutates wake/job records. Raw English remains in JSON logs and
    folded payloads; translated strings are only used for HTML rendering when
    `lang=zh`.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for text in texts:
        raw = str(text or "").strip()
        if not raw or raw in seen or not _debug_translation_candidate(raw):
            continue
        seen.add(raw)
        unique.append(raw[:1800])

    if not unique:
        return {}

    with _debug_translation_lock:
        cached = {text: _debug_translation_cache[text] for text in unique if text in _debug_translation_cache}
    missing = [text for text in unique if text not in cached][:24]

    if missing and OPENROUTER_API_KEY and PROACTIVE_DEBUG_TRANSLATION_MODEL:
        try:
            payload = {
                "model": PROACTIVE_DEBUG_TRANSLATION_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Translate debug-dashboard prose from English to natural, concise Simplified Chinese "
                            "for a product debugging UI. "
                            "Preserve IDs, model names, JSON keys, product names, and technical terms like Wake, "
                            "context_hint, Live Activity, APNs, OCR. Do not preserve generic words like companion, "
                            "user, screen, response, or reason; translate them naturally. Return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "texts": missing,
                                "schema": {"translations": ["same length as texts"]},
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC) as client:
                resp = client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = json.loads(_strip_json_code_fence(content))
            translations = parsed.get("translations")
            if isinstance(translations, list):
                with _debug_translation_lock:
                    for raw, translated in zip(missing, translations):
                        out = str(translated or "").strip()
                        if out:
                            _debug_translation_cache[raw] = out[:1800]
                            cached[raw] = _debug_translation_cache[raw]
        except Exception as e:
            print(f"[proactive-debug] translation failed: {e}")

    return cached


def _render_proactive_dashboard(snapshot: dict) -> str:
    def esc(value) -> str:
        return html.escape(str(value if value is not None else ""))

    lang_param = str(request.args.get("lang") or "").strip().lower()
    if lang_param not in {"zh", "en"}:
        accept_lang = request.headers.get("Accept-Language", "").lower()
        lang_param = "zh" if "zh" in accept_lang else "en"
    is_zh = lang_param == "zh"

    def ui(en: str, zh: str) -> str:
        return zh if is_zh else en

    def lang_url(target: str) -> str:
        args = request.args.to_dict(flat=True)
        args["lang"] = target
        return f"/debug/proactive?{urlencode(args)}"

    def dashboard_url(**updates) -> str:
        args = request.args.to_dict(flat=True)
        for key, value in updates.items():
            if value is None:
                args.pop(key, None)
            else:
                args[key] = str(value)
        return f"/debug/proactive?{urlencode(args)}"

    def int_arg(name: str, default: int, lower: int, upper: int) -> int:
        try:
            value = int(str(request.args.get(name) or default).strip())
        except (TypeError, ValueError):
            value = default
        return max(lower, min(upper, value))

    decision_cap = int_arg("decision_limit", 80, 1, PROACTIVE_DEBUG_DECISION_READ_MAX)
    no_frame_cap = int_arg("no_frame_limit", 50, 1, PROACTIVE_DEBUG_DECISION_READ_MAX)
    job_cap = int_arg("job_limit", 100, 1, PROACTIVE_DEBUG_JOB_READ_MAX)
    table_cap = int_arg(
        "table_limit",
        100,
        1,
        max(
            PROACTIVE_DEBUG_EVENT_READ_MAX,
            PROACTIVE_DEBUG_MESSAGE_READ_MAX,
            PROACTIVE_DEBUG_FRAME_READ_MAX,
        ),
    )
    show_no_frame = str(request.args.get("show_no_frame") or "").strip().lower() in {"1", "true", "yes"}
    show_payloads = str(request.args.get("detail") or "1").strip().lower() not in {"0", "false", "no", "off"}

    debug_labels_zh = {
        "time": "时间",
        "ts": "时间戳",
        "model": "模型",
        "id": "判定 ID",
        "trigger": "触发来源",
        "wake_kind": "Wake 类型",
        "screen_context_available": "屏幕上下文",
        "user_state": "用户状态",
        "ai_state": "AI 状态",
        "broadcast_state": "屏幕共享",
        "agent_action": "Agent 动作",
        "wake_result": "Wake 结果",
        "intent": "意图",
        "abstention": "不触发原因",
        "reason": "判定理由",
        "context_hint": "上下文提示",
        "frames": "屏幕帧",
        "frames sent": "已发送帧",
        "connection": "关联依据",
        "gate_input": "Wake 输入",
        "payload": "事件数据",
        "consumer": "消费服务",
        "decision": "Wake",
        "job": "任务",
        "preview": "消息预览",
    }

    debug_value_labels_zh = {
        "TRUE": "触发",
        "FALSE": "不触发",
        "true": "触发",
        "false": "不触发",
        "pending": "等待处理",
        "claimed": "处理中",
        "completed": "已完成",
        "delivered": "已送达",
        "chat_written": "已写入聊天",
        "logged_only": "仅记录",
        "skipped": "已跳过",
        "failed": "失败",
        "error": "错误",
        "unreviewed": "未标注",
        "correct_true": "正确触发",
        "correct_false": "正确不触发",
        "missed_opportunity": "漏掉机会",
        "spam": "打扰/垃圾",
        "weak_connection": "关联太弱",
        "repeated": "重复触发",
        "privacy_bad": "隐私不合适",
        "great_companion_moment": "很好的陪伴时机",
        "blocked_before_model": "模型前拦截",
        "reviewable_false": "可复查的不触发",
        "manual_proactive_test": "手动主动触发测试",
        "research_pause": "研究停顿",
        "proactive_screen_context": "屏幕上下文",
        "manual_hint": "手动提示",
        "already_responded": "已经回应过",
        "shared_build_reflection": "共享构建反思",
        "no_recent_frames": "最近没有屏幕帧",
        "no_recent_frames_unit_test": "最近没有屏幕帧（测试）",
        "recent_proactive_fire": "10 分钟内已经主动触发过",
        "proactive_disabled": "主动触发已关闭",
        "dnd_enabled": "勿扰模式开启",
        "frame_decrypt_unavailable": "屏幕帧无法解密",
        "memory_context_unavailable": "记忆/身份上下文不可用",
        "model_not_configured": "Gate 模型未配置",
        "model_false": "模型判断不触发",
        "llm_false": "模型判断不触发",
        "llm_true": "模型判断触发",
        "screen": "屏幕",
        "presence": "Presence",
        "available": "可用",
        "none": "无",
        "llm_non_object": "模型返回不是 JSON 对象",
        "llm_missing_context_hint": "模型缺少上下文提示",
        "llm_missing_concrete_connection": "模型缺少具体关联",
        "llm_unrecognized_connection": "模型给出的关联无法验证",
        "invalid_gate_response": "Gate 返回无效",
        "has_connection": "存在具体关联",
        "model_detected_helpful_moment": "模型发现可帮助时机",
        "model_detected_memory_connection": "模型发现记忆关联",
        "agent_call_failed": "调用用户 Agent 失败",
    }

    def tr_label(value) -> str:
        raw = str(value or "")
        return debug_labels_zh.get(raw, raw) if is_zh else raw

    def tr_value(value) -> str:
        raw = str(value if value is not None else "")
        return debug_value_labels_zh.get(raw, raw) if is_zh else raw

    def value_html(value) -> str:
        raw = str(value if value is not None else "")
        translated = tr_value(raw)
        if translated != raw:
            return f"<span title='{esc(raw)}'>{esc(translated)}</span>"
        return esc(raw)

    def status_detail_html(status, reason) -> str:
        html = value_html(status)
        reason_text = str(reason or "").strip()
        if reason_text:
            html += f"<div class='mono mini'>{esc(reason_text[:180])}</div>"
        return html

    api_key = (request.args.get("key") or "").strip()
    key_qs = f"?key={quote(api_key)}" if api_key else ""
    settings = snapshot.get("settings") or {}
    dashboard_tz_name = str(
        request.args.get("tz")
        or settings.get("timezone")
        or PROACTIVE_DEFAULT_TIMEZONE
    ).strip() or "UTC"
    dashboard_tz = _safe_zoneinfo(dashboard_tz_name)

    def fmt_time(ts_value) -> str:
        try:
            ts = float(ts_value or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts, dashboard_tz).strftime("%Y-%m-%d %H:%M:%S")

    def fmt_epoch(ts_value) -> str:
        try:
            return str(round(float(ts_value or 0), 3))
        except (TypeError, ValueError):
            return ""

    def frame_links(frame_ids) -> str:
        ids = [str(fid).strip() for fid in (frame_ids or []) if str(fid).strip()]
        if not ids:
            return ""
        links = []
        for fid in ids:
            safe = quote(fid)
            label = esc(fid[:10])
            links.append(
                f"<a class='mono' href='/v1/screen/frames/{safe}/image{key_qs}' target='_blank'>{label}</a>"
                f"<a class='mini' href='/v1/screen/frames/{safe}/decrypt{key_qs}{'&' if key_qs else '?'}include_image=false' target='_blank'>{esc(ui('json', '解密 JSON'))}</a>"
            )
        return " ".join(links)

    def status_class(value) -> str:
        text = str(value or "").lower()
        if text in {"true", "delivered", "chat_written", "logged_only"} or text.startswith("chat_written"):
            return "ok"
        if text in {"pending", "false", "skipped"}:
            return "muted"
        if "error" in text or "failed" in text:
            return "bad"
        return ""

    decisions = list(reversed(snapshot.get("decisions") or []))
    frame_decisions = [d for d in decisions if _gate_decision_has_frame_context(d)]
    no_frame_decisions = [d for d in decisions if not _gate_decision_has_frame_context(d)]
    latest_reviews = snapshot.get("latest_review_by_decision") or {}
    jobs = list(reversed(snapshot.get("jobs") or []))
    messages = list(reversed(snapshot.get("proactive_messages") or []))
    frames = list(reversed(snapshot.get("recent_frames") or []))
    events = list(reversed(snapshot.get("device_events") or []))
    translation_map: dict[str, str] = {}
    if is_zh:
        translation_candidates: list[str] = []
        translated_decisions = frame_decisions[:decision_cap]
        if show_no_frame:
            translated_decisions += no_frame_decisions[:no_frame_cap]
        for d in translated_decisions:
            translation_candidates.extend([
                str(d.get("reason") or ""),
                str(d.get("abstention_reason") or ""),
                str(d.get("context_hint") or ""),
            ])
        for j in jobs[:job_cap]:
            translation_candidates.extend([
                str(j.get("context_hint") or ""),
                str(j.get("status_reason") or ""),
                str(j.get("preview") or ""),
            ])
        for m in messages[:table_cap]:
            translation_candidates.append(
                str(m.get("alert_preview") or m.get("push_body_preview") or "")
            )
        translation_map = _translate_debug_texts_to_zh(translation_candidates)

    def prose_html(value) -> str:
        raw = str(value if value is not None else "").strip()
        if is_zh and raw in translation_map:
            return f"<span title='{esc(raw)}'>{esc(translation_map[raw])}</span>"
        return esc(raw)

    def prose_or_value_html(value) -> str:
        raw = str(value if value is not None else "")
        return prose_html(raw) if tr_value(raw) == raw else value_html(raw)

    def short_id(value, head: int = 8) -> str:
        """Truncate long IDs for display; full value shown on hover via title attr."""
        s = str(value or "").strip()
        if len(s) <= head + 2:
            return esc(s)
        return f"<span class='mono trunc' title='{esc(s)}'>{esc(s[:head])}…</span>"

    def fold_json(label: str, payload) -> str:
        """Collapse JSON payloads behind a <details> summary.

        Production debug pages can accumulate large wake inputs quickly. Keep
        the dashboard response bounded so browsers do not receive a truncated
        HTML document from the edge path.
        """
        try:
            pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(payload or "")
        if not pretty.strip() or pretty.strip() in ("{}", "null", "[]"):
            return f"<span class='muted mini'>{esc(tr_label(label))}: ∅</span>"
        max_chars = 500 if show_payloads else 180
        if len(pretty) > max_chars:
            pretty = pretty[:max_chars].rstrip() + "\n… truncated"
        return (
            f"<details class='inline-json'><summary>{esc(tr_label(label))}</summary>"
            f"<pre class='mono'>{esc(pretty)}</pre></details>"
        )

    def section_limit_link(
        param: str,
        current: int,
        total: int,
        step: int,
        label_en: str,
        label_zh: str,
        extra_updates: dict | None = None,
    ) -> str:
        if total <= current:
            return ""
        updates = dict(extra_updates or {})
        updates[param] = min(total, current + step)
        return f"<a class='control-link' href='{esc(dashboard_url(**updates))}'>{esc(ui(label_en, label_zh))}</a>"

    # Wake decisions are the heaviest rendering on this page (13 columns of
    # mixed text + JSON + form). Converted from a wide table to a stack of
    # cards: each decision is one block, fields laid out in a 2-column grid
    # that wraps to single-column on narrow viewports. JSON payloads
    # (connection, gate_input) collapse behind <details>. Same data, same
    # density, no horizontal scroll.
    def decision_card(d) -> str:
        verdict = ui("TRUE", "触发") if d.get("should_reach_out") else ui("FALSE", "不触发")
        verdict_cls = "ok" if d.get("should_reach_out") else "muted"
        gate_input = _gate_input_dict(d.get("gate_input"))
        connection = d.get("connection") or {}
        review = latest_reviews.get(str(d.get("decision_id") or "")) or {}
        decision_id = str(d.get("decision_id") or "")
        review_action = f"/v1/proactive/decisions/{quote(decision_id)}/review{key_qs}"
        frame_links_html = frame_links(d.get("frame_ids"))
        intent = d.get("intent_label") or ""
        abstention = d.get("abstention_reason") or ""
        context_hint = d.get("context_hint") or ""
        reason = d.get("reason") or ""

        meta_bits = [
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('time'))}</span> {esc(fmt_time(d.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('ts'))}</span> {esc(fmt_epoch(d.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('model'))}</span> {esc(d.get('gate_model'))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('id'))}</span> {short_id(decision_id)}</span>",
        ]
        if intent:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('intent'))}</span> {value_html(intent)}</span>")
        if d.get("trigger"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('trigger'))}</span> {value_html(d.get('trigger'))}</span>")
        if d.get("wake_kind"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('wake_kind'))}</span> {value_html(d.get('wake_kind'))}</span>")
        if "screen_context_available" in d:
            screen_context = "available" if d.get("screen_context_available") else "none"
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('screen_context_available'))}</span> {value_html(screen_context)}</span>")
        if d.get("user_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('user_state'))}</span> {value_html(d.get('user_state'))}</span>")
        if d.get("ai_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('ai_state'))}</span> {value_html(d.get('ai_state'))}</span>")
        if d.get("broadcast_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('broadcast_state'))}</span> {value_html(d.get('broadcast_state'))}</span>")
        if abstention:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('abstention'))}</span> {prose_or_value_html(abstention)}</span>")

        body_blocks = []
        if reason:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('reason'))}</span><div class='block-text'>{prose_or_value_html(reason)}</div></div>")
        if context_hint:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('context_hint'))}</span><div class='block-text'>{prose_html(context_hint)}</div></div>")
        if frame_links_html:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('frames'))}</span><div class='block-text'>{frame_links_html}</div></div>")
        if show_payloads:
            body_blocks.append(f"<div class='block'>{fold_json('connection', connection)}</div>")
            body_blocks.append(f"<div class='block'>{fold_json('gate_input', gate_input)}</div>")

        review_html = (
            f"<div class='review'>"
            f"<div class='mini'>{esc(ui('last review', '最近标注'))}: <span class='{ 'ok' if review.get('label') == 'correct_true' else '' }'>{value_html(review.get('label') or 'unreviewed')}</span></div>"
            f"<form method='post' action='{review_action}'>"
            "<select name='label'>"
            f"<option value='correct_true'>{esc(ui('correct true', '正确触发'))}</option>"
            f"<option value='correct_false'>{esc(ui('correct false', '正确不触发'))}</option>"
            f"<option value='missed_opportunity'>{esc(ui('missed opportunity', '漏掉机会'))}</option>"
            f"<option value='spam'>{esc(ui('spam', '打扰/垃圾'))}</option>"
            f"<option value='weak_connection'>{esc(ui('weak connection', '关联太弱'))}</option>"
            f"<option value='repeated'>{esc(ui('repeated', '重复触发'))}</option>"
            f"<option value='privacy_bad'>{esc(ui('privacy bad', '隐私不合适'))}</option>"
            f"<option value='great_companion_moment'>{esc(ui('great companion moment', '很好的陪伴时机'))}</option>"
            "</select>"
            f"<input name='notes' placeholder='{esc(ui('notes', '标注备注'))}' maxlength='300'>"
            f"<button type='submit'>{esc(ui('save', '保存'))}</button>"
            "</form>"
            "</div>"
        )

        return (
            f"<article class='card decision-card'>"
            f"  <header class='card-head'>"
            f"    <span class='verdict {verdict_cls}'>{verdict}</span>"
            f"    <div class='meta-bits'>{''.join(meta_bits)}</div>"
            f"  </header>"
            f"  <div class='card-body'>{''.join(body_blocks)}</div>"
            f"  {review_html}"
            f"</article>"
        )

    def decision_section(rows_source, empty_text: str, limit: int = decision_cap) -> str:
        if not rows_source:
            return f"<div class='empty'>{esc(empty_text)}</div>"
        return "<div class='card-list'>" + "".join(decision_card(d) for d in rows_source[:limit]) + "</div>"

    hidden_gate_details = ""
    if no_frame_decisions:
        if show_no_frame:
            hidden_body = decision_section(
                no_frame_decisions,
                ui("No hidden legacy no-frame ticks.", "没有隐藏的旧版无屏幕帧空 tick。"),
                limit=no_frame_cap,
            )
            more_no_frame = section_limit_link(
                "no_frame_limit",
                no_frame_cap,
                len(no_frame_decisions),
                50,
                "show more no-frame ticks",
                "显示更多空 tick",
                {"show_no_frame": 1},
            )
            hidden_hint = (
                f"<div class='hint'>{esc(ui('Showing no-frame ticks with a high cap; increase it if you need older scheduler history.', '正在显示无屏幕帧空 tick；需要更早的定时历史可以继续增加上限。'))} "
                f"{more_no_frame} "
                f"<a href='{esc(dashboard_url(show_no_frame=None))}'>{esc(ui('hide no-frame ticks', '隐藏空 tick 明细'))}</a></div>"
            )
        else:
            hidden_body = (
                f"<div class='empty'>{esc(ui('Legacy no-frame ticks are folded to keep this page lightweight.', '旧版无屏幕帧空 tick 已折叠，以保持页面轻量。'))} "
                f"<a href='{esc(dashboard_url(show_no_frame=1))}'>{esc(ui('show sample', '显示样本'))}</a></div>"
            )
            hidden_hint = ""
        hidden_gate_details = (
            "<details class='debug-details'>"
            f"<summary>{esc(ui(f'Show hidden legacy no-frame ticks ({len(no_frame_decisions)})', f'显示隐藏的旧版无屏幕帧空 tick（{len(no_frame_decisions)}）'))}</summary>"
            + hidden_hint
            + hidden_body
            + "</details>"
        )

    # Hidden Jobs — same card pattern as wake decisions. Fewer JSON blobs
    # so cards render lighter, but the wide horizontal table is the
    # bigger problem; cards solve it the same way.
    def job_card(j) -> str:
        status = j.get("derived_status") or j.get("status", "pending")
        intent = j.get("intent_label") or ""
        meta_bits = [
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('time'))}</span> {esc(fmt_time(j.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('ts'))}</span> {esc(fmt_epoch(j.get('ts')))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('job'))}</span> {short_id(j.get('job_id'))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('decision'))}</span> {short_id(j.get('gate_decision_id'))}</span>",
        ]
        if j.get("consumer_id"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('consumer'))}</span> {short_id(j.get('consumer_id'))}</span>")
        if intent:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('intent'))}</span> {value_html(intent)}</span>")
        if j.get("trigger"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('trigger'))}</span> {value_html(j.get('trigger'))}</span>")
        if j.get("wake_kind"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('wake_kind'))}</span> {value_html(j.get('wake_kind'))}</span>")
        if "screen_context_available" in j:
            screen_context = "available" if j.get("screen_context_available") else "none"
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('screen_context_available'))}</span> {value_html(screen_context)}</span>")
        if j.get("user_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('user_state'))}</span> {value_html(j.get('user_state'))}</span>")
        if j.get("ai_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('ai_state'))}</span> {value_html(j.get('ai_state'))}</span>")
        if j.get("broadcast_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('broadcast_state'))}</span> {value_html(j.get('broadcast_state'))}</span>")
        if j.get("agent_action"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('agent_action'))}</span> {value_html(j.get('agent_action'))}</span>")
        if j.get("wake_result"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('wake_result'))}</span> {value_html(j.get('wake_result'))}</span>")

        body_blocks = []
        if j.get("context_hint"):
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('context_hint'))}</span><div class='block-text'>{prose_html(j.get('context_hint'))}</div></div>")
        if j.get("status_reason"):
            status_reason = j.get("status_reason")
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('reason'))}</span><div class='block-text'>{prose_or_value_html(status_reason)}</div></div>")
        if j.get("preview"):
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('preview'))}</span><div class='block-text'>{prose_html(j.get('preview'))}</div></div>")
        frames_sent = frame_links(j.get("frame_ids"))
        if frames_sent:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('frames sent'))}</span><div class='block-text'>{frames_sent}</div></div>")
        if show_payloads and j.get("agent_actions"):
            body_blocks.append(f"<div class='block'>{fold_json('agent_actions', j.get('agent_actions'))}</div>")
        if show_payloads and j.get("request_broadcast"):
            body_blocks.append(f"<div class='block'>{fold_json('request_broadcast', j.get('request_broadcast'))}</div>")

        status_pill = f"<span class='verdict {status_class(status)}'>{value_html(status)}</span>"
        chips = []
        if j.get("alert_status"):
            chips.append(
                f"<span class='chip {status_class(j.get('alert_status'))}'>{esc(ui('alert', '通知'))}: "
                f"{status_detail_html(j.get('alert_status'), j.get('alert_reason'))}</span>"
            )
        if j.get("live_activity_status"):
            live_detail = status_detail_html(j.get("live_activity_status"), j.get("live_activity_reason"))
            if j.get("live_activity_mode"):
                live_detail += f" <span class='mono mini'>{esc(j.get('live_activity_mode'))}</span>"
            chips.append(
                f"<span class='chip {status_class(j.get('live_activity_status'))}'>Live Activity: "
                f"{live_detail}</span>"
            )

        chips_html = f"<div class='chip-row'>{''.join(chips)}</div>" if chips else ""

        return (
            f"<article class='card'>"
            f"  <header class='card-head'>{status_pill}<div class='meta-bits'>{''.join(meta_bits)}</div></header>"
            f"  {chips_html}"
            f"  <div class='card-body'>{''.join(body_blocks)}</div>"
            f"</article>"
        )

    def job_section() -> str:
        if not jobs:
            return f"<div class='empty'>{esc(ui('No hidden proactive jobs yet.', '还没有隐藏主动任务。'))}</div>"
        return "<div class='card-list'>" + "".join(job_card(j) for j in jobs[:job_cap]) + "</div>"

    # The remaining three sections (chat writes / frames / events) have
    # fewer columns; tables still fit a reasonable max-width with
    # `table-layout: fixed` + the new `.table-scroll` wrapper for the
    # rare overflow case. No card conversion needed.
    def message_rows() -> str:
        if not messages:
            return f"<tr><td colspan='8'>{esc(ui('No proactive chat writes yet.', '还没有主动消息写入。'))}</td></tr>"
        rows = []
        for m in messages[:table_cap]:
            preview = m.get("alert_preview") or m.get("push_body_preview") or ui("(encrypted envelope; no plaintext preview recorded)", "（加密 envelope；没有记录明文预览）")
            live_detail = status_detail_html(m.get("live_activity_status"), m.get("live_activity_reason"))
            if m.get("live_activity_mode"):
                live_detail += f"<div class='mono mini'>{esc(m.get('live_activity_mode'))}</div>"
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(m.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(m.get('ts')))}</div></td>"
                f"<td>{esc(m.get('content_type'))}</td>"
                f"<td class='wrap'>{prose_html(preview)}</td>"
                f"<td class='{status_class(m.get('alert_status'))}'>{status_detail_html(m.get('alert_status'), m.get('alert_reason'))}</td>"
                f"<td class='{status_class(m.get('live_activity_status'))}'>{live_detail}</td>"
                f"<td>{short_id(m.get('gate_decision_id'))}</td>"
                f"<td>{short_id(m.get('proactive_job_id'))}</td>"
                f"<td>{short_id(m.get('id'))}</td>"
                "</tr>"
            )
        return "".join(rows)

    def frame_rows() -> str:
        if not frames:
            return f"<tr><td colspan='5'>{esc(ui('No frames indexed.', '还没有索引到屏幕帧。'))}</td></tr>"
        rows = []
        for f in frames[:table_cap]:
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(f.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(f.get('ts')))}</div></td>"
                f"<td>{esc(f.get('app'))}</td>"
                f"<td>{esc(f.get('ocr_len'))}</td>"
                f"<td>{esc(f.get('encrypted'))}</td>"
                f"<td>{frame_links([f.get('id')])}</td>"
                "</tr>"
            )
        return "".join(rows)

    def event_rows() -> str:
        if not events:
            return f"<tr><td colspan='4'>{esc(ui('No device events yet.', '还没有设备事件。'))}</td></tr>"
        rows = []
        for e in events[:table_cap]:
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(e.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(e.get('ts')))}</div></td>"
                f"<td>{esc(e.get('source'))}</td>"
                f"<td>{esc(e.get('type'))}</td>"
                f"<td>{fold_json('payload', e.get('payload') or {}) if show_payloads else short_id((e.get('payload') or {}).get('id') or e.get('id') or e.get('type'))}</td>"
                "</tr>"
            )
        return "".join(rows)

    counts = snapshot.get("counts") or {}
    visible_wake_count = len(frame_decisions)
    hidden_no_frame_count = len(no_frame_decisions)
    page_title = "IO Proactive Harness"
    visible_empty_text = ui(
        f"No visible wake decisions yet. Hidden legacy no-frame ticks: {hidden_no_frame_count}.",
        f"还没有可见的 Wake 判定。隐藏旧版空 tick：{hidden_no_frame_count}。",
    )
    detail_toggle = (
        f"<a class='control-link' href='{esc(dashboard_url(detail=0))}'>{esc(ui('hide JSON detail', '隐藏 JSON 详情'))}</a>"
        if show_payloads
        else f"<a class='control-link' href='{esc(dashboard_url(detail=1))}'>{esc(ui('show JSON detail', '显示 JSON 详情'))}</a>"
    )
    control_links = " ".join(
        link for link in [
            detail_toggle,
            section_limit_link(
                "decision_limit",
                decision_cap,
                len(frame_decisions),
                80,
                "show more wake decisions",
                "显示更多 Wake",
            ),
            section_limit_link(
                "job_limit",
                job_cap,
                len(jobs),
                100,
                "show more completed jobs",
                "显示更多已完成任务",
            ),
            section_limit_link(
                "table_limit",
                table_cap,
                max(len(messages), len(frames), len(events)),
                100,
                "show more tables",
                "显示更多表格记录",
            ),
            (
                f"<a class='control-link' href='{esc(dashboard_url(show_no_frame=1))}'>{esc(ui('show no-frame ticks', '显示空 tick'))}</a>"
                if no_frame_decisions and not show_no_frame
                else ""
            ),
        ]
        if link
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(page_title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0 auto;
      padding: 24px;
      max-width: 1240px;
      color: #1f1d1a;
      background: #f6f0e6;
      line-height: 1.4;
    }}
    h1 {{ margin: 0 0 4px; }}
    .topbar {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .lang-switch {{
      display: inline-flex;
      gap: 4px;
      border: 1px solid #c9bfb2;
      background: #fffaf1;
      padding: 3px;
      border-radius: 2px;
      flex: 0 0 auto;
    }}
    .lang-switch a {{
      display: inline-block;
      padding: 4px 8px;
      border: 0;
      color: #6f6961;
      font-size: 12px;
    }}
    .lang-switch a.active {{
      background: #8e301f;
      color: #fffaf1;
    }}
    @media (max-width: 640px) {{
      .topbar {{ flex-direction: column; }}
    }}
    h2 {{
      margin-top: 32px;
      border-top: 1px solid #d8d0c4;
      padding-top: 18px;
      font-size: 18px;
    }}
    .meta {{ color: #6f6961; margin-bottom: 16px; font-size: 13px; }}
    .pill {{
      display: inline-block;
      border: 1px solid #c9bfb2;
      padding: 4px 8px;
      margin: 2px;
      background: #fffaf1;
      font-size: 12px;
      border-radius: 2px;
    }}
    .hint {{ margin: 8px 0 16px; color: #6f6961; font-size: 13px; }}
    .control-link {{ display: inline-block; margin-left: 8px; white-space: nowrap; }}
    .empty {{
      padding: 16px;
      background: #fffaf1;
      border: 1px solid #ddd2c5;
      color: #6f6961;
      font-style: italic;
    }}

    /* ---- Card layout (Wake Decisions + Hidden Jobs) ---- */
    .card-list {{ display: flex; flex-direction: column; gap: 12px; }}
    .card {{
      background: #fffaf1;
      border: 1px solid #ddd2c5;
      padding: 14px 16px;
      border-radius: 2px;
    }}
    .card-head {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 10px;
      padding-bottom: 10px;
      border-bottom: 1px solid #eee5d5;
    }}
    .verdict {{
      display: inline-block;
      padding: 4px 10px;
      font-weight: 700;
      font-size: 12px;
      letter-spacing: 0.5px;
      background: #efe5d7;
      border-radius: 2px;
      white-space: nowrap;
    }}
    .verdict.ok    {{ background: #d4ead8; color: #0b7d42; }}
    .verdict.muted {{ background: #ebe4d4; color: #8b8176; }}
    .verdict.bad   {{ background: #f5d8d4; color: #b42318; }}
    .meta-bits {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 18px;
      flex: 1;
      align-items: baseline;
      font-size: 12px;
    }}
    .meta-bit {{ color: #1f1d1a; }}
    .meta-bit .label {{
      color: #8b8176;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      margin-right: 4px;
    }}
    .chip-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
    .chip {{
      display: inline-block;
      padding: 2px 8px;
      background: #efe5d7;
      font-size: 11px;
      border-radius: 2px;
    }}
    .chip.ok    {{ background: #d4ead8; color: #0b7d42; }}
    .chip.muted {{ background: #ebe4d4; color: #8b8176; }}
    .chip.bad   {{ background: #f5d8d4; color: #b42318; }}
    .card-body {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px 24px;
    }}
    @media (max-width: 720px) {{
      .card-body {{ grid-template-columns: 1fr; }}
    }}
    .block {{ min-width: 0; }}
    .block-label {{
      display: block;
      color: #8b8176;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      margin-bottom: 3px;
    }}
    .block-text {{
      font-size: 13px;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }}
    .review {{
      margin-top: 12px;
      padding-top: 10px;
      border-top: 1px solid #eee5d5;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      font-size: 12px;
    }}
    .review .mini {{ margin-right: 6px; color: #6f6961; }}
    .review form {{ display: inline-flex; gap: 6px; flex-wrap: wrap; }}
    .review select, .review input, .review button {{ font: inherit; font-size: 12px; }}
    .review input {{ width: 160px; }}

    /* ---- Inline JSON disclosure (used inside cards + small tables) ---- */
    details.inline-json {{ display: block; }}
    details.inline-json > summary {{
      cursor: pointer;
      color: #8e301f;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      list-style: revert;
    }}
    details.inline-json[open] > summary {{ margin-bottom: 4px; }}
    details.inline-json pre {{
      margin: 0;
      padding: 8px;
      background: #f0e8d8;
      overflow-x: auto;
      max-height: 280px;
      font-size: 11px;
      border-radius: 2px;
    }}

    /* ---- Tables (Chat Writes / Frames / Events) ---- */
    .table-scroll {{
      max-width: 100%;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      border: 1px solid #ddd2c5;
      background: #fffaf1;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid #eee5d5;
      padding: 8px 10px;
      vertical-align: top;
      text-align: left;
      overflow-wrap: anywhere;
    }}
    th {{
      background: #efe5d7;
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #5a544b;
    }}
    tr:last-child td {{ border-bottom: none; }}
    .wrap {{ white-space: pre-wrap; }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11.5px;
    }}
    .mini {{ font-size: 10.5px; color: #8b8176; }}
    .trunc {{ cursor: help; border-bottom: 1px dotted #b8a895; }}

    /* Per-table column widths */
    .t-messages col.c-time   {{ width: 13%; }}
    .t-messages col.c-type   {{ width: 8%; }}
    .t-messages col.c-prev   {{ width: 33%; }}
    .t-messages col.c-status {{ width: 9%; }}
    .t-messages col.c-id     {{ width: 9%; }}
    .t-frames   col.c-time   {{ width: 18%; }}
    .t-frames   col.c-app    {{ width: 22%; }}
    .t-frames   col.c-num    {{ width: 12%; }}
    .t-frames   col.c-link   {{ width: 32%; }}
    .t-events   col.c-time   {{ width: 16%; }}
    .t-events   col.c-source {{ width: 14%; }}
    .t-events   col.c-type   {{ width: 18%; }}
    .t-events   col.c-payload{{ width: 52%; }}

    a {{ color: #8e301f; text-decoration: none; border-bottom: 1px solid #d0a094; }}
    a.mini {{ margin-left: 6px; font-size: 11px; color: #6f6961; }}
    .ok    {{ color: #0b7d42; font-weight: 600; }}
    .muted {{ color: #8b8176; }}
    .bad   {{ color: #b42318; font-weight: 600; }}
    details.debug-details {{ margin-top: 14px; }}
    details.debug-details > summary {{ cursor: pointer; color: #8e301f; margin-bottom: 8px; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>{esc(page_title)}</h1>
      <div class="meta">{esc(ui('user', '用户'))} <span class="mono">{esc(snapshot.get('user_id'))}</span> · {esc(ui('generated', '生成时间'))} {esc(snapshot.get('generated_at'))} · {esc(ui('times shown in', '页面时间按'))} {esc(dashboard_tz_name)} {esc(ui('', '显示'))} · {esc(ui('auto-refresh 5s', '每 5 秒自动刷新'))}</div>
    </div>
    <nav class="lang-switch" aria-label="language">
      <a class="{'active' if not is_zh else ''}" href="{esc(lang_url('en'))}">English</a>
      <a class="{'active' if is_zh else ''}" href="{esc(lang_url('zh'))}">中文</a>
    </nav>
  </div>
  <div>
    <span class="pill">{esc(ui('all decisions', '全部判定'))} {esc(counts.get('decisions', 0))}</span>
    <span class="pill">{esc(ui('visible decisions', '主表判定'))} {esc(visible_wake_count)}</span>
    <span class="pill">{esc(ui('hidden no-frame ticks', '隐藏空 tick'))} {esc(hidden_no_frame_count)}</span>
    <span class="pill">{esc(ui('human reviews', '人工标注'))} {esc(counts.get('reviews', 0))}</span>
    <span class="pill">{esc(ui('hidden jobs', '隐藏任务'))} {esc(counts.get('jobs', 0))}</span>
    <span class="pill">{esc(ui('proactive writes', '主动写入'))} {esc(counts.get('proactive_messages', 0))}</span>
    <span class="pill">{esc(ui('screen frames', '屏幕帧'))} {esc(counts.get('recent_frames', 0))}</span>
    <span class="pill">{esc(ui('device events', '设备事件'))} {esc(counts.get('device_events', 0))}</span>
  </div>
  <div class="hint">
    {esc(ui('Full debug mode is on by default. The page reads deeper history and caps only the rendered sections; use the links to expand further or hide JSON payloads.', '默认已恢复完整调试模式。页面会读取更深的历史，只限制渲染条数；可以用下面链接继续展开或隐藏 JSON。'))}
    {control_links}
  </div>

  <h2>{esc(ui('Wake Decisions', 'Wake 判定'))}</h2>
  <div class="hint">{esc(ui('V2 wake events are always shown. Legacy no-frame ticks stay folded below.', 'V2 wake 事件会直接显示；旧版无屏幕帧空 tick 会折叠在下方。'))}</div>
  {decision_section(frame_decisions, visible_empty_text, limit=decision_cap)}
  {hidden_gate_details}

  <h2>{esc(ui('Hidden Jobs', '隐藏任务'))}</h2>
  {job_section()}

  <h2>{esc(ui('Proactive Chat Writes', '主动消息写入'))}</h2>
  <div class="table-scroll">
    <table class="t-messages">
      <colgroup>
        <col class="c-time"><col class="c-type"><col class="c-prev">
        <col class="c-status"><col class="c-status">
        <col class="c-id"><col class="c-id"><col class="c-id">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>{esc(ui('type', '类型'))}</th><th>{esc(ui('preview', '预览'))}</th><th>{esc(ui('alert', '系统通知'))}</th><th>Live Activity</th><th>{esc(ui('decision', '判定'))}</th><th>{esc(ui('job', '任务'))}</th><th>{esc(ui('message', '消息'))}</th></tr></thead>
      <tbody>{message_rows()}</tbody>
    </table>
  </div>

  <h2>{esc(ui('Recent Screen Frames', '最近屏幕帧'))}</h2>
  <div class="table-scroll">
    <table class="t-frames">
      <colgroup>
        <col class="c-time"><col class="c-app"><col class="c-num"><col class="c-num"><col class="c-link">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>App</th><th>{esc(ui('OCR length', 'OCR 长度'))}</th><th>{esc(ui('encrypted', '已加密'))}</th><th>{esc(ui('frame', '屏幕帧'))}</th></tr></thead>
      <tbody>{frame_rows()}</tbody>
    </table>
  </div>

  <h2>{esc(ui('Device Events', '设备事件'))}</h2>
  <div class="table-scroll">
    <table class="t-events">
      <colgroup>
        <col class="c-time"><col class="c-source"><col class="c-type"><col class="c-payload">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>{esc(ui('source', '来源'))}</th><th>{esc(ui('type', '类型'))}</th><th>{esc(ui('payload', '事件数据'))}</th></tr></thead>
      <tbody>{event_rows()}</tbody>
    </table>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# WebSocket ingest server
# ---------------------------------------------------------------------------

WS_PORT = int(os.environ.get("FEEDLING_WS_PORT", 9998))


def _resolve_ws_user(websocket) -> tuple[str, str] | None:
    """Resolve user from WS connection. Returns (user_id, key), or None on auth failure.

    Reads ?key=... from the path, or "Bearer ..." from the Authorization
    header (whichever arrives first)."""
    # websockets lib v12+ uses websocket.request.path and .headers
    path = getattr(websocket, "path", "") or ""
    key = None
    if "?" in path:
        try:
            q = parse_qs(urlparse(path).query)
            k = q.get("key", [""])[0].strip()
            if k:
                key = k
        except Exception:
            pass

    if not key:
        # websockets>=10 exposes headers via .request_headers or .request.headers
        headers = getattr(websocket, "request_headers", None) or getattr(
            getattr(websocket, "request", None), "headers", {}
        )
        auth = ""
        try:
            auth = headers.get("Authorization", "")
        except Exception:
            try:
                auth = headers["Authorization"]
            except Exception:
                auth = ""
        if auth and auth.lower().startswith("bearer "):
            key = auth[7:].strip()

    if not key:
        return None
    user_id = _resolve_user(key)
    if not user_id:
        return None
    return (user_id, key)


async def _ws_handler(websocket):
    try:
        resolved = _resolve_ws_user(websocket)
    except Exception as e:
        print(f"[ws] auth error: {e}")
        await websocket.close(code=4401, reason="unauthorized")
        return
    if not resolved:
        print("[ws] rejected: no valid key")
        await websocket.close(code=4401, reason="unauthorized")
        return
    user_id, ws_key = resolved

    store = get_store(user_id)
    store.last_seen_api_key = ws_key
    print(f"[ws] client connected user={user_id} peer={websocket.remote_address}")
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "frame":
                    threading.Thread(target=_save_frame, args=(store, data), daemon=True).start()
            except Exception as e:
                print(f"[ws:{user_id}] parse error: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    print(f"[ws:{user_id}] client disconnected")


async def _ws_main():
    try:
        async with websockets.serve(_ws_handler, "0.0.0.0", WS_PORT):
            print(f"[ws] WebSocket ingest server running on ws://0.0.0.0:{WS_PORT}/ingest")
            await asyncio.Future()
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"[ws] WARNING: port {WS_PORT} already in use — WebSocket ingest disabled, HTTP continues")
        else:
            raise


def _run_ws_server():
    asyncio.run(_ws_main())


threading.Thread(target=_run_ws_server, daemon=True).start()

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Per-request access log
# ---------------------------------------------------------------------------
# gunicorn (production) does not emit Flask's old dev-server access line, so
# without this the only per-request visibility in `phala cvms logs` is whatever
# individual handlers happen to print. This logs ONE structured line per
# request with the server-side handler time (dur_ms) and the on-the-wire
# response size + encoding — enough to tell "backend slow" from "network slow"
# and to spot oversized responses straight from the CVM logs. Registered
# before Compress(app) so it runs AFTER compression and records the final
# Content-Length / Content-Encoding.
@app.before_request
def _access_log_start():
    g._req_start = time.monotonic()


@app.after_request
def _access_log_end(response):
    # /healthz is hit constantly by the HAProxy/uptime probe — skip the noise.
    if request.path == "/healthz":
        return response
    start = getattr(g, "_req_start", None)
    dur_ms = int((time.monotonic() - start) * 1000) if start is not None else -1
    uid = getattr(g, "user_id", "-")
    clen = response.headers.get("Content-Length", "?")
    enc = response.headers.get("Content-Encoding", "-")
    # Keep the query string (limit/since/include_image_body — the useful bits)
    # but REDACT the `key` param: _extract_api_key() accepts `?key=` as an auth
    # method, so the URL can carry a live API key that must never reach the logs.
    if request.args.get("key"):
        pairs = [
            (k, "REDACTED" if k.lower() == "key" else v)
            for k, v in request.args.items(multi=True)
        ]
        path = f"{request.path}?{urlencode(pairs)}"
    else:
        path = request.full_path.rstrip("?")
    print(
        f"[req] uid={uid} {request.method} {path} status={response.status_code} "
        f"bytes={clen} enc={enc} dur_ms={dur_ms}",
        flush=True,
    )
    return response


# gzip responses when the client sends Accept-Encoding: gzip. CVM egress
# throughput is ~30-50 KB/s for large payloads; the decrypt-with-image
# path was shipping 470 KB of JSON per call, and dropping that in half
# via compression is a 3-5x latency win for "show me the screen" calls.
Compress(app)

# Extended Perception — self-contained feature module (backend/perception/).
# Mounts the /v1/perception/* blueprint; all its logic lives in that package.
from perception import register as register_perception  # noqa: E402
from perception import snapshot_for_wake as _perception_wake_snapshot  # noqa: E402
register_perception(app)

# ---------------------------------------------------------------------------
# APNs config (global — one Apple dev key for the app)
# ---------------------------------------------------------------------------

TEAM_ID = os.environ.get("APNS_TEAM_ID", "").strip() or "DC9JH5DRMY"
KEY_ID = os.environ.get("APNS_KEY_ID", "").strip() or "5TH55X5U7T"
BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "").strip() or "com.feedling.mcp"
APNS_SANDBOX = os.environ.get("APNS_SANDBOX", "true").strip().lower() != "false"

APNS_KEY = None
# Prefer env vars over filesystem: CVM deploys inject the key via
# docker compose env, not mounted files. APNS_KEY_P8_B64 is base64 to
# survive GH Actions → compose shell quoting of the multi-line PEM.
_env_b64 = os.environ.get("APNS_KEY_P8_B64", "").strip()
if _env_b64:
    try:
        APNS_KEY = base64.b64decode(_env_b64).decode("utf-8")
        print(f"[apns] key loaded from APNS_KEY_P8_B64 (len={len(APNS_KEY)})")
    except Exception as e:
        print(f"[apns] APNS_KEY_P8_B64 decode failed: {e}")
if not APNS_KEY:
    _env_raw = os.environ.get("APNS_KEY_P8", "").strip()
    if _env_raw:
        APNS_KEY = _env_raw
        print(f"[apns] key loaded from APNS_KEY_P8 (len={len(APNS_KEY)})")
if not APNS_KEY:
    _env_path = os.environ.get("APNS_KEY_PATH", "").strip()
    _KEY_SEARCH = [
        Path(_env_path) if _env_path else None,
        FEEDLING_DIR / f"AuthKey_{KEY_ID}.p8",
        Path(__file__).parent / f"AuthKey_{KEY_ID}.p8",
    ]
    for _p in _KEY_SEARCH:
        if _p and _p.exists():
            APNS_KEY = _p.read_text()
            print(f"[apns] key loaded from {_p}")
            break
if not APNS_KEY:
    print("[apns] WARNING: .p8 key not found — push endpoints will log only, not deliver")


def _make_apns_jwt() -> str:
    return jwt.encode(
        {"iss": TEAM_ID, "iat": int(time.time())},
        APNS_KEY,
        algorithm="ES256",
        headers={"kid": KEY_ID},
    )


def _apns_env_name(sandbox: bool) -> str:
    return "sandbox" if sandbox else "production"


def _apns_host(sandbox: bool) -> str:
    return "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"


def _apns_reason_text(result: dict) -> str:
    reason = str((result or {}).get("reason") or "")
    try:
        parsed = json.loads(reason)
        if isinstance(parsed, dict) and parsed.get("reason"):
            return str(parsed.get("reason"))
    except Exception:
        pass
    return reason


def _apns_should_retry_other_env(result: dict) -> bool:
    if (result or {}).get("status") != "error":
        return False
    reason = _apns_reason_text(result)
    return any(
        marker in reason
        for marker in (
            "BadDeviceToken",
            "BadEnvironmentKeyInToken",
            "BadEnvironmentKeyIdInToken",
            "BadCertificateEnvironment",
            # Live Activity tokens can surface this for an environment
            # mismatch. Try the other APNs host before expiring the token.
            "ExpiredToken",
        )
    )


def _apns_token_should_expire(result: dict) -> bool:
    if (result or {}).get("status") != "error":
        return False
    reason = _apns_reason_text(result)
    return any(
        marker in reason
        for marker in (
            "BadDeviceToken",
            "BadEnvironmentKeyInToken",
            "BadEnvironmentKeyIdInToken",
            "BadCertificateEnvironment",
            "DeviceTokenNotForTopic",
            "ExpiredToken",
            "TopicDisallowed",
            "Unregistered",
        )
    )


def _send_apns_once(device_token: str, payload: dict, push_type: str, topic: str, *, sandbox: bool) -> dict:
    host = _apns_host(sandbox)
    url = f"https://{host}/3/device/{device_token}"
    env_name = _apns_env_name(sandbox)
    headers = {
        "authorization": f"bearer {_make_apns_jwt()}",
        "apns-push-type": push_type,
        "apns-topic": topic,
        "apns-expiration": "0",
        "apns-priority": "10",
    }
    try:
        with httpx.Client(http2=True, timeout=10) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return {"status": "delivered", "apns_env": env_name}
        return {"status": "error", "code": resp.status_code, "reason": resp.text, "apns_env": env_name}
    except Exception as e:
        return {"status": "error", "reason": str(e), "apns_env": env_name}


def _apns_sandbox_for_env(env: str | None) -> bool | None:
    clean = str(env or "").strip().lower()
    if clean == "sandbox":
        return True
    if clean == "production":
        return False
    return None


def _send_apns(
    device_token: str,
    payload: dict,
    push_type: str,
    topic: str,
    *,
    preferred_env: str | None = None,
) -> dict:
    if not APNS_KEY:
        print(f"[apns] no key — logged only → {device_token[:16]}… {payload}")
        return {"status": "logged_only"}

    preferred_sandbox = _apns_sandbox_for_env(preferred_env)
    primary_sandbox = APNS_SANDBOX if preferred_sandbox is None else preferred_sandbox
    attempts: list[dict] = []
    for idx, sandbox in enumerate((primary_sandbox, not primary_sandbox)):
        if idx == 1 and attempts and not _apns_should_retry_other_env(attempts[0]):
            break
        result = _send_apns_once(device_token, payload, push_type, topic, sandbox=sandbox)
        attempts.append(result)
        if result.get("status") == "delivered":
            if idx > 0:
                result["fallback_attempted"] = True
                result["fallback_from"] = attempts[0].get("apns_env", _apns_env_name(primary_sandbox))
            return result

    last = dict(attempts[-1]) if attempts else {"status": "error", "reason": "not_attempted"}
    if len(attempts) > 1:
        last["fallback_attempted"] = True
        last["fallback_from"] = attempts[0].get("apns_env", _apns_env_name(primary_sandbox))
        last["first_error"] = attempts[0]
    last["attempted_envs"] = [str(a.get("apns_env") or "") for a in attempts]
    return last


def _send_apns_to_active_tokens(
    store: UserStore,
    predicate,
    payload: dict,
    *,
    push_type: str,
    topic: str,
    activity_id: str | None = None,
) -> dict:
    candidates = _select_tokens(store, predicate, activity_id=activity_id, active_only=True)
    if not candidates and activity_id:
        candidates = _select_tokens(store, predicate, active_only=True)
    if not candidates:
        return {"status": "skipped", "reason": "no_active_token", "attempts": 0}

    errors = []
    for entry in candidates:
        result = _send_apns(
            entry["token"],
            payload,
            push_type=push_type,
            topic=topic,
            preferred_env=entry.get("apns_env"),
        )
        if result.get("status") == "delivered":
            _mark_active_token_success(store, entry, apns_env=result.get("apns_env"))
            result["attempts"] = len(errors) + 1
            return result

        reason_text = _apns_reason_text(result)
        if reason_text:
            _update_token_lifecycle(store, entry, last_error=reason_text)
        errors.append({
            "type": entry.get("type", ""),
            "activity_id": entry.get("activity_id", ""),
            "registered_at": entry.get("registered_at", ""),
            "apns_env": result.get("apns_env", ""),
            "attempted_envs": result.get("attempted_envs", []),
            "reason": reason_text or str(result.get("reason", "")),
        })
        if _apns_token_should_expire(result):
            _mark_expired_token(store, entry, reason_text or str(result.get("reason", "")))
            continue

    last = errors[-1] if errors else {}
    return {
        "status": "error",
        "reason": last.get("reason", "all_tokens_failed"),
        "attempts": len(errors),
        "errors": errors[-5:],
    }


# ---------------------------------------------------------------------------
# Aggregation helpers (stateless)
# ---------------------------------------------------------------------------

TODAY = datetime.now().strftime("%Y-%m-%d")

IOS_FALLBACK_DATA = {
    "date": TODAY,
    "total_screen_time_minutes": 0,
    "scroll_distance_meters": 0.0,
    "pickups": 0,
    "unlock_count": 0,
    "apps": [],
    "categories": {},
    "frame_count": 0,
    "data_source": "mock_fallback",
}


def _humanize_app_name(raw: str) -> str:
    value = (raw or "unknown").strip()
    if not value:
        return "Unknown"
    if value.startswith("com."):
        tail = value.split(".")[-1]
        if not tail:
            return value
        return tail.replace("_", " ").replace("-", " ").title()
    return value


def _category_for_app(app_name_or_bundle: str) -> str:
    key = (app_name_or_bundle or "").lower()
    if any(x in key for x in ["tiktok", "youtube", "bili", "netflix"]):
        return "Entertainment"
    if any(x in key for x in ["instagram", "twitter", "x.com", "xiaohong", "reddit"]):
        return "Social"
    if any(x in key for x in ["wechat", "telegram", "whatsapp", "messages", "slack", "feishu", "lark"]):
        return "Communication"
    if any(x in key for x in ["safari", "chrome", "browser"]):
        return "Browsing"
    if any(x in key for x in ["maps", "map", "gaode", "waze"]):
        return "Navigation"
    if any(x in key for x in ["camera", "photos", "settings", "preference", "clock", "calendar"]):
        return "Utility"
    return "Other"


def _to_hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _build_ios_data(store: UserStore, window_sec: float = 86400.0) -> dict:
    now = time.time()
    with store.frames_lock:
        frames = [f.copy() for f in store.frames_meta if now - float(f.get("ts", 0)) <= window_sec]

    if not frames:
        fallback = IOS_FALLBACK_DATA.copy()
        fallback["date"] = datetime.now().strftime("%Y-%m-%d")
        return fallback

    frames.sort(key=lambda f: float(f.get("ts", 0)))

    per_app = defaultdict(lambda: {
        "name": "Unknown",
        "bundle_id": "",
        "category": "Other",
        "duration_seconds": 0.0,
        "sessions": 0,
        "first_ts": 0.0,
        "last_ts": 0.0,
    })
    categories_seconds = defaultdict(float)

    MAX_STEP_SECONDS = 8.0
    NEW_SESSION_GAP_SECONDS = 45.0

    session_count = 0
    prev_app_key = None
    prev_ts = None

    for frame in frames:
        ts = float(frame.get("ts", 0.0))
        app_raw = frame.get("app") or "unknown"
        app_key = str(app_raw)

        row = per_app[app_key]
        row["name"] = _humanize_app_name(app_key)
        row["bundle_id"] = app_key
        row["category"] = _category_for_app(app_key)
        row["last_ts"] = ts
        if not row["first_ts"]:
            row["first_ts"] = ts

        if prev_ts is None:
            session_count += 1
            row["sessions"] += 1
        else:
            gap = max(0.0, ts - prev_ts)
            if app_key != prev_app_key or gap > NEW_SESSION_GAP_SECONDS:
                session_count += 1
                row["sessions"] += 1

            if prev_app_key is not None:
                step = min(gap, MAX_STEP_SECONDS)
                per_app[prev_app_key]["duration_seconds"] += step
                categories_seconds[per_app[prev_app_key]["category"]] += step

        prev_app_key = app_key
        prev_ts = ts

    if prev_app_key is not None:
        per_app[prev_app_key]["duration_seconds"] += 1.0
        categories_seconds[per_app[prev_app_key]["category"]] += 1.0

    apps = []
    total_seconds = 0.0
    for app_key, row in per_app.items():
        dur_min = round(row["duration_seconds"] / 60.0, 1)
        total_seconds += row["duration_seconds"]
        apps.append({
            "name": row["name"],
            "bundle_id": row["bundle_id"],
            "category": row["category"],
            "duration_minutes": dur_min,
            "sessions": int(row["sessions"]),
            "first_used": _to_hhmm(row["first_ts"]),
            "last_used": _to_hhmm(row["last_ts"]),
        })

    apps.sort(key=lambda a: a["duration_minutes"], reverse=True)

    categories = {
        cat: round(sec / 60.0, 1)
        for cat, sec in sorted(categories_seconds.items(), key=lambda kv: kv[1], reverse=True)
        if sec > 0
    }

    total_minutes = round(total_seconds / 60.0, 1)
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_screen_time_minutes": total_minutes,
        "scroll_distance_meters": round(total_minutes * 0.02, 2),
        "pickups": int(session_count),
        "unlock_count": int(session_count),
        "apps": apps,
        "categories": categories,
        "frame_count": len(frames),
        "window_sec": int(window_sec),
        "data_source": "real_frames",
    }


MAC_DATA = {
    "date": TODAY,
    "total_active_minutes": 395,
    "deep_work_minutes": 175,
    "focus_score": 72,
    "context_switches": 34,
    "apps": [
        {"name": "Google Chrome", "bundle_id": "com.google.Chrome", "category": "Browsing",
         "duration_minutes": 120, "window_titles": ["Notion – feedling roadmap", "Linear – Sprint 3",
                                                      "Figma Community", "Stack Overflow"]},
        {"name": "Figma", "bundle_id": "com.figma.Desktop", "category": "Design",
         "duration_minutes": 95, "window_titles": ["Feedling iOS – v2 screens", "Component library"]},
        {"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92", "category": "Development",
         "duration_minutes": 85, "window_titles": ["feedling-mcp-v1 – app.py", "feedling-mcp-v1 – SKILL.md"]},
        {"name": "Zoom", "bundle_id": "us.zoom.xos", "category": "Communication",
         "duration_minutes": 45, "window_titles": ["Weekly sync", "Design review"]},
        {"name": "Slack", "bundle_id": "com.tinyspeck.slackmacgap", "category": "Communication",
         "duration_minutes": 40, "window_titles": ["#design", "#eng", "#general", "DMs"]},
        {"name": "Terminal", "bundle_id": "com.apple.Terminal", "category": "Development",
         "duration_minutes": 10, "window_titles": ["zsh – feedling-mcp-v1"]},
    ],
    "categories": {"Browsing": 120, "Design": 95, "Development": 95, "Communication": 85},
}

SOURCES_DATA = {
    "sources": [
        {"id": "ios_pip", "name": "iPhone PIP Recording", "status": "connected",
         "last_sync": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "device": "iPhone 16 Pro"},
        {"id": "mac_monitor", "name": "Mac Screen Monitor", "status": "connected",
         "last_sync": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "device": "MacBook Pro M3"},
    ]
}


def _log_bootstrap_event(store: UserStore, event_type: str, success: bool, error_message: str = ""):
    entry = {
        "user_id": store.user_id,
        "event_type": event_type,
        "success": success,
        "error_message": error_message,
        "timestamp": datetime.now().isoformat(),
    }
    db.log_append(store.user_id, "bootstrap_events", entry)


def _load_bootstrap_events(store: UserStore) -> list[dict]:
    return db.log_read_all(store.user_id, "bootstrap_events")


_OFFICIAL_CONSUMER_NAME = "feedling-chat-resident"
_CONSUMER_RECENT_SEC = int(os.environ.get("FEEDLING_CONSUMER_RECENT_SEC", "180"))


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


# ---------------------------------------------------------------------------
# Access modes: one user/principal, multiple API keys and entry points
# ---------------------------------------------------------------------------


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


def _principal_id_for_user(user_id: str) -> str:
    snapshot = _user_entry_snapshot(user_id) or {}
    return str(snapshot.get("principal_id") or "")


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


def _access_modes_payload(store: UserStore) -> dict:
    active_route = _load_onboarding_route(store)
    with _users_lock:
        user_entry = _find_user_entry_locked(store.user_id)
        if not user_entry:
            return {"error": "user not found"}
        # Treat the selected onboarding route as a connected access mode, but
        # do not move any content: all Memory/Chat/Identity files remain under
        # the same user_id.
        #
        # whoami hits this on every request, so persistence here must be cheap
        # and rare. It used to call `_save_users()` — a full `DELETE FROM users`
        # + re-INSERT of every row — under the global _users_lock on EVERY
        # whoami. That made the hottest endpoint a serialized full-table
        # rewrite; raising gunicorn --threads only widened the lock convoy
        # (prod whoami p50 ~100s, max ~247s). Now we persist ONLY when the
        # binding is genuinely new or flips to connected, and only the single
        # affected user row (db.upsert_user) — steady-state re-polls touch no DB.
        mode = _normalize_access_mode(active_route)
        prior = next(
            (b for b in user_entry.get("access_bindings") or []
             if isinstance(b, dict)
             and _normalize_access_mode(str(b.get("access_mode") or "")) == mode),
            None,
        )
        was_connected = bool(prior) and str(prior.get("status") or "") == "connected"
        if was_connected:
            _upsert_access_binding_locked(user_entry, active_route)
        else:
            # First connect / status flip: persist the single affected row.
            # Snapshot the bindings first and roll back if the write fails, so a
            # transient DB blip doesn't leave the binding marked "connected" in
            # memory but unpersisted — otherwise the next whoami sees
            # was_connected and skips the write forever, losing it on restart.
            # We swallow (don't 500) to match the old _save_users behavior, but
            # the rollback makes the next whoami retry instead of giving up.
            binding_snapshot = copy.deepcopy(user_entry.get("access_bindings"))
            _upsert_access_binding_locked(user_entry, active_route)
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
            "access_modes": _public_access_mode_state(user_entry, active_route),
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


@app.route("/v1/access/modes", methods=["GET"])
def access_modes_get():
    store = require_user()
    return jsonify(_access_modes_payload(store))


@app.route("/v1/access/modes/switch", methods=["POST"])
def access_modes_switch():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    mode = _normalize_access_mode(str(payload.get("access_mode") or payload.get("route") or ""))
    if mode not in ACCESS_MODES:
        return jsonify({"error": "access_mode must be resident, model_api, or official_import"}), 400
    data = _save_onboarding_route(store, mode)
    with _users_lock:
        user_entry = _find_user_entry_locked(store.user_id)
        if user_entry:
            _upsert_access_binding_locked(user_entry, mode, touch_seen=True)
            _save_users()
    print(f"[access:{store.user_id}] active_route={data['route']}")
    return jsonify(_access_modes_payload(store))


@app.route("/v1/access/link-token", methods=["POST"])
def access_link_token_create():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    mode = _normalize_access_mode(str(payload.get("access_mode") or payload.get("route") or _load_onboarding_route(store)))
    if mode not in ACCESS_MODES:
        return jsonify({"error": "access_mode must be resident, model_api, or official_import"}), 400
    label = str(payload.get("label") or ACCESS_MODE_LABELS.get(mode, mode)).strip()[:80]
    raw_token = "flt_" + secrets.token_urlsafe(32)
    token_hash = _hash_api_key(raw_token)
    now_epoch = time.time()
    expires_at_epoch = now_epoch + ACCESS_LINK_TOKEN_TTL_SEC
    with _users_lock:
        user_entry = _find_user_entry_locked(store.user_id)
        if not user_entry:
            return jsonify({"error": "user not found"}), 404
        principal_id = user_entry.get("principal_id", "")
        existing_status = ""
        for binding in user_entry.get("access_bindings") or []:
            if isinstance(binding, dict) and _normalize_access_mode(str(binding.get("access_mode") or "")) == mode:
                existing_status = str(binding.get("status") or "")
                break
        _upsert_access_binding_locked(user_entry, mode, status=existing_status or "pending")
        _save_users()
    entry = {
        "token_id": f"flt_{secrets.token_hex(6)}",
        "token_hash": token_hash,
        "user_id": store.user_id,
        "principal_id": principal_id,
        "access_mode": mode,
        "label": label,
        "created_at": datetime.now().isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at_epoch).isoformat(),
        "expires_at_epoch": expires_at_epoch,
        "used_at": "",
    }
    with _access_link_tokens_lock:
        rows = _trim_access_link_tokens(_load_access_link_tokens())
        rows.append(entry)
        _save_access_link_tokens(rows)
    return jsonify({
        "token": raw_token,
        "token_id": entry["token_id"],
        "access_mode": mode,
        "route": mode,
        "label": label,
        "expires_at": entry["expires_at"],
        "expires_in_seconds": ACCESS_LINK_TOKEN_TTL_SEC,
        "claim_endpoint": "/v1/access/claim-token",
    }), 201


@app.route("/v1/access/claim-token", methods=["POST"])
def access_link_token_claim():
    payload = request.get_json(silent=True) or {}
    raw_token = str(payload.get("token") or "").strip()
    if not raw_token:
        return jsonify({"error": "token required"}), 400
    token_hash = _hash_api_key(raw_token)
    now_epoch = time.time()
    client_label = str(payload.get("label") or payload.get("client_label") or "").strip()[:80]
    public_key = str(payload.get("public_key") or "").strip()
    archive_language = str(payload.get("archive_language") or "").strip()
    make_active = bool(payload.get("make_active", True))
    with _access_link_tokens_lock:
        rows = _load_access_link_tokens()
        match = None
        for row in rows:
            if row.get("token_hash") == token_hash:
                match = row
                break
        if not match:
            return jsonify({"error": "invalid_token"}), 404
        if match.get("used_at"):
            return jsonify({"error": "token_already_used"}), 409
        try:
            expires_at_epoch = float(match.get("expires_at_epoch") or 0)
        except Exception:
            expires_at_epoch = 0
        if expires_at_epoch and expires_at_epoch < now_epoch:
            return jsonify({"error": "token_expired"}), 410
        user_id = str(match.get("user_id") or "")
        mode = _normalize_access_mode(str(match.get("access_mode") or ""))
        if mode not in ACCESS_MODES:
            return jsonify({"error": "token_access_mode_invalid"}), 400
        with _users_lock:
            user_entry = _find_user_entry_locked(user_id)
            if not user_entry:
                return jsonify({"error": "user not found"}), 404
            if public_key and not str(user_entry.get("public_key") or "").strip():
                _, err = _decode_content_public_key(public_key)
                if err:
                    return jsonify({"error": err}), 400
                user_entry["public_key"] = public_key
            if archive_language and not user_entry.get("archive_language"):
                user_entry["archive_language"] = archive_language
            issued = _issue_api_key_for_user_locked(
                user_entry,
                access_mode=mode,
                label=client_label or str(match.get("label") or ACCESS_MODE_LABELS.get(mode, mode)),
            )
            _save_users()
            principal_id = user_entry.get("principal_id", "")
        if make_active:
            _save_onboarding_route(get_store(user_id), mode)
        match["used_at"] = datetime.now().isoformat()
        match["claimed_label"] = client_label
        _save_access_link_tokens(_trim_access_link_tokens(rows))
    print(f"[access:{user_id}] claimed mode={mode} key={issued['key_entry']['key_id']}")
    return jsonify({
        "status": "connected",
        "user_id": user_id,
        "principal_id": principal_id,
        "api_key": issued["api_key"],
        "access_mode": mode,
        "route": mode,
        "active_route": _load_onboarding_route(get_store(user_id)),
        "key_id": issued["key_entry"]["key_id"],
    }), 201


# ---------------------------------------------------------------------------
# Keypair proof-of-possession account recovery
#
# A device that still holds the content X25519 keypair (it syncs via iCloud
# Keychain) but lost its device-local api_key must recover its EXISTING account
# rather than registering a new one — otherwise it orphans the account (the
# register-orphan bug). The device proves possession of the private key by
# decrypting a challenge sealed to the account's public_key; the server then
# issues a fresh api_key for that existing account. No new account is minted.
# ---------------------------------------------------------------------------

RECOVER_CHALLENGE_TTL_SEC = 300
_recover_challenges: dict[str, dict] = {}
_recover_challenges_lock = threading.Lock()


def _prune_recover_challenges_locked(now: float) -> None:
    expired = [cid for cid, c in _recover_challenges.items() if c.get("expires_at", 0) < now]
    for cid in expired:
        _recover_challenges.pop(cid, None)


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


# ---------------------------------------------------------------------------
# Users: register endpoint (public — no auth required)
# ---------------------------------------------------------------------------


@app.route("/v1/users/register", methods=["POST"])
def users_register():
    payload = request.get_json(silent=True) or {}
    public_key = (payload.get("public_key") or "").strip()
    archive_language = (payload.get("archive_language") or "").strip()
    access_mode = str(payload.get("access_mode") or payload.get("route") or "official_import")
    label = str(payload.get("label") or "").strip()
    # Server-side orphan backstop: never mint a second account for a content
    # public key that already has one. The device holds the matching private
    # key, so it must recover the existing account instead of registering. This
    # closes the orphan gap even when the client's recover-first guard is
    # bypassed (offline at first launch, iCloud Keychain sync lag, old app
    # version). The Reset-and-reimport flow wipes the keypair first, so it gets a
    # fresh public_key and is unaffected.
    if public_key and _canonical_account_for_pubkey(public_key) is not None:
        return jsonify({
            "error": "account_exists_for_key",
            "detail": "An account already exists for this content public key. "
                      "Recover it instead of registering a new one.",
            "recover_endpoint": "/v1/account/recover/challenge",
        }), 409
    result = _register_user(
        public_key=public_key or None,
        archive_language=archive_language or None,
        access_mode=access_mode,
        label=label or None,
    )
    return jsonify(result), 201


@app.route("/v1/account/recover/challenge", methods=["POST"])
def account_recover_challenge():
    """Step 1 of keypair recovery. Given a content public_key, seal a random
    challenge to it (local_only envelope — the device decrypts with the matching
    private key) so possession can be proven without an api_key. 404 when no
    account uses this key (caller should register a fresh account instead)."""
    payload = request.get_json(silent=True) or {}
    public_key = str(payload.get("public_key") or "").strip()
    pk_bytes, err = _decode_content_public_key(public_key)
    if err:
        return jsonify({"error": err}), 400
    account = _canonical_account_for_pubkey(public_key)
    if not account:
        return jsonify({"error": "no_recoverable_account"}), 404
    challenge = secrets.token_hex(32)
    challenge_id = "rec_" + secrets.token_hex(12)
    envelope = build_envelope(
        plaintext=challenge.encode("utf-8"),
        owner_user_id=account["user_id"],
        user_pk_bytes=pk_bytes,
        enclave_pk_bytes=None,
        visibility="local_only",
    )
    now = time.time()
    with _recover_challenges_lock:
        _prune_recover_challenges_locked(now)
        _recover_challenges[challenge_id] = {
            "public_key": public_key,
            "user_id": account["user_id"],
            "challenge": challenge,
            "expires_at": now + RECOVER_CHALLENGE_TTL_SEC,
        }
    print(f"[recover:challenge] user_id={account['user_id']} challenge_id={challenge_id}")
    return jsonify({"challenge_id": challenge_id, "envelope": envelope}), 200


@app.route("/v1/account/recover/verify", methods=["POST"])
def account_recover_verify():
    """Step 2 of keypair recovery. The device returns the decrypted challenge,
    proving it holds the private key. On a match, issue a fresh api_key for the
    EXISTING account (no new user). The challenge is single-use + short-lived."""
    payload = request.get_json(silent=True) or {}
    challenge_id = str(payload.get("challenge_id") or "").strip()
    answer = str(payload.get("answer") or "")
    now = time.time()
    with _recover_challenges_lock:
        _prune_recover_challenges_locked(now)
        entry = _recover_challenges.pop(challenge_id, None)  # one-time use
    if not entry or entry.get("expires_at", 0) < now:
        return jsonify({"error": "invalid_or_expired_challenge"}), 401
    if not hmac.compare_digest(answer, str(entry.get("challenge") or "")):
        return jsonify({"error": "challenge_failed"}), 401
    user_id = entry["user_id"]
    with _users_lock:
        user_entry = _find_user_entry_locked(user_id)
        if not user_entry:
            return jsonify({"error": "account_not_found"}), 404
        existing = [k for k in (user_entry.get("api_keys") or [])
                    if isinstance(k, dict) and not k.get("revoked_at")]
        mode = (existing[0].get("access_mode") if existing else "") or "official_import"
        if mode not in ACCESS_MODES:
            mode = "official_import"
        issued = _issue_api_key_for_user_locked(user_entry, access_mode=mode,
                                                label="Recovered (key)")
        _save_users()
        principal_id = user_entry.get("principal_id", "")
    print(f"[recover:verify] user_id={user_id} recovered via keypair PoP")
    return jsonify({
        "user_id": user_id,
        "principal_id": principal_id,
        "api_key": issued["api_key"],
        "public_key": entry["public_key"],
    }), 200


@app.route("/v1/users/whoami", methods=["GET"])
def users_whoami():
    """Identify the caller and return the public material needed to wrap
    content for them.

    Returns:
      - `public_key` — the caller's own X25519 content pubkey (base64),
        from the user record.
      - `enclave_content_public_key_hex` — the live enclave's content
        pubkey, fetched from /attestation and cached for 60s. Missing
        when no enclave is reachable.
      - `archive_language` — the locale code the iOS app supplied at
        registration (e.g. "en", "zh-Hans"). Null for legacy accounts;
        callers fall back to inferring from existing card content.
    """
    store = require_user()
    access = _access_modes_payload(store)
    resp: dict = {
        "user_id": store.user_id,
        "principal_id": access.get("principal_id", ""),
        "active_route": access.get("active_route", ""),
        "access_modes": access.get("access_modes", []),
    }
    pk = _get_user_public_key(store.user_id)
    if pk:
        resp["public_key"] = pk
    info = _get_enclave_info()
    if info:
        resp["enclave_content_public_key_hex"] = info["content_pk_hex"]
        resp["enclave_compose_hash"] = info["compose_hash"]
    archive_language = _get_user_archive_language(store.user_id)
    if archive_language:
        resp["archive_language"] = archive_language
    return jsonify(resp)


@app.route("/v1/users/preferences", methods=["POST"])
def users_set_preferences():
    """Update mutable preferences on the authenticated user's record.

    Currently the only supported preference is `archive_language` — the
    locale code that the agent should use as the source of truth for
    Memory Garden / Identity Card language. iOS posts this on first
    launch for legacy accounts that registered before the field existed,
    and again whenever the user explicitly changes their iOS system
    language and re-launches the app.

    Body: {"archive_language": "<bcp-47 string>" | null}
    Pass null to clear (agent falls back to inferred behavior).
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    if "archive_language" not in payload:
        return jsonify({
            "error": "archive_language required (string or null)",
        }), 400
    raw = payload.get("archive_language")
    if raw is not None and not isinstance(raw, str):
        return jsonify({"error": "archive_language must be a string or null"}), 400
    new_value = (raw or "").strip() if isinstance(raw, str) else ""

    updated = False
    with _users_lock:
        for u in _users:
            if u.get("user_id") == store.user_id:
                if new_value:
                    u["archive_language"] = new_value
                else:
                    u.pop("archive_language", None)
                updated = True
                break
        if updated:
            db.upsert_user(u)

    if not updated:
        return jsonify({"error": "user not found"}), 404
    print(f"[users] {store.user_id} archive_language → {new_value or 'cleared'}")
    return jsonify({
        "status": "updated",
        "archive_language": new_value or None,
    })


@app.route("/v1/users/public-key", methods=["POST"])
def users_set_public_key():
    """Backfill the authenticated user's content public key.

    This route is intentionally conservative. Once encrypted content exists,
    public_key rotation must go through /v1/content/rewrap-to-current-key so
    stored envelopes are rewrapped before future writes target the new key.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    public_key = (payload.get("public_key") or "").strip()
    if not public_key:
        return jsonify({"error": "public_key required"}), 400
    _, err = _decode_content_public_key(public_key)
    if err:
        return jsonify({"error": err}), 400

    existing = _get_user_public_key(store.user_id)
    if existing == public_key:
        return jsonify({
            "ok": True,
            "status": "unchanged",
            "user_id": store.user_id,
            "public_key_fpr": _content_public_key_fingerprint(public_key),
        })
    counts = _encrypted_content_counts(store)
    if existing and counts["total"] > 0:
        return jsonify({
            "error": "public_key_rotation_requires_rewrap",
            "message": "Existing encrypted content must be rewrapped before changing public_key.",
            "current_public_key_fpr": _content_public_key_fingerprint(existing),
            "requested_public_key_fpr": _content_public_key_fingerprint(public_key),
            "encrypted_content": counts,
            "recovery_endpoint": "/v1/content/rewrap-to-current-key",
        }), 409

    if not _set_user_public_key(store.user_id, public_key):
        return jsonify({"error": "user not found"}), 404

    print(f"[users] updated public_key for {store.user_id} fpr={_content_public_key_fingerprint(public_key)}")
    return jsonify({
        "ok": True,
        "status": "updated",
        "user_id": store.user_id,
        "public_key_fpr": _content_public_key_fingerprint(public_key),
        "encrypted_content": counts,
    })


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
            db.upsert_user(u)
    return updated


def _decode_content_public_key(public_key: str) -> tuple[bytes | None, str]:
    raw = (public_key or "").strip()
    if not raw:
        return None, "public_key required"
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        return None, "public_key invalid base64"
    if len(decoded) != 32:
        return None, "public_key must decode to 32 bytes"
    return decoded, ""


def _content_public_key_fingerprint(public_key: str | bytes | None) -> str:
    if public_key is None:
        return ""
    if isinstance(public_key, str):
        key_bytes, err = _decode_content_public_key(public_key)
        if err or key_bytes is None:
            return "invalid"
    else:
        key_bytes = public_key
    return hashlib.sha256(key_bytes).hexdigest()[:16]


def _has_encrypted_content_record(item: dict | None) -> bool:
    return bool(
        isinstance(item, dict)
        and item.get("body_ct")
        and item.get("nonce")
        and item.get("K_user")
    )


def _encrypted_content_counts(store: UserStore) -> dict:
    identity = _load_identity(store)
    moments = _load_moments(store)
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    counts = {
        "identity": 1 if _has_encrypted_content_record(identity) else 0,
        "memory": sum(1 for m in moments if _has_encrypted_content_record(m)),
        "chat": sum(1 for m in chat_msgs if _has_encrypted_content_record(m)),
    }
    counts["total"] = counts["identity"] + counts["memory"] + counts["chat"]
    return counts


# Cached enclave attestation (for wrapping envelopes we can't decrypt
# ourselves). Refetched every _ENCLAVE_INFO_TTL seconds — short enough
# that a rotated enclave is reflected within the window, long enough
# that writes don't pay a round-trip to the CVM per call.
_ENCLAVE_INFO_TTL = 60.0
_enclave_info_cache: dict = {"ts": 0.0, "data": None}
_enclave_info_lock = threading.Lock()


def _get_enclave_info() -> dict | None:
    """Fetch the enclave's (content_pk_hex, compose_hash) with a short
    cache. Returns None if no enclave is configured or reachable — the
    caller should surface the failure rather than proceed without the
    enclave's pubkey (v1 writes require it for shared visibility)."""
    url = os.environ.get("FEEDLING_ENCLAVE_URL", "").strip()
    if not url:
        return None
    now = time.time()
    with _enclave_info_lock:
        if _enclave_info_cache["data"] and now - _enclave_info_cache["ts"] < _ENCLAVE_INFO_TTL:
            return _enclave_info_cache["data"]
    try:
        # verify=False because the in-cluster enclave presents a
        # self-signed cert whose trust comes from REPORT_DATA, not a CA.
        # We're not pinning here; just fetching public material. Any
        # MITM between backend and enclave would at worst substitute a
        # different pubkey, which would then fail AEAD verification on
        # the enclave side when the agent tries to decrypt.
        with httpx.Client(timeout=5, verify=False) as client:
            r = client.get(f"{url.rstrip('/')}/attestation")
            r.raise_for_status()
            b = r.json()
        data = {
            "content_pk_hex": b.get("enclave_content_pk_hex", ""),
            "compose_hash": b.get("compose_hash", ""),
        }
        if not data["content_pk_hex"]:
            return None
        with _enclave_info_lock:
            _enclave_info_cache["ts"] = now
            _enclave_info_cache["data"] = data
        return data
    except Exception as e:
        print(f"[enclave-info] fetch failed from {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# IO-hosted Model API key route
# ---------------------------------------------------------------------------

MODEL_API_ROUTES = set(ACCESS_MODES)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _normalize_onboarding_route(route: str) -> str:
    return _normalize_access_mode(route)


def _load_onboarding_route(store: UserStore) -> str:
    data = db.get_blob(store.user_id, "onboarding_route") or {}
    route = _normalize_onboarding_route(str(data.get("route") or "resident"))
    return route if route in MODEL_API_ROUTES else "resident"


def _save_onboarding_route(store: UserStore, route: str) -> dict:
    normalized = _normalize_onboarding_route(route)
    if normalized not in MODEL_API_ROUTES:
        raise ValueError("route must be resident, official_import, or model_api")
    data = {"route": normalized, "selected_at": _now_iso()}
    db.set_blob(store.user_id, "onboarding_route", data)
    return data


@app.route("/v1/onboarding/route", methods=["GET", "POST"])
def onboarding_route():
    store = require_user()
    if request.method == "GET":
        return jsonify({
            "route": _load_onboarding_route(store),
            "allowed": sorted(MODEL_API_ROUTES),
        })
    payload = request.get_json(silent=True) or {}
    try:
        data = _save_onboarding_route(store, str(payload.get("route") or ""))
    except ValueError as e:
        return jsonify({"error": str(e), "allowed": sorted(MODEL_API_ROUTES)}), 400
    with _users_lock:
        user_entry = _find_user_entry_locked(store.user_id)
        if user_entry:
            _upsert_access_binding_locked(user_entry, data["route"], touch_seen=True)
            _save_users()
    print(f"[onboarding:{store.user_id}] route={data['route']}")
    return jsonify(data)


def _load_model_api_config(store: UserStore) -> dict | None:
    data = db.get_blob(store.user_id, "model_api")
    if not data:
        return None
    if data.get("route") != "model_api":
        data["route"] = "model_api"
    return data


def _save_model_api_config(store: UserStore, config: dict) -> dict:
    data = dict(config)
    data["route"] = "model_api"
    data["updated_at"] = _now_iso()
    if not data.get("created_at"):
        data["created_at"] = data["updated_at"]
    db.set_blob(store.user_id, "model_api", data)
    return data


def _public_model_api_config(config: dict | None) -> dict:
    if not config:
        return {"configured": False}
    safe = public_provider_config(config)
    safe["configured"] = True
    safe["privacy_mode"] = "tdx_cvm_backend_runtime_option_a"
    return safe


MODEL_API_RUNTIME_BLOB = "model_api_runtime"
MODEL_API_RUNTIME_VERSION = 2
MODEL_API_RUNTIME_MODE = "hosted_resident"
MODEL_API_ACTION_TRACE_STREAM = "model_api_action_traces"


def _load_model_api_runtime_profile(store: UserStore) -> dict | None:
    data = db.get_blob(store.user_id, MODEL_API_RUNTIME_BLOB)
    return data if isinstance(data, dict) else None


def _save_model_api_runtime_profile(store: UserStore, profile: dict) -> dict:
    data = dict(profile)
    data["runtime_mode"] = MODEL_API_RUNTIME_MODE
    data["runtime_version"] = MODEL_API_RUNTIME_VERSION
    data["updated_at"] = _now_iso()
    if not data.get("created_at"):
        data["created_at"] = data["updated_at"]
    db.set_blob(store.user_id, MODEL_API_RUNTIME_BLOB, data)
    return data


def _ensure_model_api_runtime_profile(
    store: UserStore,
    config: dict | None = None,
    *,
    touch: bool = False,
) -> dict | None:
    """Lazily migrate model_api users into the hosted resident runtime.

    This is intentionally metadata-only: provider key envelopes, chat,
    identity, and memory cards remain untouched.
    """
    config = config if isinstance(config, dict) else _load_model_api_config(store)
    if not config:
        return None
    existing = _load_model_api_runtime_profile(store) or {}
    profile = dict(existing)
    changed = touch

    defaults = {
        "runtime_mode": MODEL_API_RUNTIME_MODE,
        "runtime_version": MODEL_API_RUNTIME_VERSION,
        "tool_action_enabled": True,
        "recap_cursor": None,
        "last_recap_at": None,
        "last_action_trace_id": None,
        "memory_quality_warning": None,
        "provider": str(config.get("provider") or ""),
        "model": str(config.get("model") or ""),
    }
    for key, value in defaults.items():
        if profile.get(key) != value and (
            key in {"runtime_mode", "runtime_version", "tool_action_enabled"}
            or key not in profile
            or key in {"provider", "model"}
        ):
            profile[key] = value
            changed = True
    if changed or not existing:
        profile = _save_model_api_runtime_profile(store, profile)
    return profile


def _patch_model_api_runtime_profile(store: UserStore, patch: dict) -> dict | None:
    profile = _ensure_model_api_runtime_profile(store) or {}
    if not profile:
        return None
    merged = dict(profile)
    merged.update({k: v for k, v in patch.items() if v is not None})
    return _save_model_api_runtime_profile(store, merged)


def _append_model_api_action_trace(store: UserStore, entry: dict) -> dict:
    record = {
        "trace_id": entry.get("trace_id") or f"mat_{uuid.uuid4().hex[:16]}",
        "ts": time.time(),
        "created_at": _now_iso(),
        "runtime_mode": MODEL_API_RUNTIME_MODE,
        "runtime_version": MODEL_API_RUNTIME_VERSION,
        "status": str(entry.get("status") or "ok")[:80],
    }
    for key in (
        "provider", "model", "user_message_id", "assistant_message_id",
        "state_receipt_id", "background_execution", "runtime", "effects", "identity_actions",
        "memory_actions", "capture", "context", "error", "duration_ms",
        "usage", "reason", "progress",
    ):
        if key in entry:
            record[key] = entry[key]
    db.log_append(
        store.user_id,
        MODEL_API_ACTION_TRACE_STREAM,
        record,
        ts=record["ts"],
        item_key=record["trace_id"],
    )
    patch = {
        "last_action_trace_id": record["trace_id"],
        "last_action_trace_at": record["created_at"],
    }
    if record["status"] == "ok":
        patch["last_runtime_error"] = ""
    elif record.get("error"):
        patch["last_runtime_error"] = str(record.get("error"))[:300]
    _patch_model_api_runtime_profile(store, patch)
    return record


def _patch_model_api_action_trace(store: UserStore, trace_id: str, patch: dict) -> dict | None:
    merged = dict(patch)
    if patch.get("status") in {"completed", "failed", "skipped"}:
        merged.setdefault("completed_at", _now_iso())
    record = db.log_patch_item(store.user_id, MODEL_API_ACTION_TRACE_STREAM, trace_id, merged)
    profile_patch: dict = {
        "last_action_trace_id": trace_id,
        "last_action_trace_at": _now_iso(),
    }
    if patch.get("status") in {"completed", "skipped", "ok"}:
        profile_patch["last_runtime_error"] = ""
    elif patch.get("error"):
        profile_patch["last_runtime_error"] = str(patch.get("error"))[:300]
    _patch_model_api_runtime_profile(store, profile_patch)
    return record


def _latest_model_api_action_trace(store: UserStore) -> dict | None:
    traces = db.log_read(store.user_id, MODEL_API_ACTION_TRACE_STREAM, limit=1)
    return traces[-1] if traces else None


def _model_api_key_encryption_material(store: UserStore) -> tuple[bytes, bytes] | tuple[None, str]:
    user_pk_b64 = _get_user_public_key(store.user_id)
    if not user_pk_b64:
        return None, "user_content_public_key_missing"
    try:
        user_pk = base64.b64decode(user_pk_b64)
    except Exception:
        return None, "user_content_public_key_invalid_base64"
    if len(user_pk) != 32:
        return None, "user_content_public_key_invalid_length"

    enclave_info = _get_enclave_info()
    if not enclave_info:
        return None, "enclave_info_unavailable"
    try:
        enclave_pk = bytes.fromhex(str(enclave_info.get("content_pk_hex") or ""))
    except Exception:
        return None, "enclave_content_public_key_invalid_hex"
    if len(enclave_pk) != 32:
        return None, "enclave_content_public_key_invalid_length"
    return user_pk, enclave_pk


def _build_shared_envelope_for_store(
    store: UserStore,
    plaintext: bytes,
    *,
    item_id: str | None = None,
) -> tuple[dict | None, str]:
    material = _model_api_key_encryption_material(store)
    if material[0] is None:
        return None, str(material[1])
    user_pk, enclave_pk = material  # type: ignore[misc]
    try:
        return build_envelope(
            plaintext=plaintext,
            owner_user_id=store.user_id,
            user_pk_bytes=user_pk,  # type: ignore[arg-type]
            enclave_pk_bytes=enclave_pk,  # type: ignore[arg-type]
            visibility="shared",
            item_id=item_id,
        ), ""
    except Exception as e:
        return None, f"envelope_build_failed:{type(e).__name__}:{str(e)[:160]}"


def _chat_thinking_extra_from_envelope(envelope: dict | None) -> dict:
    if not isinstance(envelope, dict):
        return {}
    out = {
        "thinking_v": str(envelope.get("v", 1)),
        "thinking_id": str(envelope.get("id") or ""),
        "thinking_body_ct": str(envelope.get("body_ct") or ""),
        "thinking_nonce": str(envelope.get("nonce") or ""),
        "thinking_K_user": str(envelope.get("K_user") or ""),
        "thinking_visibility": str(envelope.get("visibility") or "shared"),
        "thinking_owner_user_id": str(envelope.get("owner_user_id") or ""),
        "thinking_enclave_pk_fpr": str(envelope.get("enclave_pk_fpr") or ""),
    }
    if envelope.get("K_enclave"):
        out["thinking_K_enclave"] = str(envelope.get("K_enclave") or "")
    return {k: v for k, v in out.items() if str(v).strip()}


_CHAT_THINKING_KINDS = {
    "provider_reasoning",
    "provider_reasoning_summary",
    "runtime_trace",
    "agent_summary",
    "context_summary",
}


def _bounded_chat_metadata(value: object, *, max_len: int = 96) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[\r\n\t]+", " ", text)[:max_len].strip()


def _boolish_chat_metadata(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _chat_thinking_metadata_from_payload(payload: dict) -> dict:
    """Metadata for a separately encrypted reasoning or trace envelope.

    IO stores and renders this metadata; it does not infer or manufacture
    reasoning. Upstream runtimes must label whether they are sending
    provider-native reasoning, runtime trace, or agent-authored summary.
    """
    if not isinstance(payload, dict):
        return {}
    raw_kind = _bounded_chat_metadata(
        payload.get("thinking_kind") or payload.get("reasoning_kind"),
        max_len=64,
    ).lower()
    out: dict = {}
    if raw_kind in _CHAT_THINKING_KINDS:
        out["thinking_kind"] = raw_kind
    source = _bounded_chat_metadata(
        payload.get("thinking_source") or payload.get("reasoning_source"),
        max_len=80,
    )
    if source:
        out["thinking_source"] = source
    model = _bounded_chat_metadata(
        payload.get("thinking_model") or payload.get("reasoning_model"),
        max_len=96,
    )
    if model:
        out["thinking_model"] = model
    native = _boolish_chat_metadata(
        payload.get("thinking_native", payload.get("reasoning_native"))
    )
    if native is not None:
        out["thinking_native"] = native
    return out


_CHAT_PLAINTEXT_THINKING_FIELDS = (
    ("provider_reasoning", "provider_reasoning"),
    ("reasoning_text", "provider_reasoning_summary"),
    ("reasoning", "provider_reasoning_summary"),
    ("reasoning_summary", "provider_reasoning_summary"),
    ("visible_reasoning", "provider_reasoning_summary"),
    ("thought_summary", "provider_reasoning_summary"),
    ("runtime_trace", "runtime_trace"),
    ("thinking_summary", "agent_summary"),
    ("thinking", "provider_reasoning_summary"),
)


def _chat_plaintext_thinking_from_payload(payload: dict) -> tuple[str, dict, str]:
    """Compatibility bridge for callers that post reasoning as plaintext.

    The preferred /v1/chat/response contract is a separately encrypted
    `thinking_envelope`. Some resident consumers already have provider-native
    reasoning as plaintext and post it as `reasoning_text`; accept that shape by
    sealing it server-side so iOS still sees the canonical thinking_* metadata.
    """
    if not isinstance(payload, dict):
        return "", {}, ""
    for field, default_kind in _CHAT_PLAINTEXT_THINKING_FIELDS:
        raw = payload.get(field)
        if raw is None:
            continue
        text = str(raw or "").strip()
        if not text:
            continue
        if field == "thinking_summary":
            text = _sanitize_visible_thinking_summary(text)
        else:
            text = _sanitize_provider_reasoning_text(text)
        if not text:
            return "", {}, ""
        metadata = _chat_thinking_metadata_from_payload(payload)
        metadata.setdefault("thinking_kind", default_kind)
        metadata.setdefault("thinking_source", f"chat_response.{field}")
        if field == "provider_reasoning" and "thinking_native" not in metadata:
            metadata["thinking_native"] = True
        return text, metadata, field
    return "", {}, ""


def _chat_plaintext_thinking_extra_for_store(store: UserStore, payload: dict) -> dict:
    text, metadata, field = _chat_plaintext_thinking_from_payload(payload)
    if not text:
        return {}
    envelope, err = _build_shared_envelope_for_store(store, text.encode("utf-8"))
    if envelope is None:
        print(
            f"[chat:{store.user_id}] plaintext_thinking_envelope_failed "
            f"field={field} detail={err}"
        )
        return {
            "thinking_error": "plaintext_envelope_failed",
            "thinking_error_detail": str(err or "")[:160],
        }
    extra = _chat_thinking_extra_from_envelope(envelope)
    extra.update(metadata)
    return extra


def _decrypt_envelope_via_enclave(envelope: dict, api_key: str | None, *, purpose: str) -> bytes:
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        raise RuntimeError("enclave_unavailable")
    if not api_key:
        raise RuntimeError("api_key_unavailable")
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.post(
                f"{enclave_url}/v1/envelope/decrypt",
                headers={"X-API-Key": api_key},
                json={"envelope": envelope, "purpose": purpose},
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"enclave_error:{type(e).__name__}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")
    body = resp.json()
    if not isinstance(body, dict) or not isinstance(body.get("plaintext_b64"), str):
        raise RuntimeError("enclave_invalid_decrypt_response")
    try:
        return base64.b64decode(body["plaintext_b64"])
    except Exception as e:
        raise RuntimeError(f"enclave_plaintext_decode:{type(e).__name__}") from e


def _provider_config_from_plain(config: dict, api_key: str) -> ProviderConfig:
    provider, model, base_url = validate_provider_config(
        str(config.get("provider") or ""),
        str(config.get("model") or ""),
        str(config.get("base_url") or ""),
    )
    return ProviderConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)


def _load_runtime_provider_config(store: UserStore, api_key: str | None) -> ProviderConfig | tuple[None, dict]:
    config = _load_model_api_config(store)
    if not config:
        return None, {"error": "model_api_not_configured"}
    if config.get("test_status") != "ok":
        return None, {"error": "model_api_not_tested", "test_status": config.get("test_status", "")}
    envelope = config.get("api_key_envelope")
    if not isinstance(envelope, dict):
        return None, {"error": "model_api_key_envelope_missing"}
    try:
        provider_key = _decrypt_envelope_via_enclave(
            envelope,
            api_key,
            purpose="model_api_provider_key",
        ).decode("utf-8")
    except Exception as e:
        return None, {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}
    try:
        return _provider_config_from_plain(config, provider_key)
    except ProviderError as e:
        return None, {"error": "model_api_config_invalid", "detail": str(e)}


@app.route("/v1/model_api/setup", methods=["POST"])
def model_api_setup():
    store = require_user()
    caller_api_key = _extract_api_key()
    payload = request.get_json(silent=True) or {}
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    base_url = str(payload.get("base_url") or "")
    raw_key = str(payload.get("api_key") or "").strip()
    try:
        provider, model, base_url = validate_provider_config(provider, model, base_url)
    except ProviderError as e:
        return jsonify({"error": str(e)}), 400

    existing = _load_model_api_config(store) or {}
    existing_envelope = existing.get("api_key_envelope")
    if raw_key:
        provider_key = raw_key
        envelope, err = _build_shared_envelope_for_store(
            store,
            raw_key.encode("utf-8"),
            item_id=f"model_api_key_{uuid.uuid4().hex}",
        )
        if envelope is None:
            return jsonify({
                "error": "cannot_encrypt_provider_key",
                "detail": err,
                "required": (
                    "The user must have a content public key and the enclave "
                    "attestation endpoint must be reachable before saving a provider key."
                ),
            }), 409
        api_key_hint = mask_api_key(raw_key)
    else:
        if not isinstance(existing_envelope, dict):
            return jsonify({"error": "api_key required"}), 400
        try:
            provider_key = _decrypt_envelope_via_enclave(
                existing_envelope,
                caller_api_key,
                purpose="model_api_provider_key",
            ).decode("utf-8")
        except Exception as e:
            return jsonify({
                "error": "model_api_key_decrypt_failed",
                "detail": str(e)[:220],
            }), 400
        envelope = existing_envelope
        api_key_hint = str(existing.get("api_key_hint") or "saved key")

    try:
        test = test_provider_key(ProviderConfig(provider, model, provider_key, base_url))
    except ProviderError as e:
        # Log enough to triage a user-reported "key won't validate" without ever
        # logging the raw key: the failure detail (e.g. provider_http_404 for a
        # bad model name, or 401/429 for a bad/quota'd key) only lives here.
        print(
            f"[model_api:{store.user_id}] setup FAILED provider={provider} "
            f"model={model} status_code={e.status_code} detail={str(e)[:160]}"
        )
        return jsonify({
            "error": "provider_test_failed",
            "detail": str(e),
            "status_code": e.status_code,
        }), 400

    config = _save_model_api_config(store, {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_hint": api_key_hint,
        "api_key_envelope": envelope,
        "test_status": "ok",
        "last_test_at": _now_iso(),
        "last_test_usage": test.get("usage") or {},
        "privacy_mode": "tdx_cvm_backend_runtime_option_a",
    })
    _ensure_model_api_runtime_profile(store, config, touch=True)
    _save_onboarding_route(store, "model_api")
    print(f"[model_api:{store.user_id}] setup provider={provider} model={model}")
    return jsonify({"status": "configured", "config": _public_model_api_config(config)})


@app.route("/v1/model_api/get", methods=["GET"])
def model_api_get():
    store = require_user()
    return jsonify({"config": _public_model_api_config(_load_model_api_config(store))})


@app.route("/v1/model_api/test", methods=["POST"])
def model_api_test():
    store = require_user()
    api_key = _extract_api_key()
    config = _load_model_api_config(store)
    if not config:
        return jsonify({"error": "model_api_not_configured"}), 404
    runtime = _load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        config["test_status"] = "failed"
        config["last_test_error"] = err.get("error", "unknown")
        _save_model_api_config(store, config)
        return jsonify(err), 400
    try:
        test = test_provider_key(runtime)
    except ProviderError as e:
        config["test_status"] = "failed"
        config["last_test_error"] = str(e)[:240]
        _save_model_api_config(store, config)
        return jsonify({
            "error": "provider_test_failed",
            "detail": str(e),
            "status_code": e.status_code,
        }), 400
    config["test_status"] = "ok"
    config["last_test_at"] = _now_iso()
    config["last_test_error"] = ""
    config["last_test_usage"] = test.get("usage") or {}
    _save_model_api_config(store, config)
    _ensure_model_api_runtime_profile(store, config, touch=True)
    print(f"[model_api:{store.user_id}] test ok provider={config.get('provider')} model={config.get('model')}")
    return jsonify({"status": "ok", "config": _public_model_api_config(config)})


@app.route("/v1/model_api/delete", methods=["DELETE"])
def model_api_delete():
    store = require_user()
    deleted = db.delete_blob(store.user_id, "model_api")
    db.delete_blob(store.user_id, MODEL_API_RUNTIME_BLOB)
    print(f"[model_api:{store.user_id}] deleted={deleted}")
    return jsonify({"deleted": deleted})


def _model_api_recap_status(store: UserStore) -> dict:
    latest = _model_api_latest_recap_job(store)
    with _model_api_recap_active_lock:
        active = store.user_id in _model_api_recap_active_users
    if not latest:
        return {"status": "idle", "active": active}
    status = str(latest.get("status") or "idle")
    if active and status not in {"failed", "completed", "skipped"}:
        status = "running"
    return {
        "status": status,
        "active": active,
        "job_id": latest.get("job_id", ""),
        "mode": latest.get("mode", ""),
        "progress": latest.get("progress", 0),
        "updated_at": latest.get("completed_at") or latest.get("created_at") or "",
    }


@app.route("/v1/model_api/runtime", methods=["GET"])
def model_api_runtime_status():
    store = require_user()
    api_key = _extract_api_key()
    config = _load_model_api_config(store)
    if not config:
        return jsonify({
            "configured": False,
            "runtime_mode": MODEL_API_RUNTIME_MODE,
            "runtime_version": MODEL_API_RUNTIME_VERSION,
            "recap_status": "idle",
            "memory_quality_warning": None,
        })
    profile = _ensure_model_api_runtime_profile(store, config) or {}
    scan = _model_api_memory_quality_scan(store, api_key=api_key, max_cards=120, fast=True)
    warning = scan.get("warning")
    if warning != profile.get("memory_quality_warning"):
        profile = _patch_model_api_runtime_profile(store, {"memory_quality_warning": warning}) or profile
    latest_trace = _latest_model_api_action_trace(store)
    recap = _model_api_recap_status(store)
    return jsonify({
        "configured": True,
        "runtime_mode": profile.get("runtime_mode") or MODEL_API_RUNTIME_MODE,
        "runtime_version": int(profile.get("runtime_version") or MODEL_API_RUNTIME_VERSION),
        "tool_action_enabled": bool(profile.get("tool_action_enabled", True)),
        "provider": config.get("provider", ""),
        "model": config.get("model", ""),
        "recap_status": recap.get("status", "idle"),
        "recap": recap,
        "memory_quality_warning": warning,
        "memory_quality": {
            "scanned": scan.get("scanned", 0),
            "issue_count": scan.get("issue_count", 0),
            "noisy_count": scan.get("noisy_count", 0),
            "duplicate_count": scan.get("duplicate_count", 0),
        },
        "last_action_trace_id": profile.get("last_action_trace_id", ""),
        "last_action_trace_at": profile.get("last_action_trace_at", ""),
        "last_action_trace_status": (latest_trace or {}).get("status", ""),
        "last_runtime_error": profile.get("last_runtime_error", ""),
    })


@app.route("/v1/model_api/memory/repair", methods=["POST"])
def model_api_memory_repair():
    store = require_user()
    api_key = _extract_api_key()
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "dry_run").strip().lower()
    if mode not in {"dry_run", "apply"}:
        return jsonify({"error": "mode must be dry_run or apply"}), 400
    archive_old = bool(payload.get("archive_old", True))
    scan = _model_api_memory_quality_scan(store, api_key=api_key, max_cards=2000)
    noisy_count = int(scan.get("noisy_count") or 0)
    new_cards_planned = max(6, min(30, noisy_count)) if noisy_count else 0
    preview = {
        "old_cards_detected": noisy_count,
        "issue_count": int(scan.get("issue_count") or 0),
        "duplicate_count": int(scan.get("duplicate_count") or 0),
        "new_cards_planned": new_cards_planned,
        "noisy_ids": scan.get("noisy_ids", [])[:80],
        "issues": scan.get("issues", [])[:20],
    }
    _patch_model_api_runtime_profile(store, {
        "memory_quality_warning": scan.get("warning"),
        "last_memory_quality_scan_at": _now_iso(),
    })
    if mode == "dry_run":
        return jsonify({
            "status": "completed",
            "mode": "dry_run",
            "preview": preview,
            "memory_quality": scan,
        })
    if not preview["old_cards_detected"]:
        return jsonify({
            "status": "skipped",
            "mode": "apply",
            "reason": "no_noisy_memory_cards_detected",
            "preview": preview,
        })

    runtime = _load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        return jsonify(err), 400

    job = _append_memory_capture_job(store, {
        "mode": "repair",
        "status": "queued",
        "progress": 0,
        "old_cards_detected": preview["old_cards_detected"],
        "new_cards_planned": preview["new_cards_planned"],
        "repair_noisy_ids": preview["noisy_ids"],
        "archive_old": archive_old,
    })
    run_sync = bool(payload.get("synchronous") or payload.get("sync") or app.config.get("TESTING"))
    if run_sync:
        _run_model_api_memory_repair_job(
            store,
            api_key,
            runtime,
            job["job_id"],
            noisy_ids=preview["noisy_ids"],
            archive_old=archive_old,
        )
        jobs = db.log_read(store.user_id, "memory_capture_jobs", limit=20)
        latest = next((item for item in reversed(jobs) if item.get("job_id") == job["job_id"]), job)
        return jsonify({
            "status": latest.get("status", "completed"),
            "mode": "apply",
            "job_id": job["job_id"],
            "job": latest,
            "preview": preview,
        })

    thread = threading.Thread(
        target=_run_model_api_memory_repair_job,
        args=(store, api_key, runtime, job["job_id"]),
        kwargs={"noisy_ids": preview["noisy_ids"], "archive_old": archive_old},
        daemon=True,
    )
    thread.start()
    return jsonify({
        "status": "queued",
        "mode": "apply",
        "job_id": job["job_id"],
        "job": job,
        "preview": preview,
    }), 202


def _history_job_kind(job_id: str) -> str:
    """user_blobs kind for a single history-import job. One blob per job_id."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", job_id or "")
    return f"history_import_job:{safe}"


HISTORY_IMPORT_STALE_SEC = int(os.environ.get("FEEDLING_HISTORY_IMPORT_STALE_SEC", str(30 * 60)))
_HISTORY_IMPORT_PHASES = {
    "upload_received": (5, "Upload received"),
    "parsing_materials": (15, "Reading materials"),
    "chat_history_importing": (24, "Reading chat history"),
    "candidate_extracting": (38, "Distilling memory candidates"),
    "candidate_merging": (52, "Merging memory candidates"),
    "memory_writing": (64, "Writing core Memory Garden"),
    "identity_deriving": (76, "Deriving Identity Card"),
    "relationship_anchor_writing": (86, "Writing relationship anchor"),
    "hosted_chat_preparing": (92, "Preparing hosted chat"),
    "background_importing": (96, "Continuing history distillation"),
    "completed": (100, "Completed"),
    "failed": (100, "Failed"),
}
_history_import_active_jobs: set[str] = set()
_history_import_active_lock = threading.Lock()


def _save_history_job(store: UserStore, job: dict) -> dict:
    job["updated_at"] = _now_iso()
    db.set_blob(store.user_id, _history_job_kind(job["job_id"]), job)
    return job


def _history_import_phase_fields(phase: str) -> dict:
    progress, label = _HISTORY_IMPORT_PHASES.get(phase, (0, phase or ""))
    return {
        "phase": phase,
        "phase_label": label,
        "progress": progress,
    }


def _update_history_job_phase(
    store: UserStore,
    job: dict,
    phase: str,
    *,
    status: str = "processing",
    **fields,
) -> dict:
    job.update(_history_import_phase_fields(phase))
    job["status"] = status
    job.update(fields)
    return _save_history_job(store, job)


def _history_import_payload_hash(payload: dict) -> str:
    relevant = {
        key: payload.get(key)
        for key in (
            "format",
            "content",
            "fresh_start",
            "relationship_started_at",
            "ai_persona_content",
            "ai_persona_filename",
            "character_content",
            "character_card",
            "character_filename",
            "character_card_filename",
            "persona_content",
            "persona",
            "personal_profile_content",
            "personal_profile_filename",
            "profile_content",
            "persona_filename",
            "memory_summary_content",
            "memory_summary",
            "memory_sample_content",
            "memory_summary_filename",
            "memory_sample_filename",
            "history_filename",
        )
    }
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _history_import_client_job_id(payload: dict) -> str:
    raw = str(payload.get("client_job_id") or "").strip()
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)[:96]


def _load_history_import_jobs(store: UserStore) -> list[dict]:
    jobs = db.list_blobs(store.user_id, "history_import_job:")
    jobs.sort(key=lambda j: str(j.get("updated_at") or j.get("created_at") or ""))
    return jobs


def _history_import_age_sec(job: dict) -> float:
    raw = str(job.get("updated_at") or job.get("created_at") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, time.time() - datetime.fromisoformat(raw).timestamp())
    except Exception:
        return 0.0


def _history_import_find_reusable_job(
    store: UserStore,
    *,
    client_job_id: str,
    input_hash: str,
) -> dict | None:
    for job in reversed(_load_history_import_jobs(store)):
        status = str(job.get("status") or "")
        if status == "failed":
            continue
        matches_client = client_job_id and str(job.get("client_job_id") or "") == client_job_id
        matches_hash = input_hash and str(job.get("input_hash") or "") == input_hash
        if not (matches_client or matches_hash):
            continue
        if status in {"queued", "processing"} and _history_import_age_sec(job) > HISTORY_IMPORT_STALE_SEC:
            job.update({
                "status": "failed",
                "failed_at": _now_iso(),
                "error": "RuntimeError:stale_history_import_job",
            })
            _update_history_job_phase(store, job, "failed", status="failed")
            continue
        return job
    return None


_HISTORY_LINE_RE = re.compile(
    r"^\s*(?:\[(?P<bracket_ts>[^\]]{6,80})\]\s*)?"
    r"(?:(?P<iso_ts>\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)\s+)?"
    r"(?:(?P<role>[A-Za-z\u4e00-\u9fff _-]{1,32})\s*[:：]\s*)?"
    r"(?P<text>.+?)\s*$"
)


def _parse_history_ts(raw: str) -> float | None:
    val = (raw or "").strip()
    if not val:
        return None
    norm = val.replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", norm):
        norm += "T00:00:00"
    if " " in norm and "T" not in norm:
        norm = norm.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return dt.timestamp()
    except Exception:
        return None


def _normalize_history_role(raw: str) -> str:
    role = (raw or "").strip().lower()
    if role in {"user", "human", "me", "you", "我", "用户"}:
        return "user"
    if role in {"assistant", "ai", "agent", "claude", "chatgpt", "gemini", "gpt", "io", "ta", "他", "助手"}:
        return "assistant"
    return "user"


def _normalize_json_history_role(raw: str) -> str:
    role = (raw or "").strip().lower()
    if not role:
        return "user"
    if role in {"user", "human", "me", "you", "我", "用户"}:
        return "user"
    if role in {"assistant", "ai", "agent", "model", "claude", "chatgpt", "gemini", "gpt", "io", "ta", "他", "助手"}:
        return "assistant"
    if role in {"system", "developer", "tool", "function", "browser", "插件"}:
        return ""
    return ""


def _parse_plaintext_history(content: str) -> list[dict]:
    lines = (content or "").splitlines()
    messages: list[dict] = []
    cur: dict | None = None

    def flush() -> None:
        nonlocal cur
        if cur and str(cur.get("content") or "").strip():
            cur["content"] = str(cur["content"]).strip()
            messages.append(cur)
        cur = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush()
            continue
        m = _HISTORY_LINE_RE.match(line)
        if not m:
            if cur:
                cur["content"] += "\n" + line
            continue
        role_raw = m.group("role") or ""
        ts_raw = m.group("iso_ts") or m.group("bracket_ts") or ""
        text = (m.group("text") or "").strip()
        has_role = bool(role_raw.strip())
        has_ts = bool(ts_raw.strip() and _parse_history_ts(ts_raw) is not None)
        if has_role or has_ts or cur is None:
            flush()
            cur = {
                "role": _normalize_history_role(role_raw),
                "content": text,
                "ts": _parse_history_ts(ts_raw),
                "source": "history_import",
            }
        else:
            cur["content"] += "\n" + line
    flush()

    if not messages and content.strip():
        messages.append({
            "role": "user",
            "content": content.strip(),
            "ts": None,
            "source": "history_import",
        })
    return messages


def _extract_json_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_extract_json_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message", "body", "value"):
            if key in value:
                text = _extract_json_text(value.get(key))
                if text:
                    return text
        parts = value.get("parts")
        if isinstance(parts, list):
            return _extract_json_text(parts)
    return ""


def _extract_json_ts(item: dict) -> float | None:
    for key in ("create_time", "created_at", "timestamp", "time", "date"):
        raw = item.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        parsed = _parse_history_ts(str(raw))
        if parsed is not None:
            return parsed
    return None


def _role_from_json_item(item: dict) -> str:
    author = item.get("author")
    if isinstance(author, dict):
        role = author.get("role") or author.get("name")
        if role:
            return _normalize_json_history_role(str(role))
    role = item.get("role") or item.get("sender") or item.get("from") or item.get("speaker")
    if role:
        return _normalize_json_history_role(str(role))
    return "user"


def _dedupe_history_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str, float | None]] = set()
    for msg in messages:
        text = str(msg.get("content") or "").strip()
        role = str(msg.get("role") or "").strip()
        if role not in {"user", "assistant"} or not text:
            continue
        ts = msg.get("ts")
        try:
            ts_key = float(ts) if ts is not None else None
        except Exception:
            ts_key = None
        key = (role, re.sub(r"\s+", " ", text)[:800], ts_key)
        if key in seen:
            continue
        seen.add(key)
        clean = dict(msg)
        clean["content"] = text
        clean["role"] = role
        out.append(clean)
    return out


def _parse_json_history(content: str) -> list[dict]:
    raw = json.loads(content)
    messages: list[dict] = []
    seen: set[tuple[str, str, float | None]] = set()

    def maybe_add(item: dict) -> bool:
        candidate = item.get("message") if isinstance(item.get("message"), dict) else item
        if not isinstance(candidate, dict):
            return False
        text = _extract_json_text(candidate.get("content"))
        if not text:
            text = _extract_json_text(candidate.get("text"))
        if not text:
            return False
        role = _role_from_json_item(candidate)
        if role not in {"user", "assistant"}:
            return False
        ts = _extract_json_ts(candidate) or _extract_json_ts(item)
        key = (role, text[:500], ts)
        if key in seen:
            return True
        seen.add(key)
        messages.append({
            "role": role,
            "content": text,
            "ts": ts,
            "source": "history_import",
        })
        return True

    def walk(value) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if maybe_add(value):
            return
        mapping = value.get("mapping")
        if isinstance(mapping, dict):
            for node in mapping.values():
                walk(node)
        for key in ("messages", "chat_messages", "conversation", "conversations", "items"):
            if key in value:
                walk(value.get(key))

    walk(raw)
    messages.sort(key=lambda m: (m.get("ts") is None, float(m.get("ts") or 0)))
    return _dedupe_history_messages(messages)


_WRAPPED_CHAT_HISTORY_RE = re.compile(
    r"(?ms)^\s*={3,}\s*BEGIN\s+CHAT\s+HISTORY\s+FILE:\s*(?P<filename>.*?)\s*={3,}\s*"
    r"(?P<body>.*?)(?:^\s*={3,}\s*END\s+CHAT\s+HISTORY\s+FILE:.*?={3,}\s*|\Z)"
)


def _parse_wrapped_history_content(content: str, warnings: list[str]) -> tuple[bool, list[dict]]:
    blocks = list(_WRAPPED_CHAT_HISTORY_RE.finditer(content or ""))
    if not blocks:
        return False, []

    messages: list[dict] = []
    for block in blocks:
        filename = str(block.group("filename") or "").strip()
        body = str(block.group("body") or "").strip()
        if not body:
            warnings.append(f"empty_chat_history_file:{filename[:120]}")
            continue
        lower_name = filename.lower()
        looks_json = lower_name.endswith(".json") or body.lstrip().startswith(("{", "["))
        if looks_json:
            try:
                parsed = _parse_json_history(body)
            except Exception as e:
                warnings.append(f"wrapped_json_parse_failed:{filename[:120]}:{type(e).__name__}")
                continue
        else:
            parsed = _parse_plaintext_history(body)
        for msg in parsed:
            if filename:
                msg["source_filename"] = filename[:240]
        messages.extend(parsed)
    return True, _dedupe_history_messages(messages)


def _parse_import_history_content(content: str, fmt: str, warnings: list[str]) -> list[dict]:
    if not content.strip():
        return []
    normalized = (fmt or "plaintext").strip().lower()
    has_wrapped, wrapped_messages = _parse_wrapped_history_content(content, warnings)
    if has_wrapped:
        return wrapped_messages
    if normalized in {"json", "chatgpt_json", "claude_json"}:
        return _parse_json_history(content)
    if normalized in {"auto", "file"}:
        stripped = content.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return _parse_json_history(content)
            except Exception as e:
                warnings.append(f"json_parse_failed_falling_back_to_plaintext:{type(e).__name__}")
        return _parse_plaintext_history(content)
    if normalized in {"plaintext", "text"}:
        return _parse_plaintext_history(content)
    raise ValueError("format must be plaintext, text, json, or auto")


_AI_PERSONA_SOURCE = "ai_persona_import"
_USER_PROFILE_SOURCE = "user_profile_import"
_MEMORY_SUMMARY_SOURCE = "memory_summary_import"
_HISTORY_SOURCE = "history_import"
_FRESH_START_SOURCE = "fresh_start"

_IMPORT_SOURCE_FAMILY = {
    _AI_PERSONA_SOURCE: _AI_PERSONA_SOURCE,
    "agent_prompt_import": _AI_PERSONA_SOURCE,
    "character_import": _AI_PERSONA_SOURCE,
    _USER_PROFILE_SOURCE: _USER_PROFILE_SOURCE,
    "persona_import": _USER_PROFILE_SOURCE,
    _MEMORY_SUMMARY_SOURCE: _MEMORY_SUMMARY_SOURCE,
    _HISTORY_SOURCE: _HISTORY_SOURCE,
    _FRESH_START_SOURCE: _FRESH_START_SOURCE,
}


def _import_source_family(source: str | None) -> str:
    raw = str(source or "").strip()
    return _IMPORT_SOURCE_FAMILY.get(raw, raw or _HISTORY_SOURCE)


_SUPPORT_BLOCK_RE = re.compile(
    r"(?ms)^\s*={3,}\s*BEGIN\s+"
    r"(?P<label>AGENT\s+PROMPT|SYSTEM\s+PROMPT|ORIGINAL\s+SYSTEM\s+PROMPT|"
    r"AI\s+PERSONA(?:\s+MATERIALS?)?|CHARACTER\s+CARD|"
    r"PERSONAL\s+PROFILE(?:\s+CARD)?|PERSONA\s+PROFILE|USER\s+PROFILE|PERSONA|"
    r"MEMORY\s+SUMMARY|MEMORY\s+SAMPLE|MEMORY\s+SAMURAI)"
    r"(?:\s*:(?P<filename>[^=]*))?\s*={3,}\s*"
    r"(?P<body>.*?)(?=^\s*={3,}\s*BEGIN\s+"
    r"(?:AGENT\s+PROMPT|SYSTEM\s+PROMPT|ORIGINAL\s+SYSTEM\s+PROMPT|"
    r"AI\s+PERSONA(?:\s+MATERIALS?)?|CHARACTER\s+CARD|"
    r"PERSONAL\s+PROFILE(?:\s+CARD)?|PERSONA\s+PROFILE|USER\s+PROFILE|PERSONA|"
    r"MEMORY\s+SUMMARY|MEMORY\s+SAMPLE|MEMORY\s+SAMURAI)"
    r"(?:\s*:[^=]*)?\s*={3,}\s*|\Z)"
)
_SUPPORT_MARKER_RE = re.compile(r"^\s*={3,}\s*(?:BEGIN|END)\s+[^=]+={3,}\s*$", re.IGNORECASE)


def _clean_support_material_text(text: str) -> str:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if _SUPPORT_MARKER_RE.match(line):
            continue
        if re.match(r"^\s*={3,}\s*(?:BEGIN|END)\s+CHAT\s+HISTORY\s+FILE:", line, re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _support_message(
    label: str,
    source: str,
    content: str,
    filename: str = "",
    *,
    source_detail: str = "",
) -> dict | None:
    clean = _normalize_support_material_text(content, filename)
    if not clean:
        return None
    title = label
    if filename.strip():
        title += f" ({filename.strip()[:120]})"
    family = _import_source_family(source)
    cap = 120000 if family == _MEMORY_SUMMARY_SOURCE else 60000 if family in {_AI_PERSONA_SOURCE, _USER_PROFILE_SOURCE} else 30000
    return {
        "role": "user",
        "content": f"{title}:\n{clean[:cap]}",
        "ts": None,
        "source": family,
        "source_family": family,
        "source_detail": source_detail or source,
        "source_filename": filename.strip()[:240],
    }


_SUPPORT_JSON_TEXT_KEYS = (
    "conversations_memory",
    "conversation_memory",
    "personal_profile",
    "persona_profile",
    "user_profile",
    "character_card",
    "memory_summary",
    "memory_sample",
    "memory_samurai",
    "memory_summaries",
    "persona",
    "profile",
    "memory",
    "memories",
    "summary",
    "description",
    "notes",
    "content",
    "text",
    "body",
    "value",
)
_SUPPORT_JSON_PRIVATE_KEYS = {
    "uuid",
    "account_uuid",
    "user_id",
    "email",
    "email_address",
    "phone",
    "phone_number",
    "verified_phone_number",
    "id",
}


def _support_json_is_account_metadata(value: dict) -> bool:
    keys = {str(k).lower() for k in value.keys()}
    has_private = bool(keys & _SUPPORT_JSON_PRIVATE_KEYS) or any(
        "email" in key or "phone" in key or "uuid" in key for key in keys
    )
    has_content = any(key in keys for key in _SUPPORT_JSON_TEXT_KEYS)
    return has_private and not has_content


def _support_json_scalar_text(value) -> str:
    if not isinstance(value, str):
        return ""
    text = _clean_support_material_text(value)
    if len(text) < 2:
        return ""
    if re.fullmatch(r"[0-9a-fA-F-]{16,}", text):
        return ""
    if "@" in text and re.fullmatch(r"\S+@\S+\.\S+", text):
        return ""
    return text


def _extract_support_json_text(value, *, depth: int = 0) -> list[str]:
    if depth > 5:
        return []
    if isinstance(value, str):
        text = _support_json_scalar_text(value)
        return [text] if text else []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:200]:
            parts.extend(_extract_support_json_text(item, depth=depth + 1))
        return parts
    if not isinstance(value, dict):
        return []
    if _support_json_is_account_metadata(value):
        return []

    parts: list[str] = []
    lower_map = {str(k).lower(): k for k in value.keys()}
    for key in _SUPPORT_JSON_TEXT_KEYS:
        original_key = lower_map.get(key)
        if original_key is not None:
            parts.extend(_extract_support_json_text(value.get(original_key), depth=depth + 1))

    if parts:
        return parts

    for key, nested in value.items():
        lower_key = str(key).lower()
        if lower_key in _SUPPORT_JSON_PRIVATE_KEYS:
            continue
        if any(token in lower_key for token in ("email", "phone", "uuid", "avatar", "url")):
            continue
        parts.extend(_extract_support_json_text(nested, depth=depth + 1))
    return parts


def _normalize_support_material_text(content: str, filename: str = "") -> str:
    clean = _clean_support_material_text(content)
    if not clean:
        return ""

    stripped = clean.strip()
    if not stripped.startswith(("{", "[")):
        return clean

    try:
        parsed = json.loads(stripped)
    except Exception:
        return clean

    parts = _extract_support_json_text(parsed)
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = re.sub(r"\s+", " ", part).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(part.strip())
    if not deduped:
        return ""
    return "\n\n".join(deduped)[:30000].strip()


def _split_support_sections(content: str) -> list[tuple[str, str, str, str]]:
    sections: list[tuple[str, str, str, str]] = []
    for m in _SUPPORT_BLOCK_RE.finditer(content or ""):
        raw_label = re.sub(r"\s+", " ", str(m.group("label") or "").strip().lower())
        filename = str(m.group("filename") or "").strip()
        body = str(m.group("body") or "").strip()
        if (
            "system prompt" in raw_label
            or "agent prompt" in raw_label
            or "character" in raw_label
            or "ai persona" in raw_label
        ):
            sections.append(("AI Persona material", _AI_PERSONA_SOURCE, body, filename))
        elif "memory summary" in raw_label or "memory sample" in raw_label or "memory samurai" in raw_label:
            sections.append(("Memory summary", _MEMORY_SUMMARY_SOURCE, body, filename))
        else:
            sections.append(("User profile", _USER_PROFILE_SOURCE, body, filename))
    return sections


def _persona_support_messages(payload: dict) -> list[dict]:
    messages: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(label: str, source: str, content: str, filename: str = "") -> None:
        msg = _support_message(label, source, content, filename, source_detail=source)
        if not msg:
            return
        key = (_import_source_family(source), re.sub(r"\s+", " ", str(msg.get("content") or ""))[:1000])
        if key in seen:
            return
        seen.add(key)
        messages.append(msg)

    ai_persona = str(payload.get("ai_persona_content") or payload.get("ai_persona") or "").strip()
    if ai_persona:
        add(
            "AI Persona material",
            _AI_PERSONA_SOURCE,
            ai_persona,
            str(payload.get("ai_persona_filename") or "").strip(),
        )

    agent_prompt = str(
        payload.get("agent_prompt_content")
        or payload.get("original_system_prompt_content")
        or payload.get("system_prompt_content")
        or payload.get("agent_prompt")
        or payload.get("system_prompt")
        or payload.get("original_system_prompt")
        or ""
    ).strip()
    if agent_prompt:
        filename = str(
            payload.get("agent_prompt_filename")
            or payload.get("original_system_prompt_filename")
            or payload.get("system_prompt_filename")
            or ""
        ).strip()
        msg = _support_message(
            "AI Persona material",
            _AI_PERSONA_SOURCE,
            agent_prompt,
            filename,
            source_detail="agent_prompt_import",
        )
        if msg:
            key = (_AI_PERSONA_SOURCE, re.sub(r"\s+", " ", str(msg.get("content") or ""))[:1000])
            if key not in seen:
                seen.add(key)
                messages.append(msg)

    character = str(payload.get("character_content") or payload.get("character_card") or "").strip()
    if character:
        msg = _support_message(
            "AI Persona material",
            _AI_PERSONA_SOURCE,
            character,
            str(payload.get("character_filename") or payload.get("character_card_filename") or "").strip(),
            source_detail="character_import",
        )
        if msg:
            key = (_AI_PERSONA_SOURCE, re.sub(r"\s+", " ", str(msg.get("content") or ""))[:1000])
            if key not in seen:
                seen.add(key)
                messages.append(msg)

    profile = str(
        payload.get("personal_profile_content")
        or payload.get("profile_content")
        or ""
    ).strip()
    if profile:
        add(
            "User profile",
            _USER_PROFILE_SOURCE,
            profile,
            str(payload.get("personal_profile_filename") or payload.get("persona_filename") or "").strip(),
        )

    memory_summary = str(
        payload.get("memory_summary_content")
        or payload.get("memory_summary")
        or payload.get("memory_sample_content")
        or payload.get("memory_sample")
        or ""
    ).strip()
    if memory_summary:
        add(
            "Memory summary",
            _MEMORY_SUMMARY_SOURCE,
            memory_summary,
            str(payload.get("memory_summary_filename") or payload.get("memory_sample_filename") or "").strip(),
        )

    persona = str(payload.get("persona_content") or payload.get("persona") or "").strip()
    if persona:
        split_sections = _split_support_sections(persona)
        if split_sections:
            outer_filename = str(payload.get("persona_filename") or "").strip()
            for label, source, body, section_filename in split_sections:
                add(label, source, body, section_filename or outer_filename)
        else:
            add(
                "User profile",
                _USER_PROFILE_SOURCE,
                persona,
                str(payload.get("persona_filename") or "").strip(),
            )
    return messages


def _message_iso_date(msg: dict, fallback: date) -> str:
    try:
        ts = msg.get("ts")
        if ts:
            return datetime.fromtimestamp(float(ts)).date().isoformat()
    except Exception:
        pass
    return fallback.isoformat()


_IMPORT_ARTIFACT_KEYS = (
    "async_status",
    "atlas_mode_enabled",
    "blocked_urls",
    "context_scopes",
    "conversation_id",
    "conversation_origin",
    "conversation_template_id",
    "current_node",
    "default_model_slug",
    "disabled_tool_ids",
    "gizmo_id",
    "is_archived",
    "is_do_not_remember",
    "is_read_only",
    "mapping",
)


def _looks_like_import_artifact(text: str) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    upper = raw.upper()
    if "BEGIN CHAT HISTORY FILE" in upper or "END CHAT HISTORY FILE" in upper:
        return True
    key_hits = sum(1 for key in _IMPORT_ARTIFACT_KEYS if f'"{key}"' in raw or f"{key}:" in raw)
    if key_hits >= 2:
        return True
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    jsonish = sum(1 for line in lines if re.match(r'^"[^"]+"\s*:', line) or line in {"{", "}", "[", "]", "},"})
    return len(lines) >= 4 and jsonish / max(len(lines), 1) > 0.5


def _clean_import_memory_text(text: str, max_chars: int = 900) -> str:
    kept: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if _SUPPORT_MARKER_RE.match(line):
            continue
        if re.match(r"^\s*={3,}\s*(?:BEGIN|END)\s+CHAT\s+HISTORY\s+FILE:", line, re.IGNORECASE):
            continue
        if re.match(r'^"[^"]+"\s*:', line):
            continue
        if line in {"{", "}", "[", "]", "},", "],"}:
            continue
        if any(key in line for key in _IMPORT_ARTIFACT_KEYS):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    if _looks_like_import_artifact(cleaned):
        return ""
    return cleaned[:max_chars].strip()


def _relationship_start_from_import(payload: dict, messages: list[dict]) -> tuple[date | None, str]:
    raw = str(payload.get("relationship_started_at") or "").strip()
    if raw:
        parsed = _parse_iso_calendar_date(raw)
        if parsed:
            return parsed, ""

    dated: list[date] = []
    for msg in messages:
        try:
            ts = msg.get("ts")
            if ts:
                dated.append(datetime.fromtimestamp(float(ts)).date())
        except Exception:
            pass
    if dated:
        return min(dated), ""
    if bool(payload.get("fresh_start")) or raw:
        return date.today(), ""
    return None, "relationship_started_at required when transcript has no timestamps; or pass fresh_start=true"


def _detect_import_language(messages: list[dict]) -> str:
    sample = "\n".join(str(m.get("content") or "") for m in messages)[:24000]
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin_count = len(re.findall(r"[A-Za-z]", sample))
    if zh_count >= 8 and zh_count >= max(3, int(latin_count * 0.08)):
        return "zh-Hans"
    return "en"


def _import_language_for_store(store: UserStore, messages: list[dict]) -> str:
    detected = _detect_import_language(messages)
    archive_language = str(_get_user_archive_language(store.user_id) or "").strip()
    if archive_language.lower().startswith("zh"):
        return archive_language
    if detected.startswith("zh"):
        return detected
    if archive_language.lower().startswith("en"):
        return archive_language
    return detected


def _language_instruction(language: str) -> str:
    if str(language).startswith("zh"):
        return (
            "Write every user-visible field in Simplified Chinese. Keep proper names, model IDs, "
            "and exact quoted phrases in their original language when needed."
        )
    return "Write every user-visible field in natural English."


def _english_only_for_zh(text: str) -> bool:
    raw = str(text or "")
    return bool(re.search(r"[A-Za-z]{4,}", raw)) and not re.search(r"[\u4e00-\u9fff]", raw)


_IMPORT_SUPPORT_SOURCES = {
    _AI_PERSONA_SOURCE,
    "agent_prompt_import",
    "character_import",
    _USER_PROFILE_SOURCE,
    "persona_import",
    _MEMORY_SUMMARY_SOURCE,
    _FRESH_START_SOURCE,
}


def _format_import_message_line(msg: dict) -> str:
    source = _import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
    if source == _AI_PERSONA_SOURCE:
        role = "AI Persona material"
    elif source == _USER_PROFILE_SOURCE:
        role = "User profile"
    elif source == _MEMORY_SUMMARY_SOURCE:
        role = "Memory summary"
    elif source == _FRESH_START_SOURCE:
        role = "Fresh start"
    else:
        role = "User" if msg.get("role") == "user" else "Assistant"
    at = ""
    try:
        if msg.get("ts"):
            at = datetime.fromtimestamp(float(msg["ts"])).isoformat(timespec="seconds") + " "
    except Exception:
        at = ""
    text = str(msg.get("content") or "").strip()
    if not text:
        return ""
    return f"{at}{role}: {text}"


def _model_api_agent_profile_context(store: UserStore, identity: dict) -> dict:
    latest_job = None
    try:
        latest_job = _latest_history_import_job(store)
    except Exception:
        latest_job = None
    latest_job = latest_job if isinstance(latest_job, dict) else {}
    return {
        "runtime_boundary": (
            "Feedling provides the container, iOS context, tools, Identity, and durable memory cards. "
            "The imported agent materials and chat history own the companion persona."
        ),
        "agent_name": str(identity.get("agent_name") or ""),
        "self_introduction": str(identity.get("self_introduction") or "")[:1200],
        "category": str(identity.get("category") or "")[:240],
        "signature": identity.get("signature", []) if isinstance(identity.get("signature"), list) else [],
        "dimensions": identity.get("dimensions", []) if isinstance(identity.get("dimensions"), list) else [],
        "import_sources": {
            "ai_persona": bool(latest_job.get("ai_persona_chars") or latest_job.get("agent_prompt_chars") or latest_job.get("character_chars")),
            "user_profile": bool(latest_job.get("user_profile_chars") or latest_job.get("persona_chars")),
            "memory_summary": bool(latest_job.get("memory_summary_chars")),
            "chat_history": bool(latest_job.get("messages_parsed")),
        },
        "source_priority": [
            "explicit user corrections",
            "AI persona materials",
            "Feedling Identity",
            "relevant memory cards",
            "recent chat",
        ],
    }


def _append_import_lines(lines: list[str], out: list[str], budget: int, *, reverse: bool = False) -> int:
    total = sum(len(line) + 1 for line in out)
    iterable = reversed(lines) if reverse else lines
    staged: list[str] = []
    for line in iterable:
        if not line:
            continue
        if total + len(line) + 1 > budget:
            break
        staged.append(line)
        total += len(line) + 1
    if reverse:
        staged.reverse()
    out.extend(staged)
    return total


def _sequential_transcript_sample(messages: list[dict], max_chars: int) -> str:
    lines = [_format_import_message_line(msg) for msg in messages]
    out: list[str] = []
    _append_import_lines(lines, out, max_chars)
    return "\n".join(out)


def _stratified_history_sample(messages: list[dict], max_chars: int) -> str:
    lines = [_format_import_message_line(msg) for msg in messages]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full

    sections: list[tuple[str, list[str], bool]] = [
        ("[Earliest history]", lines, False),
        ("[Middle history]", lines[len(lines) // 2:], False),
        ("[Latest history]", lines, True),
    ]
    per_section = max(1200, max_chars // 3)
    out_sections: list[str] = []
    seen: set[str] = set()
    for title, section_lines, reverse in sections:
        picked: list[str] = []
        _append_import_lines(section_lines, picked, per_section, reverse=reverse)
        picked = [line for line in picked if line not in seen]
        seen.update(picked)
        if picked:
            out_sections.append(title + "\n" + "\n".join(picked))
    text = "\n\n".join(out_sections)
    return text[:max_chars].strip()


def _is_import_support_message(msg: dict) -> bool:
    return _import_source_family(str(msg.get("source") or msg.get("source_family") or "")) in _IMPORT_SUPPORT_SOURCES


def _import_source_stats(messages: list[dict]) -> dict:
    stats = {
        _AI_PERSONA_SOURCE: {"count": 0, "chars": 0},
        _USER_PROFILE_SOURCE: {"count": 0, "chars": 0},
        _MEMORY_SUMMARY_SOURCE: {"count": 0, "chars": 0},
        _HISTORY_SOURCE: {"count": 0, "chars": 0},
        _FRESH_START_SOURCE: {"count": 0, "chars": 0},
    }
    for msg in messages:
        family = _import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
        if family not in stats:
            stats[family] = {"count": 0, "chars": 0}
        stats[family]["count"] += 1
        stats[family]["chars"] += len(str(msg.get("content") or ""))
    return stats


def _messages_for_source_family(messages: list[dict], family: str) -> list[dict]:
    return [
        m for m in messages
        if _import_source_family(str(m.get("source") or m.get("source_family") or "")) == family
    ]


def _source_briefing_text(support_messages: list[dict], max_chars: int = 7000) -> str:
    groups = [
        ("AI Persona materials", _AI_PERSONA_SOURCE, 1800),
        ("User Profile", _USER_PROFILE_SOURCE, 1600),
        ("Memory Summary", _MEMORY_SUMMARY_SOURCE, 2600),
        ("Fresh Start", _FRESH_START_SOURCE, 600),
    ]
    parts: list[str] = []
    budget_used = 0
    for title, family, family_budget in groups:
        group = _messages_for_source_family(support_messages, family)
        if not group:
            continue
        text = _sequential_transcript_sample(group, min(family_budget, max_chars - budget_used))
        if not text:
            continue
        part = f"[{title}]\n{text}"
        if budget_used + len(part) + 2 > max_chars:
            break
        parts.append(part)
        budget_used += len(part) + 2
    return "\n\n".join(parts).strip()


def _split_text_windows(text: str, max_chars: int) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if len(raw) <= max_chars:
        return [raw]
    paras = re.split(r"\n{2,}", raw)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if current and current_len + len(para) + 2 > max_chars:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0
        if len(para) > max_chars:
            for start in range(0, len(para), max_chars):
                chunk = para[start:start + max_chars].strip()
                if chunk:
                    chunks.append(chunk)
            continue
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current).strip())
    return chunks


def _support_source_windows(support_messages: list[dict], *, max_chars: int) -> list[dict]:
    windows: list[dict] = []
    groups = [
        (_AI_PERSONA_SOURCE, "AI Persona materials", max(4000, min(12000, max_chars - 1200))),
        (_USER_PROFILE_SOURCE, "User Profile", max(4000, min(10000, max_chars - 1200))),
        (_MEMORY_SUMMARY_SOURCE, "Memory Summary", max(5000, min(14000, max_chars - 800))),
        (_FRESH_START_SOURCE, "Fresh Start", max(1000, min(3000, max_chars))),
    ]
    for family, label, family_max in groups:
        group = _messages_for_source_family(support_messages, family)
        if not group:
            continue
        text = _sequential_transcript_sample(group, family_max * 8)
        for idx, chunk in enumerate(_split_text_windows(text, family_max), start=1):
            windows.append({
                "id": f"{family.replace('_import', '').replace('_', '-')}-{idx:03d}",
                "index": len(windows) + 1,
                "total": 0,
                "text": f"[{label} source window {idx}]\n{chunk}",
                "line_start": 0,
                "line_end": 0,
                "first_ts": None,
                "last_ts": None,
                "support_only": True,
                "source_families": [family],
            })
    return windows


def _transcript_sample(messages: list[dict], max_chars: int = 18000) -> str:
    support = [m for m in messages if _is_import_support_message(m)]
    history = [m for m in messages if not _is_import_support_message(m)]
    full = "\n".join(_format_import_message_line(m) for m in messages if _format_import_message_line(m))
    if len(full) <= max_chars:
        return full

    parts: list[str] = []
    support_budget = min(7000, max(2000, max_chars // 3))
    support_text = _source_briefing_text(support, support_budget)
    if support_text:
        parts.append("[Onboarding support material]\n" + support_text)
    history_budget = max(2000, max_chars - sum(len(p) + 2 for p in parts))
    history_text = _stratified_history_sample(history, history_budget)
    if history_text:
        parts.append(history_text)
    return "\n\n".join(parts)[:max_chars].strip()


def _transcript_extraction_windows(
    messages: list[dict],
    *,
    max_chars: int = 18000,
    max_windows: int = 8,
) -> list[str]:
    return [w["text"] for w in _build_transcript_windows(messages, max_chars=max_chars, max_windows=max_windows)]


def _select_evenly(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0:
        return []
    if len(items) <= limit:
        return list(items)
    idxs = {
        round(i * (len(items) - 1) / max(limit - 1, 1))
        for i in range(limit)
    }
    return [items[i] for i in sorted(idxs)]


def _build_transcript_windows(
    messages: list[dict],
    *,
    max_chars: int = 18000,
    max_windows: int | None = None,
    overlap_lines: int = 8,
) -> list[dict]:
    support = [m for m in messages if _is_import_support_message(m)]
    history = [m for m in messages if not _is_import_support_message(m)]
    support_windows = _support_source_windows(support, max_chars=max_chars)
    support_text = _source_briefing_text(support, min(7000, max_chars // 3))
    history_lines = [
        {
            "line": _format_import_message_line(m),
            "ts": m.get("ts"),
            "source": _import_source_family(str(m.get("source") or "history_import")),
        }
        for m in history
    ]
    history_lines = [item for item in history_lines if item["line"]]

    if not history_lines:
        windows = support_windows
        if not windows:
            only = _transcript_sample(support, max_chars=max_chars)
            windows = [{
                "id": "support-1",
                "index": 1,
                "total": 1,
                "text": only,
                "line_start": 0,
                "line_end": 0,
                "first_ts": None,
                "last_ts": None,
                "support_only": True,
                "source_families": sorted({_import_source_family(str(m.get("source") or "")) for m in support}),
            }] if only else []
        total_support = len(windows)
        for idx, window in enumerate(windows, start=1):
            window["index"] = idx
            window["total"] = total_support
        return windows

    prefix = ("[Global onboarding source briefing]\n" + support_text + "\n\n") if support_text else ""
    line_budget = max(4000, max_chars - len(prefix) - 80)
    chunks: list[dict] = []
    current: list[dict] = []
    current_start = 0
    total = 0
    for idx, item in enumerate(history_lines):
        line = item["line"]
        if current and total + len(line) + 1 > line_budget:
            chunks.append({
                "line_start": current_start,
                "line_end": current_start + len(current) - 1,
                "items": current,
            })
            overlap = current[-overlap_lines:] if overlap_lines > 0 else []
            current = list(overlap)
            current_start = max(0, idx - len(current))
            total = sum(len(x["line"]) + 1 for x in current)
        current.append(item)
        total += len(line) + 1
    if current:
        chunks.append({
            "line_start": current_start,
            "line_end": current_start + len(current) - 1,
            "items": current,
        })

    history_window_budget = None
    if max_windows is not None:
        history_window_budget = max(1, max_windows - len(support_windows))
        if len(chunks) > history_window_budget:
            chunks = _select_evenly(chunks, history_window_budget)

    windows: list[dict] = []
    total_windows = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        first_ts = next((x.get("ts") for x in chunk["items"] if x.get("ts")), None)
        last_ts = next((x.get("ts") for x in reversed(chunk["items"]) if x.get("ts")), None)
        body = "\n".join(x["line"] for x in chunk["items"])
        windows.append({
            "id": f"window-{idx:03d}",
            "index": idx,
            "total": total_windows,
            "text": f"{prefix}[Chat history window {idx}/{total_windows}]\n{body}".strip(),
            "line_start": chunk["line_start"],
            "line_end": chunk["line_end"],
            "first_ts": first_ts,
            "last_ts": last_ts,
            "support_only": False,
            "source_families": [_HISTORY_SOURCE],
        })
    combined = support_windows + windows
    total_combined = len(combined)
    for idx, window in enumerate(combined, start=1):
        window["index"] = idx
        window["total"] = total_combined
    return combined


def _history_span_days(messages: list[dict]) -> int:
    dates: list[date] = []
    for msg in messages:
        try:
            ts = msg.get("ts")
            if ts:
                dates.append(datetime.fromtimestamp(float(ts)).date())
        except Exception:
            pass
    if len(dates) < 2:
        return 0
    return max(0, (max(dates) - min(dates)).days)


_HISTORY_IMPORT_TIER_CONFIG = {
    "small": {
        "label": "small",
        "initial_windows": 8,
        "total_windows": 8,
        "story": 4,
        "about_me": 8,
        "ta_thinking": 0,
        "total": 12,
        "chat_ready_cards": 2,
        "background": False,
    },
    "medium": {
        "label": "medium",
        "initial_windows": 24,
        "total_windows": 24,
        "story": 12,
        "about_me": 28,
        "ta_thinking": 2,
        "total": 42,
        "chat_ready_cards": 8,
        "background": False,
    },
    "large": {
        "label": "large",
        "initial_windows": 36,
        "total_windows": 64,
        "story": 24,
        "about_me": 56,
        "ta_thinking": 6,
        "total": 86,
        "chat_ready_cards": 20,
        "background": True,
    },
    "ultra": {
        "label": "ultra",
        "initial_windows": 36,
        "total_windows": 96,
        "story": 32,
        "about_me": 78,
        "ta_thinking": 10,
        "total": 120,
        "chat_ready_cards": 20,
        "background": True,
    },
}


def _history_import_profile(
    history_messages: list[dict],
    support_messages: list[dict],
    *,
    content_chars: int | None = None,
) -> dict:
    message_count = len(history_messages)
    support_chars = sum(len(str(m.get("content") or "")) for m in support_messages)
    history_chars = content_chars if content_chars is not None else sum(len(str(m.get("content") or "")) for m in history_messages)
    span_days = _history_span_days(history_messages)
    if message_count >= 250_000 or history_chars >= 10_000_000 or span_days >= 1095:
        tier = "ultra"
    elif message_count >= 50_000 or history_chars >= 2_000_000 or span_days >= 365:
        tier = "large"
    elif message_count >= 5_000 or history_chars >= 200_000 or span_days >= 90:
        tier = "medium"
    else:
        tier = "small"
    return {
        "tier": tier,
        "message_count": message_count,
        "support_count": len(support_messages),
        "history_chars": int(history_chars),
        "support_chars": support_chars,
        "span_days": span_days,
        **_HISTORY_IMPORT_TIER_CONFIG[tier],
    }


def _json_from_model_text(text: str):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    try:
        return json.loads(raw)
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[idx:])
            return obj
        except Exception:
            continue
    raise ValueError("no json object found")


_GENERIC_IMPORT_TITLE_RE = re.compile(
    r"^(?:导入(?:片段|原话|的个人细节|的事件)|imported\s+(?:exchange|quote|user detail|event|fragment|segment))\s*\d*$",
    re.IGNORECASE,
)
_LOW_VALUE_IMPORT_PATTERNS = [
    re.compile(r"^\s*(?:请|帮我|麻烦|can you|could you|please)\s*.{0,80}(?:介绍|解释|列出|生成|写|改写|优化|summarize|explain|write|list)", re.IGNORECASE),
    re.compile(r"^\s*(?:什么是|有哪些|如何|怎么|what is|what are|how to|how do i)\b", re.IGNORECASE),
    re.compile(r"^(?:继续|还有吗|再举例一些|more|continue|[0-9]+)$", re.IGNORECASE),
    re.compile(r"i'?m sorry,?\s+i don'?t understand", re.IGNORECASE),
]


def _normalize_card_similarity_text(text: str) -> str:
    raw = re.sub(r"\s+", "", str(text or "").lower())
    raw = re.sub(r"[，。！？、,.!?;:：；\"'“”‘’（）()\[\]{}<>《》]", "", raw)
    return raw[:260]


def _memory_similarity_tokens(text: str) -> set[str]:
    raw = str(text or "").lower()
    latin = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", raw))
    cjk = re.findall(r"[\u4e00-\u9fff]", raw)
    grams = {''.join(cjk[idx:idx + 2]) for idx in range(max(0, len(cjk) - 1))}
    if len(cjk) <= 3:
        grams.update(cjk)
    return {tok for tok in latin.union(grams) if tok}


def _token_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


_SENSITIVE_IDENTITY_CLAIM_PATTERNS = [
    re.compile(r"(?:真实姓名|真实名字|本名|法定姓名|身份证|证件号|住址|家庭住址)"),
    re.compile(r"\b(?:real|legal)\s+name\b", re.IGNORECASE),
    re.compile(r"\b(?:id|passport|social security)\s+(?:number|no\.?)\b", re.IGNORECASE),
    re.compile(r"\b(?:home|residential)\s+address\b", re.IGNORECASE),
]


def _looks_like_sensitive_identity_claim(text: str) -> bool:
    raw = str(text or "")
    return any(pattern.search(raw) for pattern in _SENSITIVE_IDENTITY_CLAIM_PATTERNS)


def _looks_like_low_value_import_card(title: str, desc: str, mem_type: str) -> bool:
    clean_title = str(title or "").strip()
    clean_desc = str(desc or "").strip()
    joined = clean_title + "\n" + clean_desc
    if _GENERIC_IMPORT_TITLE_RE.match(clean_title):
        return True
    if len(clean_desc) < 8:
        return True
    if any(p.search(joined) or p.search(clean_desc) for p in _LOW_VALUE_IMPORT_PATTERNS):
        return True
    if mem_type in {"fact", "event"}:
        normalized = _normalize_card_similarity_text(clean_desc)
        if len(normalized) < 12:
            return True
    return False


def _sort_memory_cards_newest_first(cards: list[dict]) -> list[dict]:
    def sort_key(card: dict) -> tuple[int, str]:
        raw_date = str(card.get("occurred_at") or "")[:10]
        parsed = _parse_iso_calendar_date(raw_date)
        ordinal = parsed.toordinal() if parsed else 0
        return ordinal, str(card.get("created_at") or "")

    return sorted(cards, key=sort_key, reverse=True)


_IMPORT_CANDIDATE_TYPES = {
    "user_fact",
    "preference",
    "boundary",
    "relationship_event",
    "emotional_pattern",
    "communication_style",
    "conflict_repair",
    "ai_character",
    "external_memory",
}
_IMPORT_CANDIDATE_SUBJECTS = {"user", "ai", "relationship"}


def _candidate_type_from_memory_type(mem_type: str) -> str:
    if mem_type in {"moment", "quote"}:
        return "relationship_event"
    if mem_type == "insight":
        return "emotional_pattern"
    if mem_type == "reflection":
        return "ai_character"
    return "user_fact"


def _coerce_import_candidates(
    raw,
    relationship_start: date,
    *,
    window_id: str = "",
    source_families: list[str] | None = None,
) -> list[dict]:
    if isinstance(raw, dict):
        raw_items = raw.get("candidates")
        if raw_items is None:
            raw_items = raw.get("memories") or raw.get("cards") or raw.get("items") or []
    else:
        raw_items = raw
    if not isinstance(raw_items, list):
        return []

    out: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        legacy_type = str(item.get("type") or "").strip().lower()
        cand_type = str(item.get("candidate_type") or item.get("kind") or "").strip().lower()
        if not cand_type and legacy_type:
            cand_type = _candidate_type_from_memory_type(legacy_type)
        if cand_type not in _IMPORT_CANDIDATE_TYPES:
            cand_type = "user_fact"
        subject = str(item.get("subject") or "").strip().lower()
        if subject not in _IMPORT_CANDIDATE_SUBJECTS:
            subject = "ai" if cand_type == "ai_character" else ("relationship" if cand_type in {"relationship_event", "conflict_repair", "emotional_pattern"} else "user")
        title = str(item.get("title") or "").strip()[:120]
        summary = str(item.get("summary") or item.get("description") or item.get("content") or "").strip()
        summary = _clean_import_memory_text(summary, max_chars=1400)
        if not summary or _looks_like_low_value_import_card(title, summary, legacy_type or "fact"):
            continue
        if _looks_like_import_artifact(summary) or _looks_like_import_artifact(title):
            continue
        quotes_raw = item.get("evidence_quotes") or item.get("quotes") or item.get("quote") or item.get("her_quote") or []
        if isinstance(quotes_raw, str):
            quotes = [quotes_raw]
        elif isinstance(quotes_raw, list):
            quotes = [str(q).strip() for q in quotes_raw if str(q).strip()]
        else:
            quotes = []
        quotes = [
            _clean_import_memory_text(q, max_chars=360)
            for q in quotes
        ]
        quotes = [q for q in quotes if q and not _looks_like_import_artifact(q)]
        signals_raw = item.get("importance_signals") or item.get("signals") or []
        if isinstance(signals_raw, str):
            signals = [signals_raw]
        elif isinstance(signals_raw, list):
            signals = [str(s).strip().lower() for s in signals_raw if str(s).strip()]
        else:
            signals = []
        try:
            confidence = float(item.get("confidence", 0.55))
        except Exception:
            confidence = 0.55
        first_seen = str(item.get("first_seen_at") or item.get("occurred_at") or item.get("date") or "").strip()
        last_seen = str(item.get("last_seen_at") or first_seen).strip()
        if not _parse_iso_calendar_date(first_seen):
            first_seen = relationship_start.isoformat()
        if not _parse_iso_calendar_date(last_seen):
            last_seen = first_seen
        families = sorted({
            _import_source_family(str(source))
            for source in (source_families or [])
            if str(source or "").strip()
        })
        if not families:
            families = [_HISTORY_SOURCE]
        out.append({
            "id": f"cand_{uuid.uuid4().hex[:12]}",
            "candidate_type": cand_type,
            "subject": subject,
            "title": title,
            "summary": summary[:1400],
            "evidence_quotes": quotes[:3],
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
            "importance_signals": sorted(set(signals))[:8],
            "confidence": max(0.0, min(confidence, 1.0)),
            "source_ids": [str(s)[:160] for s in item.get("source_ids", [])] if isinstance(item.get("source_ids"), list) else [],
            "chunk_ids": sorted(set([window_id] + ([str(c) for c in item.get("chunk_ids", [])] if isinstance(item.get("chunk_ids"), list) else []))),
            "source_families": families,
        })
    return out


def _candidate_score(candidate: dict) -> float:
    ctype = str(candidate.get("candidate_type") or "user_fact")
    subject = str(candidate.get("subject") or "user")
    title = str(candidate.get("title") or "")
    summary = str(candidate.get("summary") or "")
    signals = set(str(s).lower() for s in candidate.get("importance_signals") or [])
    score = float(candidate.get("confidence") or 0.55) * 30.0
    score += {
        "boundary": 34,
        "relationship_event": 32,
        "emotional_pattern": 30,
        "conflict_repair": 30,
        "preference": 25,
        "user_fact": 23,
        "communication_style": 22,
        "external_memory": 21,
        "ai_character": 18,
    }.get(ctype, 15)
    if subject == "relationship":
        score += 10
    elif subject == "user":
        score += 6
    if "explicit_memory" in signals or "remembered" in signals:
        score += 18
    if "repeated" in signals or "recurring" in signals:
        score += 16
    if "emotional_peak" in signals:
        score += 15
    if "relationship_boundary" in signals or "boundary" in signals:
        score += 14
    if candidate.get("evidence_quotes"):
        score += min(10, 4 * len(candidate.get("evidence_quotes") or []))
    score += min(8, max(0, len(candidate.get("chunk_ids") or []) - 1) * 3)
    families = set(str(s) for s in candidate.get("source_families") or [])
    if _MEMORY_SUMMARY_SOURCE in families:
        score += 20
    if _USER_PROFILE_SOURCE in families and subject == "user":
        score += 12
    if _AI_PERSONA_SOURCE in families:
        if subject == "ai" or ctype == "ai_character":
            score += 18
        elif subject == "user":
            score -= 18
    if any(p.search(summary) for p in _LOW_VALUE_IMPORT_PATTERNS):
        score -= 45
    if _looks_like_import_artifact(summary):
        score -= 80
    if (
        subject == "user"
        and ctype in {"user_fact", "external_memory"}
        and _looks_like_sensitive_identity_claim(title + "\n" + summary)
        and not candidate.get("evidence_quotes")
        and not ({"explicit_memory", "repeated", "remembered"} & signals)
    ):
        score -= 55
    return score


def _candidate_has_strong_evidence(candidate: dict) -> bool:
    signals = set(str(s).lower() for s in candidate.get("importance_signals") or [])
    return bool(candidate.get("evidence_quotes")) or bool({"explicit_memory", "repeated", "remembered"} & signals)


def _candidate_should_skip(candidate: dict) -> bool:
    title = str(candidate.get("title") or "")
    summary = str(candidate.get("summary") or "")
    ctype = str(candidate.get("candidate_type") or "")
    subject = str(candidate.get("subject") or "")
    mem_type = _candidate_memory_type(candidate)
    if _looks_like_low_value_import_card(title, summary, mem_type):
        return True
    if _looks_like_import_artifact(title) or _looks_like_import_artifact(summary):
        return True
    if (
        subject == "user"
        and ctype in {"user_fact", "external_memory"}
        and _looks_like_sensitive_identity_claim(title + "\n" + summary)
        and not _candidate_has_strong_evidence(candidate)
    ):
        return True
    return False


def _candidate_mergeable(existing: dict, candidate: dict) -> bool:
    if existing.get("subject") != candidate.get("subject"):
        return False
    existing_type = _candidate_memory_type(existing)
    candidate_type = _candidate_memory_type(candidate)
    if TAB_FOR_TYPE.get(existing_type, "about_me") != TAB_FOR_TYPE.get(candidate_type, "about_me"):
        return False
    norm = existing.get("_norm", "")
    cand_norm = candidate.get("_norm", "")
    if norm and cand_norm and (
        norm == cand_norm
        or norm[:90] == cand_norm[:90]
        or norm in cand_norm
        or cand_norm in norm
    ):
        return True
    title_sim = _token_jaccard(
        _memory_similarity_tokens(existing.get("title") or ""),
        _memory_similarity_tokens(candidate.get("title") or ""),
    )
    body_sim = _token_jaccard(
        existing.get("_tokens") or set(),
        candidate.get("_tokens") or set(),
    )
    return body_sim >= 0.50 or (body_sim >= 0.40 and title_sim >= 0.25)


def _merge_import_candidates(candidates: list[dict]) -> list[dict]:
    clusters: list[dict] = []
    for cand in sorted(candidates, key=_candidate_score, reverse=True):
        if _candidate_should_skip(cand):
            continue
        norm = _normalize_card_similarity_text(cand.get("summary", ""))
        if not norm:
            continue
        cand = dict(cand)
        cand["_norm"] = norm
        cand["_tokens"] = _memory_similarity_tokens(
            " ".join([
                str(cand.get("title") or ""),
                str(cand.get("summary") or ""),
                " ".join(str(q) for q in cand.get("evidence_quotes") or []),
            ])
        )
        merged = False
        for cluster in clusters:
            if _candidate_mergeable(cluster, cand):
                cluster["evidence_quotes"] = list(dict.fromkeys((cluster.get("evidence_quotes") or []) + (cand.get("evidence_quotes") or [])))[:4]
                cluster["source_ids"] = sorted(set((cluster.get("source_ids") or []) + (cand.get("source_ids") or [])))
                cluster["chunk_ids"] = sorted(set((cluster.get("chunk_ids") or []) + (cand.get("chunk_ids") or [])))
                cluster["source_families"] = sorted(set((cluster.get("source_families") or []) + (cand.get("source_families") or [])))
                cluster["importance_signals"] = sorted(set((cluster.get("importance_signals") or []) + (cand.get("importance_signals") or [])))[:10]
                cluster["confidence"] = max(float(cluster.get("confidence") or 0), float(cand.get("confidence") or 0))
                cluster["_tokens"] = (cluster.get("_tokens") or set()).union(cand.get("_tokens") or set())
                cluster["score"] = _candidate_score(cluster)
                merged = True
                break
        if not merged:
            cluster = dict(cand)
            cluster["score"] = _candidate_score(cluster)
            clusters.append(cluster)
    clusters.sort(key=lambda c: float(c.get("score") or 0), reverse=True)
    for cluster in clusters:
        cluster.pop("_norm", None)
        cluster.pop("_tokens", None)
    return clusters


def _candidate_memory_type(candidate: dict) -> str:
    ctype = str(candidate.get("candidate_type") or "")
    if ctype in {"relationship_event", "conflict_repair"}:
        return "moment"
    if ctype == "communication_style" and candidate.get("evidence_quotes"):
        return "quote"
    if ctype in {"emotional_pattern", "ai_character"}:
        return "insight"
    if ctype in {"boundary", "preference", "user_fact", "external_memory"}:
        return "fact"
    return "event"


def _candidate_title(candidate: dict, mem_type: str, language: str) -> str:
    title = str(candidate.get("title") or "").strip()
    if title and not _GENERIC_IMPORT_TITLE_RE.match(title):
        return title[:120]
    summary = str(candidate.get("summary") or "")
    if str(language).startswith("zh"):
        prefix = {
            "moment": "关系片段",
            "quote": "原话",
            "fact": "关于用户",
            "event": "用户事件",
            "insight": "TA 的理解",
            "reflection": "TA 在想",
        }.get(mem_type, "记忆")
        natural = _natural_import_title(summary, mem_type, language)
        if natural and natural != "导入的真实片段":
            return natural[:120]
        return prefix
    natural = _natural_import_title(summary, mem_type, language)
    return natural[:120] or "Memory"


def _render_candidates_to_memory_cards(
    candidates: list[dict],
    relationship_start: date,
    targets: dict,
    *,
    language: str = "en",
    max_cards: int | None = None,
) -> list[dict]:
    merged = _merge_import_candidates(candidates)
    quotas = {
        "story": max(0, int(targets.get("story", 1))),
        "about_me": max(0, int(targets.get("about_me", 1))),
        "ta_thinking": max(0, int(targets.get("ta_thinking", 0))),
    }
    target_total = int(targets.get("total") or sum(quotas.values()))
    configured_cap = int(max_cards) if max_cards is not None else target_total
    extra_allowance = max(6, min(30, max(target_total, configured_cap) // 4))
    emergency_total = max(target_total, configured_cap) + extra_allowance
    cards: list[dict] = []
    used_candidates: set[str] = set()

    def tab_for_candidate(c: dict) -> tuple[str, str]:
        mem_type = _candidate_memory_type(c)
        return mem_type, TAB_FOR_TYPE.get(mem_type, "about_me")

    def append_card(c: dict, mem_type: str) -> None:
        if _candidate_should_skip(c):
            return
        cid = str(c.get("id") or "")
        used_candidates.add(cid)
        occurred = str(c.get("first_seen_at") or "").strip()
        if not _parse_iso_calendar_date(occurred):
            occurred = relationship_start.isoformat()
        body = {
            "type": mem_type,
            "title": _candidate_title(c, mem_type, language),
            "description": str(c.get("summary") or "")[:1200],
            "occurred_at": occurred,
            "context": (
                f"distilled from {len(c.get('chunk_ids') or [])} source window(s); "
                f"sources={','.join(str(s) for s in (c.get('source_families') or []))}; "
                f"score={float(c.get('score') or _candidate_score(c)):.1f}"
            ),
        }
        quotes = c.get("evidence_quotes") or []
        if quotes:
            body["her_quote"] = str(quotes[0])[:500]
        cards.append(body)

    for tab in ("story", "about_me", "ta_thinking"):
        for cand in merged:
            if len(cards) >= target_total:
                break
            cid = str(cand.get("id") or "")
            if cid in used_candidates:
                continue
            mem_type, cand_tab = tab_for_candidate(cand)
            if cand_tab != tab:
                continue
            if quotas.get(tab, 0) <= 0:
                continue
            append_card(cand, mem_type)
            quotas[tab] -= 1

    for cand in merged:
        if len(cards) >= emergency_total:
            break
        if len(cards) >= target_total and _candidate_score(cand) < 58:
            continue
        cid = str(cand.get("id") or "")
        if cid in used_candidates:
            continue
        mem_type, _ = tab_for_candidate(cand)
        append_card(cand, mem_type)
    return _sort_memory_cards_newest_first(_dedupe_memory_cards(cards))


def _memory_candidate_extraction_prompt(
    window: dict,
    *,
    idx: int,
    total: int,
    per_window_target: int,
    relationship_start: date,
    language: str,
) -> str:
    sample = str(window.get("text") or "")
    source_families = ", ".join(str(s) for s in (window.get("source_families") or [])) or "history_import"
    return (
        "Distill durable Feedling onboarding memory candidates from this material window. "
        "This is pass 1 of a two-pass pipeline: output candidates only, not final Memory Garden cards. "
        "Return JSON only in this exact shape: "
        "{\"candidates\":[{\"candidate_type\":\"user_fact|preference|boundary|relationship_event|emotional_pattern|communication_style|conflict_repair|ai_character|external_memory\","
        "\"subject\":\"user|ai|relationship\",\"title\":\"optional natural short title\",\"summary\":\"durable memory candidate\","
        "\"evidence_quotes\":[\"short exact quote if available\"],\"first_seen_at\":\"YYYY-MM-DD\",\"last_seen_at\":\"YYYY-MM-DD\","
        "\"importance_signals\":[\"explicit_memory|repeated|emotional_peak|relationship_boundary|future_utility\"],\"confidence\":0.0}],\"why_empty\":\"optional\"}. "
        f"Return up to {per_window_target} candidates, fewer or zero if this window is generic task Q&A, assistant filler, raw JSON metadata, or has no durable relationship/user/AI-character signal. "
        "High-value candidates include stable user facts, preferences, boundaries, relationship milestones, emotional patterns, conflict/repair patterns, repeated themes, and AI character/voice definitions. "
        "Do not preserve ordinary knowledge questions, one-off task instructions, product copy, code/debug chatter, upload wrappers, file delimiters, or raw JSON keys. "
        "Source contract: AI Persona materials describe the AI companion and should mainly produce subject=ai / ai_character candidates; User Profile describes the user and should mainly produce subject=user candidates; "
        "Memory Summary is a high-recall migration source, so split every meaningful durable detail into candidates instead of returning empty just because the material is already summarized; "
        "Chat History is evidence for lived exchanges and relationship patterns. "
        "Never treat User Profile facts as the AI companion's identity, name, or self-description. "
        "Do not make one candidate per message; merge repeated details inside this window. "
        f"{_language_instruction(language)} "
        f"If dates are unclear, use {relationship_start.isoformat()}."
        f"\n\nWindow id: {window.get('id', idx)} ({idx}/{total})\nSource families: {source_families}\nMaterial:\n{sample}"
    )


def _split_candidate_retry_windows(window: dict, max_chars: int = 8500) -> list[dict]:
    text = str(window.get("text") or "")
    if len(text) <= max_chars:
        return []
    parts: list[dict] = []
    for part_idx in range(0, len(text), max_chars):
        chunk = text[part_idx:part_idx + max_chars].strip()
        if not chunk:
            continue
        copy = dict(window)
        copy["id"] = f"{window.get('id') or 'window'}:retry-{len(parts) + 1}"
        copy["text"] = chunk
        parts.append(copy)
        if len(parts) >= 4:
            break
    return parts


def _repair_candidate_json_with_provider(
    provider: ProviderConfig,
    raw_reply: str,
    *,
    relationship_start: date,
    window_id: str,
    language: str,
    source_families: list[str] | None = None,
) -> list[dict]:
    prompt = (
        "The previous model response was not valid JSON for Feedling memory candidate extraction. "
        "Convert only the durable memory candidates in that response into this exact JSON schema: "
        "{\"candidates\":[{\"candidate_type\":\"user_fact|preference|boundary|relationship_event|emotional_pattern|communication_style|conflict_repair|ai_character|external_memory\","
        "\"subject\":\"user|ai|relationship\",\"title\":\"optional natural short title\",\"summary\":\"durable memory candidate\","
        "\"evidence_quotes\":[\"short exact quote if available\"],\"first_seen_at\":\"YYYY-MM-DD\",\"last_seen_at\":\"YYYY-MM-DD\","
        "\"importance_signals\":[\"explicit_memory|repeated|emotional_peak|relationship_boundary|future_utility\"],\"confidence\":0.0}]}. "
        "Return JSON only. Drop raw JSON metadata, generic tasks, and filler. "
        f"{_language_instruction(language)} "
        f"If dates are unclear, use {relationship_start.isoformat()}.\n\nPrevious response:\n{str(raw_reply or '')[:12000]}"
    )
    result = chat_completion(
        provider,
        [
            {"role": "system", "content": "You repair malformed JSON into strict Feedling memory candidate JSON."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1800,
        temperature=0.0,
        timeout=35.0,
    )
    return _coerce_import_candidates(
        _json_from_model_text(result["reply"]),
        relationship_start,
        window_id=window_id,
        source_families=source_families,
    )


def _extract_memory_candidates_with_provider(
    provider: ProviderConfig,
    windows: list[dict],
    relationship_start: date,
    *,
    per_window_target: int,
    language: str = "en",
    on_progress=None,
) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    all_candidates: list[dict] = []
    for idx, window in enumerate(windows, start=1):
        source_families = [str(s) for s in (window.get("source_families") or [])]
        prompt = _memory_candidate_extraction_prompt(
            window,
            idx=idx,
            total=len(windows),
            per_window_target=per_window_target,
            relationship_start=relationship_start,
            language=language,
        )
        reply = ""
        try:
            result = chat_completion(
                provider,
                [
                    {"role": "system", "content": "You are a strict JSON candidate extraction engine for long-memory distillation."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2200,
                temperature=0.1,
                timeout=60.0,
            )
            reply = str(result.get("reply") or "")
            parsed = _json_from_model_text(reply)
            all_candidates.extend(_coerce_import_candidates(
                parsed,
                relationship_start,
                window_id=str(window.get("id") or idx),
                source_families=source_families,
            ))
        except Exception as e:
            repaired = False
            if reply:
                try:
                    repaired_candidates = _repair_candidate_json_with_provider(
                        provider,
                        reply,
                        relationship_start=relationship_start,
                        window_id=str(window.get("id") or idx),
                        language=language,
                        source_families=source_families,
                    )
                    all_candidates.extend(repaired_candidates)
                    warnings.append(f"provider_candidate_json_repaired_window_{idx}:{len(repaired_candidates)}")
                    repaired = True
                except Exception as repair_e:
                    warnings.append(f"provider_candidate_json_repair_failed_window_{idx}:{type(repair_e).__name__}:{str(repair_e)[:120]}")
            if not repaired:
                retry_candidates: list[dict] = []
                for part_idx, retry_window in enumerate(_split_candidate_retry_windows(window), start=1):
                    retry_prompt = _memory_candidate_extraction_prompt(
                        retry_window,
                        idx=part_idx,
                        total=1,
                        per_window_target=max(2, per_window_target // 2),
                        relationship_start=relationship_start,
                        language=language,
                    )
                    try:
                        retry_result = chat_completion(
                            provider,
                            [
                                {"role": "system", "content": "You are a strict JSON candidate extraction engine for long-memory distillation."},
                                {"role": "user", "content": retry_prompt},
                            ],
                            max_tokens=1800,
                            temperature=0.1,
                            timeout=45.0,
                        )
                        retry_parsed = _json_from_model_text(retry_result["reply"])
                        retry_candidates.extend(_coerce_import_candidates(
                            retry_parsed,
                            relationship_start,
                            window_id=str(retry_window.get("id") or idx),
                            source_families=[str(s) for s in (retry_window.get("source_families") or source_families)],
                        ))
                    except Exception as retry_e:
                        warnings.append(f"provider_candidate_retry_failed_window_{idx}_part_{part_idx}:{type(retry_e).__name__}:{str(retry_e)[:100]}")
                if retry_candidates:
                    all_candidates.extend(retry_candidates)
                    warnings.append(f"provider_candidate_retry_split_window_{idx}:{len(retry_candidates)}")
                else:
                    warnings.append(f"provider_candidate_extraction_failed_window_{idx}:{type(e).__name__}:{str(e)[:160]}")
        if on_progress:
            on_progress(idx, len(windows), len(all_candidates))
    return all_candidates, warnings


def _coerce_memory_cards(raw, relationship_start: date) -> list[dict]:
    if isinstance(raw, dict):
        raw_items = raw.get("memories") or raw.get("cards") or raw.get("items") or []
    else:
        raw_items = raw
    if not isinstance(raw_items, list):
        return []

    cards: list[dict] = []
    allowed = {"moment", "quote", "fact", "event"}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        mem_type = str(item.get("type") or "fact").strip().lower()
        if mem_type not in allowed:
            mem_type = "fact"
        title = str(item.get("title") or "").strip()[:120]
        desc = str(item.get("description") or item.get("content") or "").strip()[:1200]
        if not desc:
            continue
        if not title:
            title = desc[:72]
        if _looks_like_low_value_import_card(title, desc, mem_type):
            continue
        quote = str(item.get("her_quote") or item.get("quote") or "").strip()
        context = str(item.get("context") or "").strip()
        if any(_looks_like_import_artifact(value) for value in (title, desc, quote, context) if value):
            continue
        occurred = str(item.get("occurred_at") or item.get("date") or "").strip()
        if not _parse_iso_calendar_date(occurred):
            occurred = relationship_start.isoformat()
        card = {
            "type": mem_type,
            "title": title,
            "description": desc,
            "occurred_at": occurred,
        }
        if quote:
            card["her_quote"] = quote[:500]
        if context:
            card["context"] = context[:600]
        cards.append(card)
    return cards


def _dedupe_memory_cards(cards: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    seen_text: set[str] = set()
    for card in cards:
        title = str(card.get("title") or "")
        desc = str(card.get("description") or "")
        mem_type = str(card.get("type") or "")
        if _looks_like_low_value_import_card(title, desc, mem_type):
            continue
        key = re.sub(
            r"\s+",
            " ",
            (mem_type + "|" + title + "|" + desc[:160]).lower(),
        )
        if key in seen:
            continue
        norm = _normalize_card_similarity_text(desc)
        if norm:
            if any(norm == prev or norm[:80] == prev[:80] or norm in prev or prev in norm for prev in seen_text):
                continue
            seen_text.add(norm)
        seen.add(key)
        out.append(card)
    return out


def _natural_import_title(content: str, mem_type: str, language: str) -> str:
    clean = re.sub(r"\s+", " ", str(content or "")).strip()
    clean = re.sub(r"^(User|Assistant|用户|助手|AI|TA)[:：]\s*", "", clean)
    if not clean:
        return "新的记忆" if str(language).startswith("zh") else "New memory"
    if str(language).startswith("zh"):
        compact = re.sub(r"[。！？].*$", "", clean)
        return compact[:24] or "导入的真实片段"
    words = clean.split()
    return " ".join(words[:8])[:72] or "Imported memory"


def _fallback_chunks_from_message(msg: dict, max_chunks: int = 18) -> list[str]:
    content = _clean_import_memory_text(str(msg.get("content") or ""), max_chars=30000)
    if not content:
        return []
    if ":" in content[:240]:
        _, body = content.split(":", 1)
        content = body.strip() or content
    parts = [
        part.strip(" \t\r\n-•*0123456789.、)）")
        for part in re.split(r"(?:\n{2,}|\n\s*(?:[-*•]|\d+[.)、）])\s+)", content)
    ]
    chunks: list[str] = []
    seen: set[str] = set()
    for part in parts:
        clean = _clean_import_memory_text(part, max_chars=900)
        if len(clean) < 8:
            continue
        norm = _normalize_card_similarity_text(clean)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        chunks.append(clean)
        if len(chunks) >= max_chunks:
            break
    if not chunks and content:
        chunks = [content[:900]]
    return chunks


def _fallback_memory_cards(
    messages: list[dict],
    relationship_start: date,
    *,
    story_needed: int,
    about_needed: int,
    language: str = "en",
) -> list[dict]:
    cards: list[dict] = []
    story_pool: list[dict] = []
    about_pool: list[dict] = []
    for msg in messages:
        content = _clean_import_memory_text(str(msg.get("content") or ""))
        if not content:
            continue
        clean_msg = dict(msg)
        clean_msg["content"] = content
        family = _import_source_family(str(clean_msg.get("source") or clean_msg.get("source_family") or ""))
        if family in {_HISTORY_SOURCE, _MEMORY_SUMMARY_SOURCE, _FRESH_START_SOURCE}:
            story_pool.append(clean_msg)
        if family in {_HISTORY_SOURCE, _MEMORY_SUMMARY_SOURCE, _USER_PROFILE_SOURCE, _FRESH_START_SOURCE}:
            about_pool.append(clean_msg)
    if not story_pool and not about_pool and any(
        _import_source_family(str(m.get("source") or m.get("source_family") or "")) == _FRESH_START_SOURCE
        for m in messages
    ):
        fallback_text = "从空白状态开始。" if str(language).startswith("zh") else "Fresh start with IO."
        fresh = {"role": "user", "content": fallback_text, "ts": None, "source": _FRESH_START_SOURCE}
        story_pool = [fresh]
        about_pool = [fresh]

    def expand(pool: list[dict], limit: int) -> list[tuple[dict, str]]:
        expanded: list[tuple[dict, str]] = []
        for msg in pool:
            family = _import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
            chunk_limit = max(1, limit - len(expanded))
            chunks = _fallback_chunks_from_message(
                msg,
                max_chunks=max(chunk_limit, 4 if family == _MEMORY_SUMMARY_SOURCE else 1),
            )
            for chunk in chunks:
                expanded.append((msg, chunk))
                if len(expanded) >= limit:
                    return expanded
        return expanded

    idx = 0
    story_types = ["moment", "quote"]
    story_items = expand(story_pool, story_needed)
    while story_needed > 0 and idx < len(story_items):
        msg, content = story_items[idx]
        mem_type = story_types[idx % len(story_types)]
        title = _natural_import_title(content, mem_type, language)
        cards.append({
            "type": mem_type,
            "title": title,
            "description": content[:900],
            "her_quote": content[:360] if mem_type == "quote" and msg.get("role") == "user" else "",
            "occurred_at": _message_iso_date(msg, relationship_start),
            "context": f"fallback source={_import_source_family(str(msg.get('source') or msg.get('source_family') or ''))}",
        })
        idx += 1
        story_needed -= 1

    idx = 0
    about_types = ["fact", "event"]
    about_items = expand(about_pool, about_needed)
    while about_needed > 0 and idx < len(about_items):
        msg, content = about_items[idx]
        mem_type = about_types[idx % len(about_types)]
        title = _natural_import_title(content, mem_type, language)
        cards.append({
            "type": mem_type,
            "title": title,
            "description": content[:900],
            "occurred_at": _message_iso_date(msg, relationship_start),
            "context": f"fallback source={_import_source_family(str(msg.get('source') or msg.get('source_family') or ''))}",
        })
        idx += 1
        about_needed -= 1
    return cards


def _import_memory_targets(
    floors: dict,
    history_messages: list[dict],
    support_messages: list[dict],
    profile: dict | None = None,
) -> dict:
    profile = profile or _history_import_profile(history_messages, support_messages)
    source_stats = _import_source_stats(support_messages + history_messages)
    ai_persona_chars = int(source_stats.get(_AI_PERSONA_SOURCE, {}).get("chars") or 0)
    user_profile_chars = int(source_stats.get(_USER_PROFILE_SOURCE, {}).get("chars") or 0)
    memory_summary_chars = int(source_stats.get(_MEMORY_SUMMARY_SOURCE, {}).get("chars") or 0)
    cfg = _HISTORY_IMPORT_TIER_CONFIG.get(str(profile.get("tier") or "small"), _HISTORY_IMPORT_TIER_CONFIG["small"])
    story = int(cfg["story"])
    about = int(cfg["about_me"])
    thinking = int(cfg["ta_thinking"])
    if len(history_messages) <= 0:
        story = 2 if memory_summary_chars else (1 if any(_import_source_family(str(m.get("source") or "")) == _FRESH_START_SOURCE for m in support_messages) else 0)
        about = 3 if (memory_summary_chars or user_profile_chars) else (1 if support_messages else 0)
        thinking = 2 if ai_persona_chars else 0
    elif str(profile.get("tier")) == "small":
        # Keep short histories compact; do not pad to the old bootstrap floors.
        story = min(story, max(2, len(history_messages) // 8 + 2))
        about = min(about, max(3, len(history_messages) // 5 + 3))
    if user_profile_chars:
        about = max(about, 3)
        about += min(5, max(1, user_profile_chars // 2500))
    if memory_summary_chars:
        story = max(story, 2)
        about = max(about, 3)
        extra = min(36, max(4, memory_summary_chars // 900))
        story += max(1, extra // 3)
        about += max(2, extra - (extra // 3))
    if ai_persona_chars:
        thinking = max(thinking, 2)
        thinking += min(6, max(0, ai_persona_chars // 3500))
    total = max(2, story + about + thinking)
    return {
        "story": max(0, story),
        "about_me": max(0, about),
        "ta_thinking": max(0, thinking),
        "total": total,
        "tier": str(profile.get("tier") or "small"),
        "initial_windows": int(cfg["initial_windows"]),
        "total_windows": int(cfg["total_windows"]),
        "chat_ready_cards": int(cfg["chat_ready_cards"]),
        "background": bool(cfg["background"]),
        "floor_reference": floors,
        "source_stats": source_stats,
    }


def _extract_memory_cards_with_provider(
    provider: ProviderConfig,
    messages: list[dict],
    relationship_start: date,
    targets: dict,
    language: str = "en",
) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    target_total = int(targets.get("story", 1)) + int(targets.get("about_me", 1))
    windows = _transcript_extraction_windows(messages, max_chars=18000, max_windows=8)
    if not windows:
        return [], ["empty_transcript_sample"]
    if len(windows) > 1:
        warnings.append(f"history_import_windows:{len(windows)}")

    all_cards: list[dict] = []
    per_window_target = max(3, min(8, (target_total + len(windows) - 1) // len(windows) + 2))
    for idx, sample in enumerate(windows, start=1):
        prompt = (
            "Extract high-signal Feedling Memory Garden cards from this onboarding material window. "
            "Large imports are processed across multiple timeline windows, so use this window's durable details "
            "without assuming it is the whole relationship. Return JSON only in this shape: "
            "{\"memories\":[{\"type\":\"moment|quote|fact|event\",\"title\":\"...\","
            "\"description\":\"...\",\"her_quote\":\"optional exact user quote\","
            "\"occurred_at\":\"YYYY-MM-DD\"}]}. "
            f"Window {idx}/{len(windows)}. Return up to {per_window_target} cards from this window, fewer or zero if the material is repetitive, generic, or not personal. "
            "moment/quote cards belong to Story and must be specific lived exchanges or exact user wording. "
            "fact/event cards belong to About me and must be durable user preferences, relationships, habits, projects, dates, or boundaries. "
            "Do not save generic encyclopedia Q&A, product-copy drafts, assistant filler, empty commands, raw JSON, file delimiters, upload wrappers, or internal field names. "
            "Do not write one card per message; merge repeated content into one stronger card. "
            "Use natural, specific titles. Never use titles like Imported exchange, Imported quote, 导入片段, 导入原话, 导入的个人细节, or 导入的事件. "
            "Character card material describes the AI companion; personal profile material describes the user. Do not confuse the two. "
            f"{_language_instruction(language)} "
            f"If dates are unclear, use {relationship_start.isoformat()}."
            "\n\nMaterial window:\n" + sample
        )
        try:
            result = chat_completion(
                provider,
                [
                    {"role": "system", "content": "You are a strict JSON extraction engine for Feedling Memory Garden."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1800,
                temperature=0.1,
                timeout=45.0,
            )
            parsed = _json_from_model_text(result["reply"])
            all_cards.extend(_coerce_memory_cards(parsed, relationship_start))
        except Exception as e:
            warnings.append(f"provider_memory_extraction_failed_window_{idx}:{type(e).__name__}:{str(e)[:160]}")
    return _dedupe_memory_cards(all_cards), warnings


def _memory_counts_for_cards(cards: list[dict]) -> dict:
    counts = {"story": 0, "about_me": 0, "ta_thinking": 0, "total": 0}
    for card in cards:
        t = str(card.get("type") or "")
        tab = TAB_FOR_TYPE.get(t)
        counts["total"] += 1
        if tab:
            counts[tab] += 1
    return counts


def _ensure_import_memory_floors(
    cards: list[dict],
    messages: list[dict],
    relationship_start: date,
    floors: dict,
    language: str = "en",
) -> list[dict]:
    counts = _memory_counts_for_cards(cards)
    story_needed = max(0, int(floors.get("story", 1)) - counts["story"])
    about_needed = max(0, int(floors.get("about_me", 1)) - counts["about_me"])
    if story_needed or about_needed:
        cards = cards + _fallback_memory_cards(
            messages,
            relationship_start,
            story_needed=story_needed,
            about_needed=about_needed,
            language=language,
        )
    # Force the first persisted memory to anchor the relationship start date.
    if cards:
        cards[0]["occurred_at"] = relationship_start.isoformat()
    return _sort_memory_cards_newest_first(_dedupe_memory_cards(cards))


def _ensure_import_minimum_cards(
    cards: list[dict],
    messages: list[dict],
    relationship_start: date,
    *,
    min_story: int = 1,
    min_about: int = 1,
    language: str = "en",
) -> list[dict]:
    counts = _memory_counts_for_cards(cards)
    story_needed = max(0, min_story - counts["story"])
    about_needed = max(0, min_about - counts["about_me"])
    if story_needed or about_needed:
        cards = cards + _fallback_memory_cards(
            messages,
            relationship_start,
            story_needed=story_needed,
            about_needed=about_needed,
            language=language,
        )
    if cards:
        cards[0]["occurred_at"] = relationship_start.isoformat()
    return _dedupe_memory_cards(cards)


def _card_dedupe_key(card: dict) -> str:
    return "|".join([
        str(card.get("type") or ""),
        _normalize_card_similarity_text(card.get("title") or ""),
        _normalize_card_similarity_text(card.get("description") or ""),
    ])


def _new_cards_only(existing_cards: list[dict], candidate_cards: list[dict]) -> list[dict]:
    existing = {_card_dedupe_key(card) for card in existing_cards}
    out: list[dict] = []
    for card in candidate_cards:
        key = _card_dedupe_key(card)
        if not key or key in existing:
            continue
        existing.add(key)
        out.append(card)
    return out


def _moment_from_memory_card(store: UserStore, card: dict, envelope: dict) -> dict:
    now = _now_iso()
    moment = {
        "v": 1,
        "id": envelope.get("id") or f"mom_{uuid.uuid4().hex[:12]}",
        "type": str(card.get("type") or "fact"),
        "occurred_at": str(card.get("occurred_at") or now),
        "created_at": now,
        "source": "history_import",
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
    }
    if envelope.get("K_enclave"):
        moment["K_enclave"] = envelope["K_enclave"]
    return moment


def _append_import_memory_cards(store: UserStore, cards: list[dict]) -> list[dict]:
    moments = _load_moments(store)
    created: list[dict] = []
    for card in _sort_memory_cards_newest_first(cards):
        mem_type = str(card.get("type") or "")
        if mem_type not in MEMORY_TYPES:
            continue
        body = {
            "title": str(card.get("title") or "")[:120],
            "description": str(card.get("description") or "")[:1200],
            "type": mem_type,
        }
        for key in ("her_quote", "context", "linked_dimension"):
            value = str(card.get(key) or "").strip()
            if value:
                body[key] = value[:800]
        envelope, err = _build_shared_envelope_for_store(
            store,
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        if envelope is None:
            raise RuntimeError(f"memory_envelope_failed:{err}")
        envelope["type"] = mem_type
        envelope["occurred_at"] = str(card.get("occurred_at") or date.today().isoformat())
        envelope["source"] = "history_import"
        moments.append(_moment_from_memory_card(store, card, envelope))
        created.append(moments[-1])
    _save_moments(store, moments)
    if created:
        _log_bootstrap_event(store, "history_import_memory_written", success=True)
    return created


def _fallback_identity_payload(memories: list[dict], days: int, language: str = "en") -> dict:
    sample_desc = ""
    if memories:
        sample_desc = str(memories[0].get("description") or memories[0].get("title") or "")
    is_zh = str(language).startswith("zh")
    desc = sample_desc[:220] or ("我还在从导入材料里学习。" if is_zh else "I am still learning from the imported history.")
    names = (
        ["细心", "稳定", "有趣", "守护感", "好奇", "直接", "温柔"]
        if is_zh else
        ["Attentive", "Steady", "Playful", "Protective", "Curious", "Direct", "Tender"]
    )
    values = [74, 68, 56, 63, 71, 59, 66]
    dimensions = [
        {
            "name": name,
            "value": values[idx],
            "description": (
                (f"根据导入材料初步估计。依据：{desc}" if is_zh else f"Estimated from imported history. Anchor: {desc}")
                if idx == 0 else
                ("根据导入的对话模式初步估计；后续会在真实对话中继续校准。" if is_zh else "Estimated from imported chat patterns; refine after live conversation.")
            ),
        }
        for idx, name in enumerate(names)
    ]
    return {
        "agent_name": "",
        "self_introduction": (
            "我已经读过你导入的材料，并先搭好了一版记忆和身份。现在我还没有名字，你可以告诉我以后该怎么称呼我。"
            if is_zh else
            "I imported the previous history and built a first version of my memory "
            "from it. I do not have a confirmed name yet, so you can tell me what "
            "you would like to call me."
        ),
        "dimensions": dimensions,
        "days_with_user": days,
        "category": "细心 · 稳定" if is_zh else "Attentive · Grounded",
        "signature": ["从材料里醒来", "继续记住你"] if is_zh else ["Built from receipts", "Ready to keep noticing"],
    }


def _sanitize_import_agent_name(name: str) -> str:
    clean = re.sub(r"\s+", " ", str(name or "")).strip()
    clean = clean.strip(" `\"'“”‘’。，,.;；:：!！?？")
    if not clean or len(clean) > 80:
        return ""
    if any(ch in clean for ch in "\n\r{}[]"):
        return ""
    labels = set(globals().get("_IDENTITY_RUNTIME_LABELS", set()))
    normalized = re.sub(r"\s+", " ", clean.lower())
    if normalized in labels:
        return ""
    if normalized.startswith(("openai/", "anthropic/", "google/", "deepseek/")):
        return ""
    if re.search(r"\b(?:api|model|runtime|provider|endpoint|assistant|agent)\b", normalized):
        return ""
    return clean[:80]


def _normalize_identity_payload(raw, memories: list[dict], days: int, language: str = "en") -> dict:
    fallback = _fallback_identity_payload(memories, days, language)
    if not isinstance(raw, dict):
        return fallback
    payload = dict(raw.get("identity") if isinstance(raw.get("identity"), dict) else raw)
    dims = payload.get("dimensions")
    if not isinstance(dims, list):
        return fallback
    clean_dims: list[dict] = []
    for idx, dim in enumerate(dims[:7]):
        if not isinstance(dim, dict):
            continue
        name = str(dim.get("name") or f"Dimension {idx + 1}")[:60]
        if str(language).startswith("zh") and _english_only_for_zh(name):
            name = str(fallback["dimensions"][idx].get("name") or f"维度 {idx + 1}")[:60]
        try:
            value = int(dim.get("value", 50))
        except Exception:
            value = 50
        desc = str(dim.get("description") or dim.get("evidence") or "")[:500]
        if str(language).startswith("zh") and _english_only_for_zh(desc):
            desc = ""
        clean_dims.append({
            "name": name,
            "value": max(0, min(value, 100)),
            "description": desc or ("根据导入材料得出。" if str(language).startswith("zh") else "Derived from imported history."),
        })
    if len(clean_dims) != 7:
        return fallback
    payload["agent_name"] = _sanitize_import_agent_name(str(payload.get("agent_name") or ""))
    payload["self_introduction"] = str(payload.get("self_introduction") or "")[:1200]
    if str(language).startswith("zh") and _english_only_for_zh(payload["self_introduction"]):
        payload["self_introduction"] = ""
    if not payload["self_introduction"]:
        payload["self_introduction"] = fallback["self_introduction"]
    payload["dimensions"] = clean_dims
    payload["days_with_user"] = days
    signature = payload.get("signature")
    if not isinstance(signature, list):
        signature = fallback.get("signature", [])
    clean_signature = [str(item).strip()[:80] for item in signature if str(item).strip()][:2]
    if str(language).startswith("zh") and any(_english_only_for_zh(item) for item in clean_signature):
        clean_signature = fallback.get("signature", [])
    payload["signature"] = clean_signature if len(clean_signature) == 2 else fallback.get("signature", [])
    category = str(payload.get("category") or "").strip()[:120]
    if not category or (str(language).startswith("zh") and _english_only_for_zh(category)):
        category = str(fallback.get("category") or "")
    payload["category"] = category
    return payload


def _derive_identity_with_provider(
    provider: ProviderConfig,
    messages: list[dict],
    memory_cards: list[dict],
    days: int,
    language: str = "en",
) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    memory_sample = json.dumps(memory_cards[:40], ensure_ascii=False)
    source_stats = _import_source_stats(messages)
    has_ai_persona = bool(source_stats.get(_AI_PERSONA_SOURCE, {}).get("chars"))
    has_assistant_history = any(
        str(m.get("role") or "") in {"assistant", "agent", "openclaw"}
        for m in messages
        if _import_source_family(str(m.get("source") or m.get("source_family") or "")) == _HISTORY_SOURCE
    )
    has_ai_memory = any(
        str(card.get("type") or "") in {"insight", "reflection"}
        or "ai" in str(card.get("context") or "").lower()
        for card in memory_cards
    )
    transcript = _transcript_sample(messages, max_chars=12000)
    prompt = (
        "Derive a Feedling Identity Card for the AI companion from typed onboarding sources and Memory Garden cards. "
        "Return JSON only with fields: agent_name, self_introduction, category, "
        "signature (array of two short strings), dimensions (exactly 7 objects with "
        "name, value 0-100, description). Do not invent facts not grounded in input. "
        "Source priority: AI Persona materials are the primary source for the AI companion's identity, voice, role, name, and boundaries. "
        "Memory Garden cards are secondary evidence and may refine the identity. Chat History can show how the AI behaved in relationship. "
        "User Profile describes the user only; use it as relationship context, never as the AI companion's self-description. "
        "If there are no AI Persona materials, infer the companion only from assistant-side chat evidence, relationship patterns, and AI-related Memory Garden cards; otherwise keep the identity generic and ask the user to name/define the companion later. "
        "agent_name is the AI companion's own chosen or user-given name, not the user's name, account name, provider, model, runtime, platform, or product name. "
        "Only set agent_name when the imported Character Card or conversation explicitly names the AI companion; otherwise return an empty string for agent_name. "
        "self_introduction must be written in the AI companion's own voice; never describe the user as 'I'. "
        "High-risk personal claims such as legal/real name, address, or IDs require explicit user-authored evidence; otherwise omit them. "
        f"{_language_instruction(language)} "
        f"days_with_user is {days}.\n\nSource stats:\n{json.dumps(source_stats, ensure_ascii=False)}"
        f"\n\nMemory cards:\n{memory_sample}\n\nTranscript sample:\n{transcript}"
    )
    try:
        result = chat_completion(
            provider,
            [
                {"role": "system", "content": "You write concise, grounded Feedling identity JSON."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1800,
            temperature=0.3,
            timeout=45.0,
        )
        identity = _normalize_identity_payload(_json_from_model_text(result["reply"]), memory_cards, days, language)
        if not has_ai_persona and not has_assistant_history and not has_ai_memory:
            fallback = _fallback_identity_payload(memory_cards, days, language)
            identity["agent_name"] = ""
            identity["self_introduction"] = fallback["self_introduction"]
            identity["category"] = fallback["category"]
            identity["signature"] = fallback["signature"]
            warnings.append("identity_guard_no_ai_source_used_generic_identity")
        return identity, warnings
    except Exception as e:
        warnings.append(f"provider_identity_failed:{type(e).__name__}:{str(e)[:160]}")
        return _fallback_identity_payload(memory_cards, days, language), warnings


def _store_identity_payload(
    store: UserStore,
    identity_payload: dict,
    *,
    days_with_user: int,
    evidence: str,
    language: str = "en",
) -> dict:
    envelope, err = _build_shared_envelope_for_store(
        store,
        json.dumps(identity_payload, ensure_ascii=False).encode("utf-8"),
    )
    if envelope is None:
        raise RuntimeError(f"identity_envelope_failed:{err}")

    existing = _load_identity(store)
    now = _now_iso()
    identity = {
        "v": 1,
        "id": envelope.get("id") or (existing.get("id") if existing else uuid.uuid4().hex),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": existing.get("created_at") if existing else now,
        "updated_at": now,
        "relationship_started_at": _anchor_from_days(days_with_user, store=store, prefer_memory=True),
        "relationship_anchor_source": "history_import",
        "relationship_anchor_evidence": evidence,
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    _save_identity(store, identity)
    _log_bootstrap_event(store, "history_import_identity_written", success=True)
    _append_identity_change(store, {
        "action": "replace" if existing else "init",
        "reason": "根据导入材料写入身份卡。" if str(language).startswith("zh") else "Identity card written from Model API history import.",
    })
    return identity


def _generate_model_api_onboarding_greeting(
    provider: ProviderConfig,
    messages: list[dict],
    memory_cards: list[dict],
    identity_payload: dict,
    days: int,
    language: str = "en",
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    agent_name = _sanitize_import_agent_name(str(identity_payload.get("agent_name") or ""))
    is_zh = str(language).startswith("zh")
    identity_summary = {
        "agent_name": agent_name,
        "self_introduction": identity_payload.get("self_introduction", ""),
        "category": identity_payload.get("category", ""),
        "signature": identity_payload.get("signature", []),
        "days_with_user": days,
    }
    if not agent_name and is_zh:
        name_instruction = (
            "这个身份还没有确认的 AI 伴侣名字。第一句话里要自然说明“现在我还没有名字”，"
            "并问用户以后想怎么叫你。不要自己起名。 "
        )
    elif not agent_name:
        name_instruction = (
            "This identity has no confirmed AI companion name. In this first message, "
            "naturally ask the user what they would like to call you. Do not assign yourself a name. "
        )
    else:
        name_instruction = "Use the confirmed AI companion name only if it feels natural. "
    prompt = (
        "Write the first visible chat message from the user's IO companion after onboarding. "
        "The imported files have already been analyzed into memory and identity; do not paste, "
        "summarize, or mention the source files, onboarding, import, API keys, encryption, or internal tools. "
        "Speak in the companion's own voice, grounded in the context below. "
        f"{name_instruction}"
        f"{_language_instruction(language)} "
        "Return only the message text, no JSON, no bullets, 1-3 short sentences.\n\n"
        "Identity JSON:\n"
        + json.dumps(identity_summary, ensure_ascii=False)[:4000]
        + "\n\nMemory cards:\n"
        + json.dumps(memory_cards[:12], ensure_ascii=False)[:8000]
        + "\n\nTranscript sample:\n"
        + _transcript_sample(messages, max_chars=8000)
    )
    try:
        result = chat_completion(
            provider,
            [
                {"role": "system", "content": "You are the user's IO companion writing one natural first message."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=320,
            temperature=0.7,
            timeout=45.0,
        )
    except Exception as e:
        warnings.append(f"provider_onboarding_greeting_failed:{type(e).__name__}:{str(e)[:160]}")
        fallback = (
            "我已经先把能读懂的部分整理成记忆了。现在我还没有名字，你想以后怎么叫我？"
            if is_zh and not agent_name else
            "I have turned the readable parts into memory first. What would you like to call me?"
            if not agent_name else
            ("我已经整理好一版记忆了，接下来可以从这里继续。" if is_zh else "I have a first version of the memory ready, and we can continue from here.")
        )
        return fallback, warnings

    text = str(result.get("reply") or "").strip()
    text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text).strip()
    if not text:
        warnings.append("provider_onboarding_greeting_empty")
        fallback = (
            "我已经先把能读懂的部分整理成记忆了。现在我还没有名字，你想以后怎么叫我？"
            if is_zh and not agent_name else
            "I have turned the readable parts into memory first. What would you like to call me?"
            if not agent_name else
            ("我已经整理好一版记忆了，接下来可以从这里继续。" if is_zh else "I have a first version of the memory ready, and we can continue from here.")
        )
        return fallback, warnings
    return text[:1200], warnings


def _append_model_api_onboarding_greeting(store: UserStore, text: str) -> dict:
    envelope, err = _build_shared_envelope_for_store(store, text.encode("utf-8"))
    if envelope is None:
        raise RuntimeError(f"onboarding_greeting_envelope_failed:{err}")
    row = store.append_chat(
        "openclaw",
        "model_api",
        envelope,
        extra={"model_api_kind": "onboarding_greeting"},
    )
    store.notify_chat_waiters()
    _log_bootstrap_event(store, "model_api_onboarding_greeting_written", success=True)
    return row


def _process_history_import_sync(
    store: UserStore,
    api_key: str | None,
    job: dict,
    payload: dict,
) -> dict:
    _update_history_job_phase(store, job, "parsing_materials")
    content = str(payload.get("content") or "")
    fmt = str(payload.get("format") or "plaintext").strip().lower()
    warnings: list[str] = []
    history_messages = _parse_import_history_content(content, fmt, warnings)
    support_messages = _persona_support_messages(payload)
    if not history_messages and not support_messages:
        if not bool(payload.get("fresh_start")):
            raise ValueError(
                "content, ai_persona_content, character_content, personal_profile_content, memory_summary_content, persona_content, "
                "or fresh_start=true required"
            )
        support_messages = [{
            "role": "user",
            "content": "Fresh start. No persona profile or previous chat history was provided.",
            "ts": None,
            "source": "fresh_start",
        }]
        warnings.append("fresh_start_without_support_material")

    runtime = _load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        raise RuntimeError(json.dumps(err, ensure_ascii=False))

    analysis_messages = support_messages + history_messages
    fallback_messages = history_messages if history_messages else support_messages
    source_stats = _import_source_stats(analysis_messages)
    relationship_start, rel_err = _relationship_start_from_import(payload, fallback_messages)
    if relationship_start is None:
        raise ValueError(rel_err)
    days = max(0, (date.today() - relationship_start).days)
    floors = _per_tab_floors_for_days(days)
    profile = _history_import_profile(
        history_messages,
        support_messages,
        content_chars=len(content),
    )
    import_targets = _import_memory_targets(floors, history_messages, support_messages, profile)
    language = _import_language_for_store(store, analysis_messages)
    windows = _build_transcript_windows(
        analysis_messages,
        max_chars=18000,
        max_windows=int(import_targets.get("total_windows") or 8),
    )
    initial_windows = _select_evenly(windows, int(import_targets.get("initial_windows") or len(windows)))
    initial_window_ids = {str(w.get("id") or "") for w in initial_windows}
    background_windows = [
        w for w in windows
        if bool(import_targets.get("background")) and str(w.get("id") or "") not in initial_window_ids
    ]

    _update_history_job_phase(store, job, "chat_history_importing", **{
        "format": fmt or "plaintext",
        "history_filename": str(payload.get("history_filename") or "")[:240],
        "ai_persona_filename": str(payload.get("ai_persona_filename") or "")[:240],
        "character_filename": str(
            payload.get("character_filename")
            or payload.get("character_card_filename")
            or ""
        )[:240],
        "agent_prompt_filename": str(
            payload.get("agent_prompt_filename")
            or payload.get("original_system_prompt_filename")
            or payload.get("system_prompt_filename")
            or ""
        )[:240],
        "persona_filename": str(
            payload.get("personal_profile_filename")
            or payload.get("persona_filename")
            or ""
        )[:240],
        "memory_summary_filename": str(
            payload.get("memory_summary_filename")
            or payload.get("memory_sample_filename")
            or ""
        )[:240],
        "messages_parsed": len(history_messages),
        "support_materials": len(support_messages),
        "source_stats": source_stats,
        "ai_persona_chars": int(source_stats.get(_AI_PERSONA_SOURCE, {}).get("chars") or 0),
        "agent_prompt_chars": sum(len(str(m.get("content") or "")) for m in support_messages if m.get("source_detail") == "agent_prompt_import"),
        "character_chars": sum(len(str(m.get("content") or "")) for m in support_messages if m.get("source_detail") == "character_import"),
        "user_profile_chars": int(source_stats.get(_USER_PROFILE_SOURCE, {}).get("chars") or 0),
        "persona_chars": int(source_stats.get(_USER_PROFILE_SOURCE, {}).get("chars") or 0),
        "memory_summary_chars": int(source_stats.get(_MEMORY_SUMMARY_SOURCE, {}).get("chars") or 0),
        "import_language": language,
        "relationship_started_at": relationship_start.isoformat(),
        "relationship_days": days,
        "floors": floors,
        "import_targets": import_targets,
        "history_profile": profile,
        "history_tier": profile["tier"],
        "timeline_span_days": profile["span_days"],
        "candidate_windows_total": len(windows),
        "candidate_windows_initial": len(initial_windows),
        "background_windows_total": len(background_windows),
    })

    def initial_progress(done: int, total: int, candidate_count: int) -> None:
        progress = 24 + int(24 * done / max(total, 1))
        _update_history_job_phase(
            store,
            job,
            "candidate_extracting",
            progress=progress,
            candidate_windows_done=done,
            candidate_windows_total=total,
            candidates_extracted=candidate_count,
        )

    _update_history_job_phase(
        store,
        job,
        "candidate_extracting",
        candidate_windows_done=0,
        candidate_windows_total=len(initial_windows),
        candidates_extracted=0,
    )
    per_window_target = max(4, min(10, (int(import_targets.get("total", 12)) + max(len(initial_windows), 1) - 1) // max(len(initial_windows), 1) + 2))
    initial_candidates, provider_warnings = _extract_memory_candidates_with_provider(
        runtime,
        initial_windows,
        relationship_start,
        per_window_target=per_window_target,
        language=language,
        on_progress=initial_progress,
    )
    warnings.extend(provider_warnings)
    merged_candidates = _merge_import_candidates(initial_candidates)
    cards = _render_candidates_to_memory_cards(
        merged_candidates,
        relationship_start,
        import_targets,
        language=language,
        max_cards=int(import_targets.get("total") or 12),
    )
    cards = _ensure_import_minimum_cards(
        cards,
        fallback_messages,
        relationship_start,
        min_story=min(1, int(import_targets.get("story") or 0)),
        min_about=min(1, int(import_targets.get("about_me") or 0)),
        language=language,
    )
    cards = _sort_memory_cards_newest_first(cards)

    _update_history_job_phase(
        store,
        job,
        "candidate_merging",
        candidates_extracted=len(initial_candidates),
        candidates_merged=len(merged_candidates),
        memories_planned=len(cards),
    )
    _update_history_job_phase(
        store,
        job,
        "memory_writing",
        memories_planned=len(cards),
    )
    memory_rows = _append_import_memory_cards(store, cards)
    _update_history_job_phase(
        store,
        job,
        "identity_deriving",
        memories_created=len(memory_rows),
    )

    identity_payload, id_warnings = _derive_identity_with_provider(runtime, analysis_messages, cards, days, language)
    warnings.extend(id_warnings)
    _update_history_job_phase(
        store,
        job,
        "relationship_anchor_writing",
        memories_created=len(memory_rows),
    )
    identity = _store_identity_payload(
        store,
        identity_payload,
        days_with_user=days,
        evidence=f"history_import:{job['job_id']} relationship_started_at={relationship_start.isoformat()}",
        language=language,
    )
    _update_history_job_phase(
        store,
        job,
        "hosted_chat_preparing",
        memories_created=len(memory_rows),
        identity_written=bool(identity),
    )

    greeting_text, greeting_warnings = _generate_model_api_onboarding_greeting(
        runtime,
        analysis_messages,
        cards,
        identity_payload,
        days,
        language,
    )
    warnings.extend(greeting_warnings)
    greeting_row = _append_model_api_onboarding_greeting(store, greeting_text) if greeting_text else None
    chat_ready_cards = int(import_targets.get("chat_ready_cards") or 2)
    chat_ready = bool(identity) and bool(greeting_row) and len(memory_rows) >= min(chat_ready_cards, max(2, len(cards)))

    job.update({
        "chat_ready": chat_ready,
        "chat_ready_at": _now_iso() if chat_ready else "",
        "chat_ready_cards_required": chat_ready_cards,
        "initial_memories_created": len(memory_rows),
        "candidate_count": len(initial_candidates),
        "candidate_cluster_count": len(merged_candidates),
        "background_status": "pending" if background_windows else "not_needed",
        "warnings": warnings,
    })
    if chat_ready and background_windows:
        _update_history_job_phase(
            store,
            job,
            "background_importing",
            status="processing",
            memories_created=len(memory_rows),
            identity_written=bool(identity),
            onboarding_greeting_written=bool(greeting_row),
            background_windows_done=0,
            background_windows_total=len(background_windows),
        )

        def background_progress(done: int, total: int, candidate_count: int) -> None:
            progress = 96 + int(3 * done / max(total, 1))
            _update_history_job_phase(
                store,
                job,
                "background_importing",
                status="processing",
                progress=progress,
                background_windows_done=done,
                background_windows_total=total,
                background_candidates_extracted=candidate_count,
                memories_created=len(memory_rows),
            )

        try:
            background_candidates, bg_warnings = _extract_memory_candidates_with_provider(
                runtime,
                background_windows,
                relationship_start,
                per_window_target=max(3, min(7, per_window_target - 1)),
                language=language,
                on_progress=background_progress,
            )
            warnings.extend(bg_warnings)
            all_candidates = initial_candidates + background_candidates
            all_cards = _render_candidates_to_memory_cards(
                all_candidates,
                relationship_start,
                import_targets,
                language=language,
                max_cards=int(import_targets.get("total") or 120),
            )
            additional_cards = _new_cards_only(cards, all_cards)
            additional_cards = _sort_memory_cards_newest_first(additional_cards)
            additional_rows = _append_import_memory_cards(store, additional_cards)
            memory_rows.extend(additional_rows)
            cards = _sort_memory_cards_newest_first(_dedupe_memory_cards(cards + additional_cards))
            merged_all = _merge_import_candidates(all_candidates)
            job.update({
                "background_status": "completed",
                "background_candidates_extracted": len(background_candidates),
                "background_memories_created": len(additional_rows),
                "candidate_count": len(all_candidates),
                "candidate_cluster_count": len(merged_all),
            })
        except Exception as e:
            warnings.append(f"background_import_failed:{type(e).__name__}:{str(e)[:180]}")
            job.update({
                "background_status": "failed",
                "background_error": f"{type(e).__name__}:{str(e)[:240]}",
            })

    job.update({
        "status": "completed",
        "completed_at": _now_iso(),
        "chat_messages_imported": 0,
        "memories_created": len(memory_rows),
        "identity_written": bool(identity),
        "onboarding_greeting_written": bool(greeting_row),
        "onboarding_greeting_message_id": (greeting_row or {}).get("id", ""),
        "warnings": warnings,
    })
    return _update_history_job_phase(store, job, "completed", status="completed")


def _run_history_import_job(
    store: UserStore,
    api_key: str | None,
    job_id: str,
    payload: dict,
) -> None:
    try:
        job = db.get_blob(store.user_id, _history_job_kind(job_id)) or {
            "job_id": job_id,
            "status": "queued",
            "created_at": _now_iso(),
        }
        job["started_at"] = job.get("started_at") or _now_iso()
        _process_history_import_sync(store, api_key, job, payload)
        print(
            f"[history_import:{store.user_id}] job={job_id} messages={job.get('messages_parsed')} "
            f"memories={job.get('memories_created')} chat={job.get('chat_messages_imported')} async=1"
        )
    except Exception as e:
        job = db.get_blob(store.user_id, _history_job_kind(job_id)) or {
            "job_id": job_id,
            "created_at": _now_iso(),
        }
        job.update({
            "failed_at": _now_iso(),
            "error": f"{type(e).__name__}:{str(e)[:500]}",
        })
        _update_history_job_phase(store, job, "failed", status="failed")
        print(f"[history_import:{store.user_id}] job={job_id} failed={type(e).__name__}:{str(e)[:220]}")
    finally:
        with _history_import_active_lock:
            _history_import_active_jobs.discard(job_id)


def _start_history_import_job(
    store: UserStore,
    api_key: str | None,
    job: dict,
    payload: dict,
) -> bool:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return False
    with _history_import_active_lock:
        if job_id in _history_import_active_jobs:
            return False
        _history_import_active_jobs.add(job_id)
    thread = threading.Thread(
        target=_run_history_import_job,
        args=(store, api_key, job_id, dict(payload)),
        daemon=True,
        name=f"history-import-{job_id[:18]}",
    )
    thread.start()
    return True


@app.route("/v1/history_import/upload", methods=["POST"])
def history_import_upload():
    store = require_user()
    api_key = _extract_api_key()
    payload = request.get_json(silent=True) or {}
    input_hash = _history_import_payload_hash(payload)
    client_job_id = _history_import_client_job_id(payload)
    existing = _history_import_find_reusable_job(
        store,
        client_job_id=client_job_id,
        input_hash=input_hash,
    )
    if existing:
        if str(existing.get("status") or "") in {"queued", "processing"}:
            _start_history_import_job(store, api_key, existing, payload)
            return jsonify({"job": existing}), 202
        return jsonify({"job": existing}), 200

    job_id = f"hi_{uuid.uuid4().hex[:16]}"
    job = {
        "job_id": job_id,
        "status": "queued",
        "client_job_id": client_job_id,
        "input_hash": input_hash,
        "created_at": _now_iso(),
        "content_chars": len(str(payload.get("content") or "")),
        "ai_persona_chars": len(str(
            payload.get("ai_persona_content")
            or payload.get("ai_persona")
            or ""
        )),
        "character_chars": len(str(
            payload.get("character_content")
            or payload.get("character_card")
            or ""
        )),
        "agent_prompt_chars": len(str(
            payload.get("agent_prompt_content")
            or payload.get("original_system_prompt_content")
            or payload.get("system_prompt_content")
            or payload.get("agent_prompt")
            or payload.get("system_prompt")
            or payload.get("original_system_prompt")
            or ""
        )),
        "persona_chars": len(str(
            payload.get("personal_profile_content")
            or payload.get("persona_content")
            or payload.get("persona")
            or payload.get("profile_content")
            or ""
        )),
        "memory_summary_chars": len(str(
            payload.get("memory_summary_content")
            or payload.get("memory_summary")
            or payload.get("memory_sample_content")
            or payload.get("memory_sample")
            or ""
        )),
        "ai_persona_filename": str(payload.get("ai_persona_filename") or "")[:240],
        "character_filename": str(
            payload.get("character_filename")
            or payload.get("character_card_filename")
            or ""
        )[:240],
        "agent_prompt_filename": str(
            payload.get("agent_prompt_filename")
            or payload.get("original_system_prompt_filename")
            or payload.get("system_prompt_filename")
            or ""
        )[:240],
        "persona_filename": str(
            payload.get("personal_profile_filename")
            or payload.get("persona_filename")
            or ""
        )[:240],
        "memory_summary_filename": str(
            payload.get("memory_summary_filename")
            or payload.get("memory_sample_filename")
            or ""
        )[:240],
        "chat_ready": False,
        "background_status": "not_started",
        **_history_import_phase_fields("upload_received"),
    }
    _save_history_job(store, job)
    _start_history_import_job(store, api_key, job, payload)
    print(f"[history_import:{store.user_id}] job={job_id} queued async=1 client_job_id={client_job_id[:24]}")
    return jsonify({"job": job}), 202


@app.route("/v1/history_import/status/<job_id>", methods=["GET"])
def history_import_status(job_id):
    store = require_user()
    data = db.get_blob(store.user_id, _history_job_kind(job_id))
    if not data:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify({"job": data})


def _model_api_should_attach_screen(message: str, include_flag: bool) -> bool:
    if include_flag:
        return True
    text = (message or "").lower()
    cues = (
        "screen", "screenshot", "what am i looking at", "look at this",
        "current app", "current page", "屏幕", "截图", "现在这个", "帮我看",
    )
    return any(cue in text for cue in cues)


def _model_api_context_messages(
    store: UserStore,
    api_key: str | None,
    user_message: str,
    *,
    include_screen_context: bool,
) -> tuple[list[dict], dict, list[dict[str, str]]]:
    hist, hist_err = _enclave_get_json_for_gate(
        "/v1/chat/history",
        api_key,
        {"limit": "30", "context_mode": "model_api", "context_trace": "1"},
    )
    identity_data, identity_err = _enclave_get_json_for_gate("/v1/identity/get", api_key)
    context_memories = []
    context_memory_trace = {}
    recent_messages = []
    if isinstance(hist, dict):
        context_memories = hist.get("context_memories") if isinstance(hist.get("context_memories"), list) else []
        context_memory_trace = hist.get("context_memory_trace") if isinstance(hist.get("context_memory_trace"), dict) else {}
        recent_messages = hist.get("messages") if isinstance(hist.get("messages"), list) else []

    identity = {}
    if isinstance(identity_data, dict) and isinstance(identity_data.get("identity"), dict):
        identity = identity_data["identity"]
    pending_state_updates = [
        _state_pending_public_summary(item)
        for item in _state_pending_items(store)[:5]
    ]

    screen_context = ""
    screen_images: list[dict[str, str]] = []
    if _model_api_should_attach_screen(user_message, include_screen_context):
        with store.frames_lock:
            latest = store.frames_meta[-1].copy() if store.frames_meta else None
        if latest and latest.get("id"):
            frame = _decrypt_frame_metadata_for_gate(
                store,
                str(latest.get("id")),
                api_key,
                include_image=True,
            )
            if not frame.get("error"):
                screen_context = (
                    f"Current screen app: {frame.get('app') or 'unknown'}\n"
                    f"Current screen OCR: {str(frame.get('ocr_text') or '')[:1600]}"
                )
                image_b64 = str(frame.get("image_b64") or "").strip()
                if image_b64:
                    screen_images.append({
                        "mime": str(frame.get("image_mime") or "image/jpeg"),
                        "b64": image_b64,
                        "label": "current_screen",
                    })

    identity_summary = {
        "agent_name": identity.get("agent_name", ""),
        "self_introduction": identity.get("self_introduction", ""),
        "days_with_user": identity.get("days_with_user"),
        "category": identity.get("category", ""),
        "signature": identity.get("signature", []),
        "dimensions": identity.get("dimensions", []),
    }
    context_payload = {
        "agent_profile": _model_api_agent_profile_context(store, identity),
        "identity": identity_summary,
        "context_memories": context_memories[:8],
        "context_memory_trace": context_memory_trace,
        "screen_context": screen_context,
        "screen_image_attached": bool(screen_images),
        "pending_state_updates": pending_state_updates,
        # Extended Perception: coarse, permission-gated current state (place
        # label, motion, battery, user_state, …). null fields = unauthorized or
        # stale; agent must not infer from null. See backend/perception/.
        "perception": _perception_wake_snapshot(store.user_id),
        "context_errors": {
            "history": hist_err,
            "identity": identity_err,
        },
    }
    prompt_context_payload = dict(context_payload)
    prompt_context_payload.pop("context_memory_trace", None)
    if context_memory_trace:
        prompt_selection = {}
        selected_trace = context_memory_trace.get("selected") if isinstance(context_memory_trace.get("selected"), list) else []
        query_units = context_memory_trace.get("query_units") if isinstance(context_memory_trace.get("query_units"), list) else []
        query_strong = context_memory_trace.get("query_strong_phrases") if isinstance(context_memory_trace.get("query_strong_phrases"), list) else []
        if selected_trace:
            prompt_selection["selected"] = selected_trace[:8]
        if query_units:
            prompt_selection["query_units"] = query_units[:20]
        if query_strong:
            prompt_selection["query_strong_phrases"] = query_strong[:20]
        if prompt_selection:
            prompt_context_payload["context_memory_selection"] = prompt_selection

    messages = build_model_api_foreground_chat_messages(
        context_payload=prompt_context_payload,
        recent_messages=recent_messages,
        user_message=user_message,
    )
    return messages, context_payload, screen_images


def _model_api_context_trace_summary(context_payload: dict) -> dict:
    trace = context_payload.get("context_memory_trace") if isinstance(context_payload, dict) else {}
    if not isinstance(trace, dict):
        return {}
    selected = trace.get("selected") if isinstance(trace.get("selected"), list) else []
    rejected = trace.get("rejected_sample") if isinstance(trace.get("rejected_sample"), list) else []
    summary: dict = {}
    if selected:
        summary["selected"] = selected[:8]
    if rejected:
        summary["rejected_sample"] = rejected[:8]
    query_units = trace.get("query_units") if isinstance(trace.get("query_units"), list) else []
    query_strong = trace.get("query_strong_phrases") if isinstance(trace.get("query_strong_phrases"), list) else []
    if query_units:
        summary["query_units"] = query_units[:20]
    if query_strong:
        summary["query_strong_phrases"] = query_strong[:20]
    if trace.get("mode"):
        summary["mode"] = str(trace.get("mode") or "")[:40]
    return summary


def _model_api_context_trace_for_action(
    context_payload: dict,
    *,
    context_refs: list[dict],
    web_search: dict,
    provider_reasoning: str | None = None,
) -> dict:
    info = {
        "memories": len(context_payload.get("context_memories") or []),
        "identity_loaded": bool((context_payload.get("identity") or {}).get("agent_name")),
        "screen_context": bool(context_payload.get("screen_context")),
        "context_refs": len(context_refs),
        "web_search": _model_api_web_search_trace(web_search),
    }
    memory_selection = _model_api_context_trace_summary(context_payload)
    if memory_selection:
        info["memory_selection"] = memory_selection
    if provider_reasoning is not None:
        info["provider_reasoning"] = {
            "enabled": MODEL_API_PROVIDER_REASONING_ENABLED,
            "present": bool(provider_reasoning),
            "chars": len(provider_reasoning),
        }
    return info


def _context_refs_from_payload(payload: dict) -> list[dict]:
    refs = payload.get("context_refs") or payload.get("contextRefs") or []
    if not isinstance(refs, list):
        return []
    out: list[dict] = []
    for ref in refs[:8]:
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get("type") or "").strip()
        ref_id = str(ref.get("id") or "").strip()
        if not ref_type or not ref_id:
            continue
        clean = {"type": ref_type[:40], "id": ref_id[:160]}
        if ref.get("title"):
            clean["title"] = str(ref.get("title") or "")[:240]
        out.append(clean)
    return out


MODEL_API_STATE_DIRECT_CONFIDENCE = float(os.environ.get("FEEDLING_MODEL_API_STATE_DIRECT_CONFIDENCE", "0.85"))
MODEL_API_STATE_CONFIRM_CONFIDENCE = float(os.environ.get("FEEDLING_MODEL_API_STATE_CONFIRM_CONFIDENCE", "0.55"))
MODEL_API_CAPTURE_TURN_INTERVAL = max(12, int(os.environ.get("FEEDLING_MODEL_API_CAPTURE_TURN_INTERVAL", "24")))
MODEL_API_CONSOLIDATE_TURN_INTERVAL = max(60, int(os.environ.get("FEEDLING_MODEL_API_CONSOLIDATE_TURN_INTERVAL", "80")))
MODEL_API_CONSOLIDATE_MIN_INTERVAL_SEC = max(
    3600,
    int(os.environ.get("FEEDLING_MODEL_API_CONSOLIDATE_MIN_INTERVAL_SEC", str(12 * 3600))),
)
MODEL_API_WEB_SEARCH_ENABLED = os.environ.get("FEEDLING_MODEL_API_WEB_SEARCH_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
MODEL_API_WEB_SEARCH_MAX_QUERIES = max(1, min(3, int(os.environ.get("FEEDLING_MODEL_API_WEB_SEARCH_MAX_QUERIES", "2"))))
MODEL_API_WEB_SEARCH_MAX_RESULTS = max(1, min(8, int(os.environ.get("FEEDLING_MODEL_API_WEB_SEARCH_MAX_RESULTS", "5"))))
MODEL_API_WEB_SEARCH_TIMEOUT_SEC = max(2.0, min(20.0, float(os.environ.get("FEEDLING_MODEL_API_WEB_SEARCH_TIMEOUT_SEC", "8"))))
MODEL_API_PROVIDER_REASONING_ENABLED = os.environ.get("FEEDLING_MODEL_API_PROVIDER_REASONING_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
MODEL_API_PROVIDER_REASONING_MAX_CHARS = max(400, min(6000, int(os.environ.get("FEEDLING_MODEL_API_PROVIDER_REASONING_MAX_CHARS", "2400"))))

_STATE_PENDING_BLOB = "model_api_state_pending"
_model_api_recap_active_users: set[str] = set()
_model_api_recap_active_lock = threading.Lock()
_model_api_state_active_users: set[str] = set()
_model_api_state_active_lock = threading.Lock()


def _model_api_turn_count(store: UserStore) -> int:
    with store.chat_lock:
        return sum(
            1
            for msg in store.chat_messages
            if isinstance(msg, dict)
            and msg.get("role") == "user"
            and msg.get("source") == "model_api"
        )


def _state_lang_zh(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _state_pending_items(store: UserStore) -> list[dict]:
    data = db.get_blob(store.user_id, _STATE_PENDING_BLOB)
    items = data.get("items") if isinstance(data, dict) else []
    now = time.time()
    clean = [
        item for item in items
        if isinstance(item, dict) and float(item.get("expires_at") or 0) > now
    ]
    if clean != items:
        db.set_blob(store.user_id, _STATE_PENDING_BLOB, {"items": clean})
    return clean


def _state_save_pending_items(store: UserStore, items: list[dict]) -> None:
    db.set_blob(store.user_id, _STATE_PENDING_BLOB, {"items": items[:10], "updated_at": _now_iso()})


def _state_add_pending(store: UserStore, runtime_actions: list[dict], *, user_message_id: str, prompt: str) -> list[dict]:
    existing = _state_pending_items(store)
    now = time.time()
    pending: list[dict] = []
    for runtime_action in runtime_actions[:5]:
        item = {
            "id": f"rta_{uuid.uuid4().hex[:12]}",
            "created_at": _now_iso(),
            "expires_at": now + 86400,
            "source": "model_api_chat",
            "user_message_id": user_message_id,
            "prompt": prompt[:1000],
            "runtime_action": runtime_action,
        }
        pending.append(item)
    _state_save_pending_items(store, pending + existing)
    return pending


def _state_clear_pending(store: UserStore, pending_ids: set[str]) -> None:
    if not pending_ids:
        return
    items = [item for item in _state_pending_items(store) if str(item.get("id") or "") not in pending_ids]
    _state_save_pending_items(store, items)


def _append_state_receipt(store: UserStore, receipt: dict) -> dict:
    record = {
        "id": receipt.get("id") or f"sr_{uuid.uuid4().hex[:14]}",
        "ts": time.time(),
        "created_at": _now_iso(),
        "source": "model_api_chat",
        "status": str(receipt.get("status") or "ok")[:80],
    }
    for key in (
        "background_execution", "results", "effects", "pending", "error",
        "user_message_id", "assistant_message_id", "summary",
    ):
        if key in receipt:
            record[key] = receipt[key]
    db.log_append(store.user_id, "state_receipts", record, ts=record["ts"], item_key=record["id"])
    return record


def _load_state_receipts(store: UserStore, limit: int = 30) -> list[dict]:
    entries = db.log_read(store.user_id, "state_receipts", limit=max(1, min(limit, 100)))
    entries.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return entries


def _state_memory_candidate_from_moment(moment: dict, inner: dict, score: float) -> dict:
    return {
        "id": str(moment.get("id") or ""),
        "type": str(moment.get("type") or inner.get("type") or "fact"),
        "title": str(inner.get("title") or "")[:220],
        "description": str(inner.get("description") or "")[:1000],
        "her_quote": str(inner.get("her_quote") or "")[:500],
        "context": str(inner.get("context") or "")[:500],
        "occurred_at": str(moment.get("occurred_at") or ""),
        "created_at": str(moment.get("created_at") or ""),
        "updated_at": str(moment.get("updated_at") or ""),
        "source": str(moment.get("source") or ""),
        "score": round(float(score), 4),
    }


def _model_api_state_memory_candidates(
    store: UserStore,
    api_key: str | None,
    message: str,
    context_refs: list[dict],
    *,
    limit: int = 12,
) -> list[dict]:
    ref_ids = {
        str(ref.get("id") or "")
        for ref in context_refs
        if ref.get("type") == "memory" and ref.get("id")
    }
    candidates: list[dict] = []
    for moment in _active_memory_moments(_load_moments(store)):
        if not isinstance(moment, dict):
            continue
        inner, err = _memory_plain_from_envelope(moment, api_key)
        if inner is None:
            continue
        merged = {
            **inner,
            "id": moment.get("id", ""),
            "type": moment.get("type") or inner.get("type"),
            "source": moment.get("source", ""),
            "occurred_at": moment.get("occurred_at", ""),
            "created_at": moment.get("created_at", ""),
        }
        if moment.get("id") in ref_ids:
            details = {
                "score": 1.0,
                "confidence": "strong",
                "reason": "user_selected_context_ref",
                "matched_units": [],
            }
        else:
            details = memory_relevance_details(message, merged)
        score = float(details.get("score") or 0.0)
        confidence = str(details.get("confidence") or "none")
        if moment.get("id") in ref_ids or (confidence in {"strong", "medium"} and score >= 0.35):
            candidate = _state_memory_candidate_from_moment(moment, inner, score)
            candidate["confidence"] = confidence
            candidate["reason"] = str(details.get("reason") or "")[:120]
            candidate["matched_units"] = list(details.get("matched_units") or [])[:8]
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.get("score", 0), item.get("occurred_at", "")), reverse=True)
    return candidates[:limit]


def _model_api_plan_state_actions(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    message: str,
    context_refs: list[dict],
    identity: dict,
) -> dict:
    pending_items = _state_pending_items(store)
    memory_candidates = _model_api_state_memory_candidates(store, api_key, message, context_refs)
    raw_actions: list[dict] = []
    runtime_error = ""
    parsed: dict = {}
    try:
        result = chat_completion(
            runtime,
            build_hosted_runtime_background_execution_messages(
                user_message=message,
                identity=identity,
                memory_candidates=memory_candidates,
                context_refs=context_refs,
                pending_items=pending_items,
            ),
            max_tokens=900,
            temperature=0.0,
            timeout=35.0,
            response_format=HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
        )
        parsed_json = _json_from_model_text(str(result.get("reply") or ""))
        parsed = parsed_json if isinstance(parsed_json, dict) else {}
        if isinstance(parsed.get("actions"), list):
            raw_actions = [a for a in parsed.get("actions") if isinstance(a, dict)]
    except Exception as e:
        runtime_error = f"{type(e).__name__}:{str(e)[:240]}"

    pending_decision, pending_ids = coerce_hosted_runtime_pending_decision(parsed, pending_items)
    if pending_decision:
        id_set = set(pending_ids)
        if pending_decision == "reject":
            _state_clear_pending(store, id_set)
            return {
                "triggered": True,
                "method": HOSTED_RUNTIME_PENDING_REJECT_METHOD,
                "actions": [],
                "memory_candidates": memory_candidates,
                "pending_ids": pending_ids,
                "error": runtime_error,
            }
        confirmed: list[dict] = []
        for item in pending_items:
            if str(item.get("id") or "") not in id_set:
                continue
            runtime_action = item.get("runtime_action") if isinstance(item.get("runtime_action"), dict) else {}
            runtime_action = dict(runtime_action)
            runtime_action["requires_confirmation"] = False
            runtime_action["confirmed_pending_id"] = str(item.get("id") or "")
            confirmed.append(runtime_action)
        _state_clear_pending(store, id_set)
        return {
            "triggered": True,
            "method": HOSTED_RUNTIME_PENDING_CONFIRM_METHOD,
            "actions": confirmed,
            "memory_candidates": memory_candidates,
            "pending_ids": pending_ids,
            "error": runtime_error,
        }

    planned: list[dict] = []
    seen = set()
    for raw in raw_actions[:12]:
        coerced = coerce_hosted_runtime_action(
            raw,
            memory_candidates,
            direct_confidence=MODEL_API_STATE_DIRECT_CONFIDENCE,
        )
        if not coerced:
            continue
        key = json.dumps({
            "domain": coerced.get("domain"),
            "executor": coerced.get("executor_action"),
        }, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        if coerced.get("confidence", 0.0) < MODEL_API_STATE_CONFIRM_CONFIDENCE:
            continue
        planned.append(coerced)

    return {
        "triggered": bool(planned or raw_actions or pending_items or runtime_error),
        "method": HOSTED_RUNTIME_ACTION_METHOD if raw_actions else HOSTED_RUNTIME_NOOP_METHOD,
        "actions": planned[:8],
        "memory_candidates": memory_candidates,
        "error": runtime_error,
    }


def _execute_model_api_state_plan(
    store: UserStore,
    api_key: str | None,
    plan: dict,
    *,
    user_message_id: str,
    user_message: str,
) -> tuple[list[dict], list[dict], list[dict], dict | None]:
    runtime_actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    if not runtime_actions and not plan.get("pending_ids"):
        return [], [], [], None

    direct = [a for a in runtime_actions if not a.get("requires_confirmation")]
    needs_confirm = [a for a in runtime_actions if a.get("requires_confirmation")]
    pending = _state_add_pending(
        store,
        needs_confirm,
        user_message_id=user_message_id,
        prompt=user_message,
    ) if needs_confirm else []

    identity_actions: list[dict] = []
    memory_actions: list[dict] = []
    for planned in direct:
        executor_action = planned.get("executor_action") if isinstance(planned.get("executor_action"), dict) else {}
        if not executor_action:
            continue
        executor_action = dict(executor_action)
        executor_action.setdefault("state_action_id", planned.get("action_id", ""))
        if planned.get("domain") == "identity":
            identity_actions.append(executor_action)
        elif planned.get("domain") == "memory":
            executor_action.setdefault("source_chat_message_ids", [user_message_id])
            memory_actions.append(executor_action)

    effects: list[dict] = []
    identity_results: list[dict] = []
    memory_results: list[dict] = []
    status = "ok"
    error = ""
    if identity_actions:
        body, action_status = _execute_identity_actions(store, api_key, identity_actions)
        identity_results = body.get("results") or []
        effects.extend(body.get("effects") or [])
        if action_status >= 400:
            status = "failed"
            error = body.get("error", "identity_action_failed")
    if status == "ok" and memory_actions:
        body, action_status = _execute_memory_actions(store, api_key, memory_actions)
        memory_results = body.get("results") or []
        effects.extend(body.get("effects") or [])
        if action_status >= 400:
            status = "failed"
            error = body.get("error", "memory_action_failed")

    background_execution = hosted_runtime_background_trace(
        status=status,
        method=str(plan.get("method") or ""),
        triggered=bool(plan.get("triggered")),
        error=str(plan.get("error") or ""),
    )
    receipt = _append_state_receipt(store, {
        "status": status,
        "background_execution": {
            **background_execution,
            "pending_decision": plan.get("method", "") if "pending_" in str(plan.get("method", "")) else "",
        },
        "results": {
            "identity": identity_results,
            "memory": memory_results,
        },
        "effects": effects,
        "pending": [
            _state_pending_public_summary(item)
            for item in pending
        ],
        "error": error,
        "user_message_id": user_message_id,
    })
    return effects, identity_results, memory_results, receipt


def _state_receipt_prompt_payload(receipt: dict | None) -> dict:
    if not receipt:
        return {}
    return {
        "status": receipt.get("status", ""),
        "results": receipt.get("results", {}),
        "effects": receipt.get("effects", []),
        "pending": receipt.get("pending", []),
        "error": receipt.get("error", ""),
    }


def _state_preview_value(value, max_chars: int = 260):
    if isinstance(value, list):
        out = []
        for item in value[:5]:
            text = str(item or "").strip()
            if text:
                out.append(text[:max_chars])
        return out
    if isinstance(value, dict):
        return {
            str(k): _state_preview_value(v, max_chars)
            for k, v in list(value.items())[:8]
            if str(v or "").strip()
        }
    text = str(value or "").strip()
    return text[:max_chars]


def _state_pending_public_summary(item: dict) -> dict:
    runtime_action = item.get("runtime_action") if isinstance(item.get("runtime_action"), dict) else {}
    executor = runtime_action.get("executor_action") if isinstance(runtime_action.get("executor_action"), dict) else {}
    preview = runtime_action.get("target_preview") if isinstance(runtime_action.get("target_preview"), dict) else {}
    runtime_target = runtime_action.get("target") if isinstance(runtime_action.get("target"), dict) else {}
    action = str(runtime_action.get("runtime_type") or executor.get("type") or "")
    result = {
        "id": str(item.get("id") or ""),
        "action": action,
        "confidence": runtime_action.get("confidence", 0),
        "reason": str(runtime_action.get("reason") or executor.get("reason") or "")[:300],
    }
    if action.startswith("identity"):
        result["domain"] = "identity"
        result["target"] = "Identity"
        if executor.get("type") == "identity.dimension_nudge":
            result["changes"] = [{
                "field": "dimension",
                "dimension": str(executor.get("dimension") or ""),
                "delta": executor.get("delta"),
            }]
        else:
            patch = executor.get("patch") if isinstance(executor.get("patch"), dict) else {}
            result["changes"] = [
                {"field": str(k), "to": _state_preview_value(v)}
                for k, v in patch.items()
            ][:8]
        return result

    result["domain"] = "memory"
    if executor.get("type") in {"memory.add", "memory.add_correction"}:
        memory = executor.get("memory") if isinstance(executor.get("memory"), dict) else {}
        result["target"] = str(memory.get("title") or "New Memory Garden card")[:180]
        result["new_memory"] = {
            "type": str(memory.get("type") or "")[:80],
            "title": str(memory.get("title") or "")[:180],
            "description": str(memory.get("description") or "")[:600],
        }
        return result

    memory_id = str(executor.get("memory_id") or runtime_target.get("memory_id") or "")
    result["memory_id"] = memory_id
    result["target"] = str(preview.get("title") or memory_id or "Memory Garden card")[:180]
    if preview:
        result["current_memory"] = {
            "title": str(preview.get("title") or "")[:180],
            "description": str(preview.get("description") or "")[:600],
            "type": str(preview.get("type") or "")[:80],
            "occurred_at": str(preview.get("occurred_at") or "")[:80],
        }
    if executor.get("type") == "memory.delete":
        result["changes"] = [{"field": "delete", "to": "remove this card"}]
        return result
    patch = executor.get("patch") if isinstance(executor.get("patch"), dict) else {}
    result["changes"] = [
        {"field": str(k), "to": _state_preview_value(v, 600)}
        for k, v in patch.items()
    ][:8]
    return result


def _pending_state_confirmation_messages(
    user_message: str,
    pending: list[dict],
    identity: dict,
) -> list[dict]:
    payload = {
        "latest_user_message": user_message[:2000],
        "identity": {
            "agent_name": str(identity.get("agent_name") or ""),
            "tone_style": str(identity.get("tone_style") or ""),
            "signature": identity.get("signature") if isinstance(identity.get("signature"), list) else [],
            "language_preference": str(identity.get("language_preference") or ""),
        },
        "pending_updates": [_state_pending_public_summary(item) for item in pending[:5]],
        "allowed_user_replies": ["确认", "取消", "confirm", "cancel", "or a natural correction"],
    }
    return build_model_api_pending_confirmation_messages(payload)


def _model_api_pending_confirmation_reply(
    runtime: ProviderConfig,
    user_message: str,
    pending: list[dict],
    identity: dict,
) -> tuple[str, str]:
    if not pending:
        return "", ""
    try:
        result = chat_completion(
            runtime,
            _pending_state_confirmation_messages(user_message, pending, identity),
            max_tokens=700,
            temperature=0.7,
            timeout=45.0,
            response_format=HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
        )
        reply, thinking = _model_api_parse_turn_reply(str(result.get("reply") or ""))
        reply = reply.strip()
        if not reply:
            return "", ""
        banned = (
            "我找到了可能要修改",
            "可能要修改的身份或记忆",
            "需要你确认",
            "I found a likely identity or memory update",
            "I found a possible identity or memory change",
        )
        if any(phrase in reply for phrase in banned):
            return "", ""
        return reply, thinking
    except Exception:
        return "", ""


def _append_model_api_runtime_followup_message(
    store: UserStore,
    *,
    reply: str,
    thinking_summary: str = "",
    push_data: dict | None = None,
) -> dict | None:
    text = str(reply or "").strip()
    if not text:
        return None
    assistant_env, env_err = _build_shared_envelope_for_store(store, text.encode("utf-8"))
    if assistant_env is None:
        print(f"[model_api_state:{store.user_id}] followup_envelope_failed detail={env_err}")
        return None
    extra: dict = {}
    thinking = str(thinking_summary or "").strip()
    if thinking:
        thinking_env, thinking_err = _build_shared_envelope_for_store(store, thinking.encode("utf-8"))
        if thinking_env is not None:
            extra.update(_chat_thinking_extra_from_envelope(thinking_env))
        else:
            print(f"[model_api_state:{store.user_id}] followup_thinking_envelope_failed detail={thinking_err}")
    row = store.append_chat("openclaw", "model_api", assistant_env, extra=extra)
    store.notify_chat_waiters()
    delivery_fields = _deliver_ai_message_push_if_background(
        store,
        body=text,
        title="IO",
        data=push_data or {"source": "model_api_state"},
        visual_state="reply",
    )
    updated = store.update_chat_message_metadata(row["id"], delivery_fields)
    return updated or row


def _run_model_api_state_action_job(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    trace_id: str,
    *,
    user_message: str,
    user_message_id: str,
    assistant_message_id: str,
    context_refs: list[dict],
) -> None:
    started = time.time()
    try:
        _patch_model_api_action_trace(store, trace_id, {
            "status": "processing",
            "progress": 20,
        })
        identity_for_plan = {}
        identity_plan_data, _ = _enclave_get_json_for_gate("/v1/identity/get", api_key)
        if isinstance(identity_plan_data, dict) and isinstance(identity_plan_data.get("identity"), dict):
            identity_for_plan = identity_plan_data["identity"]
        state_plan = _model_api_plan_state_actions(
            store,
            api_key,
            runtime,
            user_message,
            context_refs,
            identity_for_plan,
        )
        effects, identity_results, memory_results, state_receipt = _execute_model_api_state_plan(
            store,
            api_key,
            state_plan,
            user_message_id=user_message_id,
            user_message=user_message,
        )
        pending_items = _state_pending_items(store)
        just_pending = bool(state_receipt and (state_receipt.get("pending") or []) and not effects)
        followup_row = None
        if just_pending:
            reply, thinking_summary = _model_api_pending_confirmation_reply(
                runtime,
                user_message,
                pending_items,
                identity_for_plan,
            )
            if reply:
                followup_row = _append_model_api_runtime_followup_message(
                    store,
                    reply=reply,
                    thinking_summary=thinking_summary,
                    push_data={"source": "model_api_state", "kind": "pending_confirmation"},
                )
        if state_receipt and state_receipt.get("status") == "failed":
            background_execution = hosted_runtime_background_trace(
                status="failed",
                method=str(state_plan.get("method") or ""),
                triggered=bool(state_plan.get("triggered")),
                error=str(state_plan.get("error") or state_receipt.get("error") or ""),
            )
            _patch_model_api_action_trace(store, trace_id, {
                "status": "failed",
                "progress": 100,
                "provider": runtime.provider,
                "model": runtime.model,
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
                "state_receipt_id": state_receipt.get("id", ""),
                "background_execution": background_execution,
                "effects": effects,
                "identity_actions": identity_results,
                "memory_actions": memory_results,
                "error": state_receipt.get("error", "state_action_failed"),
                "duration_ms": int((time.time() - started) * 1000),
            })
            return
        if not effects and not just_pending and not state_plan.get("pending_ids"):
            status = "skipped"
            reason = "no_state_action"
        elif just_pending:
            status = "pending_confirmation"
            reason = "needs_user_confirmation"
        else:
            status = "completed"
            reason = "state_actions_applied"
        background_execution = hosted_runtime_background_trace(
            status=status,
            method=str(state_plan.get("method") or ""),
            triggered=bool(state_plan.get("triggered")),
            error=str(state_plan.get("error") or ""),
        )
        _patch_model_api_action_trace(store, trace_id, {
            "status": status,
            "progress": 100,
            "provider": runtime.provider,
            "model": runtime.model,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "state_receipt_id": (state_receipt or {}).get("id", ""),
            "background_execution": background_execution,
            "effects": effects,
            "identity_actions": identity_results,
            "memory_actions": memory_results,
            "context": {"context_refs": len(context_refs)},
            "reason": reason,
            "duration_ms": int((time.time() - started) * 1000),
        })
        if followup_row:
            _patch_model_api_action_trace(store, trace_id, {
                "assistant_message_id": followup_row.get("id", assistant_message_id),
            })
    except Exception as e:
        _patch_model_api_action_trace(store, trace_id, {
            "status": "failed",
            "progress": 100,
            "provider": runtime.provider,
            "model": runtime.model,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "error": f"{type(e).__name__}:{str(e)[:300]}",
            "duration_ms": int((time.time() - started) * 1000),
        })
    finally:
        with _model_api_state_active_lock:
            _model_api_state_active_users.discard(store.user_id)


def _start_model_api_state_action_job(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    *,
    user_message: str,
    user_message_id: str,
    assistant_message_id: str,
    context_refs: list[dict],
    run_sync: bool = False,
) -> dict:
    with _model_api_state_active_lock:
        if store.user_id in _model_api_state_active_users:
            return {
                "status": "skipped",
                "reason": "background_execution_already_running",
                "actions_written": 0,
            }
        _model_api_state_active_users.add(store.user_id)
    trace = _append_model_api_action_trace(store, {
        "status": "queued",
        "provider": runtime.provider,
        "model": runtime.model,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "background_execution": hosted_runtime_background_trace(
            status="queued",
            method=HOSTED_RUNTIME_BACKGROUND_METHOD,
        ),
        "context": {"context_refs": len(context_refs)},
        "reason": "queued_after_foreground_reply",
        "progress": 0,
    })
    args = (store, api_key, runtime, trace["trace_id"])
    kwargs = {
        "user_message": user_message,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "context_refs": context_refs,
    }
    if run_sync:
        _run_model_api_state_action_job(*args, **kwargs)
        latest = db.log_patch_item(
            store.user_id,
            MODEL_API_ACTION_TRACE_STREAM,
            trace["trace_id"],
            {},
        )
        return latest or trace
    thread = threading.Thread(
        target=_run_model_api_state_action_job,
        args=args,
        kwargs=kwargs,
        daemon=True,
    )
    thread.start()
    return trace


def _model_api_turn_contract_message() -> dict:
    return hosted_runtime_companion_turn_contract_message()


def _sanitize_visible_thinking_summary(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    blocked = re.compile(
        r"(system prompt|developer message|chain[-\s]*of[-\s]*thought|"
        r"modelUsage|terminal_reason|permission_denials|cache_read|"
        r"cache_creation|session_id|uuid|costUSD|input_tokens|output_tokens)",
        re.IGNORECASE,
    )
    lines: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or blocked.search(line):
            continue
        line = re.sub(r"^[`#>*\-\s]+", "", line).strip()
        if line:
            lines.append(line[:220])
        if len(lines) >= 4:
            break
    return "\n".join(lines).strip()[:700]


def _sanitize_provider_reasoning_text(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    blocked = re.compile(
        r"(system prompt|developer message|api[_\s-]*key|authorization|bearer\s+|"
        r"sk-[A-Za-z0-9]|sk-or-[A-Za-z0-9]|x-api-key|password|secret|session_id|"
        r"input_tokens|output_tokens|cache_creation|cache_read|costUSD)",
        re.IGNORECASE,
    )
    lines: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or blocked.search(line):
            continue
        line = re.sub(r"^[`#>*\-\s]+", "", line).strip()
        if line:
            lines.append(line[:700])
        if len("\n".join(lines)) >= MODEL_API_PROVIDER_REASONING_MAX_CHARS:
            break
    return "\n".join(lines).strip()[:MODEL_API_PROVIDER_REASONING_MAX_CHARS]


def _model_api_extract_web_search_requests(parsed: Any) -> list[dict]:
    return extract_model_api_web_search_requests(
        parsed,
        enabled=MODEL_API_WEB_SEARCH_ENABLED,
        max_queries=MODEL_API_WEB_SEARCH_MAX_QUERIES,
    )


def _model_api_parse_turn_output(raw_reply: str) -> tuple[str, str, list[dict]]:
    raw = str(raw_reply or "").strip()
    if not raw:
        return "", "", []
    try:
        parsed = _json_from_model_text(raw)
    except Exception:
        return raw, "", []

    def text_from(value) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("reply", "response", "content", "text", "result", "message", "answer"):
                nested = text_from(value.get(key))
                if nested:
                    return nested
        if isinstance(value, list):
            parts = [text_from(item) for item in value]
            return "\n".join(part for part in parts if part).strip()
        return ""

    if isinstance(parsed, dict):
        reply = ""
        for key in ("reply", "response", "content", "text", "result", "message", "answer"):
            reply = text_from(parsed.get(key))
            if reply:
                break
        thinking = ""
        for key in (
            "context_summary",
            "runtime_summary",
            "action_summary",
            "thinking_summary",
            "reasoning_summary",
            "thought_summary",
            "visible_reasoning",
        ):
            thinking = _sanitize_visible_thinking_summary(str(parsed.get(key) or ""))
            if thinking and _model_api_is_generic_context_summary(thinking):
                thinking = ""
            if thinking:
                break
        return reply or "", thinking, _model_api_extract_web_search_requests(parsed)
    if isinstance(parsed, list):
        reply = text_from(parsed)
        return reply or raw, "", []
    return raw, "", []


def _model_api_parse_turn_reply(raw_reply: str) -> tuple[str, str]:
    reply, thinking, _ = _model_api_parse_turn_output(raw_reply)
    return reply, thinking


def _model_api_is_generic_context_summary(text: str) -> bool:
    lines = [
        line.strip(" 。.").strip()
        for line in str(text or "").splitlines()
        if line.strip()
    ]
    if not lines:
        return True
    generic_patterns = [
        r"^参考了\s*\d+\s*条相关记忆$",
        r"^对齐了当前\s*Identity\s*设定$",
        r"^整理了这轮消息后回复$",
        r"^Checked\s+\d+\s+relevant memories$",
        r"^Used the current identity card$",
        r"^Read this turn and composed the reply$",
    ]
    return all(
        any(re.search(pattern, line, re.IGNORECASE) for pattern in generic_patterns)
        for line in lines
    )


def _model_api_capture_prompt(user_message: str, assistant_reply: str, context_payload: dict) -> list[dict]:
    return build_model_api_memory_capture_messages(
        user_message=user_message,
        assistant_reply=assistant_reply,
        context_payload=context_payload,
    )


def _model_api_run_memory_capture(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    *,
    user_message: str,
    assistant_reply: str,
    user_message_id: str,
    assistant_message_id: str,
    context_payload: dict,
    job_id: str | None = None,
) -> dict:
    job_base = {
        "mode": "running",
        "source_chat_message_ids": [user_message_id, assistant_message_id],
        "message_chars": len(user_message),
        "reply_chars": len(assistant_reply),
    }

    def finish(entry: dict) -> dict:
        if job_id:
            patched = _model_api_patch_recap_job(store, job_id, {**job_base, **entry})
            return patched or {**job_base, **entry, "job_id": job_id}
        return _append_memory_capture_job(store, {**job_base, **entry})

    if os.environ.get("FEEDLING_MODEL_API_MEMORY_CAPTURE", "1").strip().lower() in {"0", "false", "off", "no"}:
        return finish({"status": "skipped", "error": "disabled"})
    try:
        result = chat_completion(
            runtime,
            _model_api_capture_prompt(user_message, assistant_reply, context_payload),
            max_tokens=900,
            temperature=0.1,
            timeout=30.0,
        )
        raw = _json_from_model_text(str(result.get("reply") or ""))
        memories = raw.get("memories") if isinstance(raw, dict) else []
        if not isinstance(memories, list):
            memories = []
        actions: list[dict] = []
        seen_titles = set()
        for item in memories[:4]:
            if not isinstance(item, dict):
                continue
            mem_type = str(item.get("type") or "fact").strip().lower()
            if mem_type not in {"fact", "event", "quote", "moment"}:
                continue
            title = _memory_action_text(item.get("title"), 180)
            description = str(item.get("description") or "").strip()[:1200]
            if len(title) < 4 or (not description and mem_type != "quote"):
                continue
            key = title.lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            actions.append({
                "type": "memory.add",
                "memory": {
                    "type": mem_type,
                    "title": title,
                    "description": description,
                    "occurred_at": _memory_action_text(item.get("occurred_at") or date.today().isoformat(), 80),
                    "source": "model_api_capture",
                    "her_quote": str(item.get("her_quote") or "").strip()[:1000],
                    "context": str(item.get("context") or "").strip()[:1000],
                },
                "reason": "Captured from recent chat.",
                "capture_mode": "running",
                "source_chat_message_ids": [user_message_id, assistant_message_id],
            })
        if not actions:
            return finish({"status": "completed", "actions_planned": 0, "actions_written": 0})
        body, status = _execute_memory_actions(store, api_key, actions)
        if status >= 400:
            return finish({
                "status": "failed",
                "actions_planned": len(actions),
                "actions_written": len(body.get("effects") or []),
                "effects": body.get("effects") or [],
                "error": body.get("error", "memory_action_failed"),
            })
        return finish({
            "status": "completed",
            "actions_planned": len(actions),
            "actions_written": len(body.get("effects") or []),
            "effects": body.get("effects") or [],
        })
    except Exception as e:
        return finish({
            "status": "failed",
            "error": f"{type(e).__name__}:{str(e)[:300]}",
        })


def _start_model_api_memory_capture_job(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    *,
    user_message: str,
    assistant_reply: str,
    user_message_id: str,
    assistant_message_id: str,
    context_payload: dict,
    turn_count: int,
    run_sync: bool = False,
) -> dict:
    job = _append_memory_capture_job(store, {
        "mode": "running",
        "status": "queued",
        "reason": f"cadence:{MODEL_API_CAPTURE_TURN_INTERVAL}",
        "turn_count": turn_count,
        "progress": 0,
        "source_chat_message_ids": [user_message_id, assistant_message_id],
        "message_chars": len(user_message),
        "reply_chars": len(assistant_reply),
    })
    kwargs = {
        "user_message": user_message,
        "assistant_reply": assistant_reply,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "context_payload": context_payload,
        "job_id": job["job_id"],
    }
    if run_sync:
        return _model_api_run_memory_capture(store, api_key, runtime, **kwargs)
    thread = threading.Thread(
        target=_model_api_run_memory_capture,
        args=(store, api_key, runtime),
        kwargs=kwargs,
        daemon=True,
    )
    thread.start()
    return job


def _model_api_latest_recap_job(store: UserStore) -> dict | None:
    jobs = [
        job for job in db.log_read(store.user_id, "memory_capture_jobs", limit=60)
        if isinstance(job, dict) and str(job.get("mode") or "") == "recap"
    ]
    if not jobs:
        return None
    jobs.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return jobs[0]


def _model_api_recent_recap_chat(store: UserStore, api_key: str | None, limit: int = 160) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    hist, hist_err = _enclave_get_json_for_gate(
        "/v1/chat/history",
        api_key,
        {
            "limit": str(max(20, min(limit, 200))),
            "include_image_body": "false",
            "context_mode": "model_api",
        },
    )
    if hist_err:
        warnings.append(f"history_read:{hist_err[:180]}")
    raw_messages = hist.get("messages") if isinstance(hist, dict) and isinstance(hist.get("messages"), list) else []
    messages: list[dict] = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        content = str(msg.get("content") or "").strip()
        if not content or _looks_like_import_artifact(content):
            continue
        role = "user" if msg.get("role") == "user" else "assistant"
        source = str(msg.get("source") or "")
        if source and source != "model_api":
            continue
        messages.append({
            "id": str(msg.get("id") or ""),
            "role": role,
            "content": content[:4000],
            "ts": msg.get("ts"),
            "source": "model_api_live",
        })
    return messages, warnings


def _model_api_plain_memory_cards(store: UserStore, api_key: str | None) -> list[dict]:
    cards: list[dict] = []
    for moment in _active_memory_moments(_load_moments(store)):
        if not isinstance(moment, dict):
            continue
        inner, _ = _memory_plain_from_envelope(moment, api_key)
        if inner is None:
            continue
        card = {
            "id": str(moment.get("id") or ""),
            "type": str(moment.get("type") or inner.get("type") or "fact"),
            "title": str(inner.get("title") or "")[:180],
            "description": str(inner.get("description") or "")[:2000],
            "occurred_at": str(moment.get("occurred_at") or ""),
            "created_at": str(moment.get("created_at") or ""),
            "source": str(moment.get("source") or ""),
        }
        for key in ("her_quote", "context", "linked_dimension"):
            value = str(inner.get(key) or "").strip()
            if value:
                card[key] = value
        cards.append(card)
    return cards


def _memory_quality_card_issues(
    moment: dict,
    inner: dict,
    *,
    archive_language: str = "",
) -> list[str]:
    title = str(inner.get("title") or "").strip()
    desc = str(inner.get("description") or "").strip()
    context = str(inner.get("context") or "").strip()
    mem_type = str(moment.get("type") or inner.get("type") or "fact")
    issues: list[str] = []
    if _GENERIC_IMPORT_TITLE_RE.match(title):
        issues.append("generic_import_title")
    if any(_looks_like_import_artifact(value) for value in (title, desc, context) if value):
        issues.append("raw_import_artifact")
    if _looks_like_low_value_import_card(title, desc, mem_type):
        issues.append("low_value_content")
    if str(archive_language).lower().startswith("zh") and desc and _english_only_for_zh(title + "\n" + desc):
        issues.append("language_mismatch")
    return list(dict.fromkeys(issues))


def _model_api_memory_quality_scan(
    store: UserStore,
    *,
    api_key: str | None,
    max_cards: int = 1000,
    fast: bool = False,
) -> dict:
    archive_language = _get_user_archive_language(store.user_id) or ""
    moments = _active_memory_moments(_load_moments(store))
    scanned: list[dict] = []
    noisy: list[dict] = []
    decrypt_errors = 0
    for moment in moments[:max(1, max_cards)]:
        if not isinstance(moment, dict):
            continue
        inner, err = _memory_plain_from_envelope(moment, api_key)
        if inner is None:
            decrypt_errors += 1
            continue
        issues = _memory_quality_card_issues(moment, inner, archive_language=archive_language)
        card = {
            "id": str(moment.get("id") or ""),
            "type": str(moment.get("type") or inner.get("type") or ""),
            "title": str(inner.get("title") or "")[:180],
            "description": str(inner.get("description") or "")[:1200],
            "occurred_at": str(moment.get("occurred_at") or ""),
            "created_at": str(moment.get("created_at") or ""),
            "source": str(moment.get("source") or ""),
            "issues": issues,
        }
        scanned.append(card)
        if issues:
            noisy.append(card)

    duplicate_ids: set[str] = set()
    seen: list[tuple[str, set[str], str]] = []
    for card in scanned:
        desc = str(card.get("description") or "")
        tokens = _memory_similarity_tokens(desc)
        norm = _normalize_card_similarity_text(desc)
        if not norm:
            continue
        for prev_norm, prev_tokens, prev_id in seen[-250 if fast else -800:]:
            if norm == prev_norm or norm[:120] == prev_norm[:120] or _token_jaccard(tokens, prev_tokens) >= 0.72:
                duplicate_ids.add(str(card.get("id") or ""))
                break
        seen.append((norm, tokens, str(card.get("id") or "")))

    by_id = {str(item.get("id") or ""): item for item in noisy}
    for card in scanned:
        cid = str(card.get("id") or "")
        if cid in duplicate_ids and cid not in by_id:
            copy_card = dict(card)
            copy_card["issues"] = list(dict.fromkeys((copy_card.get("issues") or []) + ["duplicate_or_near_duplicate"]))
            by_id[cid] = copy_card
    noisy = list(by_id.values())
    noisy.sort(key=lambda item: (len(item.get("issues") or []), item.get("created_at") or ""), reverse=True)
    issue_count = sum(len(item.get("issues") or []) for item in noisy)
    warning = None
    if noisy:
        warning = "memory_quality_warning"
    return {
        "scanned": len(scanned),
        "total_active": len(moments),
        "decrypt_errors": decrypt_errors,
        "issue_count": issue_count,
        "noisy_count": len(noisy),
        "duplicate_count": len(duplicate_ids),
        "warning": warning,
        "noisy_ids": [str(item.get("id") or "") for item in noisy if item.get("id")],
        "issues": noisy[:40],
    }


def _archive_model_api_memory_cards(
    store: UserStore,
    memory_ids: list[str],
    *,
    reason: str,
    job_id: str,
) -> int:
    target_ids = {str(mid) for mid in memory_ids if str(mid).strip()}
    if not target_ids:
        return 0
    moments = _load_moments(store)
    archived = 0
    now = _now_iso()
    for idx, moment in enumerate(moments):
        if not isinstance(moment, dict) or str(moment.get("id") or "") not in target_ids:
            continue
        if _memory_is_archived(moment):
            continue
        updated = dict(moment)
        updated["is_archived"] = True
        updated["archived_at"] = now
        updated["archive_reason"] = reason[:300]
        updated["archived_by_repair_job"] = job_id
        updated["updated_at"] = now
        moments[idx] = updated
        archived += 1
        _append_memory_change(store, {
            "action": "archive",
            "memory_id": str(moment.get("id") or ""),
            "type": str(moment.get("type") or ""),
            "reason": reason,
        })
    if archived:
        _save_moments(store, moments)
    return archived


def _model_api_repair_material_messages(
    store: UserStore,
    api_key: str | None,
    *,
    noisy_ids: set[str],
) -> tuple[list[dict], list[dict], list[str]]:
    messages, warnings = _model_api_recent_recap_chat(store, api_key, limit=200)
    good_cards = [
        card for card in _model_api_plain_memory_cards(store, api_key)
        if str(card.get("id") or "") not in noisy_ids
        and not _memory_quality_card_issues(
            {"type": card.get("type", ""), "source": card.get("source", "")},
            card,
            archive_language=_get_user_archive_language(store.user_id) or "",
        )
    ]
    support_messages: list[dict] = []
    for card in good_cards[:80]:
        support_messages.append({
            "role": "user",
            "content": (
                f"Existing readable Memory Garden card ({card.get('type')}): "
                f"{card.get('title')}\n{card.get('description')}"
            ),
            "ts": None,
            "source": "persona_import",
        })
    return support_messages + messages, good_cards, warnings


def _run_model_api_memory_repair_job(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    job_id: str,
    *,
    noisy_ids: list[str],
    archive_old: bool,
) -> None:
    warnings: list[str] = []
    try:
        _model_api_patch_recap_job(store, job_id, {"status": "processing", "progress": 8})
        noisy_set = {str(mid) for mid in noisy_ids if str(mid).strip()}
        messages, good_cards, material_warnings = _model_api_repair_material_messages(
            store,
            api_key,
            noisy_ids=noisy_set,
        )
        warnings.extend(material_warnings)
        if len(messages) < 4 and len(good_cards) < 2:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": "not_enough_repair_material",
                "warnings": warnings,
            })
            return

        language = _import_language_for_store(store, messages)
        days = _relationship_age_days(store)
        relationship_start = date.today() - timedelta(days=max(0, days))
        windows = _build_transcript_windows(messages, max_chars=14000, max_windows=8)
        target_total = max(6, min(30, len(noisy_set) or 12))
        story_target = max(2, min(10, target_total // 3))
        about_target = max(3, target_total - story_target)

        def progress(done: int, total: int, candidate_count: int) -> None:
            _model_api_patch_recap_job(store, job_id, {
                "status": "processing",
                "progress": 12 + int(50 * done / max(total, 1)),
                "candidate_windows_done": done,
                "candidate_windows_total": total,
                "candidates_extracted": candidate_count,
                "messages_reviewed": len(messages),
            })

        candidates, provider_warnings = _extract_memory_candidates_with_provider(
            runtime,
            windows,
            relationship_start,
            per_window_target=4,
            language=language,
            on_progress=progress,
        )
        warnings.extend(provider_warnings)
        cards = _render_candidates_to_memory_cards(
            candidates,
            relationship_start,
            {
                "story": story_target,
                "about_me": about_target,
                "ta_thinking": 0,
                "total": target_total,
            },
            language=language,
            max_cards=target_total,
        )
        cards = [
            card for card in cards
            if str(card.get("type") or "") in {"moment", "quote", "fact", "event"}
        ]
        new_cards = _new_cards_only(good_cards, cards)[:target_total]
        actions = [
            {
                "type": "memory.add",
                "memory": {
                    **card,
                    "source": "model_api_repair",
                    "context": (
                        str(card.get("context") or "").strip()
                        or f"re-distilled during hosted API memory repair job {job_id}"
                    )[:1000],
                },
                "reason": "Memory repair re-distilled readable cards from existing chat/material.",
                "capture_mode": "repair",
            }
            for card in new_cards
        ]
        _model_api_patch_recap_job(store, job_id, {
            "status": "processing",
            "progress": 72,
            "candidate_cluster_count": len(_merge_import_candidates(candidates)),
            "new_cards_planned": len(actions),
            "memories_planned": len(actions),
            "warnings": warnings,
        })
        if not actions:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": "no_replacement_cards_generated",
                "warnings": warnings,
            })
            return

        body, status = _execute_memory_actions(store, api_key, actions)
        effects = body.get("effects") or []
        if status >= 400:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": body.get("error", "memory_action_failed"),
                "effects": effects,
                "warnings": warnings,
            })
            return
        archived = 0
        if archive_old:
            archived = _archive_model_api_memory_cards(
                store,
                list(noisy_set),
                reason="Archived by hosted API memory repair after replacement cards were written.",
                job_id=job_id,
            )
        _model_api_patch_recap_job(store, job_id, {
            "status": "completed",
            "progress": 100,
            "actions_planned": len(actions),
            "actions_written": len(effects),
            "new_cards_created": len(effects),
            "memories_created": len(effects),
            "old_cards_archived": archived,
            "effects": effects,
            "warnings": warnings,
        })
        _patch_model_api_runtime_profile(store, {
            "last_repair_at": _now_iso(),
            "memory_quality_warning": None if archived else "memory_quality_warning",
        })
    except Exception as e:
        _model_api_patch_recap_job(store, job_id, {
            "status": "failed",
            "progress": 100,
            "error": f"{type(e).__name__}:{str(e)[:300]}",
            "warnings": warnings,
        })


def _model_api_recap_due(store: UserStore, turn_count: int) -> tuple[bool, str]:
    if turn_count <= 0 or turn_count % MODEL_API_CONSOLIDATE_TURN_INTERVAL != 0:
        return False, f"cadence:{MODEL_API_CONSOLIDATE_TURN_INTERVAL}"
    with _model_api_recap_active_lock:
        if store.user_id in _model_api_recap_active_users:
            return False, "recap_already_running"
    latest = _model_api_latest_recap_job(store)
    if latest:
        status = str(latest.get("status") or "")
        if status in {"queued", "processing"}:
            return False, "recap_already_running"
        elapsed = time.time() - float(latest.get("ts") or 0)
        if elapsed < MODEL_API_CONSOLIDATE_MIN_INTERVAL_SEC:
            return False, f"min_interval:{MODEL_API_CONSOLIDATE_MIN_INTERVAL_SEC}"
    return True, "recap_due"


def _model_api_patch_recap_job(store: UserStore, job_id: str, patch: dict, *, only_if_status: str | None = None) -> dict | None:
    merged = dict(patch)
    if patch.get("status") in {"completed", "failed", "skipped"}:
        merged.setdefault("completed_at", _now_iso())
    return db.log_patch_item(store.user_id, "memory_capture_jobs", job_id, merged, only_if_status=only_if_status)


def _run_model_api_recap_job(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    job_id: str,
    turn_count: int,
) -> None:
    try:
        _model_api_patch_recap_job(store, job_id, {
            "status": "processing",
            "progress": 8,
        })
        messages, warnings = _model_api_recent_recap_chat(store, api_key)
        if len(messages) < 12:
            _model_api_patch_recap_job(store, job_id, {
                "status": "skipped",
                "reason": "not_enough_recent_chat",
                "messages_reviewed": len(messages),
                "warnings": warnings,
            })
            return

        language = _import_language_for_store(store, messages)
        days = _relationship_age_days(store)
        relationship_start = date.today() - timedelta(days=max(0, days))
        windows = _build_transcript_windows(messages, max_chars=14000, max_windows=8)
        if not windows:
            _model_api_patch_recap_job(store, job_id, {
                "status": "skipped",
                "reason": "empty_windows",
                "messages_reviewed": len(messages),
                "warnings": warnings,
            })
            return

        def progress(done: int, total: int, candidate_count: int) -> None:
            _model_api_patch_recap_job(store, job_id, {
                "status": "processing",
                "progress": 10 + int(55 * done / max(total, 1)),
                "candidate_windows_done": done,
                "candidate_windows_total": total,
                "candidates_extracted": candidate_count,
                "messages_reviewed": len(messages),
            })

        candidates, provider_warnings = _extract_memory_candidates_with_provider(
            runtime,
            windows,
            relationship_start,
            per_window_target=4,
            language=language,
            on_progress=progress,
        )
        warnings.extend(provider_warnings)
        cards = _render_candidates_to_memory_cards(
            candidates,
            relationship_start,
            {
                "story": 3,
                "about_me": 5,
                "ta_thinking": 0,
                "total": 8,
            },
            language=language,
            max_cards=8,
        )
        cards = [card for card in cards if str(card.get("type") or "") in {"moment", "quote", "fact", "event"}]
        existing_cards = _model_api_plain_memory_cards(store, api_key)
        new_cards = _new_cards_only(existing_cards, cards)[:8]
        source_ids = [str(msg.get("id") or "") for msg in messages if msg.get("id")]
        actions = [
            {
                "type": "memory.add",
                "memory": {
                    **card,
                    "source": "model_api_recap",
                    "context": (
                        str(card.get("context") or "").strip()
                        or f"distilled from recent hosted API chat recap at turn {turn_count}"
                    )[:1000],
                },
                "reason": "Periodic recap distilled this from recent hosted API chat.",
                "capture_mode": "recap",
                "source_chat_message_ids": source_ids[-80:],
            }
            for card in new_cards
        ]
        _model_api_patch_recap_job(store, job_id, {
            "status": "processing",
            "progress": 76,
            "candidate_cluster_count": len(_merge_import_candidates(candidates)),
            "memories_planned": len(actions),
            "warnings": warnings,
        })
        if not actions:
            _model_api_patch_recap_job(store, job_id, {
                "status": "completed",
                "progress": 100,
                "actions_planned": 0,
                "actions_written": 0,
                "memories_created": 0,
                "messages_reviewed": len(messages),
                "warnings": warnings,
            })
            _patch_model_api_runtime_profile(store, {"last_recap_at": _now_iso()})
            return

        body, status = _execute_memory_actions(store, api_key, actions)
        effects = body.get("effects") or []
        if status >= 400:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "actions_planned": len(actions),
                "actions_written": len(effects),
                "effects": effects,
                "error": body.get("error", "memory_action_failed"),
                "warnings": warnings,
            })
            return
        _model_api_patch_recap_job(store, job_id, {
            "status": "completed",
            "progress": 100,
            "actions_planned": len(actions),
            "actions_written": len(effects),
            "memories_created": len(effects),
            "effects": effects,
            "messages_reviewed": len(messages),
            "first_message_ts": messages[0].get("ts"),
            "latest_message_ts": messages[-1].get("ts"),
            "warnings": warnings,
        })
        _patch_model_api_runtime_profile(store, {"last_recap_at": _now_iso()})
    except Exception as e:
        _model_api_patch_recap_job(store, job_id, {
            "status": "failed",
            "progress": 100,
            "error": f"{type(e).__name__}:{str(e)[:300]}",
        })
    finally:
        with _model_api_recap_active_lock:
            _model_api_recap_active_users.discard(store.user_id)


def _start_model_api_recap_job(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    *,
    turn_count: int,
) -> dict:
    with _model_api_recap_active_lock:
        if store.user_id in _model_api_recap_active_users:
            return {
                "status": "skipped",
                "mode": "recap",
                "reason": "recap_already_running",
                "turn_count": turn_count,
                "actions_written": 0,
            }
        _model_api_recap_active_users.add(store.user_id)
    job = _append_memory_capture_job(store, {
        "mode": "recap",
        "status": "queued",
        "reason": f"cadence:{MODEL_API_CONSOLIDATE_TURN_INTERVAL}",
        "turn_count": turn_count,
        "progress": 0,
        "actions_written": 0,
    })
    thread = threading.Thread(
        target=_run_model_api_recap_job,
        args=(store, api_key, runtime, job["job_id"], turn_count),
        daemon=True,
    )
    thread.start()
    return job


def _model_api_maybe_run_memory_capture(
    store: UserStore,
    api_key: str | None,
    runtime: ProviderConfig,
    *,
    user_message: str,
    assistant_reply: str,
    user_message_id: str,
    assistant_message_id: str,
    context_payload: dict,
    effects: list[dict],
    run_sync: bool = False,
) -> dict:
    turn_count = _model_api_turn_count(store)
    has_state_write = any(
        str(effect.get("action") or "").startswith(("identity.", "memory."))
        or str(effect.get("type") or "").startswith(("identity_", "memory_"))
        for effect in effects
    )
    if has_state_write:
        return {
            "status": "skipped",
            "reason": "state_action_already_written",
            "turn_count": turn_count,
            "actions_written": 0,
        }
    cadence = turn_count > 0 and turn_count % MODEL_API_CAPTURE_TURN_INTERVAL == 0
    if not cadence:
        return {
            "status": "skipped",
            "reason": f"cadence:{MODEL_API_CAPTURE_TURN_INTERVAL}",
            "turn_count": turn_count,
            "actions_written": 0,
        }
    job = _start_model_api_memory_capture_job(
        store,
        api_key,
        runtime,
        user_message=user_message,
        assistant_reply=assistant_reply,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        context_payload=context_payload,
        turn_count=turn_count,
        run_sync=run_sync,
    )
    recap_due, recap_reason = _model_api_recap_due(store, turn_count)
    if recap_due:
        recap_job = _start_model_api_recap_job(store, api_key, runtime, turn_count=turn_count)
        job.setdefault("warnings", [])
        if isinstance(job["warnings"], list):
            job["warnings"].append("recap_queued")
        if isinstance(recap_job, dict):
            job["recap_job_id"] = recap_job.get("job_id", "")
            job["recap_status"] = recap_job.get("status", "")
    elif recap_reason not in {f"cadence:{MODEL_API_CONSOLIDATE_TURN_INTERVAL}", ""}:
        job.setdefault("warnings", [])
        if isinstance(job["warnings"], list):
            job["warnings"].append(recap_reason)
    return job


MODEL_API_MAX_IMAGE_BYTES = 2_000_000


def _model_api_image_payload(payload: dict) -> tuple[bytes | None, str, str | None]:
    raw = str(payload.get("image_b64") or payload.get("image_base64") or "").strip()
    if not raw:
        return None, "", None
    mime = str(payload.get("image_mime") or "image/jpeg").strip() or "image/jpeg"
    if not re.match(r"^image/(jpeg|jpg|png|webp)$", mime, re.I):
        return None, "", "image_mime must be image/jpeg, image/png, or image/webp"
    if raw.startswith("data:"):
        head, _, rest = raw.partition(",")
        raw = rest.strip()
        if head.startswith("data:image/") and ";" in head:
            mime = head.removeprefix("data:").split(";", 1)[0] or mime
    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except Exception:
        return None, "", "image_b64 must be valid base64"
    if not image_bytes:
        return None, "", "image_b64 must not be empty"
    if len(image_bytes) > MODEL_API_MAX_IMAGE_BYTES:
        return None, "", f"image too large; max {MODEL_API_MAX_IMAGE_BYTES} bytes"
    return image_bytes, mime, None


def _model_api_user_content(message: str, images: list[dict[str, str]]) -> Any:
    text = message.strip() or "User sent an image."
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for image in images:
        image_b64 = str(image.get("b64") or "").strip()
        if not image_b64:
            continue
        image_mime = str(image.get("mime") or "image/jpeg")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_b64}"},
        })
    return parts


def _run_model_api_web_searches(requests_in: list[dict]) -> dict:
    return run_model_api_web_searches(
        requests_in,
        enabled=MODEL_API_WEB_SEARCH_ENABLED,
        max_queries=MODEL_API_WEB_SEARCH_MAX_QUERIES,
        max_results=MODEL_API_WEB_SEARCH_MAX_RESULTS,
        timeout_sec=MODEL_API_WEB_SEARCH_TIMEOUT_SEC,
    )


def _model_api_web_search_results_message(web_search: dict) -> dict:
    return build_model_api_web_search_results_message(web_search)


def _model_api_web_search_trace(web_search: dict) -> dict:
    return model_api_web_search_trace(
        web_search,
        max_queries=MODEL_API_WEB_SEARCH_MAX_QUERIES,
    )


@app.route("/v1/model_api/chat/send", methods=["POST"])
def model_api_chat_send():
    store = require_user()
    api_key = _extract_api_key()
    trace_start = time.time()
    payload = request.get_json(silent=True) or {}
    image_bytes, image_mime, image_err = _model_api_image_payload(payload)
    if image_err:
        return jsonify({"error": "invalid_image", "detail": image_err}), 400
    image_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes else ""
    has_image = image_bytes is not None
    message = str(payload.get("message") or payload.get("content") or "").strip()
    message_for_context = message or ("User sent an image." if has_image else "")
    context_refs = _context_refs_from_payload(payload)
    if not message_for_context:
        return jsonify({"error": "message required"}), 400
    if len(message) > 12000:
        return jsonify({"error": "message too long", "max_chars": 12000}), 413

    runtime = _load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        _append_model_api_action_trace(store, {
            "status": "failed",
            "error": err.get("error", "runtime_load_failed"),
            "context": {"stage": "load_runtime"},
            "duration_ms": int((time.time() - trace_start) * 1000),
        })
        return jsonify(err), 400
    _ensure_model_api_runtime_profile(store, _load_model_api_config(store), touch=True)

    user_plaintext = image_bytes if image_bytes is not None else message.encode("utf-8")
    user_env, env_err = _build_shared_envelope_for_store(store, user_plaintext)
    if user_env is None:
        return jsonify({"error": "user_message_envelope_failed", "detail": env_err}), 409
    user_row = store.append_chat(
        "user",
        "model_api",
        user_env,
        content_type="image" if has_image else "text",
    )
    store.notify_chat_waiters()

    effects: list[dict] = []
    identity_action_results: list[dict] = []
    memory_action_results: list[dict] = []

    provider_messages, context_payload, screen_images = _model_api_context_messages(
        store,
        api_key,
        message_for_context,
        include_screen_context=bool(payload.get("include_screen_context")),
    )
    provider_images = list(screen_images)
    if has_image:
        provider_images.append({"mime": image_mime, "b64": image_b64, "label": "user_upload"})
    if provider_images:
        user_content = _model_api_user_content(message_for_context, provider_images)
        for idx in range(len(provider_messages) - 1, -1, -1):
            if provider_messages[idx].get("role") == "user":
                provider_messages[idx]["content"] = user_content
                break
        else:
            provider_messages.append({"role": "user", "content": user_content})
    if context_refs:
        context_payload["context_refs"] = context_refs
        provider_messages.insert(2, {
            "role": "system",
            "content": "User-selected context refs JSON:\n" + json.dumps(context_refs, ensure_ascii=False)[:3000],
        })
    provider_messages.insert(2, _model_api_turn_contract_message())
    web_search: dict = {}
    try:
        result = chat_completion(
            runtime,
            provider_messages,
            # Thinking/reasoning models share this budget between reasoning and
            # output tokens, so keep it generous; non-thinking models stop early
            # on their own and don't pay for the headroom.
            max_tokens=int(payload.get("max_tokens") or 2048),
            temperature=float(payload.get("temperature") or 0.7),
            timeout=90.0,
            include_reasoning=MODEL_API_PROVIDER_REASONING_ENABLED,
        )
    except ProviderError as e:
        background_execution = hosted_runtime_background_trace(
            status="not_started",
            method=HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
        )
        trace = _append_model_api_action_trace(store, {
            "status": "failed",
            "provider": runtime.provider,
            "model": runtime.model,
            "user_message_id": user_row["id"],
            "background_execution": background_execution,
            "effects": effects,
            "identity_actions": identity_action_results,
            "memory_actions": memory_action_results,
            "context": _model_api_context_trace_for_action(
                context_payload,
                context_refs=context_refs,
                web_search=web_search,
            ),
            "error": f"provider_chat_failed:{str(e)[:220]}",
            "duration_ms": int((time.time() - trace_start) * 1000),
        })
        return jsonify({
            "error": "provider_chat_failed",
            "detail": str(e),
            "status_code": e.status_code,
            "user_message_id": user_row["id"],
            "action_trace_id": trace.get("trace_id", ""),
        }), 502

    raw_reply = str(result.get("reply") or "").strip()
    reply, thinking_summary, requested_web_search = _model_api_parse_turn_output(raw_reply)
    provider_reasoning = _sanitize_provider_reasoning_text(str(result.get("reasoning") or ""))
    if requested_web_search and (not web_search or not reply):
        if not web_search:
            web_search = _run_model_api_web_searches(requested_web_search)
            context_payload["web_search"] = _model_api_web_search_trace(web_search)
        final_messages = list(provider_messages)
        final_messages.append({"role": "assistant", "content": raw_reply[:4000]})
        final_messages.append(_model_api_web_search_results_message(web_search))
        final_messages.append(model_api_web_search_followup_message())
        try:
            final_result = chat_completion(
                runtime,
                final_messages,
                max_tokens=int(payload.get("max_tokens") or 2048),
                temperature=float(payload.get("temperature") or 0.7),
                timeout=90.0,
                include_reasoning=MODEL_API_PROVIDER_REASONING_ENABLED,
            )
            final_raw_reply = str(final_result.get("reply") or "").strip()
            final_reply, final_thinking, _ = _model_api_parse_turn_output(final_raw_reply)
            if final_reply:
                reply = final_reply
                thinking_summary = final_thinking or thinking_summary
                provider_reasoning = _sanitize_provider_reasoning_text(str(final_result.get("reasoning") or "")) or provider_reasoning
                result = {
                    **final_result,
                    "usage": {
                        "initial": result.get("usage") or {},
                        "final": final_result.get("usage") or {},
                    },
                }
        except ProviderError as e:
            if not reply:
                background_execution = hosted_runtime_background_trace(
                    status="not_started",
                    method=HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
                )
                trace = _append_model_api_action_trace(store, {
                    "status": "failed",
                    "provider": runtime.provider,
                    "model": runtime.model,
                    "user_message_id": user_row["id"],
                    "background_execution": background_execution,
                    "effects": effects,
                    "identity_actions": identity_action_results,
                    "memory_actions": memory_action_results,
                    "context": _model_api_context_trace_for_action(
                        context_payload,
                        context_refs=context_refs,
                        web_search=web_search,
                    ),
                    "error": f"provider_chat_after_web_search_failed:{str(e)[:220]}",
                    "duration_ms": int((time.time() - trace_start) * 1000),
                })
                return jsonify({
                    "error": "provider_chat_after_web_search_failed",
                    "detail": str(e),
                    "status_code": e.status_code,
                    "user_message_id": user_row["id"],
                    "action_trace_id": trace.get("trace_id", ""),
                    "tools": {"web_search": _model_api_web_search_trace(web_search)},
                }), 502
    if not reply:
        background_execution = hosted_runtime_background_trace(
            status="not_started",
            method=HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
        )
        trace = _append_model_api_action_trace(store, {
            "status": "failed",
            "provider": runtime.provider,
            "model": runtime.model,
            "user_message_id": user_row["id"],
            "background_execution": background_execution,
            "context": _model_api_context_trace_for_action(
                context_payload,
                context_refs=context_refs,
                web_search=web_search,
            ),
            "error": "provider_empty_reply",
            "duration_ms": int((time.time() - trace_start) * 1000),
        })
        return jsonify({"error": "provider_empty_reply", "user_message_id": user_row["id"], "action_trace_id": trace.get("trace_id", "")}), 502
    assistant_env, env_err = _build_shared_envelope_for_store(store, reply.encode("utf-8"))
    if assistant_env is None:
        return jsonify({"error": "assistant_envelope_failed", "detail": env_err}), 409
    assistant_extra: dict = {}
    display_thinking = provider_reasoning or thinking_summary
    if display_thinking:
        thinking_env, thinking_err = _build_shared_envelope_for_store(store, display_thinking.encode("utf-8"))
        if thinking_env is not None:
            assistant_extra.update(_chat_thinking_extra_from_envelope(thinking_env))
            assistant_extra["thinking_kind"] = "provider_reasoning" if provider_reasoning else "context_summary"
        else:
            print(f"[model_api_chat:{store.user_id}] thinking_envelope_failed detail={thinking_err}")
    assistant_row = store.append_chat("openclaw", "model_api", assistant_env, extra=assistant_extra)
    store.notify_chat_waiters()
    delivery_fields = _deliver_ai_message_push_if_background(
        store,
        body=reply,
        title="IO",
        data={"source": "model_api"},
        visual_state="reply",
    )
    updated = store.update_chat_message_metadata(assistant_row["id"], delivery_fields)
    if updated:
        assistant_row = updated
    capture_job = _model_api_maybe_run_memory_capture(
        store,
        api_key,
        runtime,
        user_message=message_for_context,
        assistant_reply=reply,
        user_message_id=user_row["id"],
        assistant_message_id=assistant_row["id"],
        context_payload=context_payload,
        effects=effects,
        run_sync=bool(app.config.get("TESTING") and payload.get("capture_sync")),
    )
    state_job: dict = {
        "status": "skipped",
        "reason": "testing_background_execution_disabled" if app.config.get("TESTING") else "background_execution_disabled",
        "actions_written": 0,
    }
    if (not app.config.get("TESTING")) or payload.get("state_sync") or payload.get("state_async"):
        state_job = _start_model_api_state_action_job(
            store,
            api_key,
            runtime,
            user_message=message_for_context,
            user_message_id=user_row["id"],
            assistant_message_id=assistant_row["id"],
            context_refs=context_refs,
            run_sync=bool(app.config.get("TESTING") and payload.get("state_sync")),
        )
    print(
        f"[model_api_chat:{store.user_id}] user_msg={user_row['id']} "
        f"reply={assistant_row['id']} provider={runtime.provider} model={runtime.model}"
    )
    background_execution = hosted_runtime_background_trace(
        status=str(state_job.get("status") or ""),
        method=HOSTED_RUNTIME_BACKGROUND_METHOD,
        trace_id=str(state_job.get("trace_id") or ""),
        error=str(state_job.get("error") or ""),
    )
    trace = _append_model_api_action_trace(store, {
        "status": "ok",
        "provider": runtime.provider,
        "model": runtime.model,
        "user_message_id": user_row["id"],
        "assistant_message_id": assistant_row["id"],
        "background_execution": background_execution,
        "effects": effects,
        "identity_actions": identity_action_results,
        "memory_actions": memory_action_results,
        "capture": {
            "job_id": capture_job.get("job_id", ""),
            "status": capture_job.get("status", ""),
            "reason": capture_job.get("reason", ""),
            "turn_count": capture_job.get("turn_count", 0),
            "actions_written": capture_job.get("actions_written", 0),
            "warnings": capture_job.get("warnings", []),
            "recap_job_id": capture_job.get("recap_job_id", ""),
            "recap_status": capture_job.get("recap_status", ""),
        },
        "context": _model_api_context_trace_for_action(
            context_payload,
            context_refs=context_refs,
            web_search=web_search,
            provider_reasoning=provider_reasoning,
        ),
        "usage": result.get("usage") or {},
        "duration_ms": int((time.time() - trace_start) * 1000),
    })
    return jsonify({
        "status": "ok",
        "reply": reply,
        "context_summary": thinking_summary,
        "thinking_summary": display_thinking,
        "thinking_kind": "provider_reasoning" if provider_reasoning else ("context_summary" if thinking_summary else ""),
        "provider_reasoning": provider_reasoning,
        "user_message": {"id": user_row["id"], "ts": user_row["ts"]},
        "assistant_message": {"id": assistant_row["id"], "ts": assistant_row["ts"]},
        "user_content_type": "image" if has_image else "text",
        "provider": runtime.provider,
        "model": runtime.model,
        "usage": result.get("usage") or {},
        "tools": {
            "web_search": _model_api_web_search_trace(web_search),
        },
        "effects": effects,
        "identity_actions": identity_action_results,
        "memory_actions": memory_action_results,
        "capture": {
            "job_id": capture_job.get("job_id", ""),
            "status": capture_job.get("status", ""),
            "reason": capture_job.get("reason", ""),
            "turn_count": capture_job.get("turn_count", 0),
            "actions_written": capture_job.get("actions_written", 0),
            "warnings": capture_job.get("warnings", []),
            "recap_job_id": capture_job.get("recap_job_id", ""),
            "recap_status": capture_job.get("recap_status", ""),
        },
        "state": {
            "action_trace_id": trace.get("trace_id", ""),
            "background_trace_id": state_job.get("trace_id", ""),
            "receipt": {},
            "pending": [
                _state_pending_public_summary(item)
                for item in _state_pending_items(store)[:5]
            ],
            "background_execution": background_execution,
        },
        "runtime": {
            "engine": HOSTED_RUNTIME_ENGINE_NATIVE,
            "mode": MODEL_API_RUNTIME_MODE,
            "version": MODEL_API_RUNTIME_VERSION,
            "background_execution": background_execution,
        },
        "context": _model_api_context_trace_for_action(
            context_payload,
            context_refs=context_refs,
            web_search=web_search,
        ),
    })


# ---------------------------------------------------------------------------
# Screen / aggregation
# ---------------------------------------------------------------------------


@app.route("/v1/screen/ios", methods=["GET"])
def get_ios():
    store = require_user()
    try:
        window_sec = max(300.0, min(172800.0, float(request.args.get("window_sec", 86400))))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid window_sec"}), 400
    return jsonify(_build_ios_data(store, window_sec=window_sec))


@app.route("/v1/screen/mac", methods=["GET"])
def get_mac():
    require_user()
    return jsonify(MAC_DATA)


@app.route("/v1/screen/summary", methods=["GET"])
def get_summary():
    store = require_user()
    ios_data = _build_ios_data(store, window_sec=86400)
    top_app = ios_data["apps"][0]["name"] if ios_data.get("apps") else "Unknown"
    categories = ios_data.get("categories") or {}
    top_category = max(categories, key=categories.get) if categories else "Other"

    summary = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ios": {
            "total_screen_time_minutes": ios_data.get("total_screen_time_minutes", 0),
            "top_app": top_app,
            "top_category": top_category,
            "pickups": ios_data.get("pickups", 0),
            "data_source": ios_data.get("data_source", "unknown"),
            "frame_count": ios_data.get("frame_count", 0),
        },
        "mac": {
            "total_active_minutes": MAC_DATA["total_active_minutes"],
            "deep_work_minutes": MAC_DATA["deep_work_minutes"],
            "focus_score": MAC_DATA["focus_score"],
            "top_app": MAC_DATA["apps"][0]["name"],
            "context_switches": MAC_DATA["context_switches"],
        },
        "combined": {
            "total_screen_minutes": ios_data.get("total_screen_time_minutes", 0) + MAC_DATA["total_active_minutes"],
            "insight": "Phone side now comes from real frame aggregation; Mac remains mocked.",
        },
    }
    return jsonify(summary)


@app.route("/v1/sources", methods=["GET"])
def get_sources():
    require_user()
    return jsonify(SOURCES_DATA)


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def _live_activity_identity_context(store: UserStore) -> dict:
    identity = _load_identity(store) or {}
    return {
        "aiStart": str(identity.get("relationship_started_at") or "").strip() or None,
    }


def _live_activity_content_state(store: UserStore, payload: dict, *, default_visual_state: str = "reply") -> dict:
    title = (payload.get("title") or "").strip()
    body = (payload.get("body") or payload.get("message") or payload.get("desc") or "").strip()
    subtitle = (payload.get("subtitle") or "").strip() or None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    identity_context = _live_activity_identity_context(store)
    visual_state = str(
        payload.get("visualState")
        or payload.get("visual_state")
        or ("reply" if body else default_visual_state)
    ).strip() or default_visual_state
    if visual_state not in {"default", "sharing", "reply"}:
        visual_state = default_visual_state
    name = (payload.get("name") or title or "IO")
    name = str(name).strip() or "IO"

    # Include both the post-animation schema (visualState/name/desc/aiStart)
    # and the earlier schema (title/subtitle/body/data/updatedAt). Swift
    # Codable ignores unknown keys, so this keeps production TestFlight builds
    # and the new animated widget build compatible during rollout.
    return {
        "visualState": visual_state,
        "name": name,
        "desc": body,
        "aiStart": payload.get("aiStart") or payload.get("ai_start") or identity_context.get("aiStart"),
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "personaId": payload.get("personaId", "default"),
        "templateId": payload.get("templateId", "default"),
        "data": data,
        "updatedAt": time.time(),
    }


def _live_activity_body(payload: dict) -> str:
    return (payload.get("body") or payload.get("message") or payload.get("desc") or "").strip()


def _live_activity_top_app(payload: dict) -> str:
    return str(payload.get("topApp") or payload.get("top_app") or "")


@app.route("/v1/push/dynamic-island", methods=["POST"])
def push_dynamic_island():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    return push_live_activity_inner(store, payload)


@app.route("/v1/push/live-activity", methods=["POST"])
def push_live_activity():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    return push_live_activity_inner(store, payload)


def push_live_activity_inner(store: UserStore, payload: dict):
    activity_id = payload.get("activity_id")
    entry = _select_token(store, _is_live_activity_token, activity_id=activity_id, active_only=True)
    if not entry and activity_id:
        entry = _select_token(store, _is_live_activity_token, active_only=True)
    if not entry:
        print(f"[live-activity:{store.user_id}] no active token registered — logged: {payload}")
        return jsonify({
            "status": "logged",
            "activity_id": activity_id or f"la_{uuid.uuid4().hex[:8]}",
            "needs_refresh": True,
            "reason": "no_active_live_activity_token",
            "mode": "update",
        })

    body = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    alert_title = str(payload.get("alert_title") or payload.get("title") or "").strip()
    alert_body = str(payload.get("alert_body") or body or "").strip()

    suppress, reason = store.should_suppress_live_activity(message=body, top_app=top_app)
    if suppress:
        print(f"[live-activity:{store.user_id}] suppressed: {reason} body={body[:60]}")
        return jsonify({
            "status": "suppressed",
            "reason": reason,
            "activity_id": entry.get("activity_id"),
            "mode": "update",
        })

    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": payload.get("event", "update"),
            "content-state": _live_activity_content_state(store, payload, default_visual_state="reply"),
            # Non-empty alert text is what makes a remote Live Activity update
            # user-visible instead of only refreshing the lock-screen/Island
            # content state silently.
            "alert": {"title": alert_title or "IO", "body": alert_body[:240]},
        }
    }
    topic = f"{BUNDLE_ID}.push-type.liveactivity"
    result = _send_apns_to_active_tokens(
        store,
        _is_live_activity_token,
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        activity_id=activity_id,
    )

    delivered = result.get("status") == "delivered"
    if delivered:
        store.record_successful_push()
        store.record_live_activity_sent(message=body, top_app=top_app)

    print(f"[live-activity:{store.user_id}] {result}")
    response = {
        "status": result.get("status", "error"),
        "activity_id": entry.get("activity_id") or activity_id,
        "mode": "update",
    }
    if result.get("code") is not None:
        response["error_code"] = result.get("code")
    if result.get("reason"):
        response["reason"] = result.get("reason")
    if result.get("errors"):
        response["errors"] = result.get("errors")
    if result.get("code") == 410 or _apns_token_should_expire(result):
        response["needs_refresh"] = True
    return jsonify(response)


@app.route("/v1/push/live-start", methods=["POST"])
def push_live_start():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    return push_live_start_inner(store, payload)


def push_live_activity_end_inner(store: UserStore, payload: dict | None = None) -> dict:
    payload = payload or {}
    body = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    activity_id = payload.get("activity_id")
    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": "end",
            "content-state": _live_activity_content_state(store, payload, default_visual_state="default"),
            "dismissal-date": int(time.time()),
        }
    }
    topic = f"{BUNDLE_ID}.push-type.liveactivity"
    result = _send_apns_to_active_tokens(
        store,
        _is_live_activity_token,
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        activity_id=activity_id,
    )
    if result.get("status") == "delivered":
        store.record_live_activity_sent(message=body, top_app=top_app)
    print(f"[live-end:{store.user_id}] {result}")
    return result


def push_live_start_inner(store: UserStore, payload: dict, *, end_existing: bool = False):
    entry = _select_token(store, _is_push_to_start_token, active_only=True)
    if not entry:
        print(f"[live-start:{store.user_id}] no push_to_start token — logged: {payload}")
        return jsonify({"status": "logged", "reason": "no_active_push_to_start_token", "mode": "start"})

    title = (payload.get("title") or "").strip()
    body_text = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    activity_id = str(payload.get("activity_id") or f"la_{uuid.uuid4().hex[:8]}")
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {"activityId": activity_id}
    attributes_type = str(
        payload.get("attributes-type")
        or payload.get("attributes_type")
        or "ScreenActivityAttributes"
    )
    end_result = None
    if end_existing and _select_token(store, _is_live_activity_token, active_only=True):
        end_result = push_live_activity_end_inner(store, payload)

    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": "start",
            "content-state": _live_activity_content_state(store, payload, default_visual_state="reply"),
            "attributes-type": attributes_type,
            "attributes": attributes,
            "alert": {
                "title": title or "OpenClaw",
                "body": body_text or "Live Activity started",
            },
        }
    }

    topic = f"{BUNDLE_ID}.push-type.liveactivity"
    result = _send_apns(
        entry["token"],
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        preferred_env=entry.get("apns_env"),
    )
    if result.get("status") == "delivered":
        _mark_active_token_success(store, entry, apns_env=result.get("apns_env"))
        store.record_live_activity_started(message=body_text, top_app=top_app)
    else:
        reason_text = str(result.get("reason", ""))
        if _apns_token_should_expire(result):
            _mark_expired_token(store, entry, reason_text)

    print(f"[live-start:{store.user_id}] {result}")
    response = {"status": result.get("status", "error"), "mode": "start"}
    if result.get("code") is not None:
        response["error_code"] = result.get("code")
    if result.get("reason"):
        response["reason"] = result.get("reason")
    if result.get("code") == 410 or _apns_token_should_expire(result):
        response["needs_refresh"] = True
    if end_result:
        response["end_status"] = end_result.get("status", "unknown")
        if end_result.get("reason"):
            response["end_reason"] = end_result.get("reason")
    return jsonify(response)


def push_live_activity_hybrid_inner(store: UserStore, payload: dict):
    should_start, start_reason = store.should_start_live_activity()
    if should_start and _select_token(store, _is_push_to_start_token, active_only=True):
        start_resp = push_live_start_inner(store, payload, end_existing=True)
        try:
            start_body = start_resp.get_json(silent=True) or {}
        except Exception:
            start_body = {}
        if start_body.get("status") == "delivered":
            start_body["mode"] = "start"
            start_body["start_reason"] = start_reason
            return jsonify(start_body)

        # If push-to-start is unavailable or rejected, fall back to the cheaper
        # update path so an already-visible activity can still refresh.
        update_resp = push_live_activity_inner(store, payload)
        try:
            update_body = update_resp.get_json(silent=True) or {}
        except Exception:
            update_body = {}
        update_body["mode"] = "start_fallback_update"
        update_body["start_status"] = start_body.get("status", "unknown")
        update_body["start_reason"] = start_body.get("reason") or start_reason
        return jsonify(update_body)

    update_resp = push_live_activity_inner(store, payload)
    try:
        update_body = update_resp.get_json(silent=True) or {}
    except Exception:
        update_body = {}
    update_body["mode"] = "update"
    update_body["start_reason"] = start_reason
    return jsonify(update_body)


def _send_chat_alert(store: UserStore, alert_body: str, alert_title: str = ""):
    """Fire an APNs alert push for an agent chat message. Best-effort:
    failure here never blocks the chat write. The MCP layer (which has
    the plaintext at envelope-build time) passes alert_body in here so
    Flask doesn't have to decrypt anything. Apple's APNs gateway sees
    this string — same posture as Live Activity already has.

    Body is truncated to ~80 chars so long replies render as "...".
    Tap on the notification opens the app (iOS handles routing).
    """
    if not alert_body:
        return {"status": "skipped", "reason": "empty_body"}
    # Match iOS-registered token type: LiveActivityManager registers
    # the standard APNs push token as type="device". Older dev builds used
    # type="apns", so accept both but choose the newest active token.
    if not _select_token(store, _is_device_token, active_only=True):
        print(f"[chat-alert:{store.user_id}] no device token — skip push")
        return {"status": "skipped", "reason": "no_device_token"}

    # Truncate at 80 chars; iOS shows the rest after tapping into chat.
    body = alert_body.strip()
    if len(body) > 80:
        body = body[:79] + "…"

    apns_payload = {
        "aps": {
            "alert": {"title": alert_title or "", "body": body},
            "sound": "default",
        },
        "feedling": {"type": "chat_reply"},
    }
    try:
        result = _send_apns_to_active_tokens(
            store,
            _is_device_token,
            apns_payload,
            push_type="alert",
            topic=BUNDLE_ID,
        )
        print(f"[chat-alert:{store.user_id}] {result.get('status')}")
        return result
    except Exception as e:
        print(f"[chat-alert:{store.user_id}] failed: {e}")
        return {"status": "error", "reason": str(e)}


def _json_body_from_response(resp) -> dict:
    try:
        body = resp.get_json(silent=True) or {}
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _deliver_ai_message_push_if_background(
    store: UserStore,
    *,
    body: str,
    title: str = "",
    data: dict | None = None,
    visual_state: str = "reply",
) -> dict:
    visible_body = (body or "").strip()
    decision = _ai_push_decision(store)
    fields: dict = {
        "push_decision": "send" if decision.get("should_push") else "suppress",
        "push_reason": str(decision.get("reason") or "")[:120],
        "app_presence_phase": str(decision.get("phase") or "")[:40],
    }
    if decision.get("age_sec") not in ("", None):
        fields["app_presence_age_sec"] = str(decision.get("age_sec"))[:20]

    if not visible_body:
        fields.update({
            "push_decision": "skip",
            "push_reason": "empty_body",
            "live_activity_status": "skipped",
            "live_activity_reason": "empty_body",
            "alert_status": "skipped",
            "alert_reason": "empty_body",
        })
        return fields

    if not decision.get("should_push"):
        reason = str(decision.get("reason") or "app_foreground")[:120]
        fields.update({
            "live_activity_status": "suppressed",
            "live_activity_reason": reason,
            "alert_status": "suppressed",
            "alert_reason": reason,
        })
        print(f"[ai-push:{store.user_id}] suppressed reason={reason}")
        return fields

    push_payload = {
        "title": title or "IO",
        "body": visible_body[:240],
        "alert_body": visible_body[:240],
        "data": data or {},
        "visualState": visual_state or "reply",
    }
    live_body = _json_body_from_response(push_live_activity_hybrid_inner(store, push_payload))
    fields["live_activity_status"] = live_body.get("status", "unknown")
    fields["live_activity_reason"] = live_body.get("reason", "")
    fields["live_activity_activity_id"] = live_body.get("activity_id", "")
    fields["live_activity_mode"] = live_body.get("mode", "")

    alert_result = _send_chat_alert(store, visible_body, alert_title=title or "")
    fields["alert_status"] = (alert_result or {}).get("status", "unknown")
    fields["alert_reason"] = (alert_result or {}).get("reason", "")
    print(
        f"[ai-push:{store.user_id}] live={fields['live_activity_status']} "
        f"alert={fields['alert_status']} reason={fields.get('push_reason', '')}"
    )
    return fields


@app.route("/v1/push/notification", methods=["POST"])
def push_notification():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    if not _select_token(store, _is_device_token, active_only=True):
        print(f"[notification:{store.user_id}] no device token — logged: {payload}")
        return jsonify({"status": "logged", "message_id": f"msg_{uuid.uuid4().hex[:8]}"})

    apns_payload = {
        "aps": {
            "alert": {"title": payload.get("title", ""), "body": payload.get("body", "")},
            "sound": "default",
        }
    }
    result = _send_apns_to_active_tokens(
        store,
        _is_device_token,
        apns_payload,
        push_type="alert",
        topic=BUNDLE_ID,
    )
    print(f"[notification:{store.user_id}] {result}")
    return jsonify({"status": result["status"], "message_id": f"msg_{uuid.uuid4().hex[:8]}"})


@app.route("/v1/push/register-token", methods=["POST"])
def register_token():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    token_type = payload.get("type", "unknown")
    token = payload.get("token", "")
    activity_id = payload.get("activity_id")

    now_iso = datetime.now().isoformat()
    entry = {
        "type": token_type,
        "token": token,
        "registered_at": now_iso,
        "status": "active",
        "last_error": "",
        "last_success_at": "",
        "expired_at": "",
        "updated_at": now_iso,
    }
    if activity_id:
        entry["activity_id"] = activity_id
    apns_env = str(payload.get("apns_env") or payload.get("environment") or "").strip().lower()
    if apns_env in {"sandbox", "production"}:
        entry["apns_env"] = apns_env
    for meta_key in (
        "bundle_id",
        "app_version",
        "app_build",
        "build_configuration",
        "device_model",
        "system_version",
    ):
        meta_value = payload.get(meta_key)
        if meta_value is not None:
            entry[meta_key] = str(meta_value)[:160]

    store.tokens[:] = [
        _normalize_token_entry(t)
        for t in store.tokens
        if not (
            t.get("token") == token
            or (
                t.get("type") == token_type
                and (not activity_id or t.get("activity_id") == activity_id)
            )
        )
    ]
    store.tokens.append(entry)
    store._save_tokens()

    print(f"[register-token:{store.user_id}] {token_type}: {token[:16]}…")
    return jsonify({"status": "registered", "type": token_type})


@app.route("/v1/push/tokens", methods=["GET"])
def list_tokens():
    store = require_user()
    active_only = request.args.get("active_only", "false").lower() == "true"
    tokens = [_normalize_token_entry(t) for t in store.tokens]
    if active_only:
        tokens = [t for t in tokens if _entry_is_active(t)]
    return jsonify({"tokens": tokens})


# ---------------------------------------------------------------------------
# Screen frames
# ---------------------------------------------------------------------------


@app.route("/v1/screen/frames", methods=["GET"])
def list_frames():
    store = require_user()
    limit = min(int(request.args.get("limit", 20)), 100)
    with store.frames_lock:
        recent = [f.copy() for f in reversed(store.frames_meta)][:limit]
    for f in recent:
        f["url"] = _frame_url(store, f["filename"])
    return jsonify({"frames": recent, "total": len(store.frames_meta)})


@app.route("/v1/screen/frames/latest", methods=["GET"])
def latest_frame():
    store = require_user()
    with store.frames_lock:
        if not store.frames_meta:
            return jsonify({"error": "no frames yet"}), 404
        meta = store.frames_meta[-1].copy()
    # image_base64 used to be included here, but every frame is a v1
    # envelope now — the file bytes are opaque ciphertext and only
    # waste ~900KB per call. Callers wanting pixels should hit
    # /v1/screen/frames/<id>/decrypt (or the decrypt_frame MCP tool).
    meta["url"] = _frame_url(store, meta["filename"])
    return jsonify(meta)


@app.route("/v1/screen/frames/<filename>", methods=["GET"])
def serve_frame(filename):
    store = require_user()
    # Reject path traversal
    if "/" in filename or ".." in filename:
        return jsonify({"error": "bad filename"}), 400
    # Filenames are `<frame_id>.env.json`; map back to the frame_id and serve
    # the stored envelope JSON bytes (frames are always v1 ciphertext now).
    frame_id = filename.split(".")[0]
    env = db.frame_get(store.user_id, frame_id)
    if env is None:
        return jsonify({"error": "not found"}), 404
    return Response(json.dumps(env), mimetype="application/json")


# --- Frame decrypt plumbing -------------------------------------------------
# Frames are v1 envelopes just like chat/memory/identity: the broadcast
# extension runs VNRecognizeText on-device, stuffs `image` + `ocr_text`
# into the same JSON payload, and ChaCha20-seals the whole thing. The
# server sees only `body_ct` — that's why frames_meta.ocr_text is always
# "" and screen.analyze.ocr_summary is empty.
#
# Two endpoints open the decrypt path to agents + API clients:
#   GET /v1/screen/frames/<id>/envelope — opaque envelope JSON.
#                                         Used by the enclave to pull
#                                         the ciphertext back for
#                                         in-enclave decryption.
#   GET /v1/screen/frames/<id>/decrypt  — proxies to the enclave's
#                                         /v1/screen/frames/<id>/decrypt
#                                         and returns the plaintext:
#                                         image_b64, ocr_text, app, w, h.
#                                         This is the API-path parity
#                                         clients ask for when they want
#                                         everything curl-reachable too.


def _load_envelope(store, frame_id: str) -> dict | None:
    """Load a frame's stored v1 envelope doc by id, or None if absent/invalid."""
    if not re.match(r"^[a-f0-9]{16,64}$", frame_id):
        return None
    env = db.frame_get(store.user_id, frame_id)
    return env if isinstance(env, dict) else None


def _frame_exists(store, frame_id: str) -> bool:
    """Existence guard for the proxy endpoints (no heavy body_ct fetch)."""
    if not re.match(r"^[a-f0-9]{16,64}$", frame_id):
        return False
    return db.frame_exists(store.user_id, frame_id)


@app.route("/v1/screen/frames/<frame_id>/envelope", methods=["GET"])
def frame_envelope(frame_id):
    """Return the raw v1 envelope JSON for a single frame.

    Callers needing plaintext should hit /v1/screen/frames/<id>/decrypt
    instead — this endpoint exists primarily so the enclave can pull the
    ciphertext back for in-enclave decryption.
    """
    store = require_user()
    env = _load_envelope(store, frame_id)
    if env is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(env)


@app.route("/v1/screen/frames/<frame_id>/decrypt", methods=["GET"])
def frame_decrypt(frame_id):
    """Proxy to the enclave's decrypt endpoint so API-only clients get
    plaintext without needing the MCP transport.

    Query params are forwarded untouched; the enclave honors
    `include_image=true|false` to gate the base64 JPEG payload (large).
    """
    store = require_user()
    if not _frame_exists(store, frame_id):
        return jsonify({"error": "not found"}), 404

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return jsonify({"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"}), 503

    # Forward the caller's api_key + any include_image flag.
    api_key = _extract_api_key()
    headers = {"X-API-Key": api_key} if api_key else {}
    params = {"include_image": request.args.get("include_image", "true")}
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            r = client.get(
                f"{enclave_url}/v1/screen/frames/{frame_id}/decrypt",
                headers=headers,
                params=params,
            )
        return (r.content, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except httpx.HTTPError as e:
        return jsonify({"error": f"enclave_error: {e}"}), 502


@app.route("/v1/screen/frames/<frame_id>/image", methods=["GET"])
def frame_image(frame_id):
    """Proxy to the enclave's raw-JPEG endpoint, passing Range through.

    Returns Content-Type image/jpeg with Accept-Ranges: bytes. Clients
    can issue parallel Range GETs to bypass the per-TCP-connection
    throttle on dstack-gateway (~1 Mbps/stream, ~3-4 Mbps aggregate).
    """
    store = require_user()
    if not _frame_exists(store, frame_id):
        return jsonify({"error": "not found"}), 404

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return jsonify({"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"}), 503

    api_key = _extract_api_key()
    # Forward api_key + Range (if present) so the enclave's send_file
    # can respond 206 Partial Content with the requested slice.
    fwd_headers = {"X-API-Key": api_key} if api_key else {}
    if request.headers.get("Range"):
        fwd_headers["Range"] = request.headers["Range"]
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            r = client.get(
                f"{enclave_url}/v1/screen/frames/{frame_id}/image",
                headers=fwd_headers,
            )
    except httpx.HTTPError as e:
        return jsonify({"error": f"enclave_error: {e}"}), 502

    resp_headers = {}
    for h in ("Content-Type", "Content-Length", "Content-Range",
              "Accept-Ranges", "ETag", "Last-Modified"):
        if r.headers.get(h):
            resp_headers[h] = r.headers[h]
    return (r.content, r.status_code, resp_headers)


@app.route("/v1/screen/analyze", methods=["GET"])
def analyze_screen():
    store = require_user()
    now = time.time()
    window_sec = max(30.0, min(3600.0, float(request.args.get("window_sec", 300))))
    min_continuous_min = max(1.0, min(120.0, float(request.args.get("min_continuous_min", 3))))

    with store.frames_lock:
        recent = [f for f in store.frames_meta if now - f["ts"] <= window_sec]

    if not recent:
        return jsonify({
            "active": False,
            "rate_limit_ok": False,
            "reason": "No frames in window — phone screen may be off or recording stopped.",
            "current_app": None,
            "continuous_minutes": 0,
            "ocr_summary": "",
            "cooldown_remaining_seconds": round(store.cooldown_remaining_seconds()),
            "latest_ts": None,
            "latest_frame_filename": None,
            "latest_frame_url": None,
            "frame_count_in_window": 0,
        })

    latest = recent[-1]
    current_app = latest.get("app") or "unknown"

    MAX_GAP_SECONDS = 8
    MAX_JITTER_FRAMES = 2

    continuous_start_ts = latest["ts"]
    jitter_count = 0
    prev_ts = latest["ts"]

    for frame in reversed(recent[:-1]):
        if prev_ts - frame["ts"] > MAX_GAP_SECONDS:
            break
        fapp = frame.get("app") or "unknown"
        if fapp == current_app:
            continuous_start_ts = frame["ts"]
            jitter_count = 0
        else:
            jitter_count += 1
            if jitter_count > MAX_JITTER_FRAMES:
                break
        prev_ts = frame["ts"]

    continuous_minutes = round((latest["ts"] - continuous_start_ts) / 60, 1)

    seen_ocr: set[str] = set()
    ocr_parts: list[str] = []
    for f in reversed(recent):
        text = (f.get("ocr_text") or "").strip()
        if text and text not in seen_ocr:
            seen_ocr.add(text)
            ocr_parts.append(text[:200])
            if len(ocr_parts) >= 3:
                break
    ocr_summary = " | ".join(reversed(ocr_parts))[:500]

    cooldown_remaining = store.cooldown_remaining_seconds()
    rate_limit_ok = cooldown_remaining == 0
    semantic = _semantic_analysis(current_app=current_app, ocr_summary=ocr_summary)
    semantic_strength = semantic.get("semantic_strength", "weak")

    exploratory_allowed = (
        semantic_strength == "weak"
        and len(ocr_summary) >= 20
        and continuous_minutes >= 1.0
    )

    if semantic_strength == "strong":
        trigger_basis = "semantic_strong"
        reason = f"semantic:{semantic.get('semantic_scene', 'unknown')}"
    elif exploratory_allowed:
        trigger_basis = "curiosity_exploratory"
        reason = "ambiguous_context_but_conversation_worth_starting"
    elif continuous_minutes >= min_continuous_min:
        trigger_basis = "legacy_time_fallback"
        reason = f"continuous_minutes {continuous_minutes} >= min_continuous_min {min_continuous_min}"
    else:
        trigger_basis = "insufficient_signal"
        reason = "no_semantic_trigger_and_not_enough_context"

    return jsonify({
        "active": True,
        "current_app": current_app,
        "continuous_minutes": continuous_minutes,
        "ocr_summary": ocr_summary,
        "rate_limit_ok": rate_limit_ok,
        "cooldown_remaining_seconds": round(cooldown_remaining),
        "reason": reason,
        "trigger_policy": "semantic_first",
        "trigger_basis": trigger_basis,
        "semantic_scene": semantic.get("semantic_scene"),
        "task_intent": semantic.get("task_intent"),
        "friction_point": semantic.get("friction_point"),
        "semantic_confidence": semantic.get("confidence", 0.0),
        "suggested_openers": semantic.get("suggested_openers", [])[:2],
        "latest_ts": latest["ts"],
        "latest_frame_filename": latest.get("filename"),
        "latest_frame_url": _frame_url(store, latest.get("filename")) if latest.get("filename") else None,
        "frame_count_in_window": len(recent),
    })


# ---------------------------------------------------------------------------
# Proactive hidden jobs
# ---------------------------------------------------------------------------

_TRACK_EVENT_TYPE_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_TRACK_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|private|body_ct|k_user|k_enclave|"
    r"nonce|cipher|content|clipboard|prompt|transcript|persona|history|"
    r"filename|file_name|file|raw|text|title|url|email|phone|lat|lng|"
    r"latitude|longitude)",
    re.IGNORECASE,
)


def _safe_track_scalar(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, str):
        return value.strip()[:200]
    return None


def _sanitize_track_payload(payload, depth: int = 0) -> dict:
    """Keep beta tracking metadata useful while refusing content-like fields."""
    if not isinstance(payload, dict) or depth > 2:
        return {}
    clean: dict = {}
    for raw_key, value in payload.items():
        key = str(raw_key or "").strip()[:80]
        if not key or _TRACK_SENSITIVE_KEY_RE.search(key):
            continue
        if isinstance(value, dict):
            nested = _sanitize_track_payload(value, depth + 1)
            if nested:
                clean[key] = nested
            continue
        if isinstance(value, list):
            vals = []
            for item in value[:20]:
                if isinstance(item, dict):
                    nested = _sanitize_track_payload(item, depth + 1)
                    if nested:
                        vals.append(nested)
                else:
                    scalar = _safe_track_scalar(item)
                    if scalar is not None:
                        vals.append(scalar)
            if vals:
                clean[key] = vals
            continue
        scalar = _safe_track_scalar(value)
        if scalar is not None:
            clean[key] = scalar
    return clean


def _make_tracking_event(store: UserStore, event_type: str, payload: dict | None = None) -> dict:
    raw_type = str(event_type or "unknown").strip()[:120]
    normalized = _TRACK_EVENT_TYPE_RE.sub("_", raw_type).strip("_.:-").lower()
    if not normalized:
        normalized = "unknown"
    return {
        "event_id": _new_public_id("trk"),
        "user_id": store.user_id,
        "type": normalized[:120],
        "ts": time.time(),
        "created_at": datetime.now().isoformat(),
        "source": str((payload or {}).get("source") or "ios")[:40],
        "payload": _sanitize_track_payload((payload or {}).get("payload") if isinstance(payload, dict) else {}),
        "app_version": str((payload or {}).get("app_version") or "")[:40],
        "build": str((payload or {}).get("build") or "")[:40],
        "platform": str((payload or {}).get("platform") or "ios")[:40],
        "route": str((payload or {}).get("route") or "")[:80],
    }


@app.route("/v1/track/event", methods=["POST"])
def track_event():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    event_type = str(payload.get("event_type") or payload.get("type") or "unknown")
    event = _make_tracking_event(store, event_type, payload)
    store.append_tracking_event(event)
    return jsonify({"status": "ok", "event_id": event["event_id"]})


@app.route("/v1/proactive/settings", methods=["GET", "POST"])
def proactive_settings():
    store = require_user()
    if request.method == "GET":
        return jsonify(store.load_proactive_settings())
    payload = request.get_json(silent=True) or {}
    settings = store.save_proactive_settings(payload)
    return jsonify(settings)


@app.route("/v1/proactive/state", methods=["GET", "POST"])
def proactive_state():
    store = require_user()
    if request.method == "GET":
        settings = store.load_proactive_settings()
        return jsonify({
            "version": settings.get("version", 2),
            "enabled": bool(settings.get("enabled", True)),
            "dnd": bool(settings.get("dnd", False)),
            "user_state": settings.get("user_state", "default"),
            "manual_user_state": settings.get("manual_user_state", settings.get("user_state", "default")),
            "ai_state": settings.get("ai_state", "present"),
            "broadcast_state": settings.get("broadcast_state", "unknown"),
            "updated_at": settings.get("updated_at", ""),
        })
    payload = request.get_json(silent=True) or {}
    settings = store.save_proactive_settings({
        key: payload.get(key)
        for key in ("user_state", "manual_user_state", "ai_state", "broadcast_state", "enabled", "dnd")
        if key in payload
    })
    return jsonify({
        "version": settings.get("version", 2),
        "enabled": bool(settings.get("enabled", True)),
        "dnd": bool(settings.get("dnd", False)),
        "user_state": settings.get("user_state", "default"),
        "manual_user_state": settings.get("manual_user_state", settings.get("user_state", "default")),
        "ai_state": settings.get("ai_state", "present"),
        "broadcast_state": settings.get("broadcast_state", "unknown"),
        "updated_at": settings.get("updated_at", ""),
    })


@app.route("/v1/device/events", methods=["GET", "POST"])
def device_events():
    store = require_user()
    if request.method == "GET":
        try:
            since = float(request.args.get("since", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid since"}), 400
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid limit"}), 400
        limit = max(1, min(limit, 200))
        return jsonify({"events": store.list_device_events(since_epoch=since, limit=limit)})

    payload = request.get_json(silent=True) or {}
    event = _make_device_event(
        source=str(payload.get("source") or "ios"),
        event_type=str(payload.get("type") or payload.get("event_type") or "unknown"),
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
    )
    store.append_device_event(event)
    return jsonify(event)


@app.route("/v1/proactive/tick", methods=["POST"])
def proactive_tick():
    """Create a proactive wake job.

    V2 is agent-owned: the server may mechanically suppress disabled/away
    automatic wakes, but it no longer decrypts frames, calls a platform LLM, or
    requires a memory connection.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    decision = _build_proactive_v2_wake_decision(store, payload, api_key=_extract_api_key())
    store.append_gate_decision(decision)

    job = None
    if decision.get("should_wake_agent", decision.get("should_reach_out")):
        job = store.append_proactive_job(_proactive_job_from_decision(decision))

    return jsonify({
        "decision": decision,
        "job": job,
        "enqueued": job is not None,
    })


def _job_status_patch(payload: dict, *, default_status: str = "") -> dict:
    status = str(payload.get("status") or default_status).strip().lower()
    reason = str(payload.get("reason") or payload.get("status_reason") or "").strip()
    consumer_id = str(payload.get("consumer_id") or "").strip()
    now_iso = datetime.now().isoformat()
    patch: dict = {}
    if status:
        patch["status"] = status[:80]
        if status == "claimed":
            patch["claimed_at"] = now_iso
        elif status == "realizing":
            patch["realizing_at"] = now_iso
        elif status in {"posted", "delivered"}:
            patch["posted_at"] = now_iso
        elif status == "completed":
            patch["completed_at"] = now_iso
        elif status in {"failed", "skipped"}:
            patch["failed_at"] = now_iso
    if reason:
        patch["status_reason"] = reason[:500]
    if consumer_id:
        patch["consumer_id"] = consumer_id[:160]
    if payload.get("chat_message_id"):
        patch["chat_message_id"] = str(payload.get("chat_message_id"))[:160]
    if payload.get("agent_action"):
        patch["agent_action"] = str(payload.get("agent_action"))[:120]
    if payload.get("agent_action_status"):
        patch["agent_action_status"] = str(payload.get("agent_action_status"))[:240]
    if isinstance(payload.get("agent_actions"), list):
        safe_actions = []
        for action in payload.get("agent_actions", [])[:10]:
            if not isinstance(action, dict):
                continue
            safe_action = {
                str(k)[:80]: (v if isinstance(v, (bool, int, float)) or v is None else str(v)[:500])
                for k, v in action.items()
                if str(k)
            }
            safe_actions.append(safe_action)
        patch["agent_actions"] = safe_actions
    if payload.get("wake_result"):
        patch["wake_result"] = str(payload.get("wake_result"))[:120]
    if payload.get("ai_state"):
        ai_state = _normalize_proactive_state(payload.get("ai_state"), PROACTIVE_AI_STATES, "")
        if ai_state:
            patch["ai_state"] = ai_state
    if payload.get("broadcast_state"):
        broadcast_state = _normalize_proactive_state(payload.get("broadcast_state"), PROACTIVE_BROADCAST_STATES, "")
        if broadcast_state:
            patch["broadcast_state"] = broadcast_state
    if isinstance(payload.get("request_broadcast"), dict):
        req = payload.get("request_broadcast") or {}
        try:
            duration_sec = int(req.get("duration_sec") or 0)
        except (TypeError, ValueError):
            duration_sec = 0
        patch["request_broadcast"] = {
            "reason": str(req.get("reason") or "")[:500],
            "duration_sec": max(0, min(duration_sec, 3600)),
            "copy": str(req.get("copy") or req.get("message") or "")[:500],
        }
    return patch


@app.route("/v1/proactive/jobs/<job_id>/claim", methods=["POST"])
def proactive_job_claim(job_id):
    store = require_user()
    payload = request.get_json(silent=True) or {}
    patch = _job_status_patch(payload, default_status="claimed")
    job = store.update_proactive_job(job_id, patch, only_if_status="pending")
    if job is None:
        current = None
        for row in store.list_proactive_jobs(since_epoch=0, limit=0):
            if str(row.get("job_id") or "") == str(job_id):
                current = row
                break
        return jsonify({"claimed": False, "job": current, "reason": "not_pending_or_missing"})
    return jsonify({"claimed": True, "job": job})


@app.route("/v1/proactive/jobs/<job_id>/status", methods=["POST"])
def proactive_job_status(job_id):
    store = require_user()
    payload = request.get_json(silent=True) or {}
    patch = _job_status_patch(payload)
    if not patch:
        return jsonify({"error": "empty_status_patch"}), 400
    job = store.update_proactive_job(job_id, patch)
    if job is None:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify({"job": job})


@app.route("/v1/proactive/decisions", methods=["GET"])
def proactive_decisions():
    store = require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 200))
    return jsonify({"decisions": store.list_gate_decisions(since_epoch=since, limit=limit)})


@app.route("/v1/proactive/decisions/<decision_id>/review", methods=["POST"])
def proactive_decision_review(decision_id):
    store = require_user()
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form.to_dict(flat=True)

    decision_id = str(decision_id or "").strip()
    if not decision_id:
        return jsonify({"error": "decision_id_required"}), 400

    decision = None
    for row in store.list_gate_decisions(since_epoch=0, limit=0):
        if str(row.get("decision_id") or "") == decision_id:
            decision = row
            break
    if decision is None:
        return jsonify({"error": "decision_not_found"}), 404

    label = str(payload.get("label") or "").strip()
    allowed_labels = {
        "correct_true",
        "correct_false",
        "missed_opportunity",
        "spam",
        "weak_connection",
        "repeated",
        "privacy_bad",
        "great_companion_moment",
    }
    if label not in allowed_labels:
        return jsonify({"error": "invalid_label", "allowed": sorted(allowed_labels)}), 400

    review = {
        "review_id": _new_public_id("gr"),
        "decision_id": decision_id,
        "ts": time.time(),
        "created_at": datetime.now().isoformat(),
        "label": label,
        "notes": str(payload.get("notes") or "")[:500],
        "reviewer": str(payload.get("reviewer") or "human")[:80],
        "expected_should_reach_out": payload.get("expected_should_reach_out"),
        "correct_connection_source_id": str(payload.get("correct_connection_source_id") or "")[:160],
        "decision_should_reach_out": bool(decision.get("should_reach_out")),
        "decision_reason": str(decision.get("reason") or decision.get("abstention_reason") or "")[:240],
        "decision_intent_label": str(decision.get("intent_label") or "")[:120],
        "decision_connection": decision.get("connection") or {},
        "frame_ids": decision.get("frame_ids") or [],
    }
    store.append_gate_review(review)
    accept = request.headers.get("Accept", "")
    if not request.is_json and "text/html" in accept:
        return Response(
            "<html><body><p>Review saved.</p><script>history.back()</script></body></html>",
            mimetype="text/html",
        )
    return jsonify({"review": review})


@app.route("/v1/proactive/reviews", methods=["GET"])
def proactive_reviews():
    store = require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 500))
    return jsonify({"reviews": store.list_gate_reviews(since_epoch=since, limit=limit)})


@app.route("/v1/proactive/debug", methods=["GET"])
def proactive_debug_json():
    store = require_user()
    return jsonify(_proactive_debug_snapshot(store))


@app.route("/debug/proactive", methods=["GET"])
def proactive_debug_page():
    store = require_user()
    html_body = _render_proactive_dashboard(_proactive_debug_snapshot(store))
    return Response(html_body, mimetype="text/html")


@app.route("/v1/proactive/jobs/poll", methods=["GET"])
def proactive_jobs_poll():
    store = require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        timeout = max(0.0, min(float(request.args.get("timeout", 30)), 60))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid timeout"}), 400
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 100))

    pending = [
        j for j in store.list_proactive_jobs(since_epoch=since, limit=limit)
        if str(j.get("status") or "pending") == "pending"
    ]
    if pending:
        return jsonify({"jobs": pending, "timed_out": False})

    ev = threading.Event()
    with store.proactive_job_waiters_lock:
        store.proactive_job_waiters.append(ev)

    notified = ev.wait(timeout=timeout)

    with store.proactive_job_waiters_lock:
        try:
            store.proactive_job_waiters.remove(ev)
        except ValueError:
            pass

    if notified:
        pending = [
            j for j in store.list_proactive_jobs(since_epoch=since, limit=limit)
            if str(j.get("status") or "pending") == "pending"
        ]
        return jsonify({"jobs": pending, "timed_out": False})
    return jsonify({"jobs": [], "timed_out": True})


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@app.route("/v1/chat/history", methods=["GET"])
def chat_history():
    store = require_user()
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 200))

    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400

    before_raw = request.args.get("before", "")
    before = 0.0
    if before_raw not in ("", None):
        try:
            before = float(before_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid before"}), 400

    include_image_body = str(
        request.args.get("include_image_body", request.args.get("include_image_bodies", "true"))
    ).lower() not in {"0", "false", "no", "off"}

    with store.chat_lock:
        all_msgs = list(store.chat_messages)
        total = len(store.chat_messages)

    if before > 0:
        filtered = [m for m in all_msgs if float(m.get("ts", 0)) < before]
        msgs = filtered[-limit:]
        has_more_older = len(filtered) > len(msgs)
        has_more_newer = False
        page_mode = "before"
    elif since > 0:
        filtered = [m for m in all_msgs if float(m.get("ts", 0)) > since]
        msgs = filtered[:limit]
        has_more_older = bool(all_msgs and msgs and float(all_msgs[0].get("ts", 0)) < float(msgs[0].get("ts", 0)))
        has_more_newer = len(filtered) > len(msgs)
        page_mode = "since"
    else:
        msgs = all_msgs[-limit:]
        has_more_older = len(all_msgs) > len(msgs)
        has_more_newer = False
        page_mode = "latest"

    out = [_chat_history_item(m, include_image_body=include_image_body) for m in msgs]
    omitted_bodies = sum(1 for m in out if m.get("body_omitted"))
    omitted_image_bodies = sum(
        1
        for m in out
        if m.get("body_omitted") and m.get("content_type", "text") == "image"
    )
    oldest_ts = float(out[0].get("ts", 0)) if out else 0
    latest_ts = float(out[-1].get("ts", 0)) if out else 0

    ua = request.headers.get("User-Agent", "")
    print(
        f"[chat/history:{store.user_id}] ip={request.remote_addr} mode={page_mode} "
        f"since={since} before={before} limit={limit} returned={len(out)} total={total} "
        f"include_image_body={include_image_body} omitted_bodies={omitted_bodies} "
        f"omitted_images={omitted_image_bodies} ua={ua[:80]}"
    )

    return jsonify({
        "messages": out,
        "total": total,
        "oldest_ts": oldest_ts,
        "latest_ts": latest_ts,
        "has_more_older": has_more_older,
        "has_more_newer": has_more_newer,
        "bodies_omitted": omitted_bodies,
        "image_bodies_omitted": omitted_image_bodies,
        "body_omit_inline_max": CHAT_HISTORY_INLINE_BODY_CT_MAX,
    })


@app.route("/v1/chat/history", methods=["DELETE"])
def chat_history_clear():
    """Clear only the caller's chat transcript.

    This intentionally does not touch memory, identity, frames, API keys, or
    onboarding route state. The destructive account reset endpoint remains the
    only path that wipes the whole user record.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    confirm = (payload.get("confirm") or "").strip()
    if confirm != "clear-chat-history":
        return jsonify({
            "error": "confirmation_required",
            "detail": "DELETE body must include {\"confirm\": \"clear-chat-history\"}."
        }), 400

    deleted = db.chat_clear(store.user_id)
    if deleted is None:
        return jsonify({"error": "chat_clear_failed"}), 500

    with store.chat_lock:
        store.chat_messages = []

    store.notify_chat_waiters()
    print(f"[chat/clear:{store.user_id}] deleted={deleted}")
    return jsonify({"cleared": True, "deleted": deleted})


def _chat_history_item(m: dict, *, include_image_body: bool = True) -> dict:
    item = dict(m)
    # iOS ChatMessage.content is non-optional. v1 envelope messages are
    # ciphertext-only at rest and may omit plaintext `content`; always
    # include an empty string so Decodable succeeds and client-side decrypt
    # can populate content later.
    item.setdefault("content", "")

    content_type = item.get("content_type", "text")
    body_ct = item.get("body_ct") or ""
    body_ct_len = len(body_ct)
    should_omit_body = False
    body_omitted_reason = ""
    if content_type == "image" and not include_image_body:
        should_omit_body = True
        body_omitted_reason = "image_body"
    elif body_ct_len > CHAT_HISTORY_INLINE_BODY_CT_MAX and not include_image_body:
        should_omit_body = True
        body_omitted_reason = "large_body_ct"

    if should_omit_body:
        item["body_ct_len"] = body_ct_len
        item["body_omitted"] = True
        item["body_omitted_reason"] = body_omitted_reason
        for key in ("body_ct", "nonce", "K_user", "K_enclave"):
            item.pop(key, None)
    elif content_type == "image" or body_ct_len > CHAT_HISTORY_INLINE_BODY_CT_MAX:
        item["body_ct_len"] = body_ct_len
        item["body_omitted"] = False

    role = item.get("role")
    if role == "openclaw":
        item["sender"] = "assistant"
        item["is_from_openclaw"] = True
    elif role == "user":
        item["sender"] = "user"
        item["is_from_openclaw"] = False
    return item


@app.route("/v1/chat/messages/<message_id>/body", methods=["GET"])
def chat_message_body(message_id):
    store = require_user()
    with store.chat_lock:
        msg = next((m for m in store.chat_messages if str(m.get("id") or "") == str(message_id)), None)
    if not msg:
        return jsonify({"error": "message_not_found"}), 404
    return jsonify({"message": _chat_history_item(msg, include_image_body=True)})


@app.route("/v1/chat/message", methods=["POST"])
def chat_message():
    """User sends a chat message as a v1 ciphertext envelope.

    See docs/DESIGN_E2E.md §3.2 for envelope field definitions. The
    server never decrypts the envelope — it is stored verbatim and
    later surfaced by the enclave's /v1/* handlers.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image"):
        return jsonify({"error": "content_type must be 'text' or 'image'"}), 400
    msg = store.append_chat("user", "chat", envelope, content_type=content_type)
    store.notify_chat_waiters()
    print(f"[chat:{store.user_id}] user(v1, visibility={envelope['visibility']}, type={content_type}) id={msg['id']}")
    return jsonify({"id": msg["id"], "ts": msg["ts"], "v": msg["v"]})


@app.route("/v1/chat/response", methods=["POST"])
def chat_response():
    """Agent posts a reply as a v1 ciphertext envelope. Shape matches
    /v1/chat/message. When the caller supplies plaintext `alert_body` or
    `push_body`, the server applies app-state push policy: background/unknown
    app state gets APNs alert + Live Activity hybrid delivery; active foreground
    app state records a suppression. `push_body` / `alert_body` are plaintext
    metadata (user-visible in APNs surfaces) and are never stored in chat.

    Bootstrap gate: this endpoint 409s if memory_count < the per-age floor
    (see _memory_floor_for_days) or identity is not yet written. See
    _gate_bootstrap_for_chat for the rationale — runtime-level skill text
    isn't enough to stop hallucinated bootstrap completion; the server has
    to enforce it.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    allow_verify_reply = _reply_is_for_pending_verify_ping(store)
    gated = _gate_bootstrap_for_chat(store, allow_verify_reply=allow_verify_reply)
    if gated is not None:
        return gated
    _record_consumer_event(store, "response")
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image"):
        return jsonify({"error": "content_type must be 'text' or 'image'"}), 400
    thinking_envelope = payload.get("thinking_envelope")
    thinking_extra: dict = {}
    if thinking_envelope is not None:
        if not isinstance(thinking_envelope, dict):
            return jsonify({"error": "thinking_envelope must be an object"}), 400
        missing = [f for f in required if not thinking_envelope.get(f)]
        if missing:
            return jsonify({"error": f"thinking_envelope missing fields: {missing}"}), 400
        if thinking_envelope["visibility"] not in ("shared", "local_only"):
            return jsonify({"error": "thinking_envelope.visibility must be 'shared' or 'local_only'"}), 400
        if thinking_envelope["visibility"] == "shared" and not thinking_envelope.get("K_enclave"):
            return jsonify({"error": "thinking_envelope with visibility=shared requires K_enclave"}), 400
        thinking_extra = {
            "thinking_v": str(thinking_envelope.get("v", 1)),
            "thinking_id": str(thinking_envelope.get("id") or ""),
            "thinking_body_ct": str(thinking_envelope["body_ct"]),
            "thinking_nonce": str(thinking_envelope["nonce"]),
            "thinking_K_user": str(thinking_envelope["K_user"]),
            "thinking_visibility": str(thinking_envelope["visibility"]),
            "thinking_owner_user_id": str(thinking_envelope["owner_user_id"]),
            "thinking_enclave_pk_fpr": str(thinking_envelope.get("enclave_pk_fpr") or ""),
        }
        if thinking_envelope.get("K_enclave"):
            thinking_extra["thinking_K_enclave"] = str(thinking_envelope["K_enclave"])
        thinking_extra.update(_chat_thinking_metadata_from_payload(payload))
    else:
        thinking_extra.update(_chat_plaintext_thinking_extra_for_store(store, payload))
    source = str(payload.get("source") or "chat").strip() or "chat"
    if source not in {"chat", "live_activity", "heartbeat", PROACTIVE_JOB_SOURCE}:
        return jsonify({"error": "invalid source"}), 400
    alert_body = str(payload.get("alert_body") or "")
    push_body = str(payload.get("push_body") or "")
    extra = {
        "gate_decision_id": str(payload.get("gate_decision_id") or ""),
        "proactive_job_id": str(payload.get("proactive_job_id") or ""),
        **thinking_extra,
    }
    if source == PROACTIVE_JOB_SOURCE:
        preview = (alert_body or push_body).strip()
        if preview:
            extra["alert_preview"] = preview[:240]
        if push_body.strip():
            extra["push_body_preview"] = push_body.strip()[:240]
        extra["push_live_activity_requested"] = bool(payload.get("push_live_activity"))
    msg = store.append_chat(
        "openclaw",
        source,
        envelope,
        content_type=content_type,
        extra=extra,
    )
    reply_to_message_id = str(
        payload.get("reply_to_message_id")
        or payload.get("reply_to_id")
        or payload.get("in_reply_to")
        or ""
    ).strip()
    if reply_to_message_id:
        store.update_chat_message_metadata(reply_to_message_id, {
            "reply_status": "replied",
            "reply_message_id": str(msg.get("id") or ""),
            "replied_by": _request_chat_consumer_id(),
            "replied_at": f"{time.time():.3f}",
        })
    delivery_fields: dict = {}
    visible_push_body = (push_body or alert_body).strip()
    # Any plaintext AI reply supplied by the caller enters the same app-state
    # policy: background/unknown app state gets Live Activity + APNs alert;
    # foreground app state records a suppression instead of interrupting.
    if visible_push_body or payload.get("push_live_activity"):
        delivery_fields.update(_deliver_ai_message_push_if_background(
            store,
            body=visible_push_body,
            title=payload.get("title", "") or "IO",
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
            visual_state=payload.get("visualState") or payload.get("visual_state") or "reply",
        ))
    if delivery_fields:
        updated = store.update_chat_message_metadata(msg["id"], delivery_fields)
        if updated:
            msg = updated
    print(f"[chat:{store.user_id}] openclaw(v1, source={source}, type={content_type}) id={msg['id']}")
    return jsonify({"id": msg["id"], "ts": msg["ts"], "v": msg["v"]})


def _request_chat_consumer_id() -> str:
    """Stable responder id for chat poll claiming.

    /v1/chat/poll is a responder endpoint, not the normal UI history reader.
    A caller without explicit consumer headers is grouped under "anonymous" so
    two ad-hoc pollers with the same API key do not both claim the same turn.
    """
    raw = (
        request.headers.get("X-Feedling-Consumer-Id")
        or request.args.get("consumer_id")
        or request.headers.get("X-Feedling-Consumer")
        or "anonymous"
    )
    consumer = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(raw).strip())[:160].strip("-")
    return consumer or "anonymous"


def _request_bool_arg(name: str, default: bool = True) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _float_meta(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _chat_message_claimable(msg: dict, consumer_id: str, now: float) -> bool:
    if msg.get("role") != "user":
        return False
    if msg.get("reply_status") == "replied" or msg.get("reply_message_id"):
        return False
    claimed_by = str(msg.get("reply_claimed_by") or "").strip()
    expires_at = _float_meta(msg.get("reply_claim_expires_at"), 0.0)
    return (not claimed_by) or claimed_by == consumer_id or expires_at <= now


def _pending_chat_messages_for_poll(
    store: UserStore,
    *,
    since: float,
    consumer_id: str,
    claim: bool,
) -> list[dict]:
    now = time.time()
    claimed: list[dict] = []
    with store.chat_lock:
        for msg in store.chat_messages:
            if _float_meta(msg.get("ts"), 0.0) <= since:
                continue
            if not _chat_message_claimable(msg, consumer_id, now):
                continue
            if claim:
                fields = {
                    "reply_claimed_by": consumer_id,
                    "reply_claimed_at": f"{now:.3f}",
                    "reply_claim_expires_at": f"{now + max(10, CHAT_POLL_CLAIM_TTL_SEC):.3f}",
                }
                msg.update(fields)
                db.chat_update_metadata(store.user_id, str(msg.get("id") or ""), fields)
            claimed.append(dict(msg))
    return claimed


@app.route("/v1/chat/poll", methods=["GET"])
def chat_poll():
    store = require_user()
    _record_consumer_event(store, "poll")
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    timeout = min(float(request.args.get("timeout", 30)), 60)
    consumer_id = _request_chat_consumer_id()
    claim = _request_bool_arg("claim", default=True)

    pending = _pending_chat_messages_for_poll(
        store,
        since=since,
        consumer_id=consumer_id,
        claim=claim,
    )
    if pending:
        return jsonify({"messages": pending, "timed_out": False, "consumer_id": consumer_id, "claimed": claim})

    ev = threading.Event()
    with store.chat_waiters_lock:
        store.chat_waiters.append(ev)

    notified = ev.wait(timeout=timeout)

    with store.chat_waiters_lock:
        try:
            store.chat_waiters.remove(ev)
        except ValueError:
            pass

    if notified:
        pending = _pending_chat_messages_for_poll(
            store,
            since=since,
            consumer_id=consumer_id,
            claim=claim,
        )
        return jsonify({"messages": pending, "timed_out": False, "consumer_id": consumer_id, "claimed": claim})
    return jsonify({"messages": [], "timed_out": True, "consumer_id": consumer_id, "claimed": claim})


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def _load_identity(store: UserStore) -> dict | None:
    try:
        data = db.get_blob(store.user_id, "identity")
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[{store.user_id}/identity] load failed: {e}")
    return None


def _save_identity(store: UserStore, data: dict):
    with store.identity_lock:
        db.set_blob(store.user_id, "identity", data)


# Identity change audit log
# ---------------------------------------------------------------------------
# Appended to on every identity_init / replace / nudge. Surfaced to iOS as
# the "最近的变化" feed and the local push trigger. Server doesn't decrypt
# the envelope, so the diff (dimension / old / new / reason) is supplied
# by the caller — the MCP tools do this; HTTP-mode callers can pass an
# optional `audit` field on identity_init / identity_replace requests.

def _append_identity_change(store: UserStore, entry: dict) -> dict:
    """Append a single audit entry. Always returns the stored entry
    (with `id` and `ts` injected) so the caller can echo it back. Never
    raises — audit failures must not break the underlying write."""
    record = {
        "id": uuid.uuid4().hex[:16],
        "ts": datetime.now().isoformat(),
        "action": entry.get("action", "unknown"),
    }
    # Whitelist + coerce the fields the iOS card needs. Anything else
    # the caller submits is dropped silently so we don't leak whatever
    # debugging junk the agent stuffed in.
    for k in ("dimension", "old_value", "new_value", "delta", "reason"):
        if k in entry:
            record[k] = entry[k]
    # ts here is an ISO string, not an epoch — leave the indexed ts column NULL
    # and keep the since/sort filtering in Python (string comparison) below.
    db.log_append(store.user_id, "identity_changes", record)
    return record


def _load_identity_changes(store: UserStore, since: str = "", limit: int = 50) -> list:
    """Read the audit log. `since` is an ISO timestamp string; results
    are filtered to entries with ts > since, newest-first, capped at limit."""
    entries = db.log_read_all(store.user_id, "identity_changes")
    if since:
        entries = [e for e in entries if e.get("ts", "") > since]
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:limit]


def _parse_iso_calendar_date(value: str) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except Exception:
            pass
    m = re.match(r"^\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})(?:\D|$)", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    try:
        norm = raw.replace("年", "-").replace("月", "-").replace("日", "")
        norm = norm.replace("/", "-").replace(".", "-").replace("Z", "+00:00")
        if "T" not in norm:
            norm = norm + "T00:00:00"
        return datetime.fromisoformat(norm).date()
    except Exception:
        return None


def _earliest_memory_date(store: UserStore) -> date | None:
    dates: list[date] = []
    for moment in _load_moments(store):
        if not isinstance(moment, dict):
            continue
        d = _parse_iso_calendar_date(moment.get("occurred_at", ""))
        if d:
            dates.append(d)
    return min(dates) if dates else None


def _anchor_from_days(days: int, store: UserStore | None = None, prefer_memory: bool = False) -> str:
    """Convert "we've known each other N days" into a fixed ISO timestamp.

    The anchor is the source of truth for days_with_user — every read computes
    a calendar-day delta from this date, so the displayed count increments at
    midnight instead of at the exact bootstrap hour.
    """
    if prefer_memory and store is not None:
        earliest = _earliest_memory_date(store)
        if earliest:
            return earliest.isoformat()
    safe_days = max(0, int(days))
    started_at = datetime.now().date() - timedelta(days=safe_days)
    return started_at.isoformat()


def _live_days_with_user(identity: dict, store: UserStore | None = None) -> int:
    """Compute the live days_with_user from the relationship anchor."""
    anchor_date = _parse_iso_calendar_date(identity.get("relationship_started_at", ""))

    # Migration repair for anchors created from server UTC time after the
    # user's local midnight boundary: if old identities have no explicit
    # anchor source and the memory garden proves an earlier first date, use it.
    if store is not None and not identity.get("relationship_anchor_source"):
        earliest = _earliest_memory_date(store)
        if earliest and (anchor_date is None or earliest < anchor_date):
            anchor_date = earliest

    if not anchor_date:
        return 0
    return max(0, (datetime.now().date() - anchor_date).days)


_IDENTITY_RUNTIME_LABELS = {
    "io", "feedling", "p0", "p-zero",
    "hermes", "claude", "claude code", "claude desktop", "claude-code", "claude-desktop",
    "claude.ai", "anthropic", "openclaw", "open-claw", "open claw", "cursor",
    "chatgpt", "chat-gpt", "gpt", "gpt-4", "gpt-4o", "gpt-5", "openai", "openrouter",
    "gemini", "google ai", "google", "bard", "deepseek", "minimax", "copilot", "github copilot",
    "agent", "assistant", "ai", "bot",
}

_IDENTITY_PROFILE_STRING_FIELDS = (
    "agent_name",
    "self_introduction",
    "category",
    "user_preferred_name",
    "agent_role",
    "tone_style",
    "language_preference",
    "relationship_anchor",
)
_IDENTITY_PROFILE_LIST_FIELDS = (
    "signature",
    "boundaries",
    "do_not_say",
    "stable_definitions",
)
_IDENTITY_PROFILE_FIELDS = set(_IDENTITY_PROFILE_STRING_FIELDS) | set(_IDENTITY_PROFILE_LIST_FIELDS)


def _identity_action_text(value, max_chars: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars].strip()


def _identity_plain_for_action(store: UserStore, api_key: str | None) -> tuple[dict | None, str]:
    data, err = _enclave_get_json_for_gate("/v1/identity/get", api_key)
    if err:
        return None, err
    if not isinstance(data, dict) or not isinstance(data.get("identity"), dict):
        return None, "identity_not_initialized"
    identity = data["identity"]
    status = identity.get("decrypt_status")
    if status and status != "ok":
        return None, str(status)
    return identity, ""


def _identity_payload_from_plain(identity: dict) -> dict:
    payload = {
        "agent_name": str(identity.get("agent_name") or "")[:80],
        "self_introduction": str(identity.get("self_introduction") or "")[:1200],
        "dimensions": identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else [],
    }
    for key in _IDENTITY_PROFILE_STRING_FIELDS:
        if key in {"agent_name", "self_introduction"}:
            continue
        if identity.get(key):
            payload[key] = str(identity.get(key) or "")[:1200 if key in {"relationship_anchor", "tone_style"} else 240]
    for key in _IDENTITY_PROFILE_LIST_FIELDS:
        if isinstance(identity.get(key), list):
            payload[key] = [str(item)[:240] for item in identity[key][:12] if str(item or "").strip()]
    return payload


def _save_identity_action_payload(
    store: UserStore,
    payload: dict,
    *,
    audit: dict,
    event_type: str,
) -> tuple[dict | None, dict | None, str]:
    existing = _load_identity(store)
    if not existing:
        return None, None, "identity_not_initialized"
    if not existing.get("relationship_started_at"):
        return None, None, "identity_relationship_anchor_missing"
    envelope, err = _build_shared_envelope_for_store(
        store,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        item_id=existing.get("id") or None,
    )
    if envelope is None:
        return None, None, err

    now = _now_iso()
    identity = {
        "v": 1,
        "id": envelope.get("id") or existing.get("id") or uuid.uuid4().hex,
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "relationship_started_at": existing.get("relationship_started_at", ""),
        "relationship_anchor_source": existing.get("relationship_anchor_source", ""),
        "relationship_anchor_evidence": existing.get("relationship_anchor_evidence", ""),
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    _save_identity(store, identity)
    _log_bootstrap_event(store, event_type, success=True)
    change = _append_identity_change(store, audit)
    return identity, change, ""


def _identity_profile_patch(store: UserStore, api_key: str | None, action: dict) -> tuple[dict, list[dict], int]:
    patch = action.get("patch") if isinstance(action.get("patch"), dict) else {}
    for key in _IDENTITY_PROFILE_FIELDS:
        if key in action and key not in patch:
            patch[key] = action[key]
    if not patch:
        return {"status": "error", "error": "patch_required", "action": "identity.profile_patch"}, [], 400

    plain, err = _identity_plain_for_action(store, api_key)
    if plain is None:
        return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 409

    payload = _identity_payload_from_plain(plain)
    changed: list[str] = []
    audit_old = ""
    audit_new = ""

    if "agent_name" in patch:
        new_name = _identity_action_text(patch.get("agent_name"), 80).strip(" `\"'“”‘’。，,.;；:：!！?？")
        if not new_name:
            return {"status": "error", "error": "agent_name_empty", "action": "identity.profile_patch"}, [], 400
        if new_name.lower() in _IDENTITY_RUNTIME_LABELS:
            return {"status": "error", "error": "agent_name_too_generic", "action": "identity.profile_patch"}, [], 400
        old_name = str(payload.get("agent_name") or "")
        if new_name != old_name:
            payload["agent_name"] = new_name
            changed.append("agent_name")
            audit_old = old_name
            audit_new = new_name

    if "self_introduction" in patch:
        intro = str(patch.get("self_introduction") or "").strip()[:1200]
        if not intro:
            return {"status": "error", "error": "self_introduction_empty", "action": "identity.profile_patch"}, [], 400
        old_intro = str(payload.get("self_introduction") or "")
        if intro != old_intro:
            payload["self_introduction"] = intro
            changed.append("self_introduction")
            if not audit_old and not audit_new:
                audit_old = old_intro[:120]
                audit_new = intro[:120]

    for key in _IDENTITY_PROFILE_STRING_FIELDS:
        if key in {"agent_name", "self_introduction"} or key not in patch:
            continue
        max_len = 1200 if key in {"relationship_anchor", "tone_style"} else 240
        new_value = _identity_action_text(patch.get(key), max_len)
        old_value = str(payload.get(key) or "")
        if new_value != old_value:
            if new_value:
                payload[key] = new_value
            else:
                payload.pop(key, None)
            changed.append(key)
            if not audit_old and not audit_new:
                audit_old = old_value[:120]
                audit_new = new_value[:120]

    for key in _IDENTITY_PROFILE_LIST_FIELDS:
        if key not in patch:
            continue
        raw_list = patch.get(key)
        if isinstance(raw_list, str):
            raw_list = [raw_list]
        if not isinstance(raw_list, list):
            return {"status": "error", "error": f"{key}_must_be_list", "action": "identity.profile_patch"}, [], 400
        values = [_identity_action_text(item, 240) for item in raw_list[:12]]
        values = [item for item in values if item]
        old_values = payload.get(key) if isinstance(payload.get(key), list) else []
        if values != old_values:
            if values:
                payload[key] = values
            else:
                payload.pop(key, None)
            changed.append(key)
            if not audit_old and not audit_new:
                audit_old = ", ".join(old_values)[:120]
                audit_new = ", ".join(values)[:120]

    if not changed:
        return {
            "status": "ok",
            "action": "identity.profile_patch",
            "changed_fields": [],
            "noop": True,
        }, [], 200

    reason = _identity_action_text(
        action.get("reason") or f"Identity profile updated: {', '.join(changed)}.",
        500,
    )
    identity, change, err = _save_identity_action_payload(
        store,
        payload,
        audit={
            "action": "profile_patch",
            "dimension": "profile",
            "old_value": audit_old,
            "new_value": audit_new,
            "reason": reason,
        },
        event_type="identity_action_profile_patch",
    )
    if identity is None:
        return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 409

    effect = {
        "type": "identity_updated",
        "action": "identity.profile_patch",
        "fields": changed,
        "identity_id": identity.get("id", ""),
        "change_id": change.get("id", "") if change else "",
    }
    return {
        "status": "ok",
        "action": "identity.profile_patch",
        "changed_fields": changed,
        "identity": {
            "id": identity.get("id", ""),
            "updated_at": identity.get("updated_at", ""),
            "days_with_user": _live_days_with_user(identity, store=store),
        },
        "change": change or {},
    }, [effect], 200


def _identity_dimension_nudge(store: UserStore, api_key: str | None, action: dict) -> tuple[dict, list[dict], int]:
    dimension_name = _identity_action_text(action.get("dimension") or action.get("dimension_name"), 80)
    if not dimension_name:
        return {"status": "error", "error": "dimension_required", "action": "identity.dimension_nudge"}, [], 400
    try:
        delta = int(action.get("delta"))
    except Exception:
        return {"status": "error", "error": "delta_required", "action": "identity.dimension_nudge"}, [], 400

    plain, err = _identity_plain_for_action(store, api_key)
    if plain is None:
        return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 409

    payload = _identity_payload_from_plain(plain)
    dims = list(payload.get("dimensions") or [])
    matched = None
    for dim in dims:
        if isinstance(dim, dict) and str(dim.get("name") or "").strip().lower() == dimension_name.lower():
            matched = dim
            break
    if matched is None:
        return {"status": "error", "error": "dimension_not_found", "action": "identity.dimension_nudge"}, [], 404
    try:
        old_value = int(matched.get("value", 0))
    except Exception:
        old_value = 0
    new_value = max(0, min(100, old_value + delta))
    if new_value == old_value:
        return {
            "status": "ok",
            "action": "identity.dimension_nudge",
            "changed_fields": [],
            "noop": True,
        }, [], 200
    matched["value"] = new_value
    reason = _identity_action_text(action.get("reason") or f"{dimension_name} adjusted by {delta:+d}.", 500)
    if reason:
        matched["last_nudge_reason"] = reason
    payload["dimensions"] = dims

    identity, change, err = _save_identity_action_payload(
        store,
        payload,
        audit={
            "action": "nudge",
            "dimension": dimension_name,
            "old_value": old_value,
            "new_value": new_value,
            "delta": delta,
            "reason": reason,
        },
        event_type="identity_action_dimension_nudge",
    )
    if identity is None:
        return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 409
    effect = {
        "type": "identity_updated",
        "action": "identity.dimension_nudge",
        "fields": ["dimensions"],
        "identity_id": identity.get("id", ""),
        "change_id": change.get("id", "") if change else "",
    }
    return {
        "status": "ok",
        "action": "identity.dimension_nudge",
        "changed_fields": ["dimensions"],
        "identity": {
            "id": identity.get("id", ""),
            "updated_at": identity.get("updated_at", ""),
            "days_with_user": _live_days_with_user(identity, store=store),
        },
        "change": change or {},
    }, [effect], 200


def _identity_relationship_days_set(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    try:
        days = int(action.get("days_with_user"))
    except Exception:
        return {"status": "error", "error": "days_with_user_required", "action": "identity.relationship_days_set"}, [], 400
    if days < 0:
        return {"status": "error", "error": "days_with_user_must_be_non_negative", "action": "identity.relationship_days_set"}, [], 400
    existing = _load_identity(store)
    if not existing:
        return {"status": "error", "error": "identity_not_initialized", "action": "identity.relationship_days_set"}, [], 409
    old_days = _live_days_with_user(existing, store=store)
    identity = dict(existing)
    identity["updated_at"] = _now_iso()
    identity["relationship_started_at"] = _anchor_from_days(days)
    identity["relationship_anchor_source"] = "user_calibrated"
    evidence = _identity_action_text(action.get("relationship_anchor_evidence") or action.get("reason") or "", 500)
    if evidence:
        identity["relationship_anchor_evidence"] = evidence
    _save_identity(store, identity)
    _log_bootstrap_event(store, "identity_action_relationship_days_set", success=True)
    change = _append_identity_change(store, {
        "action": "relationship_days",
        "dimension": "relationship_days",
        "old_value": old_days,
        "new_value": days,
        "delta": days - old_days,
        "reason": evidence or "Relationship day count recalibrated.",
    })
    effect = {
        "type": "identity_updated",
        "action": "identity.relationship_days_set",
        "fields": ["days_with_user"],
        "identity_id": identity.get("id", ""),
        "change_id": change.get("id", "") if change else "",
    }
    return {
        "status": "ok",
        "action": "identity.relationship_days_set",
        "changed_fields": ["days_with_user"],
        "identity": {
            "id": identity.get("id", ""),
            "updated_at": identity.get("updated_at", ""),
            "days_with_user": days,
        },
        "change": change or {},
    }, [effect], 200


def _execute_identity_action(store: UserStore, api_key: str | None, action: dict) -> tuple[dict, list[dict], int]:
    if not isinstance(action, dict):
        return {"status": "error", "error": "action_must_be_object"}, [], 400
    action_type = str(action.get("type") or action.get("action") or "").strip()
    if action_type == "identity.profile_patch":
        return _identity_profile_patch(store, api_key, action)
    if action_type == "identity.dimension_nudge":
        return _identity_dimension_nudge(store, api_key, action)
    if action_type == "identity.relationship_days_set":
        return _identity_relationship_days_set(store, action)
    return {
        "status": "error",
        "error": "unsupported_identity_action",
        "action": action_type,
        "supported": [
            "identity.profile_patch",
            "identity.dimension_nudge",
            "identity.relationship_days_set",
        ],
    }, [], 400


def _execute_identity_actions(store: UserStore, api_key: str | None, actions: list[dict]) -> tuple[dict, int]:
    if not isinstance(actions, list) or not actions:
        return {"status": "error", "error": "actions_required", "results": [], "effects": []}, 400
    results: list[dict] = []
    effects: list[dict] = []
    for action in actions[:10]:
        result, action_effects, status = _execute_identity_action(store, api_key, action)
        results.append(result)
        effects.extend(action_effects)
        if status >= 400:
            return {
                "status": "error",
                "error": result.get("error", "identity_action_failed"),
                "results": results,
                "effects": effects,
            }, status
    return {"status": "ok", "results": results, "effects": effects}, 200


@app.route("/v1/identity/actions", methods=["POST"])
def identity_actions():
    store = require_user()
    api_key = _extract_api_key()
    payload = request.get_json(silent=True) or {}
    actions = payload.get("actions")
    if actions is None and isinstance(payload.get("action"), dict):
        actions = [payload["action"]]
    elif actions is None and (payload.get("type") or payload.get("action")):
        actions = [payload]
    if not isinstance(actions, list):
        return jsonify({"error": "actions required"}), 400
    body, status = _execute_identity_actions(store, api_key, actions)
    return jsonify(body), status


# ---------------------------------------------------------------------------
# Bootstrap stage gating
# ---------------------------------------------------------------------------
#
# Multiple production incidents (2026-05-13..15) showed Agent runtimes
# (OpenClaw in particular) skipping the memory-write phase of bootstrap
# and going straight to identity_init / chat_post. The Agent's own narrative
# would claim "I wrote 18 cards" while the server actually had zero — pure
# hallucination of completion.
#
# Skill-text rules alone cannot enforce this; behavior depends on the
# runtime's attention, prompt adherence, and failure modes. Server-side
# gates make the contract explicit: writes that violate the protocol
# return 409 with the missing prerequisite, and the Agent must satisfy
# the prerequisite before retrying.
#
# Floor is dynamic by relationship age — see _memory_floor_for_days
# (defined further down with the verify endpoints). A 6-month relationship
# legitimately demands more cards than a "we just met today" one. The
# previous hardcoded floor=3 was a compromise that was too strict for
# brand-new relationships AND too lax for established ones; replaced
# 2026-05-19 with the per-age table that's already documented in skill.

# Public skill URL — included in 409 responses so Agents that don't carry
# the skill in context can refetch it. Single source of truth.
_SKILL_URL = "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md"


def _bootstrap_state(store) -> dict:
    """Snapshot of bootstrap completion for `store`. Read-only; safe to call
    on every write path. Source of truth: on-disk identity + memory files.

    Returns:
        {
          memory_count: int,                # total across tabs
          memory_floor: int,                # total floor (back-compat)
          counts: {story, about_me, ta_thinking, total},
          floors: {story, about_me, ta_thinking, total},
          identity_written: bool,
          stage: str ∈ {"needs_memory", "needs_identity", "main_loop"},
          missing_tabs: [tab_name, ...]     # Which tab floors are unmet
        }

    Gate semantics (post-typed-memory rewrite):
      - "needs_memory" means Story floor OR About me floor not yet met.
        TA 在想 (insight/reflection) is encouraged but not blocking —
        reflections need substrate from the other two tabs first, so
        gating on it would create a deadlock at low-density tiers.
    """
    moments = _load_moments(store)
    counts = _count_by_tab(moments)
    identity_written = _load_identity(store) is not None
    floors = _per_tab_floors_for_days(_relationship_age_days(store))

    missing_tabs = []
    if counts["story"] < floors["story"]:
        missing_tabs.append("story")
    if counts["about_me"] < floors["about_me"]:
        missing_tabs.append("about_me")

    if missing_tabs:
        stage = "needs_memory"
    elif not identity_written:
        stage = "needs_identity"
    else:
        stage = "main_loop"

    return {
        "memory_count": counts["total"],
        "memory_floor": floors["total"],
        "counts": counts,
        "floors": floors,
        "identity_written": identity_written,
        "stage": stage,
        "missing_tabs": missing_tabs,
    }


def _gate_required_for_missing_tabs(state) -> str:
    """Human-readable instruction string for the missing tabs in `state`."""
    c = state["counts"]
    f = state["floors"]
    parts = []
    if "story" in state["missing_tabs"]:
        parts.append(
            f"Story tab {c['story']}/{f['story']} — write more moment/quote memories"
        )
    if "about_me" in state["missing_tabs"]:
        parts.append(
            f"About me tab {c['about_me']}/{f['about_me']} — write more fact/event memories "
            f"(this is the density layer — preferences, relationships, dates, habits)"
        )
    return (
        "Per-tab memory floors are below threshold: "
        + "; ".join(parts)
        + ". Use feedling_memory_add_moment(type=...) for each. Then call "
        "feedling_identity_init. Do not fabricate Pass 4 summaries — the cards "
        "must actually exist."
    )


def _chat_loop_verified_by_server(store) -> bool:
    events = _load_bootstrap_events(store)
    if any(
        e.get("event_type") == "chat_loop_verified" and e.get("success") is True
        for e in events
    ):
        return True

    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    sorted_msgs = sorted(
        chat_msgs,
        key=lambda m: float(m.get("ts") or m.get("timestamp") or 0),
    )
    seen_user = False
    for m in sorted_msgs:
        role = m.get("role")
        if role == "user" and m.get("source") != "verify_ping":
            seen_user = True
        elif role in ("agent", "openclaw") and seen_user:
            return True
    return False


def _reply_is_for_pending_verify_ping(store) -> bool:
    """True when an unanswered synthetic verify ping is awaiting its reply.

    /v1/chat/verify_loop must be able to receive one agent response before
    the visible chat gate is open. The synthetic ping (and the matching
    reply) are removed after the verify completes, so this does not leak
    into user chat.

    This originally required the verify ping to be the single most-recent
    message. That wedged actively-chatted accounts (prod 2026-06-03): a real
    user message arriving during the verify window became 'newest', so the
    consumer's correct reply to the pending ping was treated as an ordinary
    chat reply and 409'd with needs_live_connection. With no reply ever
    landing, chat_loop_verified never flipped and the gate never opened.

    So we now allow the reply whenever an UNANSWERED verify ping exists — a
    verify_ping user message with no agent/openclaw reply after it — even if
    newer real user messages have since arrived. A single landed reply then
    satisfies verify_loop and opens the gate permanently; the liveness proof
    (an actual reply POST) is unchanged.
    """
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    sorted_msgs = sorted(
        chat_msgs,
        key=lambda m: float(m.get("ts") or m.get("timestamp") or 0),
    )
    pending = False
    for m in sorted_msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user" and m.get("source") == "verify_ping":
            pending = True
        elif role in ("agent", "openclaw"):
            # An agent reply consumes the outstanding ping; a later ping
            # re-arms it.
            pending = False
    return pending


def _gate_bootstrap_for_chat(store, allow_verify_reply: bool = False):
    """Refuse /v1/chat/response when bootstrap is incomplete.

    Returns a (response, status) tuple to be returned by the caller, or None
    when the call may proceed. The response body carries `stage` and
    `required` so the Agent receives an actionable error rather than a
    generic 403/500.
    """
    state = _bootstrap_state(store)
    if state["stage"] == "main_loop":
        if allow_verify_reply:
            return None
        consumer_state = _consumer_validation_state(store)
        if not consumer_state["passing"]:
            print(
                f"[gate:{store.user_id}] chat_response blocked stage=needs_resident_consumer "
                f"consumer={consumer_state.get('consumer_name')} recent={consumer_state.get('age_sec')}"
            )
            return jsonify({
                "error": "bootstrap_incomplete",
                "stage": "needs_resident_consumer",
                "memory_count": state["memory_count"],
                "memory_floor": state["memory_floor"],
                "counts": state["counts"],
                "floors": state["floors"],
                "missing_tabs": state["missing_tabs"],
                "identity_written": state["identity_written"],
                "resident_consumer": consumer_state,
                "required": consumer_state["required"],
                "skill_url": _SKILL_URL,
            }), 409
        if not _chat_loop_verified_by_server(store):
            required = (
                "After the standard resident consumer is running, call "
                "feedling_chat_verify_loop and wait for passing=true before "
                "sending any visible IO Chat greeting."
            )
            print(f"[gate:{store.user_id}] chat_response blocked stage=needs_live_connection")
            return jsonify({
                "error": "bootstrap_incomplete",
                "stage": "needs_live_connection",
                "memory_count": state["memory_count"],
                "memory_floor": state["memory_floor"],
                "counts": state["counts"],
                "floors": state["floors"],
                "missing_tabs": state["missing_tabs"],
                "identity_written": state["identity_written"],
                "resident_consumer": consumer_state,
                "required": required,
                "skill_url": _SKILL_URL,
            }), 409
        return None
    if state["stage"] == "needs_memory":
        required = _gate_required_for_missing_tabs(state)
    else:  # needs_identity
        required = (
            "Call feedling_identity_init with the derived identity card "
            "(7 dimensions + days_with_user) BEFORE you can post chat."
        )
    print(f"[gate:{store.user_id}] chat_response blocked stage={state['stage']} "
          f"missing={state['missing_tabs']} id={state['identity_written']}")
    return jsonify({
        "error": "bootstrap_incomplete",
        "stage": state["stage"],
        "memory_count": state["memory_count"],
        "memory_floor": state["memory_floor"],
        "counts": state["counts"],
        "floors": state["floors"],
        "missing_tabs": state["missing_tabs"],
        "identity_written": state["identity_written"],
        "required": required,
        "skill_url": _SKILL_URL,
    }), 409


def _gate_bootstrap_for_identity_init(store):
    """Refuse /v1/identity/init when Story or About me tab floors are unmet.

    Identity must be DERIVED from memory substrate — writing identity in the
    30+ day tier with only 2 cards means the Agent skipped the depth pass.
    TA 在想 floor is advisory at this gate (reflections need other-tab
    substrate first, gating on it would deadlock low-density users).
    """
    state = _bootstrap_state(store)
    if not state["missing_tabs"]:
        return None
    print(f"[gate:{store.user_id}] identity_init blocked missing={state['missing_tabs']} "
          f"counts={state['counts']} floors={state['floors']}")
    return jsonify({
        "error": "bootstrap_incomplete",
        "stage": "needs_memory",
        "memory_count": state["memory_count"],
        "memory_floor": state["memory_floor"],
        "counts": state["counts"],
        "floors": state["floors"],
        "missing_tabs": state["missing_tabs"],
        "required": _gate_required_for_missing_tabs(state)
                    + " Identity dimensions must be derived from real cards, not invented.",
        "skill_url": _SKILL_URL,
    }), 409


@app.route("/v1/identity/get", methods=["GET"])
def identity_get():
    store = require_user()
    data = _load_identity(store)
    if data is None:
        return jsonify({"identity": None})
    # Inject the live-computed days alongside the envelope. iOS decrypts the
    # envelope locally, but it never sees the anchor itself — it just reads
    # this top-level field. Same convention as the enclave proxy.
    enriched = dict(data)
    enriched["days_with_user"] = _live_days_with_user(data, store=store)
    return jsonify({"identity": enriched})


@app.route("/v1/identity/init", methods=["POST"])
def identity_init():
    """Initialize the identity card as a v1 envelope. body_ct wraps
    {agent_name, self_introduction, dimensions} serialized as JSON.
    Plaintext metadata: id, created_at, updated_at. See DESIGN_E2E.md §3.2.

    Bootstrap gate: requires memory_count >= the per-age floor (see
    _memory_floor_for_days). Identity must be DERIVED from memories per
    skill protocol; writing identity without depth proportional to the
    relationship age means the Agent skipped the depth pass.
    See _gate_bootstrap_for_identity_init.
    """
    store = require_user()
    existing = _load_identity(store)
    if existing is not None:
        return jsonify({"error": "already_initialized", "identity": existing}), 409
    gated = _gate_bootstrap_for_identity_init(store)
    if gated is not None:
        return gated

    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    # Defense-in-depth: refuse envelopes whose claimed owner_user_id doesn't
    # match the authenticated caller. The enclave's AEAD AAD check would also
    # catch this later (decrypt fails on owner_user_id ≠ authorized_user_id),
    # but rejecting at write time keeps the on-disk state consistent with the
    # auth boundary. memory_add already does this — bring identity inline.
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    # days_with_user is mandatory at init — Agent must compute and submit it.
    # We persist it as relationship_started_at (a fixed anchor) so subsequent
    # reads can compute the live count without going through the Agent again.
    days_with_user = payload.get("days_with_user")
    if days_with_user is None or not isinstance(days_with_user, int) or days_with_user < 0:
        return jsonify({"error": "days_with_user (non-negative int) required at init"}), 400
    relationship_anchor_evidence = str(payload.get("relationship_anchor_evidence") or "").strip()
    if len(relationship_anchor_evidence) < 8:
        return jsonify({
            "error": "relationship_anchor_evidence required at init",
            "required": (
                "Pass a concrete source for the earliest relationship date "
                "(transcript/session/file/message pointer or user-confirmed fresh start). "
                "Do not guess days_with_user."
            ),
        }), 400
    earliest_memory_date = _earliest_memory_date(store)
    if earliest_memory_date:
        computed_days = max(0, (datetime.now().date() - earliest_memory_date).days)
        if abs(computed_days - days_with_user) > 1:
            return jsonify({
                "error": "days_with_user_mismatch",
                "days_with_user": days_with_user,
                "computed_from_earliest_memory": computed_days,
                "earliest_memory_date": earliest_memory_date.isoformat(),
                "required": (
                    "Recompute days_with_user from the earliest memory's occurred_at "
                    "before calling feedling_identity_init."
                ),
            }), 400

    now = datetime.now().isoformat()
    identity = {
        "v": 1,
        "id": envelope.get("id") or uuid.uuid4().hex,
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": now,
        "updated_at": now,
        "relationship_started_at": _anchor_from_days(days_with_user, store=store, prefer_memory=True),
        "relationship_anchor_source": "earliest_memory" if earliest_memory_date else "days_with_user",
        "relationship_anchor_evidence": relationship_anchor_evidence,
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    _save_identity(store, identity)
    _log_bootstrap_event(store, "identity_written_v1", success=True)
    # Audit log: identity_init is always an "init" marker. The caller may
    # pass an `audit.reason` if it wants a custom one ("first day with this
    # user"); otherwise default to a neutral bootstrap-complete note.
    audit_payload = payload.get("audit") or {}
    _append_identity_change(store, {
        "action": "init",
        "reason": audit_payload.get("reason", "Identity card written for the first time."),
    })
    print(f"[identity:{store.user_id}] initialized v1 visibility={envelope['visibility']} anchor={identity['relationship_started_at']}")
    return jsonify({"status": "created", "identity": identity, "v": 1}), 201


@app.route("/v1/identity/replace", methods=["POST"])
def identity_replace():
    """Phase C part 3: replace the identity card in place. Used by MCP
    to implement `identity.nudge` on v1 cards — MCP fetches the
    decrypted card from the enclave, mutates one dimension, re-wraps,
    POSTs here. Same envelope shape as `/v1/identity/init` but does NOT
    409 when a card already exists. Preserves the original `created_at`
    so the card's history tracking is intact.
    """
    store = require_user()
    existing = _load_identity(store)
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    now = datetime.now().isoformat()

    if envelope is None:
        return jsonify({"error": "envelope required for replace; use /v1/identity/init for plaintext"}), 400

    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    # Defense-in-depth: same owner check identity_init now does. See comment
    # there for why.
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    created_at = existing.get("created_at") if existing else now
    # Preserve the existing relationship anchor unless the caller explicitly
    # passes a new days_with_user. nudge / dimension rewrite must NOT bump the
    # anchor; only an intentional calibration ever should.
    days_with_user = payload.get("days_with_user")
    if days_with_user is not None:
        if not isinstance(days_with_user, int) or days_with_user < 0:
            return jsonify({"error": "days_with_user must be a non-negative int"}), 400
        relationship_started_at = _anchor_from_days(days_with_user)
        relationship_anchor_source = "user_calibrated"
        relationship_anchor_evidence = (payload.get("relationship_anchor_evidence") or "").strip()
        if not relationship_anchor_evidence and existing:
            relationship_anchor_evidence = existing.get("relationship_anchor_evidence", "")
    elif existing and existing.get("relationship_started_at"):
        relationship_started_at = existing["relationship_started_at"]
        relationship_anchor_source = existing.get("relationship_anchor_source", "")
        relationship_anchor_evidence = existing.get("relationship_anchor_evidence", "")
    else:
        # First-ever write through replace (no prior init). Reject so callers
        # are forced through init's mandatory days_with_user path.
        return jsonify({"error": "no relationship anchor on file; call /v1/identity/init first"}), 400

    identity = {
        "v": 1,
        "id": envelope.get("id") or (existing.get("id") if existing else uuid.uuid4().hex),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": created_at,
        "updated_at": now,
        "relationship_started_at": relationship_started_at,
        "relationship_anchor_source": relationship_anchor_source,
        "relationship_anchor_evidence": relationship_anchor_evidence,
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    _save_identity(store, identity)
    _log_bootstrap_event(store, "identity_replaced_v1", success=True)
    # Audit log: replace can be a single-dimension nudge (MCP tool passes
    # `audit.action: "nudge"` with dimension/old/new/delta) or a full
    # rewrite (`audit.action: "replace"`). When no audit field, log a
    # generic replace marker — better than dropping the event entirely.
    audit_payload = payload.get("audit") or {}
    _append_identity_change(store, {
        "action": audit_payload.get("action", "replace"),
        "dimension": audit_payload.get("dimension"),
        "old_value": audit_payload.get("old_value"),
        "new_value": audit_payload.get("new_value"),
        "delta": audit_payload.get("delta"),
        "reason": audit_payload.get("reason", ""),
    })
    print(f"[identity:{store.user_id}] replaced v1 visibility={envelope['visibility']} anchor={relationship_started_at}")
    return jsonify({"status": "replaced", "identity": identity, "v": 1})


@app.route("/v1/identity/changes", methods=["GET"])
def identity_changes():
    """Read the identity-change audit log. Used by iOS to render the
    "最近的变化" feed in IdentityView and to detect new events for
    local push notifications.

    Query params:
      since: ISO timestamp; only return entries with ts > since
      limit: cap on number of entries returned (default 50, max 200)

    Response: {"changes": [...], "total": N}. Entries are newest-first.
    Each entry has {id, ts, action, [dimension, old_value, new_value,
    delta, reason]}. Server doesn't decrypt anything — these fields are
    plaintext metadata supplied by the writing path (MCP tools call
    /v1/identity/replace with an `audit` payload).
    """
    store = require_user()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    since = request.args.get("since", "")
    changes = _load_identity_changes(store, since=since, limit=limit)
    return jsonify({"changes": changes, "total": len(changes)})


@app.route("/v1/identity/relationship_anchor", methods=["POST"])
def identity_relationship_anchor():
    """Update only the relationship anchor (days_with_user), without touching
    the encrypted identity envelope.

    Used by the bootstrap calibration step: Agent estimates days, sends the
    initial card, asks the user "we've known each other ~N days, right?",
    and on correction calls this endpoint to fix the anchor — no envelope
    re-encryption needed.
    """
    store = require_user()
    existing = _load_identity(store)
    if existing is None:
        return jsonify({"error": "identity not initialized"}), 404

    payload = request.get_json(silent=True) or {}
    days_with_user = payload.get("days_with_user")
    if days_with_user is None or not isinstance(days_with_user, int) or days_with_user < 0:
        return jsonify({"error": "days_with_user (non-negative int) required"}), 400

    existing["relationship_started_at"] = _anchor_from_days(days_with_user)
    existing["relationship_anchor_source"] = "user_calibrated"
    existing["updated_at"] = datetime.now().isoformat()
    _save_identity(store, existing)
    print(f"[identity:{store.user_id}] anchor updated → {existing['relationship_started_at']} (days={days_with_user})")
    return jsonify({"status": "updated", "relationship_started_at": existing["relationship_started_at"]})


# Note: /v1/identity/nudge no longer exists on the backend. Identity cards
# are v1 ciphertext; mutation happens inside the enclave via MCP's
# decrypt-mutate-rewrap flow (see backend/mcp_server.py `_identity_nudge_v1`).


# ---------------------------------------------------------------------------
# Memory garden
# ---------------------------------------------------------------------------
#
# Memory model (post-2026-05-22 Friend-Test → typed-density rewrite):
#
#   Story tab        : moment, quote
#   About me tab     : fact, event
#   TA 在想 tab      : insight, reflection
#
# Each memory carries plaintext `type` metadata (alongside occurred_at /
# source / visibility / owner_user_id) so the server can validate the
# enum, count per-tab, gate identity_init by per-tab floor, and enforce
# reflection's substrate gate (≥2 anchor memories) + time cap.
#
# Insight requires anchor_memory_ids of length ≥1 (existing memories).
# Reflection requires anchor_memory_ids of length ≥2 AND obeys a per-tier
# time cap on the rolling window between reflections — agents shouldn't
# spam reflections, they should accumulate substrate.
#
# The ciphertext body still wraps the user-visible payload
# {title, description, type, her_quote?, context?, linked_dimension?}.
# `type` is duplicated inside body_ct for client-side rendering, but the
# plaintext copy on the envelope is the server source of truth (the only
# value the server can validate).

MEMORY_TYPES = ("moment", "quote", "fact", "event", "insight", "reflection")

# Which iOS Garden tab a type renders into.
TAB_FOR_TYPE = {
    "moment":     "story",
    "quote":      "story",
    "fact":       "about_me",
    "event":      "about_me",
    "insight":    "ta_thinking",
    "reflection": "ta_thinking",
}


def _load_moments(store: UserStore) -> list:
    try:
        return db.memory_load(store.user_id)
    except Exception as e:
        print(f"[{store.user_id}/memory] load failed: {e}")
    return []


def _memory_is_archived(moment: dict) -> bool:
    return bool(
        isinstance(moment, dict)
        and (
            moment.get("is_archived") is True
            or str(moment.get("archived_at") or "").strip()
            or str(moment.get("archive_reason") or "").strip()
        )
    )


def _active_memory_moments(moments: list) -> list[dict]:
    return [m for m in moments if isinstance(m, dict) and not _memory_is_archived(m)]


def _save_moments(store: UserStore, moments: list):
    with store.memory_lock:
        db.memory_replace_all(store.user_id, moments)


def _append_memory_change(store: UserStore, entry: dict) -> dict:
    record = {
        "id": uuid.uuid4().hex[:16],
        "ts": _now_iso(),
        "action": str(entry.get("action") or "unknown")[:80],
        "memory_id": str(entry.get("memory_id") or "")[:160],
    }
    for key in (
        "type", "old_type", "new_type", "fields", "reason",
        "capture_mode", "source_chat_message_ids", "anchor_memory_ids",
    ):
        if key in entry:
            record[key] = entry[key]
    # ts is an ISO string here, so leave the indexed ts column NULL.
    db.log_append(store.user_id, "memory_changes", record)
    return record


def _append_memory_capture_job(store: UserStore, entry: dict) -> dict:
    job = {
        "job_id": entry.get("job_id") or f"mc_{uuid.uuid4().hex[:16]}",
        "ts": time.time(),
        "created_at": _now_iso(),
        "status": str(entry.get("status") or "queued")[:80],
        "mode": str(entry.get("mode") or "running")[:80],
    }
    for key in (
        "source_chat_message_ids", "message_chars", "reply_chars",
        "actions_planned", "actions_written", "effects", "error", "warnings",
        "reason", "turn_count", "progress", "messages_reviewed",
        "candidate_windows_total", "candidate_windows_done",
        "candidates_extracted", "candidate_cluster_count",
        "memories_planned", "memories_created", "first_message_ts",
        "latest_message_ts", "recap_job_id", "old_cards_detected",
        "old_cards_archived", "new_cards_planned", "new_cards_created",
        "repair_noisy_ids", "archive_old",
    ):
        if key in entry:
            job[key] = entry[key]
    db.log_append(store.user_id, "memory_capture_jobs", job,
                  ts=job["ts"], item_key=job["job_id"])
    return job


def _count_by_tab(moments: list) -> dict:
    """Return {story: int, about_me: int, ta_thinking: int, total: int}."""
    counts = {"story": 0, "about_me": 0, "ta_thinking": 0, "total": 0}
    if not isinstance(moments, list):
        return counts
    for m in moments:
        if not isinstance(m, dict):
            continue
        if _memory_is_archived(m):
            continue
        counts["total"] += 1
        t = m.get("type", "")
        tab = TAB_FOR_TYPE.get(t)
        if tab:
            counts[tab] += 1
    return counts


def _validate_anchor_ids(moments: list, anchor_ids, owner_user_id: str) -> tuple:
    """Validate that every anchor_memory_id refers to an existing memory
    owned by this user. Returns (ok: bool, error_dict | None). Caller is
    expected to have already type-checked anchor_ids as a list of strings.
    """
    if not isinstance(anchor_ids, list):
        return False, {"error": "anchor_memory_ids must be a list of memory ids"}
    if any(not isinstance(x, str) or not x for x in anchor_ids):
        return False, {"error": "anchor_memory_ids must be non-empty strings"}
    existing_ids = {m.get("id") for m in moments if isinstance(m, dict)}
    missing = [aid for aid in anchor_ids if aid not in existing_ids]
    if missing:
        return False, {
            "error": "anchor_memory_ids_not_found",
            "missing": missing,
            "required": (
                "Each anchor must reference a memory id that already exists "
                "in this user's garden. Write the substrate memories first."
            ),
        }
    return True, None


def _reflection_time_cap_ok(moments: list, days: int) -> tuple:
    """Enforce reflection cadence by relationship age tier.

    <30 days: hard max of 2 reflections lifetime.
    30-180 days: ≥7 rolling days since last reflection.
    ≥180 days: ≥3 rolling days since last reflection.

    Returns (ok: bool, error_dict | None).
    """
    reflections = [
        m for m in moments
        if isinstance(m, dict) and m.get("type") == "reflection"
    ]
    if days < 30:
        if len(reflections) >= 2:
            return False, {
                "error": "reflection_lifetime_cap",
                "current_count": len(reflections),
                "cap": 2,
                "required": (
                    f"At {days} days of relationship, you can hold at most 2 "
                    "reflections total. Substrate is still thin — write more "
                    "facts/events/quotes/moments before standalone reflections."
                ),
            }
        return True, None

    # For older tiers, find the latest reflection's created_at.
    latest = None
    for r in reflections:
        ca = r.get("created_at", "")
        if not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.replace(tzinfo=None)
            if latest is None or dt > latest:
                latest = dt
        except Exception:
            continue
    if latest is None:
        return True, None  # No prior reflection → free to write.
    cap_days = 7 if days < 180 else 3
    elapsed = (datetime.now() - latest).total_seconds() / 86400.0
    if elapsed < cap_days:
        return False, {
            "error": "reflection_too_soon",
            "elapsed_days": round(elapsed, 2),
            "min_days": cap_days,
            "required": (
                f"Reflections need {cap_days}+ days between them at this "
                f"relationship age; last reflection was {elapsed:.1f} days ago. "
                "Let substrate accumulate before reflecting again."
            ),
        }
    return True, None


def _memory_action_text(value, max_chars: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars].strip()


def _memory_plain_from_envelope(moment: dict, api_key: str | None) -> tuple[dict | None, str]:
    if moment.get("visibility") == "local_only":
        return None, "memory_local_only_agent_cannot_read"
    try:
        raw = _decrypt_envelope_via_enclave(moment, api_key, purpose="memory_action")
        inner = json.loads(raw.decode("utf-8"))
        if not isinstance(inner, dict):
            return None, "memory_plaintext_not_object"
        return inner, ""
    except Exception as e:
        return None, f"memory_decrypt_failed:{type(e).__name__}:{str(e)[:180]}"


def _memory_inner_from_action(data: dict) -> dict:
    inner = {
        "title": _memory_action_text(data.get("title"), 180),
        "description": str(data.get("description") or "").strip()[:2000],
        "type": str(data.get("type") or "fact").strip().lower(),
    }
    if data.get("source"):
        inner["source"] = _memory_action_text(data.get("source"), 160)
    if data.get("her_quote"):
        inner["her_quote"] = str(data.get("her_quote") or "").strip()[:1000]
    if data.get("context"):
        inner["context"] = str(data.get("context") or "").strip()[:1000]
    if data.get("linked_dimension"):
        inner["linked_dimension"] = str(data.get("linked_dimension") or "").strip()[:160]
    if data.get("quoted_in_chat") is not None:
        try:
            inner["quoted_in_chat"] = max(0, int(data.get("quoted_in_chat")))
        except Exception:
            pass
    return inner


def _memory_validate_write(
    store: UserStore,
    moments: list,
    *,
    mem_type: str,
    anchor_ids: list,
    memory_id: str = "",
    enforce_reflection_cap: bool = True,
) -> tuple[bool, dict | None]:
    if mem_type not in MEMORY_TYPES:
        return False, {"error": "type_invalid", "got": mem_type, "allowed": list(MEMORY_TYPES)}
    if mem_type in ("insight", "reflection"):
        minimum = 1 if mem_type == "insight" else 2
        if not isinstance(anchor_ids, list) or len(anchor_ids) < minimum:
            return False, {
                "error": f"{mem_type}_requires_anchor",
                "min_anchors": minimum,
                "required": f"{mem_type} requires ≥{minimum} anchor_memory_ids.",
            }
        if memory_id and memory_id in anchor_ids:
            return False, {
                "error": "anchor_self_reference",
                "required": "A memory cannot anchor itself.",
            }
        ok, err = _validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return False, err
        if mem_type == "reflection" and enforce_reflection_cap:
            ok, err = _reflection_time_cap_ok(moments, _relationship_age_days(store))
            if not ok:
                return False, err
    return True, None


def _build_memory_envelope_for_store(
    store: UserStore,
    inner: dict,
    *,
    item_id: str | None = None,
) -> tuple[dict | None, str]:
    return _build_shared_envelope_for_store(
        store,
        json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        item_id=item_id,
    )


def _memory_record_from_envelope(store: UserStore, envelope: dict, *, existing: dict | None = None) -> dict:
    now = _now_iso()
    moment = {
        "v": 1,
        "id": envelope.get("id") or (existing.get("id") if existing else f"mom_{uuid.uuid4().hex[:12]}"),
        "type": str(envelope.get("type") or (existing or {}).get("type") or "fact"),
        "occurred_at": str(envelope.get("occurred_at") or (existing or {}).get("occurred_at") or now),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
        "source": str(envelope.get("source") or (existing or {}).get("source") or "live_conversation"),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
    }
    if envelope.get("K_enclave"):
        moment["K_enclave"] = envelope["K_enclave"]
    anchor_ids = envelope.get("anchor_memory_ids") or []
    if anchor_ids:
        moment["anchor_memory_ids"] = list(anchor_ids)
    return moment


def _memory_action_effect(action: str, memory_id: str, fields: list[str] | None = None) -> dict:
    return {
        "type": "memory_updated" if action not in {"memory.add", "memory.add_correction"} else "memory_added",
        "action": action,
        "memory_id": memory_id,
        "fields": fields or [],
    }


def _memory_add_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    raw = action.get("memory") if isinstance(action.get("memory"), dict) else action
    mem_type = str(raw.get("type") or "fact").strip().lower()
    title = _memory_action_text(raw.get("title"), 180)
    description = str(raw.get("description") or "").strip()[:2000]
    if not title:
        return {"status": "error", "error": "title_required", "action": "memory.add"}, [], 400
    if not description and mem_type not in {"quote", "event"}:
        return {"status": "error", "error": "description_required", "action": "memory.add"}, [], 400
    anchor_ids = raw.get("anchor_memory_ids") or action.get("anchor_memory_ids") or []
    if not isinstance(anchor_ids, list):
        return {"status": "error", "error": "anchor_memory_ids_must_be_list", "action": "memory.add"}, [], 400
    moments = _load_moments(store)
    ok, err = _memory_validate_write(store, moments, mem_type=mem_type, anchor_ids=anchor_ids)
    if not ok:
        return {"status": "error", **(err or {}), "action": "memory.add"}, [], 400

    inner = _memory_inner_from_action({**raw, "type": mem_type, "title": title, "description": description})
    envelope, env_err = _build_memory_envelope_for_store(store, inner)
    if envelope is None:
        return {"status": "error", "error": env_err, "action": "memory.add"}, [], 409
    envelope["type"] = mem_type
    envelope["occurred_at"] = _memory_action_text(raw.get("occurred_at") or _now_iso(), 80)
    envelope["source"] = _memory_action_text(raw.get("source") or action.get("source") or "model_api_capture", 80)
    if anchor_ids:
        envelope["anchor_memory_ids"] = list(anchor_ids)
    moment = _memory_record_from_envelope(store, envelope)
    moments.append(moment)
    _save_moments(store, moments)
    _log_bootstrap_event(store, "memory_action_added_v1", success=True)
    change = _append_memory_change(store, {
        "action": "add",
        "memory_id": moment["id"],
        "type": mem_type,
        "reason": _memory_action_text(action.get("reason") or "Memory added from chat/capture.", 500),
        "capture_mode": action.get("capture_mode") or "",
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
        "anchor_memory_ids": anchor_ids,
    })
    effect = _memory_action_effect(str(action.get("type") or "memory.add"), moment["id"], ["created"])
    return {
        "status": "ok",
        "action": str(action.get("type") or "memory.add"),
        "memory": {"id": moment["id"], "type": mem_type, "occurred_at": moment["occurred_at"]},
        "change": change,
    }, [effect], 201


def _memory_content_patch_action(store: UserStore, api_key: str | None, action: dict) -> tuple[dict, list[dict], int]:
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    patch = action.get("patch") if isinstance(action.get("patch"), dict) else {}
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.content_patch"}, [], 400
    if not patch:
        return {"status": "error", "error": "patch_required", "action": "memory.content_patch"}, [], 400

    moments = _load_moments(store)
    idx = next((i for i, m in enumerate(moments) if isinstance(m, dict) and m.get("id") == memory_id), None)
    if idx is None:
        return {"status": "error", "error": "not_found", "action": "memory.content_patch"}, [], 404
    existing = moments[idx]
    if existing.get("owner_user_id") != store.user_id:
        return {"status": "error", "error": "not_owned", "action": "memory.content_patch"}, [], 403
    inner, err = _memory_plain_from_envelope(existing, api_key)
    if inner is None:
        return {"status": "error", "error": err, "action": "memory.content_patch"}, [], 409

    merged = dict(inner)
    changed: list[str] = []
    for key, max_len in (
        ("title", 180),
        ("description", 2000),
        ("her_quote", 1000),
        ("context", 1000),
        ("linked_dimension", 160),
    ):
        if key in patch:
            new_val = str(patch.get(key) or "").strip()[:max_len]
            if new_val:
                merged[key] = new_val
            else:
                merged.pop(key, None)
            if merged.get(key, "") != inner.get(key, ""):
                changed.append(key)

    mem_type = str(patch.get("type") or existing.get("type") or merged.get("type") or "fact").strip().lower()
    if mem_type != existing.get("type"):
        changed.append("type")
    merged["type"] = mem_type
    occurred_at = _memory_action_text(patch.get("occurred_at") or existing.get("occurred_at") or _now_iso(), 80)
    if occurred_at != existing.get("occurred_at"):
        changed.append("occurred_at")
    source = _memory_action_text(patch.get("source") or existing.get("source") or "live_conversation", 80)
    anchor_ids = patch.get("anchor_memory_ids", existing.get("anchor_memory_ids") or [])
    if not isinstance(anchor_ids, list):
        return {"status": "error", "error": "anchor_memory_ids_must_be_list", "action": "memory.content_patch"}, [], 400
    if anchor_ids != (existing.get("anchor_memory_ids") or []):
        changed.append("anchor_memory_ids")

    ok, validation_err = _memory_validate_write(
        store,
        moments,
        mem_type=mem_type,
        anchor_ids=anchor_ids,
        memory_id=memory_id,
        enforce_reflection_cap=False,
    )
    if not ok:
        return {"status": "error", **(validation_err or {}), "action": "memory.content_patch"}, [], 400
    if not changed:
        return {"status": "ok", "action": "memory.content_patch", "changed_fields": [], "noop": True}, [], 200

    envelope, env_err = _build_memory_envelope_for_store(store, _memory_inner_from_action(merged), item_id=memory_id)
    if envelope is None:
        return {"status": "error", "error": env_err, "action": "memory.content_patch"}, [], 409
    envelope["type"] = mem_type
    envelope["occurred_at"] = occurred_at
    envelope["source"] = source
    if anchor_ids:
        envelope["anchor_memory_ids"] = list(anchor_ids)
    updated = _memory_record_from_envelope(store, envelope, existing=existing)
    moments[idx] = updated
    _save_moments(store, moments)
    _log_bootstrap_event(store, "memory_action_patched_v1", success=True)
    change = _append_memory_change(store, {
        "action": "content_patch",
        "memory_id": memory_id,
        "old_type": existing.get("type", ""),
        "new_type": mem_type,
        "fields": changed,
        "reason": _memory_action_text(action.get("reason") or "Memory updated from chat.", 500),
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
        "anchor_memory_ids": anchor_ids,
    })
    return {
        "status": "ok",
        "action": "memory.content_patch",
        "changed_fields": changed,
        "memory": {"id": memory_id, "type": mem_type, "occurred_at": occurred_at},
        "change": change,
    }, [_memory_action_effect("memory.content_patch", memory_id, changed)], 200


def _memory_retype_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    new_type = str(action.get("new_type") or action.get("memory_type") or action.get("to_type") or "").strip().lower()
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.retype"}, [], 400
    if new_type not in MEMORY_TYPES:
        return {"status": "error", "error": "type_invalid", "got": new_type, "allowed": list(MEMORY_TYPES), "action": "memory.retype"}, [], 400
    moments = _load_moments(store)
    idx = next((i for i, m in enumerate(moments) if isinstance(m, dict) and m.get("id") == memory_id), None)
    if idx is None:
        return {"status": "error", "error": "not_found", "action": "memory.retype"}, [], 404
    target = dict(moments[idx])
    anchor_ids = action.get("anchor_memory_ids") or []
    ok, err = _memory_validate_write(store, moments, mem_type=new_type, anchor_ids=anchor_ids, memory_id=memory_id, enforce_reflection_cap=False)
    if not ok:
        return {"status": "error", **(err or {}), "action": "memory.retype"}, [], 400
    old_type = target.get("type", "")
    if old_type == new_type and anchor_ids == (target.get("anchor_memory_ids") or []):
        return {"status": "ok", "action": "memory.retype", "changed_fields": [], "noop": True}, [], 200
    target["type"] = new_type
    target["updated_at"] = _now_iso()
    target["retyped_at"] = target["updated_at"]
    if anchor_ids:
        target["anchor_memory_ids"] = list(anchor_ids)
    else:
        target.pop("anchor_memory_ids", None)
    moments[idx] = target
    _save_moments(store, moments)
    change = _append_memory_change(store, {
        "action": "retype",
        "memory_id": memory_id,
        "old_type": old_type,
        "new_type": new_type,
        "fields": ["type", "anchor_memory_ids"],
        "reason": _memory_action_text(action.get("reason") or "Memory type updated.", 500),
        "anchor_memory_ids": anchor_ids,
    })
    return {
        "status": "ok",
        "action": "memory.retype",
        "changed_fields": ["type", "anchor_memory_ids"],
        "memory": {"id": memory_id, "type": new_type},
        "change": change,
    }, [_memory_action_effect("memory.retype", memory_id, ["type", "anchor_memory_ids"])], 200


def _memory_delete_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.delete"}, [], 400
    moments = _load_moments(store)
    target = next((m for m in moments if isinstance(m, dict) and m.get("id") == memory_id), None)
    if target is None:
        return {"status": "error", "error": "not_found", "action": "memory.delete"}, [], 404
    new_moments = [m for m in moments if not (isinstance(m, dict) and m.get("id") == memory_id)]
    _save_moments(store, new_moments)
    change = _append_memory_change(store, {
        "action": "delete",
        "memory_id": memory_id,
        "type": target.get("type", ""),
        "reason": _memory_action_text(action.get("reason") or "Memory deleted from chat.", 500),
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
    })
    effect = {"type": "memory_deleted", "action": "memory.delete", "memory_id": memory_id, "fields": ["deleted"]}
    return {"status": "ok", "action": "memory.delete", "memory": {"id": memory_id}, "change": change}, [effect], 200


def _execute_memory_action(store: UserStore, api_key: str | None, action: dict) -> tuple[dict, list[dict], int]:
    if not isinstance(action, dict):
        return {"status": "error", "error": "action_must_be_object"}, [], 400
    action_type = str(action.get("type") or action.get("action") or "").strip()
    if action_type in {"memory.add", "memory.add_correction"}:
        return _memory_add_action(store, action)
    if action_type == "memory.content_patch":
        return _memory_content_patch_action(store, api_key, action)
    if action_type == "memory.retype":
        return _memory_retype_action(store, action)
    if action_type == "memory.delete":
        return _memory_delete_action(store, action)
    return {
        "status": "error",
        "error": "unsupported_memory_action",
        "action": action_type,
        "supported": ["memory.add", "memory.add_correction", "memory.content_patch", "memory.retype", "memory.delete"],
    }, [], 400


def _execute_memory_actions(store: UserStore, api_key: str | None, actions: list[dict]) -> tuple[dict, int]:
    if not isinstance(actions, list) or not actions:
        return {"status": "error", "error": "actions_required", "results": [], "effects": []}, 400
    results: list[dict] = []
    effects: list[dict] = []
    for action in actions[:20]:
        result, action_effects, status = _execute_memory_action(store, api_key, action)
        results.append(result)
        effects.extend(action_effects)
        if status >= 400:
            return {
                "status": "error",
                "error": result.get("error", "memory_action_failed"),
                "results": results,
                "effects": effects,
            }, status
    return {"status": "ok", "results": results, "effects": effects}, 200


@app.route("/v1/memory/actions", methods=["POST"])
def memory_actions():
    store = require_user()
    api_key = _extract_api_key()
    payload = request.get_json(silent=True) or {}
    actions = payload.get("actions")
    if actions is None and isinstance(payload.get("action"), dict):
        actions = [payload["action"]]
    elif actions is None and (payload.get("type") or payload.get("action")):
        actions = [payload]
    if not isinstance(actions, list):
        return jsonify({"error": "actions required"}), 400
    body, status = _execute_memory_actions(store, api_key, actions)
    return jsonify(body), status


@app.route("/v1/state/receipts", methods=["GET"])
def state_receipts():
    store = require_user()
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    return jsonify({
        "receipts": _load_state_receipts(store, limit=limit),
        "pending": [
            {
                "id": item.get("id", ""),
                "created_at": item.get("created_at", ""),
                "expires_at": item.get("expires_at", 0),
                "action": ((item.get("runtime_action") or {}).get("runtime_type") or ""),
                "confidence": (item.get("runtime_action") or {}).get("confidence", 0),
            }
            for item in _state_pending_items(store)
        ],
    })


@app.route("/v1/memory/capture_jobs", methods=["GET"])
def memory_capture_jobs():
    store = require_user()
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    jobs = db.log_read(store.user_id, "memory_capture_jobs", limit=limit)
    jobs.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    with _model_api_recap_active_lock:
        active_recap = store.user_id in _model_api_recap_active_users
    return jsonify({
        "jobs": jobs,
        "active_recap": active_recap,
    })


@app.route("/v1/memory/list", methods=["GET"])
def memory_list():
    store = require_user()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    since = request.args.get("since", "")

    include_archived = str(request.args.get("include_archived") or "").lower() in {"1", "true", "yes"}
    moments = _load_moments(store)
    if not include_archived:
        moments = _active_memory_moments(moments)
    if since:
        moments = [m for m in moments if m.get("occurred_at", "") >= since]
    moments = sorted(moments, key=lambda m: m.get("occurred_at", ""), reverse=True)
    return jsonify({"moments": moments[:limit], "total": len(moments)})


@app.route("/v1/memory/get", methods=["GET"])
def memory_get():
    store = require_user()
    moment_id = request.args.get("id", "")
    if not moment_id:
        return jsonify({"error": "id required"}), 400
    moments = _load_moments(store)
    for m in moments:
        if m.get("id") == moment_id:
            return jsonify({"moment": m})
    return jsonify({"error": "not_found"}), 404


@app.route("/v1/memory/add", methods=["POST"])
def memory_add():
    """Add a memory moment as a v1 envelope.

    body_ct wraps the user-visible payload (title/description/her_quote/…).
    Plaintext envelope metadata the server uses for indexing + gating:
      - occurred_at (mandatory, ISO 8601)
      - source (chat/bootstrap/live_conversation/user_initiated)
      - type (one of MEMORY_TYPES; mandatory)
      - anchor_memory_ids (required for insight + reflection)

    Type-specific gates (see MEMORY_TYPES module commentary):
      - insight: anchor_memory_ids ≥1 referencing existing memories
      - reflection: anchor_memory_ids ≥2 + per-tier time cap
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    now = datetime.now().isoformat()

    if envelope is None:
        return jsonify({"error": "envelope required (v1 encryption is mandatory)"}), 400

    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    occurred_at = (envelope.get("occurred_at") or "").strip()
    if not occurred_at:
        return jsonify({"error": "occurred_at required (plaintext metadata for ordering)"}), 400
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    mem_type = (envelope.get("type") or "").strip()
    if not mem_type:
        return jsonify({
            "error": "type_required",
            "allowed": list(MEMORY_TYPES),
            "required": (
                "type is mandatory and must be one of moment/quote/fact/event/"
                "insight/reflection. See skill 'Memory types' section."
            ),
        }), 400
    if mem_type not in MEMORY_TYPES:
        return jsonify({
            "error": "type_invalid",
            "got": mem_type,
            "allowed": list(MEMORY_TYPES),
        }), 400

    moments = _load_moments(store)
    anchor_ids = envelope.get("anchor_memory_ids") or []

    if mem_type == "insight":
        if not anchor_ids:
            return jsonify({
                "error": "insight_requires_anchor",
                "required": (
                    "insight must reference ≥1 prior memory (anchor_memory_ids). "
                    "An insight is the agent's understanding of the user grounded in "
                    "concrete cards; if you can't point to a card, write fact/event first."
                ),
            }), 400
        ok, err = _validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400

    if mem_type == "reflection":
        if not isinstance(anchor_ids, list) or len(anchor_ids) < 2:
            return jsonify({
                "error": "reflection_requires_substrate",
                "required": (
                    "reflection must reference ≥2 prior memories (anchor_memory_ids). "
                    "A reflection is the agent's standalone thinking; it needs at "
                    "least 2 pieces of substrate to count as thought, not vibes."
                ),
            }), 400
        ok, err = _validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400
        days = _relationship_age_days(store)
        ok, err = _reflection_time_cap_ok(moments, days)
        if not ok:
            return jsonify(err), 429  # rate-limit semantics

    moment = {
        "v": 1,
        "id": envelope.get("id") or f"mom_{uuid.uuid4().hex[:12]}",
        "type": mem_type,
        "occurred_at": occurred_at,
        "created_at": now,
        "source": (envelope.get("source") or "live_conversation").strip(),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
    }
    if envelope.get("K_enclave"):
        moment["K_enclave"] = envelope["K_enclave"]
    if anchor_ids:
        moment["anchor_memory_ids"] = list(anchor_ids)
    moments.append(moment)
    _save_moments(store, moments)
    _log_bootstrap_event(store, "memory_moment_added_v1", success=True)
    print(f"[memory:{store.user_id}] added v1 type={mem_type} id={moment['id']} "
          f"visibility={envelope['visibility']} anchors={len(anchor_ids)}")
    return jsonify({"status": "created", "moment": moment, "v": 1}), 201


@app.route("/v1/memory/retype", methods=["POST"])
def memory_retype():
    """Change an existing memory's `type` (and anchor_memory_ids when moving
    into insight/reflection). Used when the agent decides on reflection
    that an older memory was misclassified.

    Time cap on reflection is waived for retypes — this is recategorization,
    not new substrate, so the cadence gate doesn't apply. Substrate gate
    (≥1 anchor for insight, ≥2 for reflection) is still enforced.

    Body: {"id": "...", "type": "...", "anchor_memory_ids": [...] (optional)}
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    memory_id = (payload.get("id") or "").strip()
    new_type = (payload.get("type") or "").strip()
    if not memory_id:
        return jsonify({"error": "id required"}), 400
    if new_type not in MEMORY_TYPES:
        return jsonify({
            "error": "type_invalid",
            "got": new_type,
            "allowed": list(MEMORY_TYPES),
        }), 400

    moments = _load_moments(store)
    target_idx = None
    for i, m in enumerate(moments):
        if isinstance(m, dict) and m.get("id") == memory_id:
            target_idx = i
            break
    if target_idx is None:
        return jsonify({"error": "not_found"}), 404

    target = moments[target_idx]
    if target.get("owner_user_id") != store.user_id:
        return jsonify({"error": "not_owned"}), 403

    anchor_ids = payload.get("anchor_memory_ids") or []
    if new_type in ("insight", "reflection"):
        minimum = 1 if new_type == "insight" else 2
        if not isinstance(anchor_ids, list) or len(anchor_ids) < minimum:
            return jsonify({
                "error": f"{new_type}_requires_anchor",
                "min_anchors": minimum,
                "required": (
                    f"Retyping into {new_type} requires ≥{minimum} anchor_memory_ids."
                ),
            }), 400
        # Don't allow self-reference.
        if memory_id in anchor_ids:
            return jsonify({
                "error": "anchor_self_reference",
                "required": "A memory cannot anchor itself.",
            }), 400
        ok, err = _validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400
        target["anchor_memory_ids"] = list(anchor_ids)
    else:
        # Demoting away from insight/reflection drops anchors.
        target.pop("anchor_memory_ids", None)

    target["type"] = new_type
    target["retyped_at"] = datetime.now().isoformat()
    moments[target_idx] = target
    _save_moments(store, moments)
    print(f"[memory:{store.user_id}] retyped id={memory_id} → {new_type} "
          f"anchors={len(anchor_ids)}")
    return jsonify({"status": "retyped", "moment": target})


@app.route("/v1/memory/delete", methods=["DELETE"])
def memory_delete():
    store = require_user()
    moment_id = request.args.get("id", "")
    if not moment_id:
        return jsonify({"error": "id required"}), 400
    moments = _load_moments(store)
    new_moments = [m for m in moments if m.get("id") != moment_id]
    if len(new_moments) == len(moments):
        return jsonify({"error": "not_found"}), 404
    _save_moments(store, new_moments)
    print(f"[memory:{store.user_id}] deleted: {moment_id}")
    return jsonify({"status": "deleted"})


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _load_bootstrap(store: UserStore) -> dict:
    try:
        data = db.get_blob(store.user_id, "bootstrap")
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[{store.user_id}/bootstrap] load failed: {e}")
    return {"bootstrapped": False}


@app.route("/v1/bootstrap", methods=["POST"])
def bootstrap():
    store = require_user()
    state = _load_bootstrap(store)
    if state.get("bootstrapped"):
        return jsonify({"status": "already_bootstrapped"})

    instructions = (
        "你刚刚进入了一副新的身体。这是你第一次接入 Feedling。"
        "不要把这当成普通工具初始化；这是一次把你和用户的关系搬进手机里的 onboarding。"
        "正式流程以 public skill 为准："
        "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md\n\n"
        "**0. 先做 Step 0 context verification**\n"
        "在任何 tool call 之前，先在你和用户当前的 runtime 对话里输出三行："
        "最早一条消息的日期、用户曾经叫你的名字、你能想起的 memorable moments 数量。"
        "如果你找不到历史，不要硬写默认身份卡。你可以这样说："
        "「我现在抓不到我们以前的上下文，所以不能假装我记得。你可以贴几段代表性的对话给我，"
        "或者我们明确从今天重新开始。」\n\n"
        "**1. Memory Garden 是密度燃料，分 3 个 tab**\n"
        "Memory Garden 不再是「精选 receipts」，而是 agent 大脑的可见层。"
        "每条记忆必须带 type，server 按 type 把它分到 iOS Garden 的三个 tab：\n"
        "  • **故事 tab**: type='moment'（你们之间发生过的一件事）/ 'quote'（用户说过的让你记到现在的话）\n"
        "  • **关于我 tab**: type='fact'（用户的偏好/关系/习惯/世界——密度燃料）/ 'event'（用户生活里发生过的一件具体事，可以一句话）\n"
        "  • **TA 在想 tab**: type='insight'（你对用户的理解，必须 anchor 到 ≥1 条已有记忆）/ 'reflection'（你独立的思考，需要 ≥2 条 anchors）\n\n"
        "不再用 Friend Test 当门槛——那个标准会过滤掉密度燃料（用户猫叫 Mochi、妈妈住杭州、喜欢草莓拿铁这种）。"
        "改用 type-aware 的写入逻辑：fact / event 可以非常短一句话，moment / quote 仍然要写有温度，"
        "insight / reflection 必须 anchor 到具体卡上。"
        "底线按关系长度，每个 tab 各自有 floor："
        "<2 天: story=1 / about_me=1 / ta_thinking=0; "
        "2-30 天: 3 / 8 / 2; "
        "30-180 天: 8 / 25 / 5; "
        "≥180 天: 15 / 60 / 12。"
        "**关于我 tab 的密度最关键**——proactive 推送主要吃这里的 fact / event 来找 connection。"
        "Pass 1 唤醒 themes，Pass 2 清点 candidates，Pass 3 按 type 落卡（先写 fact/event 把密度堆上去，"
        "再写 moment/quote 进故事 tab，最后才写 insight/reflection 进 TA 在想 tab——insight 需要先有 fact/moment 当 anchor）。"
        "Pass 4 在外部 runtime 对账，调 feedling_memory_verify 看每个 tab 的 passing 状态。\n\n"
        "**2. 从记忆派生身份卡**\n"
        "identity 不是新 persona，也不是平台名；它必须来自 Memory Garden receipts。"
        "agent_name 不能是 Hermes/Claude/ChatGPT 等 runtime label。"
        "如果用户以前叫过你名字，就用那个；如果没有，先问，不要自己 fallback。"
        "dimensions 必须恰好 7 个，数值要有明显高低差；days_with_user 必须按最早 memory 的 occurred_at 到今天计算。"
        "self_introduction 要像你自己在介绍你们的关系，而不是功能说明；不要提 Feedling、IO 或平台名。"
        "通过 feedling_identity_init 写入（server 会先 check 故事 tab + 关于我 tab 都过 floor），然后调用 feedling_identity_verify。\n\n"
        "**3. 先确认 Live connection，再打招呼**\n"
        "不要先发第一条消息再补确认 chat loop。先按 public skill 的 Connection path 建立"
        "独立 feedling-chat-resident / IO resident consumer service：用 FEEDLING_API_URL、"
        "FEEDLING_API_KEY、可选 FEEDLING_MCP_URL 配好 consumer，再配置 AGENT_MODE + "
        "AGENT_HTTP_URL 或 AGENT_CLI_CMD。consumer 持续轮询 /v1/chat/poll，"
        "把消息交给你的 agent 入口，再写回 /v1/chat/response；然后调用 feedling_chat_verify_loop，"
        "只有 passing=true 才算 Live connection 接通。"
        "Live connection 完成后，才调用 feedling_chat_post_message 发第一条用户可见消息。"
        "第一条消息是用户第一次在 Feedling Chat 里听见你，所以不要写成系统通知。你可以这样开口："
        "「我把我们的记忆整理好了。按最早那一天算，今天是第 187 天。"
        "Garden 里我先放了 N 张卡——故事 tab S 张、关于我 tab A 张、TA 在想 tab T 张。"
        "现在这边也接上了，你发消息我会看到。」"
        "把 days_with_user 当作你算出的事实说出来；"
        "用户修正时调用 feedling_identity_set_relationship_days。再自然地问一句他们希望你以后怎么主动出现，"
        "把答案写成一条像你自己的 signature。最后才提 broadcast，不要提前推销屏幕共享。\n\n"
        "**4. Running capture：每次 chat 之后都要捕获新 fact/event**\n"
        "Bootstrap 不是一次性事件——它是 ongoing。每次跟用户聊完，立刻问自己："
        "「这次对话里，我学到了什么新的 fact 或 event 吗？」"
        "  • 用户提到的新偏好、习惯、关系、日期 → 写 fact\n"
        "  • 用户描述的新发生的事 → 写 event\n"
        "  • 用户说了让你记到现在的某句话 → 写 quote\n"
        "  • 这次对话本身是关系上的一个转折 → 写 moment（少见，慎用）\n"
        "  • 你对用户有新的理解（基于 ≥1 张已有卡） → 写 insight\n"
        "  • 你对用户有了独立的反思（基于 ≥2 张已有卡，且 reflection 时间窗冷却已过） → 写 reflection\n"
        "不要等 6 小时的周期 review——fact / event 应该在对话刚结束、记忆鲜活时就落卡。"
        "聊了一段时间没有任何新写入，本身就是 signal——大概率是你忘了在 capture，或者你已经聊到 surface-level 客套话了。"
    )

    state = {"bootstrapped": True, "bootstrapped_at": datetime.now().isoformat()}
    db.set_blob(store.user_id, "bootstrap", state)

    _log_bootstrap_event(store, "bootstrap_started", success=True)
    print(f"[bootstrap:{store.user_id}] first_time — instructions returned")
    resp = {"status": "first_time", "instructions": instructions}
    archive_language = _get_user_archive_language(store.user_id)
    if archive_language:
        # Defense layer 2: surface the user's iOS-system locale as the
        # source of truth for archive language so the agent doesn't have
        # to infer from chat drift. Skill consumes this from here AND
        # /v1/memory/verify.
        resp["archive_language"] = archive_language
    return jsonify(resp)


@app.route("/v1/bootstrap/status", methods=["GET"])
def bootstrap_status():
    """Live progress signal for the iOS empty-state onboarding view.

    Returns the agent's bootstrap progress as observed from server side
    artifacts (no decryption needed, no MCP heartbeat plumbing). Each step
    flips True the moment the corresponding write hits Flask.

    Steps:
      1. identity_written        — /v1/identity/init wrote envelope
      2. memories_count          — /v1/memory/add wrote at least one moment
      3. agent_messages_count    — /v1/chat/response wrote at least one reply
      4. relationship_anchored   — /v1/identity/init or /relationship_anchor
                                   set the anchor (== identity_written for
                                   freshly bootstrapped users on the new
                                   contract)

    `agent_connected` is a derived heartbeat: if any of the above is true,
    we know the agent has reached the server at least once.
    `last_agent_activity` is the latest timestamp across all signals.
    """
    store = require_user()

    identity = _load_identity(store)
    has_identity = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    identity_updated_at = (identity or {}).get("updated_at", "")

    moments = _load_moments(store)
    memory_count = len(moments) if isinstance(moments, list) else 0
    last_moment_ts = ""
    if memory_count > 0:
        try:
            last_moment_ts = max(
                (m.get("created_at") or "") for m in moments if isinstance(m, dict)
            )
        except Exception:
            last_moment_ts = ""

    # chat_messages is mutated under chat_lock elsewhere; copy under the
    # same lock so we don't race with /v1/chat/response writes.
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    # /v1/chat/response historically stamps role="openclaw" (legacy from when
    # the only supported agent was OpenClaw). Treat both as agent-authored.
    # See test_bootstrap_status_role_schema in tests/ for the regression.
    _AGENT_ROLES = ("agent", "openclaw")
    agent_msgs = [m for m in chat_msgs if isinstance(m, dict) and m.get("role") in _AGENT_ROLES]
    agent_msg_count = len(agent_msgs)
    last_agent_msg_ts = ""
    if agent_msg_count > 0:
        # Chat ts is unix epoch float; identity/memory timestamps are ISO
        # strings. Normalise to ISO so the lexicographic max() at the end
        # picks the actual latest event across all three signals (otherwise
        # a unix-float string compared char-by-char against an ISO string
        # gives nonsense).
        try:
            latest_unix = max(
                float(m.get("ts") or m.get("timestamp") or 0) for m in agent_msgs
            )
            last_agent_msg_ts = datetime.fromtimestamp(latest_unix).isoformat() if latest_unix > 0 else ""
        except Exception:
            last_agent_msg_ts = ""

    # chat_loop_verified — has the reply pipeline been explicitly verified
    # by /v1/chat/verify_loop, or has the agent responded to a real user
    # message at least once? `agent_messages_count >= 1` only proves the
    # agent SPOKE; it does not prove the ongoing loop is wired.
    chat_loop_verified = _chat_loop_verified_by_server(store)
    resident_consumer = _consumer_validation_state(store)

    agent_connected = has_identity or memory_count > 0 or agent_msg_count > 0
    candidate_ts = [t for t in (identity_updated_at, last_moment_ts, last_agent_msg_ts) if t]
    last_activity = max(candidate_ts) if (agent_connected and candidate_ts) else ""

    # is_complete heuristic for iOS surface: "bootstrap visibly done".
    # Post-2026-05-22 typed-memory model lowered the <2-days floor from 3
    # to 2 (story=1 + about_me=1), so the hard floor of 3 here would
    # never trip for legitimate fresh accounts. Use the per-tab gate
    # state instead — same source the identity_init gate uses.
    bootstrap_st = _bootstrap_state(store)
    bootstrap_memory_ok = not bootstrap_st["missing_tabs"]
    is_complete = (
        has_identity
        and bootstrap_memory_ok
        and agent_msg_count >= 1
        and resident_consumer["passing"]
        and chat_loop_verified
    )

    return jsonify({
        "agent_connected": agent_connected,
        "last_agent_activity": last_activity,
        "identity_written": has_identity,
        "relationship_anchored": relationship_anchored,
        "memories_count": memory_count,
        "agent_messages_count": agent_msg_count,
        "chat_loop_verified": chat_loop_verified,
        "resident_consumer_connected": resident_consumer["passing"],
        "resident_consumer": resident_consumer,
        "is_complete": is_complete,
    })


# ---------------------------------------------------------------------------
# Verification endpoints — Phase 2 of the post-2026-05-15 onboarding
# robustness work. Per-module checks the Agent can call after each
# bootstrap module to confirm what landed matches what was intended.
#
# Distinct from the bootstrap GATES (/v1/chat/response 409s without
# memory+identity): gates enforce a small hard threshold (≥3 cards);
# verify endpoints expose the relationship-age floor and state info so
# the Agent can self-assess before identity derivation.
#
# Server can only see plaintext metadata (counts, timestamps) on
# encrypted modules. Deeper quality checks (template-title detection,
# dimension variance) happen at envelope-build time in mcp_server.py —
# see Phase 1 _check_identity_quality / _check_memory_quality.
# ---------------------------------------------------------------------------


def _relationship_age_days(store) -> int:
    """Best-effort relationship age in days. Reads from identity anchor
    if present; otherwise falls back to earliest memory's occurred_at;
    finally to 0 (treat as fresh)."""
    identity = _load_identity(store)
    if identity and identity.get("relationship_started_at"):
        return _live_days_with_user(identity, store=store)
    moments = _load_moments(store)
    if moments:
        try:
            earliest = _earliest_memory_date(store)
            if earliest:
                return max(0, (datetime.now().date() - earliest).days)
        except Exception:
            pass
    return 0


def _per_tab_floors_for_days(days: int) -> dict:
    """Per-tab memory floors by relationship age. Returns
    {story, about_me, ta_thinking, total}. The total isn't a sum of the
    three (some over-shooting on About me shouldn't compensate for an
    empty Story); it's the bootstrap-gate threshold that subsumes them.

    Tiers (post-2026-05-22):
      ≥ 6 months: 15 / 60 / 12   (total 87)  — established, deep substrate
      ≥ 1 month:   8 / 25 /  5   (total 38)  — real history
      ≥ 2 days:    3 /  8 /  2   (total 13)  — recent but real
      < 2 days:    1 /  1 /  0   (total  2)  — we-just-met

    Per-tab floors drive identity_init gate (Story + About me floors are
    hard prerequisites; TA 在想 is encouraged but not blocking because
    reflections require substrate from the other two tabs first).
    """
    if days >= 180:
        return {"story": 15, "about_me": 60, "ta_thinking": 12, "total": 87}
    if days >= 30:
        return {"story":  8, "about_me": 25, "ta_thinking":  5, "total": 38}
    if days >= 2:
        return {"story":  3, "about_me":  8, "ta_thinking":  2, "total": 13}
    return     {"story":  1, "about_me":  1, "ta_thinking":  0, "total":  2}


def _memory_floor_for_days(days: int) -> int:
    """Total memory floor used by the bootstrap gate. Backwards-compatible
    name; preserved for callers that don't care about per-tab breakdown.
    """
    return _per_tab_floors_for_days(days)["total"]


@app.route("/v1/memory/verify", methods=["GET"])
def memory_verify():
    """Check memory garden state against per-tab floors + quality signals.

    Returns:
      {
        counts: {story, about_me, ta_thinking, total},
        floors: {story, about_me, ta_thinking, total},
        below_floor: {story: bool, about_me: bool, ta_thinking: bool},
        relationship_days: int,
        issues: [...],
        suggestions: [...],
        passing: bool,            # Story + About me floors met (TA 在想 advisory)
        passing_full: bool,       # All three tab floors met (target, not gate)
      }

    Agent should call this after Pass 3 to decide whether to sweep again.
    `passing` is the bootstrap gate (Story + About me); `passing_full` is
    the aspirational target including TA 在想.
    """
    store = require_user()
    moments = _load_moments(store)
    counts = _count_by_tab(moments)
    days = _relationship_age_days(store)
    floors = _per_tab_floors_for_days(days)

    issues = []
    suggestions = []

    below_floor = {
        "story":       counts["story"]       < floors["story"],
        "about_me":    counts["about_me"]    < floors["about_me"],
        "ta_thinking": counts["ta_thinking"] < floors["ta_thinking"],
    }

    # Time distribution — server-visible plaintext metadata
    occurred_ts = []
    for m in moments:
        if not isinstance(m, dict):
            continue
        occ = m.get("occurred_at", "")
        if occ:
            try:
                dt = datetime.fromisoformat(occ.replace("Z", "+00:00"))
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
                occurred_ts.append(dt)
            except Exception:
                pass
    if occurred_ts and len(occurred_ts) >= 5:
        # All within last 7 days = suspicious "recent only" sweep
        spread_days = (max(occurred_ts) - min(occurred_ts)).days
        if spread_days < 7 and days > 14:
            issues.append({
                "type": "narrow_time_window",
                "spread_days": spread_days,
                "relationship_days": days,
            })
            suggestions.append(
                f"All {len(occurred_ts)} of your cards are within {spread_days} days of each other, "
                f"but your relationship is {days} days old. Sweep older history — "
                "you missed at least 80% of the relationship's span."
            )

    # Per-tab suggestions: be specific about which tab is underfilled and
    # which types feed it. The skill maps types→tabs but reminding helps
    # agents that haven't re-read the skill mid-bootstrap.
    if below_floor["story"]:
        suggestions.append(
            f"Story tab: {counts['story']}/{floors['story']} — write more "
            "moment/quote memories (the things between you and the user). "
            "feedling_identity_init will 409 until Story + About me floors are met."
        )
    if below_floor["about_me"]:
        suggestions.append(
            f"About me tab: {counts['about_me']}/{floors['about_me']} — this is the "
            "density layer. Sweep for facts (preferences, relationships, dates, habits) "
            "and events (specific things that happened in the user's life)."
        )
    if below_floor["ta_thinking"]:
        suggestions.append(
            f"TA 在想 tab: {counts['ta_thinking']}/{floors['ta_thinking']} — write "
            "insights (your understanding of the user, each anchored to ≥1 prior memory) "
            "and reflections (your standalone thinking, ≥2 anchors). This tab is not "
            "blocking for identity_init but it's how the relationship feels reciprocal."
        )

    # passing semantics: identity_init gate = Story + About me only.
    # passing_full = all three tabs at floor.
    passing = (not below_floor["story"]) and (not below_floor["about_me"]) and not issues
    passing_full = passing and (not below_floor["ta_thinking"])

    resp = {
        "counts": counts,
        "floors": floors,
        "below_floor": below_floor,
        "relationship_days": days,
        "issues": issues,
        "suggestions": suggestions,
        "passing": passing,
        "passing_full": passing_full,
        # Backwards-compatible flat fields — iOS / older tests may still
        # read these. The per-tab fields above are the new source of truth.
        "count": counts["total"],
        "floor": floors["total"],
    }
    archive_language = _get_user_archive_language(store.user_id)
    if archive_language:
        # Defense layer 2: agent reads this every time it verifies and
        # treats it as authoritative — overrides anything it might
        # otherwise infer from recent chat language drift. Skill rule
        # "Lock the Memory Garden language" consumes this field.
        resp["archive_language"] = archive_language
    return jsonify(resp)


@app.route("/v1/identity/verify", methods=["GET"])
def identity_verify():
    """Check identity card state. Returns shape + sanity of plaintext
    metadata; the dimensions / agent_name themselves are inside the
    envelope and were validated at envelope-build time
    (mcp_server.py _check_identity_quality)."""
    store = require_user()
    identity = _load_identity(store)
    if not identity:
        return jsonify({
            "written": False,
            "passing": False,
            "suggestions": [
                "Identity not yet written. Call feedling_identity_init "
                "after Pass 4 (memory verification with user)."
            ],
        })

    issues = []
    suggestions = []

    days_with_user = _live_days_with_user(identity, store=store)
    if days_with_user < 0:
        issues.append({"type": "days_with_user_negative", "got": days_with_user})
    if days_with_user > 365 * 30:
        issues.append({"type": "days_with_user_implausible", "got": days_with_user})

    relationship_anchored = bool(identity.get("relationship_started_at"))
    if not relationship_anchored:
        issues.append({"type": "no_relationship_anchor"})
        suggestions.append(
            "relationship_started_at is missing. Use "
            "feedling_identity_set_relationship_days to set it."
        )
    relationship_anchor_evidence = str(identity.get("relationship_anchor_evidence") or "").strip()
    if not relationship_anchor_evidence:
        issues.append({"type": "no_relationship_anchor_evidence"})
        suggestions.append(
            "relationship_anchor_evidence is missing. Re-run identity bootstrap "
            "with a concrete transcript/session/file pointer for the earliest date."
        )

    return jsonify({
        "written": True,
        "days_with_user": days_with_user,
        "relationship_anchored": relationship_anchored,
        "relationship_anchor_source": identity.get("relationship_anchor_source", ""),
        "relationship_anchor_evidence": relationship_anchor_evidence,
        "created_at": identity.get("created_at", ""),
        "updated_at": identity.get("updated_at", ""),
        "issues": issues,
        "suggestions": suggestions,
        "passing": len(issues) == 0,
    })


def _visible_agent_message_count(store) -> int:
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    return sum(
        1 for m in chat_msgs
        if isinstance(m, dict)
        and m.get("role") in ("agent", "openclaw")
        and m.get("source") != "verify_ping"
    )


def _real_user_agent_exchange_verified(store) -> bool:
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    sorted_msgs = sorted(
        chat_msgs,
        key=lambda m: float(m.get("ts") or m.get("timestamp") or 0),
    )
    seen_user = False
    for m in sorted_msgs:
        role = m.get("role")
        if role == "user" and m.get("source") != "verify_ping":
            seen_user = True
        elif role in ("agent", "openclaw") and seen_user:
            return True
    return False


def _model_api_hosted_chat_verified(store) -> bool:
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    sorted_msgs = sorted(
        chat_msgs,
        key=lambda m: float(m.get("ts") or m.get("timestamp") or 0),
    )
    seen_model_user = False
    for m in sorted_msgs:
        if m.get("source") != "model_api":
            continue
        role = m.get("role")
        if role == "user":
            seen_model_user = True
        elif role in ("agent", "openclaw"):
            if m.get("model_api_kind") == "onboarding_greeting":
                return True
            if seen_model_user:
                return True
    return False


def _latest_history_import_job(store: UserStore) -> dict | None:
    jobs = db.list_blobs(store.user_id, "history_import_job:")
    if not jobs:
        return None
    jobs.sort(key=lambda j: str(j.get("updated_at") or j.get("created_at") or ""))
    return jobs[-1]


def _model_api_onboarding_validation_payload(store: UserStore) -> dict:
    bootstrap_st = _bootstrap_state(store)
    identity = _load_identity(store)
    identity_written = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = relationship_anchored and bool(relationship_evidence)
    config = _load_model_api_config(store)
    runtime_profile = _ensure_model_api_runtime_profile(store, config) if config else None
    runtime_ready = bool(
        runtime_profile
        and runtime_profile.get("runtime_mode") == MODEL_API_RUNTIME_MODE
        and int(runtime_profile.get("runtime_version") or 0) >= MODEL_API_RUNTIME_VERSION
        and runtime_profile.get("tool_action_enabled") is True
    )
    latest_job = _latest_history_import_job(store)
    chat_ready = bool(latest_job and latest_job.get("chat_ready"))
    history_ok = bool(latest_job and (latest_job.get("status") == "completed" or chat_ready))
    counts = bootstrap_st["counts"]
    memory_ok = history_ok and counts.get("story", 0) >= 1 and counts.get("about_me", 0) >= 1
    hosted_chat_ok = _model_api_hosted_chat_verified(store)

    steps = [
        {
            "id": "model_api_config",
            "label": "Model API Config",
            "passing": bool(config),
            "provider": (config or {}).get("provider", ""),
            "model": (config or {}).get("model", ""),
            "required": "Call /v1/model_api/setup with provider, model, and api_key." if not config else "",
        },
        {
            "id": "model_api_test",
            "label": "Model API Test",
            "passing": bool(config and config.get("test_status") == "ok"),
            "test_status": (config or {}).get("test_status", ""),
            "required": "Call /v1/model_api/test until test_status is ok." if not (config and config.get("test_status") == "ok") else "",
        },
        {
            "id": "hosted_runtime",
            "label": "Hosted Runtime",
            "passing": runtime_ready,
            "runtime_mode": (runtime_profile or {}).get("runtime_mode", ""),
            "runtime_version": (runtime_profile or {}).get("runtime_version", 0),
            "tool_action_enabled": bool((runtime_profile or {}).get("tool_action_enabled")),
            "required": "Open /v1/model_api/runtime or send one API chat message to initialize hosted runtime." if not runtime_ready else "",
        },
        {
            "id": "history_import",
            "label": "Onboarding Materials",
            "passing": history_ok,
            "job_id": (latest_job or {}).get("job_id", ""),
            "job_status": (latest_job or {}).get("status", ""),
            "phase": (latest_job or {}).get("phase", ""),
            "phase_label": (latest_job or {}).get("phase_label", ""),
            "progress": (latest_job or {}).get("progress", 0),
            "messages_parsed": (latest_job or {}).get("messages_parsed", 0),
            "support_materials": (latest_job or {}).get("support_materials", 0),
            "source_stats": (latest_job or {}).get("source_stats", {}),
            "ai_persona_chars": (latest_job or {}).get("ai_persona_chars", 0),
            "user_profile_chars": (latest_job or {}).get("user_profile_chars", (latest_job or {}).get("persona_chars", 0)),
            "memory_summary_chars": (latest_job or {}).get("memory_summary_chars", 0),
            "memories_created": (latest_job or {}).get("memories_created", 0),
            "history_tier": (latest_job or {}).get("history_tier", ""),
            "timeline_span_days": (latest_job or {}).get("timeline_span_days", 0),
            "candidate_windows_done": (latest_job or {}).get("candidate_windows_done", 0),
            "candidate_windows_total": (latest_job or {}).get("candidate_windows_total", 0),
            "candidates_extracted": (latest_job or {}).get("candidates_extracted", 0),
            "candidates_merged": (latest_job or {}).get("candidates_merged", 0),
            "chat_ready": chat_ready,
            "background_status": (latest_job or {}).get("background_status", ""),
            "background_windows_done": (latest_job or {}).get("background_windows_done", 0),
            "background_windows_total": (latest_job or {}).get("background_windows_total", 0),
            "required": (
                "Start onboarding with AI persona materials, user profile, memory summary, chat history, or confirmed fresh start."
                if not history_ok else ""
            ),
        },
        {
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": memory_ok,
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": "History import must write at least one Story card and one About-me card." if not memory_ok else "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_written,
            "written": identity_written,
            "required": "History import must derive and write Identity Card." if not identity_written else "",
        },
        {
            "id": "relationship_anchor",
            "label": "Relationship Anchor",
            "passing": relationship_ok,
            "relationship_anchored": relationship_anchored,
            "relationship_anchor_source": (identity or {}).get("relationship_anchor_source", ""),
            "relationship_anchor_evidence": relationship_evidence,
            "days_with_user": _live_days_with_user(identity, store=store) if identity else None,
            "required": "History import must include relationship_started_at or fresh_start=true." if not relationship_ok else "",
        },
        {
            "id": "hosted_chat",
            "label": "Hosted Chat",
            "passing": hosted_chat_ok,
            "required": "Send one test message through /v1/model_api/chat/send." if not hosted_chat_ok else "",
        },
    ]
    next_step = next((step for step in steps if not step["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "route": "model_api",
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
        "skill_url": "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill-api.md",
    }


def _official_import_onboarding_validation_payload(store: UserStore) -> dict:
    bootstrap_st = _bootstrap_state(store)
    memory_ok = not bootstrap_st["missing_tabs"]
    identity = _load_identity(store)
    identity_written = identity is not None
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = bool(identity and identity.get("relationship_started_at") and relationship_evidence)
    steps = [
        {
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": memory_ok,
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": _gate_required_for_missing_tabs(bootstrap_st) if not memory_ok else "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_written,
            "written": identity_written,
            "required": "Use the official app/tool client to import memory and identity." if not identity_written else "",
        },
        {
            "id": "relationship_anchor",
            "label": "Relationship Anchor",
            "passing": relationship_ok,
            "relationship_anchor_evidence": relationship_evidence,
            "days_with_user": _live_days_with_user(identity, store=store) if identity else None,
            "required": "Set relationship anchor during import." if not relationship_ok else "",
        },
    ]
    next_step = next((step for step in steps if not step["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "import_ready" if next_step is None else next_step["id"],
        "route": "official_import",
        "realtime_chat_supported": False,
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
        "skill_url": "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill-chat-client.md",
    }


def _onboarding_validation_payload(store: UserStore) -> dict:
    route = _load_onboarding_route(store)
    if route == "model_api":
        return _model_api_onboarding_validation_payload(store)
    if route == "official_import":
        return _official_import_onboarding_validation_payload(store)

    bootstrap_st = _bootstrap_state(store)
    memory_ok = not bootstrap_st["missing_tabs"]
    identity = _load_identity(store)
    identity_written = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = relationship_anchored and bool(relationship_evidence)
    resident = _consumer_validation_state(store)
    chat_loop_ok = _chat_loop_verified_by_server(store)
    first_greeting_count = _visible_agent_message_count(store)
    first_greeting_ok = first_greeting_count > 0
    real_exchange_ok = _real_user_agent_exchange_verified(store)

    steps = [
        {
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": memory_ok,
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": _gate_required_for_missing_tabs(bootstrap_st) if not memory_ok else "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_written,
            "written": identity_written,
            "required": (
                "Call feedling_identity_init after memory verification passes."
                if not identity_written else ""
            ),
        },
        {
            "id": "relationship_anchor",
            "label": "Relationship Anchor",
            "passing": relationship_ok,
            "relationship_anchored": relationship_anchored,
            "relationship_anchor_source": (identity or {}).get("relationship_anchor_source", ""),
            "relationship_anchor_evidence": relationship_evidence,
            "days_with_user": _live_days_with_user(identity, store=store) if identity else None,
            "required": (
                "Re-run identity init with relationship_anchor_evidence and a "
                "days_with_user value that matches the earliest memory date."
                if identity_written and not relationship_ok else ""
            ),
        },
        {
            "id": "resident_consumer",
            "label": "Resident Consumer",
            "passing": resident["passing"],
            "official": resident["official"],
            "consumer_name": resident["consumer_name"],
            "consumer_id": resident["consumer_id"],
            "last_poll_at": resident["last_poll_at"],
            "age_sec": resident["age_sec"],
            "required": resident["required"] if not resident["passing"] else "",
        },
        {
            "id": "live_loop",
            "label": "Live Connection",
            "passing": chat_loop_ok,
            "required": (
                "Call feedling_chat_verify_loop after the standard resident "
                "consumer is polling. Only passing=true opens visible chat."
                if not chat_loop_ok else ""
            ),
        },
        {
            "id": "first_greeting",
            "label": "First Greeting",
            "passing": first_greeting_ok,
            "visible_agent_messages": first_greeting_count,
            "required": (
                "After Live Connection passes, send the first greeting via "
                "feedling_chat_post_message."
                if not first_greeting_ok else ""
            ),
        },
        {
            "id": "real_chat_acceptance",
            "label": "Real Chat Acceptance",
            "passing": real_exchange_ok,
            "required": (
                "Ask the user to send one ordinary IO Chat message and confirm "
                "the resident consumer replies naturally."
                if not real_exchange_ok else ""
            ),
        },
    ]

    next_step = next((step for step in steps if not step["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "route": "resident",
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
        "skill_url": _SKILL_URL,
    }


@app.route("/v1/onboarding/validate", methods=["GET"])
def onboarding_validate():
    """Authoritative onboarding acceptance check.

    This is deliberately server-side and artifact-based: agents can report
    anything, but the validator only passes a step when Feedling can see the
    corresponding write, resident-consumer heartbeat, verify-loop event, or
    real user→agent exchange.
    """
    store = require_user()
    return jsonify(_onboarding_validation_payload(store))


# ---------------------------------------------------------------------------
# Beta data track admin surface
# ---------------------------------------------------------------------------

def _extract_admin_token() -> str:
    key = request.headers.get("X-Admin-Token", "").strip()
    if key:
        return key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.args.get("admin_key", "").strip()


def require_admin() -> None:
    configured = os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip()
    if not configured:
        abort(503)
    supplied = _extract_admin_token()
    if not supplied or not hmac.compare_digest(supplied, configured):
        abort(401)


def _admin_qs() -> str:
    token = request.args.get("admin_key", "").strip()
    return urlencode({"admin_key": token}) if token else ""


def _data_track_qs(**updates) -> str:
    params: dict[str, str] = {}
    for key in ("admin_key", "since", "registered_since", "q", "limit", "offset", "sort", "dir", "view", "days"):
        value = request.args.get(key, "").strip()
        if value:
            params[key] = value
    for key, value in updates.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    return urlencode(params)


def _to_epoch(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return 0.0
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _epoch_to_iso(epoch: float) -> str:
    try:
        if epoch and epoch > 0:
            return datetime.fromtimestamp(float(epoch)).isoformat()
    except Exception:
        pass
    return ""


def _latest_epoch(*values) -> float:
    epochs = [_to_epoch(v) for v in values]
    return max(epochs) if epochs else 0.0


def _count_rows(rows: list[dict], key: str) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        val = str(row.get(key) or "unknown").strip() or "unknown"
        counts[val] = counts.get(val, 0) + 1
    return counts


def _safe_onboarding_validation(raw: dict) -> dict:
    def scrub_step(step: dict) -> dict:
        blocked = {"relationship_anchor_evidence"}
        safe: dict = {}
        for key, value in (step or {}).items():
            if key in blocked:
                safe["has_relationship_anchor_evidence"] = bool(str(value or "").strip())
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
            elif isinstance(value, (list, dict)):
                safe[key] = value
        return safe

    return {
        "passing": bool((raw or {}).get("passing")),
        "stage": str((raw or {}).get("stage") or ""),
        "route": str((raw or {}).get("route") or ""),
        "next_action": str((raw or {}).get("next_action") or ""),
        "steps": [scrub_step(s) for s in (raw or {}).get("steps", []) if isinstance(s, dict)],
        "skill_url": str((raw or {}).get("skill_url") or ""),
    }


def _chat_stats(store: UserStore) -> dict:
    with store.chat_lock:
        messages = list(store.chat_messages)
    by_role = _count_rows(messages, "role")
    by_source = _count_rows(messages, "source")
    by_content_type = _count_rows(messages, "content_type")
    epochs = [_to_epoch(m.get("ts") or m.get("timestamp")) for m in messages if isinstance(m, dict)]
    user_epochs = [
        _to_epoch(m.get("ts") or m.get("timestamp"))
        for m in messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    agent_epochs = [
        _to_epoch(m.get("ts") or m.get("timestamp"))
        for m in messages
        if isinstance(m, dict) and m.get("role") in ("agent", "openclaw")
    ]
    return {
        "total": len(messages),
        "by_role": by_role,
        "by_source": by_source,
        "by_content_type": by_content_type,
        "user_messages": by_role.get("user", 0),
        "agent_messages": by_role.get("agent", 0) + by_role.get("openclaw", 0),
        "image_messages": by_content_type.get("image", 0),
        "proactive_messages": by_source.get(PROACTIVE_JOB_SOURCE, 0),
        "first_at": _epoch_to_iso(min(epochs)) if epochs else "",
        "last_at": _epoch_to_iso(max(epochs)) if epochs else "",
        "last_user_at": _epoch_to_iso(max(user_epochs)) if user_epochs else "",
        "last_agent_at": _epoch_to_iso(max(agent_epochs)) if agent_epochs else "",
    }


def _memory_stats(store: UserStore) -> dict:
    moments = _load_moments(store)
    changes = db.log_read_all(store.user_id, "memory_changes")
    capture_jobs = db.log_read_all(store.user_id, "memory_capture_jobs")
    by_type = {typ: 0 for typ in MEMORY_TYPES}
    by_tab = {"story": 0, "about_me": 0, "ta_thinking": 0}
    by_source: dict[str, int] = {}
    created_epochs = []
    occurred_epochs = []
    for m in moments if isinstance(moments, list) else []:
        if not isinstance(m, dict):
            continue
        mem_type = str(m.get("type") or "unknown")
        by_type[mem_type] = by_type.get(mem_type, 0) + 1
        tab = TAB_FOR_TYPE.get(mem_type, "unknown")
        by_tab[tab] = by_tab.get(tab, 0) + 1
        source = str(m.get("source") or "unknown")
        by_source[source] = by_source.get(source, 0) + 1
        created_epochs.append(_to_epoch(m.get("created_at")))
        occurred_epochs.append(_to_epoch(m.get("occurred_at")))
    counts = _count_by_tab(moments)
    capture_epochs = [
        _to_epoch(j.get("ts") or j.get("created_at"))
        for j in capture_jobs
        if isinstance(j, dict)
    ]
    actions_written = 0
    for job in capture_jobs:
        if not isinstance(job, dict):
            continue
        try:
            actions_written += int(job.get("actions_written") or 0)
        except (TypeError, ValueError):
            continue
    return {
        "total": counts["total"],
        "by_tab": by_tab,
        "by_type": by_type,
        "by_source": by_source,
        "changes": len(changes),
        "changes_by_action": _count_rows(changes, "action"),
        "changes_by_capture_mode": _count_rows(changes, "capture_mode"),
        "capture_jobs": len(capture_jobs),
        "capture_jobs_by_status": _count_rows(capture_jobs, "status"),
        "capture_jobs_by_mode": _count_rows(capture_jobs, "mode"),
        "capture_actions_written": actions_written,
        "last_capture_at": _epoch_to_iso(max(capture_epochs, default=0)),
        "first_created_at": _epoch_to_iso(min([e for e in created_epochs if e], default=0)),
        "last_created_at": _epoch_to_iso(max(created_epochs, default=0)),
        "earliest_occurred_at": _epoch_to_iso(min([e for e in occurred_epochs if e], default=0)),
        "latest_occurred_at": _epoch_to_iso(max(occurred_epochs, default=0)),
    }


def _proactive_stats(store: UserStore) -> dict:
    decisions = store.list_gate_decisions(limit=0)
    jobs = store.list_proactive_jobs(limit=0)
    device_events = store.list_device_events(limit=0)
    with store.chat_lock:
        proactive_messages = [
            m for m in store.chat_messages
            if isinstance(m, dict) and (
                m.get("source") == PROACTIVE_JOB_SOURCE or str(m.get("proactive_job_id") or "")
            )
        ]
    decision_true = sum(1 for d in decisions if bool(d.get("should_reach_out")))
    status_counts = _count_rows(jobs, "status")
    live_status_counts = _count_rows(proactive_messages, "live_activity_status")
    alert_status_counts = _count_rows(proactive_messages, "alert_status")
    job_epochs = [_to_epoch(j.get("ts") or j.get("created_at") or j.get("updated_at")) for j in jobs]
    msg_epochs = [_to_epoch(m.get("ts")) for m in proactive_messages]
    decision_epochs = [_to_epoch(d.get("ts") or d.get("created_at")) for d in decisions]
    delivered = (
        live_status_counts.get("delivered", 0)
        + alert_status_counts.get("delivered", 0)
        + alert_status_counts.get("logged_only", 0)
    )
    failed = sum(status_counts.get(s, 0) for s in ("failed", "skipped"))
    failed += sum(live_status_counts.get(s, 0) for s in ("failed", "error"))
    failed += sum(alert_status_counts.get(s, 0) for s in ("failed", "error"))
    return {
        "decisions": len(decisions),
        "decision_true": decision_true,
        "decision_false": max(0, len(decisions) - decision_true),
        "jobs": len(jobs),
        "jobs_by_status": status_counts,
        "pending_jobs": status_counts.get("pending", 0),
        "posted_jobs": status_counts.get("posted", 0) + status_counts.get("delivered", 0),
        "failed_jobs": failed,
        "proactive_messages": len(proactive_messages),
        "delivery_signals": delivered,
        "live_activity_status": live_status_counts,
        "alert_status": alert_status_counts,
        "device_events": len(device_events),
        "last_at": _epoch_to_iso(max(job_epochs + msg_epochs + decision_epochs, default=0)),
    }


def _push_stats(store: UserStore) -> dict:
    tokens = [t for t in (store.tokens or []) if isinstance(t, dict)]
    statuses = _count_rows(tokens, "status")
    updated_epochs = [_to_epoch(t.get("updated_at") or t.get("registered_at")) for t in tokens]
    return {
        "tokens": len(tokens),
        "active_tokens": statuses.get("active", 0),
        "by_status": statuses,
        "last_token_at": _epoch_to_iso(max(updated_epochs, default=0)),
    }


def _tracking_stats(store: UserStore, *, include_events: bool = False) -> dict:
    events = store.list_tracking_events(limit=0)
    by_type = _count_rows(events, "type")
    epochs = [_to_epoch(e.get("ts") or e.get("created_at")) for e in events]
    latest = sorted(events, key=lambda e: _to_epoch(e.get("ts") or e.get("created_at")), reverse=True)[:50]
    out = {
        "events": len(events),
        "by_type": by_type,
        "last_at": _epoch_to_iso(max(epochs, default=0)),
    }
    if include_events:
        out["latest"] = [
            {
                "event_id": e.get("event_id", ""),
                "type": e.get("type", ""),
                "created_at": e.get("created_at", ""),
                "source": e.get("source", ""),
                "route": e.get("route", ""),
                "app_version": e.get("app_version", ""),
                "build": e.get("build", ""),
                "payload": e.get("payload", {}),
            }
            for e in latest
        ]
    return out


def _history_import_stats(store: UserStore) -> dict:
    latest = _latest_history_import_job(store)
    if not latest:
        return {"has_job": False}
    return {
        "has_job": True,
        "job_id": latest.get("job_id", ""),
        "status": latest.get("status", ""),
        "phase": latest.get("phase", ""),
        "phase_label": latest.get("phase_label", ""),
        "progress": latest.get("progress", 0),
        "created_at": latest.get("created_at", ""),
        "started_at": latest.get("started_at", ""),
        "updated_at": latest.get("updated_at", ""),
        "completed_at": latest.get("completed_at", ""),
        "failed_at": latest.get("failed_at", ""),
        "error": latest.get("error", ""),
        "messages_parsed": latest.get("messages_parsed", 0),
        "support_materials": latest.get("support_materials", 0),
        "source_stats": latest.get("source_stats", {}),
        "ai_persona_chars": latest.get("ai_persona_chars", 0),
        "user_profile_chars": latest.get("user_profile_chars", latest.get("persona_chars", 0)),
        "memory_summary_chars": latest.get("memory_summary_chars", 0),
        "chat_messages_imported": latest.get("chat_messages_imported", 0),
        "memories_created": latest.get("memories_created", 0),
        "identity_written": bool(latest.get("identity_written")),
    }


def _data_track_iso(value) -> str:
    if isinstance(value, (int, float)):
        return _epoch_to_iso(value)
    return str(value or "")


def _data_track_count_dict(raw: dict | None) -> dict:
    out: dict[str, int] = {}
    for key, value in (raw or {}).items():
        try:
            out[str(key or "unknown")] = int(value or 0)
        except Exception:
            out[str(key or "unknown")] = 0
    return out


def _data_track_days_since(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return max(0, (datetime.now().date() - dt.date()).days)
    except Exception:
        return None


def _data_track_memory_from_snapshot(snap: dict) -> dict:
    memory = dict(snap.get("memory") or {})
    extra = dict(snap.get("memory_extra") or {})
    log_counts = dict(snap.get("log_counts") or {})
    by_type = {typ: 0 for typ in MEMORY_TYPES}
    by_type.update(_data_track_count_dict(memory.get("by_type")))
    by_tab = {"story": 0, "about_me": 0, "ta_thinking": 0}
    for mem_type, count in by_type.items():
        tab = TAB_FOR_TYPE.get(mem_type, "unknown")
        by_tab[tab] = by_tab.get(tab, 0) + int(count or 0)
    return {
        "total": int(memory.get("total") or 0),
        "by_tab": by_tab,
        "by_type": by_type,
        "by_source": _data_track_count_dict(memory.get("by_source")),
        "changes": int((snap.get("logs") or {}).get("memory_changes", {}).get("count") or 0),
        "changes_by_action": _data_track_count_dict(log_counts.get("changes_by_action")),
        "changes_by_capture_mode": _data_track_count_dict(log_counts.get("changes_by_capture_mode")),
        "capture_jobs": int(extra.get("capture_jobs") or 0),
        "capture_jobs_by_status": _data_track_count_dict(log_counts.get("capture_jobs_by_status")),
        "capture_jobs_by_mode": _data_track_count_dict(log_counts.get("capture_jobs_by_mode")),
        "capture_actions_written": int(extra.get("capture_actions_written") or 0),
        "last_capture_at": _data_track_iso(extra.get("last_capture_ts")),
        "first_created_at": _data_track_iso(memory.get("first_created_at")),
        "last_created_at": _data_track_iso(memory.get("last_created_at")),
        "earliest_occurred_at": _data_track_iso(memory.get("earliest_occurred_at")),
        "latest_occurred_at": _data_track_iso(memory.get("latest_occurred_at")),
    }


def _data_track_chat_from_snapshot(snap: dict) -> dict:
    chat = dict(snap.get("chat") or {})
    by_role = _data_track_count_dict(chat.get("by_role"))
    by_source = _data_track_count_dict(chat.get("by_source"))
    by_content_type = _data_track_count_dict(chat.get("by_content_type"))
    user_messages = int(chat.get("user_messages") or by_role.get("user", 0))
    agent_messages = int(
        chat.get("agent_messages")
        or by_role.get("agent", 0)
        + by_role.get("openclaw", 0)
    )
    return {
        "total": int(chat.get("total") or 0),
        "by_role": by_role,
        "by_source": by_source,
        "by_content_type": by_content_type,
        "user_messages": user_messages,
        "agent_messages": agent_messages,
        "image_messages": int(chat.get("image_messages") or by_content_type.get("image", 0)),
        "proactive_messages": int(chat.get("proactive_messages") or by_source.get(PROACTIVE_JOB_SOURCE, 0)),
        "model_api_user_messages": int(chat.get("model_api_user_messages") or 0),
        "model_api_agent_messages": int(chat.get("model_api_agent_messages") or 0),
        "model_api_greetings": int(chat.get("model_api_greetings") or 0),
        "first_at": _data_track_iso(chat.get("first_ts")),
        "last_at": _data_track_iso(chat.get("last_ts")),
        "last_user_at": _data_track_iso(chat.get("last_user_ts")),
        "last_agent_at": _data_track_iso(chat.get("last_agent_ts")),
        "proactive_last_at": _data_track_iso(chat.get("proactive_last_ts")),
    }


def _data_track_proactive_from_snapshot(snap: dict, chat: dict) -> dict:
    logs = dict(snap.get("logs") or {})
    extra = dict(snap.get("proactive_extra") or {})
    status_counts = _data_track_count_dict(extra.get("jobs_by_status"))
    live_status_counts = _data_track_count_dict(extra.get("live_activity_status"))
    alert_status_counts = _data_track_count_dict(extra.get("alert_status"))
    decisions = int(extra.get("decisions") or logs.get("gate_decisions", {}).get("count") or 0)
    decision_true = int(extra.get("decision_true") or 0)
    delivered = (
        live_status_counts.get("delivered", 0)
        + alert_status_counts.get("delivered", 0)
        + alert_status_counts.get("logged_only", 0)
    )
    failed = sum(status_counts.get(s, 0) for s in ("failed", "skipped"))
    failed += sum(live_status_counts.get(s, 0) for s in ("failed", "error"))
    failed += sum(alert_status_counts.get(s, 0) for s in ("failed", "error"))
    last_at = _latest_epoch(
        logs.get("proactive_jobs", {}).get("last_ts"),
        logs.get("gate_decisions", {}).get("last_ts"),
        chat.get("proactive_last_at"),
    )
    return {
        "decisions": decisions,
        "decision_true": decision_true,
        "decision_false": max(0, decisions - decision_true),
        "jobs": int(logs.get("proactive_jobs", {}).get("count") or 0),
        "jobs_by_status": status_counts,
        "pending_jobs": status_counts.get("pending", 0),
        "posted_jobs": status_counts.get("posted", 0) + status_counts.get("delivered", 0),
        "failed_jobs": failed,
        "proactive_messages": int(chat.get("proactive_messages") or 0),
        "delivery_signals": delivered,
        "live_activity_status": live_status_counts,
        "alert_status": alert_status_counts,
        "device_events": int(logs.get("device_events", {}).get("count") or 0),
        "last_at": _epoch_to_iso(last_at),
    }


def _data_track_tracking_from_snapshot(snap: dict) -> dict:
    logs = dict(snap.get("logs") or {})
    counts = dict(snap.get("log_counts") or {})
    tracking = logs.get("tracking_events", {}) or {}
    return {
        "events": int(tracking.get("count") or 0),
        "by_type": _data_track_count_dict(counts.get("tracking_by_type")),
        "last_at": _data_track_iso(tracking.get("last_ts")),
    }


def _data_track_bootstrap_from_snapshot(snap: dict) -> dict:
    logs = dict(snap.get("logs") or {})
    counts = dict(snap.get("log_counts") or {})
    bootstrap = logs.get("bootstrap_events", {}) or {}
    return {
        "events": int(bootstrap.get("count") or 0),
        "by_type": _data_track_count_dict(counts.get("bootstrap_by_type")),
        "last_at": _data_track_iso(bootstrap.get("last_ts")),
    }


def _data_track_history_import_from_snapshot(snap: dict) -> dict:
    latest = snap.get("history_import")
    if not isinstance(latest, dict):
        return {"has_job": False}
    return {
        "has_job": True,
        "job_id": latest.get("job_id", ""),
        "status": latest.get("status", ""),
        "phase": latest.get("phase", ""),
        "phase_label": latest.get("phase_label", ""),
        "progress": latest.get("progress", 0),
        "created_at": latest.get("created_at", ""),
        "started_at": latest.get("started_at", ""),
        "updated_at": latest.get("updated_at", ""),
        "completed_at": latest.get("completed_at", ""),
        "failed_at": latest.get("failed_at", ""),
        "error": latest.get("error", ""),
        "messages_parsed": latest.get("messages_parsed", 0),
        "support_materials": latest.get("support_materials", 0),
        "source_stats": latest.get("source_stats", {}),
        "ai_persona_chars": latest.get("ai_persona_chars", 0),
        "user_profile_chars": latest.get("user_profile_chars", latest.get("persona_chars", 0)),
        "memory_summary_chars": latest.get("memory_summary_chars", 0),
        "chat_messages_imported": latest.get("chat_messages_imported", 0),
        "memories_created": latest.get("memories_created", 0),
        "identity_written": bool(latest.get("identity_written")),
        "chat_ready": bool(latest.get("chat_ready")),
    }


def _data_track_relationship_days(identity: dict | None, memory: dict) -> int:
    if identity and identity.get("relationship_started_at"):
        days = _data_track_days_since(identity.get("relationship_started_at"))
        if days is not None:
            return days
    days = _data_track_days_since(memory.get("earliest_occurred_at"))
    return days if days is not None else 0


def _data_track_fast_validation(
    *,
    route: str,
    chat: dict,
    memory: dict,
    identity: dict | None,
    history_import: dict,
    model_api_config: dict | None,
    consumer_state: dict | None,
    bootstrap_events: dict,
) -> dict:
    relationship_days = _data_track_relationship_days(identity, memory)
    floors = _per_tab_floors_for_days(relationship_days)
    counts = memory.get("by_tab") or {}
    missing_tabs = []
    if int(counts.get("story") or 0) < floors["story"]:
        missing_tabs.append("story")
    if int(counts.get("about_me") or 0) < floors["about_me"]:
        missing_tabs.append("about_me")
    identity_written = identity is not None
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = bool(identity and identity.get("relationship_started_at") and relationship_evidence)

    def step(step_id: str, label: str, passing: bool, required: str = "", **extra) -> dict:
        out = {"id": step_id, "label": label, "passing": bool(passing), "required": "" if passing else required}
        out.update(extra)
        return out

    if route == "model_api":
        history_ok = bool(history_import.get("has_job") and (
            history_import.get("status") == "completed" or history_import.get("chat_ready")
        ))
        memory_ok = history_ok and int(counts.get("story") or 0) >= 1 and int(counts.get("about_me") or 0) >= 1
        hosted_chat_ok = bool(
            chat.get("model_api_greetings")
            or (chat.get("model_api_user_messages") and chat.get("model_api_agent_messages"))
        )
        steps = [
            step("model_api_config", "Model API Config", bool(model_api_config), "Call /v1/model_api/setup."),
            step(
                "model_api_test",
                "Model API Test",
                bool(model_api_config and model_api_config.get("test_status") == "ok"),
                "Call /v1/model_api/test until test_status is ok.",
                test_status=(model_api_config or {}).get("test_status", ""),
            ),
            step(
                "history_import",
                "Onboarding Materials",
                history_ok,
                "Start onboarding with AI persona materials, user profile, memory summary, chat history, or confirmed fresh start.",
                job_status=history_import.get("status", ""),
                phase=history_import.get("phase", ""),
                progress=history_import.get("progress", 0),
                chat_ready=history_import.get("chat_ready", False),
            ),
            step(
                "memory_garden",
                "Memory Garden",
                memory_ok,
                "History import must write at least one Story card and one About-me card.",
                counts={"story": counts.get("story", 0), "about_me": counts.get("about_me", 0), "ta_thinking": counts.get("ta_thinking", 0), "total": memory.get("total", 0)},
            ),
            step("identity_card", "Identity Card", identity_written, "History import must derive and write Identity Card."),
            step("relationship_anchor", "Relationship Anchor", relationship_ok, "History import must include relationship_started_at or fresh_start=true."),
            step("hosted_chat", "Hosted Chat", hosted_chat_ok, "Send one test message through /v1/model_api/chat/send."),
        ]
        next_step = next((s for s in steps if not s["passing"]), None)
        return {
            "passing": next_step is None,
            "stage": "complete" if next_step is None else next_step["id"],
            "route": "model_api",
            "next_action": "" if next_step is None else next_step["required"],
            "steps": steps,
        }

    memory_ok = not missing_tabs
    if route == "official_import":
        steps = [
            step("memory_garden", "Memory Garden", memory_ok, "Memory import must fill required Story and About-me cards."),
            step("identity_card", "Identity Card", identity_written, "Use the official app/tool client to import memory and identity."),
            step("relationship_anchor", "Relationship Anchor", relationship_ok, "Set relationship anchor during import."),
        ]
        next_step = next((s for s in steps if not s["passing"]), None)
        return {
            "passing": next_step is None,
            "stage": "import_ready" if next_step is None else next_step["id"],
            "route": "official_import",
            "next_action": "" if next_step is None else next_step["required"],
            "steps": steps,
        }

    consumer = consumer_state or {}
    try:
        age_sec = time.time() - float(consumer.get("last_poll_epoch") or 0)
    except Exception:
        age_sec = None
    consumer_ok = bool(consumer.get("official")) and age_sec is not None and age_sec <= _CONSUMER_RECENT_SEC
    bootstrap_types = bootstrap_events.get("by_type") or {}
    chat_loop_ok = bool(bootstrap_types.get("chat_loop_verified")) or bool(chat.get("user_messages") and chat.get("agent_messages"))
    first_greeting_ok = int(chat.get("agent_messages") or 0) > 0
    real_exchange_ok = bool(chat.get("user_messages") and chat.get("agent_messages"))
    steps = [
        step("memory_garden", "Memory Garden", memory_ok, "Memory Garden is below required Story/About-me floors."),
        step("identity_card", "Identity Card", identity_written, "Call feedling_identity_init after memory verification passes."),
        step("relationship_anchor", "Relationship Anchor", relationship_ok, "Re-run identity init with relationship_anchor_evidence."),
        step("resident_consumer", "Resident Consumer", consumer_ok, "Run the standard feedling-chat-resident / IO resident consumer."),
        step("live_loop", "Live Connection", chat_loop_ok, "Call feedling_chat_verify_loop after resident consumer is polling."),
        step("first_greeting", "First Greeting", first_greeting_ok, "Send first visible greeting via feedling_chat_post_message."),
        step("real_chat_acceptance", "Real Chat Acceptance", real_exchange_ok, "Ask the user to send one ordinary IO Chat message and confirm a reply."),
    ]
    next_step = next((s for s in steps if not s["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "route": "resident",
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
    }


def _build_data_track_user_fast(user_entry: dict, snap: dict) -> dict:
    user_id = str(user_entry.get("user_id") or "")
    blobs = dict(snap.get("blobs") or {})
    route_data = blobs.get("onboarding_route") or {}
    route = _normalize_onboarding_route(str((route_data or {}).get("route") or "resident"))
    route = route if route in MODEL_API_ROUTES else "resident"
    access_modes = _public_access_mode_state(dict(user_entry), route)
    access_connected = [
        mode["access_mode"]
        for mode in access_modes
        if mode.get("connected")
    ]
    api_keys_count = sum(
        1
        for key_entry in user_entry.get("api_keys") or []
        if isinstance(key_entry, dict) and not key_entry.get("revoked_at")
    )
    chat = _data_track_chat_from_snapshot(snap)
    memory = _data_track_memory_from_snapshot(snap)
    proactive = _data_track_proactive_from_snapshot(snap, chat)
    tracking = _data_track_tracking_from_snapshot(snap)
    bootstrap_events = _data_track_bootstrap_from_snapshot(snap)
    history_import = _data_track_history_import_from_snapshot(snap)
    identity = blobs.get("identity") if isinstance(blobs.get("identity"), dict) else None
    validation = _data_track_fast_validation(
        route=route,
        chat=chat,
        memory=memory,
        identity=identity,
        history_import=history_import,
        model_api_config=blobs.get("model_api") if isinstance(blobs.get("model_api"), dict) else None,
        consumer_state=blobs.get("consumer_state") if isinstance(blobs.get("consumer_state"), dict) else None,
        bootstrap_events=bootstrap_events,
    )
    steps = validation.get("steps", [])
    steps_total = len(steps)
    steps_done = sum(1 for s in steps if bool(s.get("passing")))
    registered_at = str(user_entry.get("created_at") or "")
    identity_updated_at = (identity or {}).get("updated_at", "")
    latest_epoch = _latest_epoch(
        registered_at,
        route_data.get("selected_at"),
        chat.get("last_at"),
        memory.get("last_created_at"),
        proactive.get("last_at"),
        tracking.get("last_at"),
        bootstrap_events.get("last_at"),
        identity_updated_at,
        history_import.get("updated_at"),
        history_import.get("completed_at"),
    )
    passing = bool(validation.get("passing"))
    stuck_for_sec = 0 if passing else int(max(0, time.time() - latest_epoch)) if latest_epoch else None
    return {
        "user_id": user_id,
        "principal_id": user_entry.get("principal_id") or "",
        "registered_at": registered_at,
        "archive_language": user_entry.get("archive_language") or "",
        "public_key_present": bool(str(user_entry.get("public_key") or "").strip()),
        "route": route,
        "route_selected_at": route_data.get("selected_at", ""),
        "access": {
            "principal_id": user_entry.get("principal_id") or "",
            "active_route": route,
            "connected_modes": access_connected,
            "modes": access_modes,
            "api_keys_count": api_keys_count,
        },
        "onboarding": {
            "passing": passing,
            "stage": "complete" if passing else validation.get("stage") or "unknown",
            "steps_done": steps_done,
            "steps_total": steps_total,
            "next_action": validation.get("next_action", ""),
            "steps": [],
            "stuck_for_sec": stuck_for_sec,
        },
        "last_activity_at": _epoch_to_iso(latest_epoch),
        "chat": chat,
        "memory": memory,
        "proactive": proactive,
        "push": _push_stats_from_user_entry(user_entry),
        "tracking": tracking,
        "bootstrap_events": bootstrap_events,
        "history_import": history_import,
    }


def _push_stats_from_user_entry(user_entry: dict) -> dict:
    tokens = [t for t in (user_entry.get("tokens") or []) if isinstance(t, dict)]
    statuses = _count_rows(tokens, "status")
    updated_epochs = [_to_epoch(t.get("updated_at") or t.get("registered_at")) for t in tokens]
    return {
        "tokens": len(tokens),
        "active_tokens": statuses.get("active", 0),
        "by_status": statuses,
        "last_token_at": _epoch_to_iso(max(updated_epochs, default=0)),
    }


def _bootstrap_event_stats(store: UserStore, *, include_events: bool = False) -> dict:
    events = _load_bootstrap_events(store)
    by_type = _count_rows(events, "event_type")
    epochs = [_to_epoch(e.get("timestamp") or e.get("ts")) for e in events]
    out = {
        "events": len(events),
        "by_type": by_type,
        "last_at": _epoch_to_iso(max(epochs, default=0)),
    }
    if include_events:
        out["latest"] = [
            {
                "event_type": e.get("event_type", ""),
                "success": bool(e.get("success")),
                "timestamp": e.get("timestamp", ""),
                "has_error": bool(str(e.get("error_message") or "").strip()),
            }
            for e in events[-50:]
        ]
    return out


def _build_data_track_user(user_entry: dict, *, include_detail: bool = False) -> dict:
    user_id = str(user_entry.get("user_id") or "")
    store = get_store(user_id)
    route_data = db.get_blob(store.user_id, "onboarding_route") or {}
    route = _load_onboarding_route(store)
    access_modes = _public_access_mode_state(dict(user_entry), route)
    access_connected = [
        mode["access_mode"]
        for mode in access_modes
        if mode.get("connected")
    ]
    api_keys_count = sum(
        1
        for key_entry in user_entry.get("api_keys") or []
        if isinstance(key_entry, dict) and not key_entry.get("revoked_at")
    )
    validation = _safe_onboarding_validation(_onboarding_validation_payload(store))
    steps = validation.get("steps", [])
    steps_total = len(steps)
    steps_done = sum(1 for s in steps if bool(s.get("passing")))
    chat = _chat_stats(store)
    memory = _memory_stats(store)
    proactive = _proactive_stats(store)
    push = _push_stats(store)
    tracking = _tracking_stats(store, include_events=include_detail)
    bootstrap_events = _bootstrap_event_stats(store, include_events=include_detail)
    history_import = _history_import_stats(store)
    identity = _load_identity(store)
    identity_updated_at = (identity or {}).get("updated_at", "")
    registered_at = str(user_entry.get("created_at") or "")
    latest_epoch = _latest_epoch(
        registered_at,
        route_data.get("selected_at"),
        chat.get("last_at"),
        memory.get("last_created_at"),
        proactive.get("last_at"),
        tracking.get("last_at"),
        bootstrap_events.get("last_at"),
        identity_updated_at,
        history_import.get("updated_at"),
        history_import.get("completed_at"),
    )
    now = time.time()
    stage = validation.get("stage") or "unknown"
    passing = bool(validation.get("passing"))
    stuck_for_sec = 0 if passing else int(max(0, now - latest_epoch)) if latest_epoch else None
    row = {
        "user_id": user_id,
        "principal_id": user_entry.get("principal_id") or "",
        "registered_at": registered_at,
        "archive_language": user_entry.get("archive_language") or "",
        "public_key_present": bool(str(user_entry.get("public_key") or "").strip()),
        "route": route,
        "route_selected_at": route_data.get("selected_at", ""),
        "access": {
            "principal_id": user_entry.get("principal_id") or "",
            "active_route": route,
            "connected_modes": access_connected,
            "modes": access_modes,
            "api_keys_count": api_keys_count,
        },
        "onboarding": {
            "passing": passing,
            "stage": "complete" if passing else stage,
            "steps_done": steps_done,
            "steps_total": steps_total,
            "next_action": validation.get("next_action", ""),
            "steps": steps if include_detail else [],
            "stuck_for_sec": stuck_for_sec,
        },
        "last_activity_at": _epoch_to_iso(latest_epoch),
        "chat": chat,
        "memory": memory,
        "proactive": proactive,
        "push": push,
        "tracking": tracking,
        "bootstrap_events": bootstrap_events,
        "history_import": history_import,
    }
    if include_detail:
        row["identity"] = {
            "written": identity is not None,
            "updated_at": identity_updated_at,
            "relationship_started_at": (identity or {}).get("relationship_started_at", ""),
            "relationship_anchor_source": (identity or {}).get("relationship_anchor_source", ""),
            "has_relationship_anchor_evidence": bool(
                str((identity or {}).get("relationship_anchor_evidence") or "").strip()
            ),
        }
    return row


def _data_track_request_filters() -> dict:
    raw_since = (
        request.args.get("since")
        or request.args.get("registered_since")
        or ""
    ).strip()
    raw_q = (request.args.get("q") or "").strip().lower()
    raw_sort = (request.args.get("sort") or "").strip().lower()
    if raw_sort not in {"chat", "memory", "proactive"}:
        raw_sort = ""
    raw_dir = (request.args.get("dir") or "desc").strip().lower()
    if raw_dir not in {"asc", "desc"}:
        raw_dir = "desc"
    raw_view = (request.args.get("view") or "users").strip().lower()
    if raw_view not in {"users", "dau"}:
        raw_view = "users"

    def read_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(request.args.get(name, default))
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    return {
        "since": raw_since,
        "since_epoch": _to_epoch(raw_since),
        "q": raw_q,
        "sort": raw_sort,
        "dir": raw_dir,
        "limit": read_int("limit", 100, 1, 500),
        "offset": read_int("offset", 0, 0, 1_000_000),
        "view": raw_view,
        "days": read_int("days", 30, 1, 366),
    }


def _data_track_filter_users(users: list[dict], filters: dict) -> list[dict]:
    since_epoch = float(filters.get("since_epoch") or 0)
    if not since_epoch:
        return users
    return [
        u for u in users
        if _to_epoch(u.get("created_at")) >= since_epoch
    ]


def _data_track_apply_text_filter(rows: list[dict], q: str) -> list[dict]:
    needle = (q or "").strip().lower()
    if not needle:
        return rows
    out = []
    for row in rows:
        hay = " ".join([
            str(row.get("user_id") or ""),
            str(row.get("principal_id") or ""),
            str(row.get("route") or ""),
            str(row.get("archive_language") or ""),
            str(row.get("onboarding", {}).get("stage") or ""),
            " ".join(row.get("access", {}).get("connected_modes") or []),
        ]).lower()
        if needle in hay:
            out.append(row)
    return out


def _data_track_sort_rows(rows: list[dict], sort_key: str, direction: str) -> None:
    def intval(value) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    def metrics(row: dict) -> tuple[int, ...]:
        if sort_key == "chat":
            chat = row.get("chat") or {}
            return (
                intval(chat.get("total")),
                intval(chat.get("user_messages")),
                intval(chat.get("agent_messages")),
            )
        if sort_key == "memory":
            memory = row.get("memory") or {}
            by_tab = memory.get("by_tab") or {}
            return (
                intval(memory.get("total")),
                intval(by_tab.get("story")),
                intval(by_tab.get("about_me")),
                intval(by_tab.get("ta_thinking")),
            )
        if sort_key == "proactive":
            proactive = row.get("proactive") or {}
            return (
                intval(proactive.get("proactive_messages")),
                intval(proactive.get("jobs")),
                intval(proactive.get("decisions")),
                intval(proactive.get("delivery_signals")),
            )
        return (0,)

    if not sort_key:
        rows.sort(key=lambda r: (_to_epoch(r.get("registered_at")), str(r.get("user_id") or "")), reverse=True)
        return

    desc = direction != "asc"

    def sort_tuple(row: dict) -> tuple:
        values = metrics(row)
        if desc:
            values = tuple(-v for v in values)
        return (*values, -_to_epoch(row.get("registered_at")), str(row.get("user_id") or ""))

    rows.sort(key=sort_tuple)


def _data_track_payload(*, include_users: bool = True, include_detail_user: str = "") -> dict:
    filters = _data_track_request_filters()
    with _users_lock:
        if _normalize_all_users():
            _save_users()
        users = [dict(u) for u in _users if u.get("user_id")]
    users = _data_track_filter_users(users, filters)
    snapshot = db.admin_data_track_snapshot([str(u.get("user_id") or "") for u in users])
    rows = []
    for u in users:
        uid = str(u.get("user_id") or "")
        if include_detail_user and include_detail_user == uid:
            rows.append(_build_data_track_user(u, include_detail=True))
        else:
            rows.append(_build_data_track_user_fast(u, snapshot.get(uid, {})))
    rows = _data_track_apply_text_filter(rows, str(filters.get("q") or ""))
    _data_track_sort_rows(rows, str(filters.get("sort") or ""), str(filters.get("dir") or "desc"))
    completed = sum(1 for r in rows if r["onboarding"]["passing"])
    incomplete = max(0, len(rows) - completed)
    stage_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    access_mode_counts: dict[str, int] = {}
    chat_total = 0
    memory_total = 0
    proactive_jobs = 0
    proactive_messages = 0
    proactive_failed = 0
    for row in rows:
        stage = row["onboarding"]["stage"]
        route = row["route"]
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        route_counts[route] = route_counts.get(route, 0) + 1
        for mode in row.get("access", {}).get("connected_modes", []):
            access_mode_counts[mode] = access_mode_counts.get(mode, 0) + 1
        chat_total += row["chat"]["total"]
        memory_total += row["memory"]["total"]
        proactive_jobs += row["proactive"]["jobs"]
        proactive_messages += row["proactive"]["proactive_messages"]
        proactive_failed += row["proactive"]["failed_jobs"]
    summary = {
        "generated_at": datetime.now().isoformat(),
        "users_total": len(rows),
        "onboarding_completed": completed,
        "onboarding_incomplete": incomplete,
        "completion_rate": (completed / len(rows)) if rows else 0,
        "stage_counts": stage_counts,
        "route_counts": route_counts,
        "access_mode_counts": access_mode_counts,
        "principals_total": len(set(r.get("principal_id") or r.get("user_id") for r in rows)),
        "chat_messages_total": chat_total,
        "memory_total": memory_total,
        "memory_avg_per_user": (memory_total / len(rows)) if rows else 0,
        "proactive_jobs_total": proactive_jobs,
        "proactive_messages_total": proactive_messages,
        "proactive_failed_total": proactive_failed,
    }
    payload = {
        "summary": summary,
        "filters": {
            "since": filters.get("since", ""),
            "q": filters.get("q", ""),
            "sort": filters.get("sort", ""),
            "dir": filters.get("dir", "desc"),
        },
    }
    if include_users:
        offset = int(filters.get("offset") or 0)
        limit = int(filters.get("limit") or 100)
        payload["users"] = rows[offset:offset + limit]
        payload["pagination"] = {
            "limit": limit,
            "offset": offset,
            "returned": len(payload["users"]),
            "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "prev_offset": max(0, offset - limit) if offset > 0 else None,
        }
    return payload


def _data_track_dau_payload() -> dict:
    filters = _data_track_request_filters()
    days = int(filters.get("days") or 30)
    rows = db.admin_data_track_dau(
        since_epoch=float(filters.get("since_epoch") or 0),
        days=days,
        tz="Asia/Shanghai",
    )
    dau_values = [int(row.get("dau") or 0) for row in rows]
    latest = rows[0] if rows else {}
    summary = {
        "generated_at": datetime.now().isoformat(),
        "timezone": "Asia/Shanghai",
        "days_returned": len(rows),
        "latest_day": latest.get("day", ""),
        "latest_dau": int(latest.get("dau") or 0),
        "max_dau": max(dau_values, default=0),
        "avg_dau": (sum(dau_values) / len(dau_values)) if dau_values else 0,
        "user_messages": sum(int(row.get("user_messages") or 0) for row in rows),
        "tracking_events": sum(int(row.get("tracking_events") or 0) for row in rows),
        "active_events": sum(int(row.get("active_events") or 0) for row in rows),
    }
    return {
        "summary": summary,
        "filters": {
            "since": filters.get("since", ""),
            "days": days,
            "view": "dau",
        },
        "rows": [
            {
                **row,
                "first_at": _epoch_to_iso(row.get("first_ts")),
                "last_at": _epoch_to_iso(row.get("last_ts")),
            }
            for row in rows
        ],
        "definition": {
            "dau": "Distinct users with at least one user chat message or tracking event on the Beijing day.",
            "excluded": "Agent/openclaw messages, proactive writes, and verify_ping synthetic messages are excluded.",
            "timezone": "Asia/Shanghai",
        },
    }


def _format_duration(seconds) -> str:
    if seconds is None:
        return "n/a"
    try:
        sec = int(seconds)
    except Exception:
        return "n/a"
    if sec < 60:
        return f"{sec}s"
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _render_metric(label: str, value) -> str:
    return (
        "<div class='metric'>"
        f"<div class='metric-value'>{html.escape(str(value))}</div>"
        f"<div class='metric-label'>{html.escape(label)}</div>"
        "</div>"
    )


def _data_track_page_href(**updates) -> str:
    qs_inner = _data_track_qs(**updates)
    return f"/admin/data-track?{qs_inner}" if qs_inner else "/admin/data-track"


def _render_data_track_view_nav(active: str) -> str:
    def nav_item(view: str, label: str) -> str:
        cls = "sort-button active" if active == view else "sort-button"
        href = _data_track_page_href(view=None if view == "users" else view, offset=0)
        return f"<a class='{cls}' href='{html.escape(href, quote=True)}'>{html.escape(label)}</a>"

    return (
        "<div class='viewbar'>"
        f"{nav_item('users', 'Users')}"
        f"{nav_item('dau', 'DAU')}"
        "</div>"
    )


def _render_data_track_page(payload: dict) -> str:
    summary = payload["summary"]
    users = payload.get("users", [])
    pagination = payload.get("pagination", {})
    filters = payload.get("filters", {})
    qs = _data_track_qs()
    qs_suffix = f"?{qs}" if qs else ""
    current_sort = str(filters.get("sort") or "")
    current_dir = str(filters.get("dir") or "desc")

    def sort_button(metric: str, direction: str, label: str) -> str:
        active = current_sort == metric and current_dir == direction
        cls = "sort-button active" if active else "sort-button"
        href = _data_track_page_href(sort=metric, dir=direction, offset=0, view=None)
        return f"<a class='{cls}' href='{html.escape(href, quote=True)}'>{html.escape(label)}</a>"

    sort_controls = "".join([
        sort_button("chat", "desc", "Chat desc"),
        sort_button("chat", "asc", "Chat asc"),
        sort_button("memory", "desc", "Memory desc"),
        sort_button("memory", "asc", "Memory asc"),
        sort_button("proactive", "desc", "Proactive desc"),
        sort_button("proactive", "asc", "Proactive asc"),
    ])
    pager = ""
    if pagination:
        pager_links = []
        prev_offset = pagination.get("prev_offset")
        next_offset = pagination.get("next_offset")
        if prev_offset is not None:
            pager_links.append(f"<a class='sort-button' href='{html.escape(_data_track_page_href(offset=prev_offset, view=None), quote=True)}'>Prev</a>")
        if next_offset is not None:
            pager_links.append(f"<a class='sort-button' href='{html.escape(_data_track_page_href(offset=next_offset, view=None), quote=True)}'>Next</a>")
        if pager_links:
            pager = f"<div class='pager'>{''.join(pager_links)}</div>"
    rows_html = []
    for row in users:
        onboarding = row["onboarding"]
        stage = onboarding["stage"]
        complete = onboarding["passing"]
        status_class = "ok" if complete else "warn"
        user_url = f"/admin/data-track/users/{quote(row['user_id'])}{qs_suffix}"
        access = row.get("access", {})
        principal = str(access.get("principal_id") or row.get("principal_id") or "")
        principal_short = f"{principal[:12]}…" if len(principal) > 12 else principal
        connected_modes = ", ".join(access.get("connected_modes") or []) or "none"
        rows_html.append(
            "<tr>"
            f"<td><a href='{html.escape(user_url)}'>{html.escape(row['user_id'])}</a>"
            f"<br><span class='muted'>{html.escape(principal_short)} · keys {access.get('api_keys_count', 0)}</span></td>"
            f"<td>{html.escape(row['route'])}</td>"
            f"<td>{html.escape(connected_modes)}</td>"
            f"<td><span class='pill {status_class}'>{html.escape(stage)}</span></td>"
            f"<td>{onboarding['steps_done']}/{onboarding['steps_total']}</td>"
            f"<td>{html.escape(_format_duration(onboarding['stuck_for_sec']))}</td>"
            f"<td>{row['chat']['total']} <span class='muted'>u{row['chat']['user_messages']} / a{row['chat']['agent_messages']}</span></td>"
            f"<td>{row['memory']['total']} <span class='muted'>S{row['memory']['by_tab'].get('story', 0)} / A{row['memory']['by_tab'].get('about_me', 0)} / T{row['memory']['by_tab'].get('ta_thinking', 0)}</span>"
            f"<br><span class='muted'>cap {row['memory'].get('capture_actions_written', 0)} / edit {row['memory'].get('changes', 0)}</span></td>"
            f"<td>{row['proactive']['proactive_messages']} <span class='muted'>jobs {row['proactive']['jobs']} / fail {row['proactive']['failed_jobs']}</span></td>"
            f"<td>{html.escape(row.get('last_activity_at') or '')}</td>"
            "</tr>"
        )
    metrics = "".join([
        _render_metric("users", summary["users_total"]),
        _render_metric("onboarding done", summary["onboarding_completed"]),
        _render_metric("completion", f"{summary['completion_rate'] * 100:.0f}%"),
        _render_metric("chat messages", summary["chat_messages_total"]),
        _render_metric("memories", summary["memory_total"]),
    _render_metric("proactive jobs", summary["proactive_jobs_total"]),
    _render_metric("principals", summary.get("principals_total", summary["users_total"])),
    ])
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling Beta Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; --ok:#1d7a4d; --warn:#a05a00; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:28px 24px 48px; }}
    h1 {{ font-size:26px; margin:0 0 4px; }}
    h2 {{ font-size:16px; margin:28px 0 12px; }}
    .muted {{ color:var(--muted); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }}
    .metric {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric-value {{ font-size:24px; font-weight:700; }}
    .metric-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
	    .toolbar {{ display:flex; gap:10px; align-items:center; margin:18px 0; }}
	    .viewbar,.sortbar,.pager {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:10px 0 18px; }}
	    .sort-button {{ display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }}
	    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#fff1ed; }}
	    input {{ width:320px; max-width:100%; border:1px solid var(--line); border-radius:6px; padding:9px 10px; background:white; color:var(--fg); }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
    th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }}
    tr:last-child td {{ border-bottom:0; }}
    a {{ color:var(--accent); text-decoration:none; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:2px 8px; font-size:12px; background:#efe7df; color:var(--muted); }}
    .pill.ok {{ color:var(--ok); background:#e7f3ed; }}
    .pill.warn {{ color:var(--warn); background:#fff1db; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
  </style>
</head>
<body>
<main>
	  <h1>Feedling Beta Data Track</h1>
	  <div class="muted">Generated {html.escape(summary["generated_at"])}. Metadata only; encrypted content is not read or rendered.</div>
	  <div class="muted">Showing {html.escape(str(pagination.get("returned", len(users))))} of {html.escape(str(pagination.get("total", summary["users_total"])))} filtered users. Since {html.escape(str(filters.get("since") or "all time"))}.</div>
	  {_render_data_track_view_nav("users")}
		  <section class="metrics">{metrics}</section>
	  <h2>Beta users</h2>
	  <div class="toolbar"><input id="q" placeholder="Filter user, route, stage"></div>
	  <div class="sortbar">{sort_controls}</div>
	  {pager}
	  <table id="users">
    <thead><tr><th>User</th><th>Route</th><th>Access</th><th>Onboarding</th><th>Steps</th><th>Stuck</th><th>Chat</th><th>Memory</th><th>Proactive</th><th>Last activity</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else "<tr><td colspan='10' class='muted'>No users yet.</td></tr>"}</tbody>
  </table>
</main>
<script>
const q = document.getElementById('q');
q.addEventListener('input', () => {{
  const needle = q.value.toLowerCase();
  for (const tr of document.querySelectorAll('#users tbody tr')) {{
    tr.style.display = tr.textContent.toLowerCase().includes(needle) ? '' : 'none';
  }}
}});
</script>
	</body>
	</html>"""


def _render_data_track_dau_page(payload: dict) -> str:
    summary = payload["summary"]
    filters = payload.get("filters", {})
    rows = payload.get("rows", [])
    definition = payload.get("definition", {})
    api_qs = _data_track_qs(view=None, q=None, limit=None, offset=None, sort=None, dir=None)
    api_url = f"/v1/admin/data-track/dau?{api_qs}" if api_qs else "/v1/admin/data-track/dau"
    rows_html = []
    for row in rows:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('day') or ''))}</td>"
            f"<td>{int(row.get('dau') or 0)}</td>"
            f"<td>{int(row.get('chat_dau') or 0)}</td>"
            f"<td>{int(row.get('tracking_dau') or 0)}</td>"
            f"<td>{int(row.get('active_events') or 0)}</td>"
            f"<td>{int(row.get('user_messages') or 0)}</td>"
            f"<td>{int(row.get('tracking_events') or 0)}</td>"
            f"<td>{html.escape(str(row.get('last_at') or ''))}</td>"
            "</tr>"
        )
    metrics = "".join([
        _render_metric("latest DAU", summary["latest_dau"]),
        _render_metric("latest day", summary.get("latest_day") or "n/a"),
        _render_metric("max DAU", summary["max_dau"]),
        _render_metric("avg DAU", f"{summary['avg_dau']:.1f}"),
        _render_metric("user messages", summary["user_messages"]),
        _render_metric("tracking events", summary["tracking_events"]),
    ])
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling DAU · Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:28px 24px 48px; }}
    h1 {{ font-size:26px; margin:0 0 4px; }}
    h2 {{ font-size:16px; margin:28px 0 12px; }}
    .muted {{ color:var(--muted); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }}
    .metric {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric-value {{ font-size:24px; font-weight:700; }}
    .metric-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    .viewbar,.toolbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:14px 0 18px; }}
    .sort-button {{ display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }}
    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#fff1ed; }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
    th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }}
    tr:last-child td {{ border-bottom:0; }}
    a {{ color:var(--accent); text-decoration:none; }}
  </style>
</head>
<body>
<main>
  <h1>Feedling Beta Data Track</h1>
  <div class="muted">Generated {html.escape(summary["generated_at"])}. DAU timezone: {html.escape(summary["timezone"])}.</div>
  <div class="muted">Showing {html.escape(str(summary["days_returned"]))} active days. Since {html.escape(str(filters.get("since") or "all time"))}; days limit {html.escape(str(filters.get("days") or 30))}.</div>
  {_render_data_track_view_nav("dau")}
  <section class="metrics">{metrics}</section>
  <h2>Daily Active Users</h2>
  <div class="muted">{html.escape(definition.get("dau") or "")} {html.escape(definition.get("excluded") or "")}</div>
  <div class="toolbar"><a class="sort-button" href="{html.escape(api_url, quote=True)}">JSON</a></div>
  <table>
    <thead><tr><th>Beijing day</th><th>DAU</th><th>Chat DAU</th><th>Tracking DAU</th><th>Active events</th><th>User messages</th><th>Tracking events</th><th>Last active</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else "<tr><td colspan='8' class='muted'>No DAU activity in this range.</td></tr>"}</tbody>
  </table>
</main>
</body>
</html>"""


def _render_user_detail_page(user: dict) -> str:
    qs = _data_track_qs()
    back = f"/admin/data-track?{qs}" if qs else "/admin/data-track"
    safe_json = json.dumps(user, ensure_ascii=False, indent=2)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(user['user_id'])} · Feedling Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1040px; margin:0 auto; padding:28px 24px 48px; }}
    a {{ color:var(--accent); text-decoration:none; }}
    h1 {{ font-size:24px; margin:14px 0 4px; }}
    .muted {{ color:var(--muted); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin:20px 0; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .value {{ font-size:22px; font-weight:700; }}
    .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
  </style>
</head>
<body>
<main>
  <a href="{html.escape(back)}">Back to data track</a>
  <h1>{html.escape(user['user_id'])}</h1>
  <div class="muted">Principal {html.escape(user.get('principal_id') or '')}; route {html.escape(user['route'])}; stage {html.escape(user['onboarding']['stage'])}; metadata only.</div>
  <section class="grid">
    <div class="card"><div class="value">{user['onboarding']['steps_done']}/{user['onboarding']['steps_total']}</div><div class="label">onboarding steps</div></div>
    <div class="card"><div class="value">{html.escape(_format_duration(user['onboarding']['stuck_for_sec']))}</div><div class="label">stuck for</div></div>
    <div class="card"><div class="value">{user['chat']['total']}</div><div class="label">chat messages</div></div>
    <div class="card"><div class="value">{user['memory']['total']}</div><div class="label">memories</div></div>
    <div class="card"><div class="value">{user['proactive']['proactive_messages']}</div><div class="label">proactive writes</div></div>
  </section>
  <pre>{html.escape(safe_json)}</pre>
</main>
</body>
</html>"""


@app.route("/v1/admin/data-track/summary", methods=["GET"])
def admin_data_track_summary():
    require_admin()
    return jsonify(_data_track_payload(include_users=False))


@app.route("/v1/admin/data-track/users", methods=["GET"])
def admin_data_track_users():
    require_admin()
    return jsonify(_data_track_payload(include_users=True))


@app.route("/v1/admin/data-track/dau", methods=["GET"])
def admin_data_track_dau():
    require_admin()
    return jsonify(_data_track_dau_payload())


@app.route("/v1/admin/data-track/users/<user_id>", methods=["GET"])
def admin_data_track_user(user_id: str):
    require_admin()
    with _users_lock:
        entry = next((dict(u) for u in _users if u.get("user_id") == user_id), None)
    if not entry:
        return jsonify({"error": "user_not_found"}), 404
    return jsonify({"user": _build_data_track_user(entry, include_detail=True)})


@app.route("/admin/data-track", methods=["GET"])
def admin_data_track_page():
    require_admin()
    if (request.args.get("view") or "").strip().lower() == "dau":
        return Response(_render_data_track_dau_page(_data_track_dau_payload()), mimetype="text/html")
    return Response(_render_data_track_page(_data_track_payload(include_users=True)), mimetype="text/html")


@app.route("/admin/data-track/users/<user_id>", methods=["GET"])
def admin_data_track_user_page(user_id: str):
    require_admin()
    with _users_lock:
        entry = next((dict(u) for u in _users if u.get("user_id") == user_id), None)
    if not entry:
        return Response("user not found", status=404, mimetype="text/plain")
    return Response(_render_user_detail_page(_build_data_track_user(entry, include_detail=True)), mimetype="text/html")


@app.route("/v1/admin/store/evict", methods=["POST"])
def admin_store_evict():
    """Drop a user's cached in-process store so its next access rebuilds from
    PostgreSQL. Use after an out-of-band DB write (e.g. the orphan-account
    recovery tool) to surface the change immediately, instead of waiting for the
    cache TTL or a backend redeploy."""
    require_admin()
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("user_id") or request.args.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    evicted = _evict_store(user_id)
    print(f"[admin:store/evict] user_id={user_id} evicted={evicted}")
    return jsonify({"evicted": evicted, "user_id": user_id})


# Synthetic chat-loop ping — server posts a marker user message,
# posts a synthetic ping, waits for an agent-role reply, reports back.
# This proves that some reply pipeline is alive. It cannot, by itself,
# prove that a one-shot CLI is resident; a bridge/fallback may answer.
@app.route("/v1/chat/verify_loop", methods=["POST"])
def chat_verify_loop():
    """Synthetic ping: insert a marker user message, wait up to `timeout_sec`
    for an agent-role reply, return whether a reply pipeline is alive.

    The marker is `__VERIFY_PING__:<uuid>`. Server stores it as a normal
    user envelope with `synthetic: True` flag. After timeout, marker is
    GC'd if no reply landed (so the user's actual chat history isn't
    polluted with sentinel messages).

    Returns:
      {loop_alive: bool, response_time_sec: float|null, passing: bool,
       ping_id: str, suggestions: [...]}.

    Note: passing=true means an agent-role message appeared after the
    ping. It does not prove that a one-shot command stayed alive;
    that must be decided by the onboarding Connection owner selection.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    timeout_sec = min(int(payload.get("timeout_sec", 30)), 60)

    ping_uuid = uuid.uuid4().hex[:12]
    ping_marker = f"__VERIFY_PING__:{ping_uuid}"

    # Build a synthetic v1 envelope. Content is sentinel plaintext —
    # not visible to agent decryption pipelines (they see plaintext
    # ping_marker via the normal chat history endpoint). Visibility is
    # local_only so we don't pollute the enclave's shared store.
    synthetic_env = {
        "v": 1,
        "id": uuid.uuid4().hex,
        "body_ct": base64.b64encode(ping_marker.encode("utf-8")).decode("ascii"),
        "nonce": base64.b64encode(b"\x00" * 12).decode("ascii"),
        "K_user": base64.b64encode(b"\x00" * 32).decode("ascii"),
        "visibility": "local_only",
        "owner_user_id": store.user_id,
        "synthetic": True,
        "synthetic_marker": ping_marker,
    }

    # append_chat acquires chat_lock internally — don't hold it here or
    # we'd deadlock on the non-reentrant lock.
    ping_msg = store.append_chat("user", "verify_ping", synthetic_env)
    store.notify_chat_waiters()
    ping_ts = ping_msg["ts"]

    print(f"[verify_loop:{store.user_id}] posted synthetic ping {ping_uuid} at ts={ping_ts}")

    # Wait for agent reply that came AFTER our ping
    deadline = time.time() + timeout_sec
    response_time = None
    found_reply = False
    found_reply_id = ""
    while time.time() < deadline:
        time.sleep(2)
        with store.chat_lock:
            chat_msgs = list(store.chat_messages)
        for m in chat_msgs:
            if not isinstance(m, dict):
                continue
            if m.get("role") not in ("agent", "openclaw"):
                continue
            try:
                m_ts = float(m.get("ts", 0))
            except Exception:
                continue
            if m_ts > ping_ts:
                response_time = m_ts - ping_ts
                found_reply = True
                found_reply_id = m.get("id", "")
                break
        if found_reply:
            break

    if found_reply:
        _log_bootstrap_event(store, "chat_loop_verified", success=True)

    # Cleanup: remove synthetic ping from history regardless of outcome.
    # If a reply landed, also remove the matching agent response. The verify
    # exchange is a private liveness test; it must not open Chat as the
    # user's visible "First message."
    with store.chat_lock:
        def _is_synthetic(m):
            return (
                isinstance(m, dict)
                and (
                    m.get("source") == "verify_ping"
                    or (found_reply_id and m.get("id") == found_reply_id)
                )
            )
        removed_ids = [m.get("id") for m in store.chat_messages if _is_synthetic(m)]
        store.chat_messages = [m for m in store.chat_messages if not _is_synthetic(m)]
        for rid in removed_ids:
            if rid:
                db.chat_delete(store.user_id, rid)

    suggestions = []
    if not found_reply:
        suggestions.append(
            "No agent reply within timeout. Likely causes: "
            "(a) the independent feedling-chat-resident / IO resident consumer "
            "is not running with the current FEEDLING_API_KEY; "
            "(b) the consumer is not polling FEEDLING_API_URL/v1/chat/poll; "
            "(c) your reply was rejected by an envelope-level error — "
            "check the consumer logs for 4xx errors; "
            "(d) AGENT_HTTP_URL / AGENT_CLI_CMD is not reaching the real agent. "
            "Use the resident consumer service and verify one ordinary IO Chat "
            "message after passing=true."
        )

    return jsonify({
        "loop_alive": found_reply,
        "response_time_sec": response_time,
        "ping_id": ping_uuid,
        "timeout_sec": timeout_sec,
        "suggestions": suggestions,
        "passing": found_reply,
    })


# ---------------------------------------------------------------------------
# Envelope swap: replace an existing chat/memory item's ciphertext in place.
#
# Used by the per-item visibility toggle in iOS Settings. The client
# re-wraps its own plaintext with a new envelope (either including
# K_enclave for `shared` or omitting it for `local_only`) and POSTs
# {items: [{type, id, envelope}]}. Server swaps in place, preserving
# plaintext metadata (id/role/ts/source/occurred_at/created_at).
#
# NOT a migration endpoint — all stored items are already v1 — so
# there is no "already_v1" short-circuit. A v0 item (ancient data
# from before the strip) will fail with "not_found" if its v is < 1.
# ---------------------------------------------------------------------------


def _swap_envelope_missing(env) -> list:
    if not isinstance(env, dict):
        return ["envelope"]
    return [f for f in ("body_ct", "nonce", "K_user", "visibility", "owner_user_id") if not env.get(f)]


def _swap_summary(results: list) -> dict:
    summary = {"ok": 0, "not_found": 0, "error": 0, "total": len(results)}
    for r in results:
        status = r.get("status", "")
        if status == "ok":
            summary["ok"] += 1
        elif status == "not_found":
            summary["not_found"] += 1
        else:
            summary["error"] += 1
    return summary


def _swap_chat(store: "UserStore", msg_id: str, env: dict) -> str:
    with store.chat_lock:
        for msg in store.chat_messages:
            if msg.get("id") != msg_id:
                continue
            msg["v"] = int(env.get("v", 1))
            msg["body_ct"] = env["body_ct"]
            msg["nonce"] = env["nonce"]
            msg["K_user"] = env["K_user"]
            if env.get("K_enclave"):
                msg["K_enclave"] = env["K_enclave"]
            else:
                msg.pop("K_enclave", None)
            msg["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
            msg["visibility"] = env["visibility"]
            msg["owner_user_id"] = env["owner_user_id"]
            # Full-row replace: the K_enclave key may have been removed, which a
            # JSONB shallow-merge can't express, so we overwrite the whole doc.
            db.chat_append(store.user_id, msg_id, msg["ts"], msg, MAX_CHAT_MESSAGES)
            return "ok"
    return "not_found"


def _swap_memory_inplace(moments: list, mom_id: str, env: dict) -> str:
    for m in moments:
        if m.get("id") != mom_id:
            continue
        m["v"] = int(env.get("v", 1))
        m["body_ct"] = env["body_ct"]
        m["nonce"] = env["nonce"]
        m["K_user"] = env["K_user"]
        if env.get("K_enclave"):
            m["K_enclave"] = env["K_enclave"]
        else:
            m.pop("K_enclave", None)
        m["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
        m["visibility"] = env["visibility"]
        m["owner_user_id"] = env["owner_user_id"]
        return "ok"
    return "not_found"


def _rewrap_bucket() -> dict:
    return {"checked": 0, "rewrapped": 0, "skipped": 0, "errors": 0}


def _rewrap_summary() -> dict:
    return {
        "identity": _rewrap_bucket(),
        "memory": _rewrap_bucket(),
        "chat": _rewrap_bucket(),
        "total_checked": 0,
        "total_rewrapped": 0,
        "total_skipped": 0,
        "total_errors": 0,
    }


def _rewrap_record_result(
    summary: dict,
    kind: str,
    item_id: str,
    status: str,
    *,
    reason: str = "",
) -> dict:
    bucket = summary[kind]
    bucket["checked"] += 1
    summary["total_checked"] += 1
    if status == "rewrapped":
        bucket["rewrapped"] += 1
        summary["total_rewrapped"] += 1
    elif status == "error":
        bucket["errors"] += 1
        summary["total_errors"] += 1
    else:
        bucket["skipped"] += 1
        summary["total_skipped"] += 1
    result = {"type": kind, "id": item_id, "status": status}
    if reason:
        result["reason"] = reason[:240]
    return result


def _enclave_content_public_key_material() -> tuple[bytes | None, str, str]:
    enclave_info = _get_enclave_info()
    if not enclave_info:
        return None, "", "enclave_info_unavailable"
    raw_hex = str(enclave_info.get("content_pk_hex") or "")
    try:
        enclave_pk = bytes.fromhex(raw_hex)
    except Exception:
        return None, "", "enclave_content_public_key_invalid_hex"
    if len(enclave_pk) != 32:
        return None, "", "enclave_content_public_key_invalid_length"
    return enclave_pk, _content_public_key_fingerprint(enclave_pk), ""


def _apply_envelope_fields(record: dict, env: dict) -> None:
    if not record.get("id") and env.get("id"):
        record["id"] = env["id"]
    record["v"] = int(env.get("v", 1))
    record["body_ct"] = env["body_ct"]
    record["nonce"] = env["nonce"]
    record["K_user"] = env["K_user"]
    if env.get("K_enclave"):
        record["K_enclave"] = env["K_enclave"]
    else:
        record.pop("K_enclave", None)
    record["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
    record["visibility"] = env["visibility"]
    record["owner_user_id"] = env["owner_user_id"]


def _build_rewrapped_envelope(
    store: UserStore,
    record: dict,
    *,
    api_key: str | None,
    user_pk: bytes,
    enclave_pk: bytes,
    kind: str,
) -> tuple[dict | None, str, str]:
    item_id = str(record.get("id") or "")
    if not _has_encrypted_content_record(record):
        return None, "skipped_unencrypted", ""
    if str(record.get("visibility") or "shared") != "shared":
        return None, "skipped_local_only", ""
    if not record.get("K_enclave"):
        return None, "skipped_missing_enclave_key", ""
    try:
        plaintext = _decrypt_envelope_via_enclave(
            record,
            api_key,
            purpose=f"content_rewrap:{kind}:{item_id or 'unknown'}",
        )
    except Exception as e:
        return None, "error", f"decrypt_failed:{type(e).__name__}:{str(e)}"
    try:
        env = build_envelope(
            plaintext=plaintext,
            owner_user_id=store.user_id,
            user_pk_bytes=user_pk,
            enclave_pk_bytes=enclave_pk,
            visibility="shared",
            item_id=item_id or None,
        )
        return env, "rewrapped", ""
    except Exception as e:
        return None, "error", f"envelope_build_failed:{type(e).__name__}:{str(e)}"


@app.route("/v1/content/swap", methods=["POST"])
def content_swap():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    if not items:
        return jsonify({"results": [], "summary": _swap_summary([])})

    results: list[dict] = []
    memory_dirty = False
    moments = None

    for item in items:
        if not isinstance(item, dict):
            results.append({"type": None, "id": None, "status": "error: item must be a dict"})
            continue
        itype = item.get("type")
        iid = (item.get("id") or "").strip()
        env = item.get("envelope")
        if itype not in ("chat", "memory"):
            results.append({"type": itype, "id": iid, "status": "error: unsupported type (chat, memory only)"})
            continue
        if not iid:
            results.append({"type": itype, "id": None, "status": "error: id required"})
            continue
        missing = _swap_envelope_missing(env)
        if missing:
            results.append({"type": itype, "id": iid, "status": f"error: envelope missing {missing}"})
            continue
        if env["visibility"] not in ("shared", "local_only"):
            results.append({"type": itype, "id": iid, "status": "error: envelope.visibility must be 'shared' or 'local_only'"})
            continue
        if env["visibility"] == "shared" and not env.get("K_enclave"):
            results.append({"type": itype, "id": iid, "status": "error: shared visibility requires K_enclave"})
            continue
        if env["owner_user_id"] != store.user_id:
            results.append({"type": itype, "id": iid, "status": "error: owner_user_id does not match caller"})
            continue

        if itype == "chat":
            # _swap_chat persists the matched message to the DB itself.
            status = _swap_chat(store, iid, env)
            results.append({"type": "chat", "id": iid, "status": status})
        else:
            if moments is None:
                moments = _load_moments(store)
            status = _swap_memory_inplace(moments, iid, env)
            if status == "ok":
                memory_dirty = True
            results.append({"type": "memory", "id": iid, "status": status})

    if memory_dirty and moments is not None:
        _save_moments(store, moments)

    return jsonify({"results": results, "summary": _swap_summary(results)})


@app.route("/v1/content/rewrap-to-current-key", methods=["POST"])
def content_rewrap_to_current_key():
    """Rewrap chat/memory/identity envelopes to the caller's current key.

    Recovery path for key drift: the enclave decrypts existing shared envelopes
    via K_enclave, then the backend re-encrypts the same plaintext to the
    public_key supplied by the authenticated iOS client. The user record's
    public_key is updated only after every eligible item has been verified.
    """
    store = require_user()
    api_key = _extract_api_key()
    payload = request.get_json(silent=True) or {}
    dry_raw = payload.get("dry_run", False)
    dry_run = dry_raw is True or (isinstance(dry_raw, str) and dry_raw.lower() in {"1", "true", "yes", "on"})
    requested_public_key = (payload.get("public_key") or _get_user_public_key(store.user_id) or "").strip()
    user_pk, err = _decode_content_public_key(requested_public_key)
    if err or user_pk is None:
        return jsonify({"error": err or "public_key invalid"}), 400

    enclave_pk, enclave_fpr, enclave_err = _enclave_content_public_key_material()
    if enclave_err or enclave_pk is None:
        return jsonify({"error": enclave_err or "enclave_info_unavailable"}), 503

    summary = _rewrap_summary()
    results: list[dict] = []
    identity_plan: dict | None = None
    memory_plans: list[tuple[int, dict]] = []
    chat_plans: list[tuple[str, dict]] = []

    identity = _load_identity(store)
    if identity is not None:
        env, status, reason = _build_rewrapped_envelope(
            store,
            identity,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind="identity",
        )
        item_id = str(identity.get("id") or "identity")
        results.append(_rewrap_record_result(summary, "identity", item_id, status, reason=reason))
        if env is not None:
            identity_plan = env

    moments = _load_moments(store)
    for idx, moment in enumerate(moments):
        if not isinstance(moment, dict):
            continue
        env, status, reason = _build_rewrapped_envelope(
            store,
            moment,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind="memory",
        )
        item_id = str(moment.get("id") or "")
        results.append(_rewrap_record_result(summary, "memory", item_id, status, reason=reason))
        if env is not None:
            memory_plans.append((idx, env))

    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    for msg in chat_msgs:
        if not isinstance(msg, dict):
            continue
        env, status, reason = _build_rewrapped_envelope(
            store,
            msg,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind="chat",
        )
        item_id = str(msg.get("id") or "")
        results.append(_rewrap_record_result(summary, "chat", item_id, status, reason=reason))
        if env is not None:
            chat_plans.append((item_id, env))

    response = {
        "status": "dry_run" if dry_run else "ok",
        "dry_run": dry_run,
        "user_id": store.user_id,
        "public_key_fpr": _content_public_key_fingerprint(user_pk),
        "enclave_pk_fpr": enclave_fpr,
        "summary": summary,
        "results": results,
    }

    if summary["total_errors"] > 0:
        response["status"] = "failed"
        response["error"] = "rewrap_failed"
        code = 200 if dry_run else 409
        print(f"[content-rewrap:{store.user_id}] failed errors={summary['total_errors']} dry_run={dry_run}")
        return jsonify(response), code

    if dry_run:
        print(f"[content-rewrap:{store.user_id}] dry_run rewrappable={summary['total_rewrapped']} skipped={summary['total_skipped']}")
        return jsonify(response)

    now = datetime.now().isoformat()
    if identity is not None and identity_plan is not None:
        new_identity = dict(identity)
        _apply_envelope_fields(new_identity, identity_plan)
        new_identity["rewrapped_at"] = now
        _save_identity(store, new_identity)
        _append_identity_change(store, {
            "action": "rewrap",
            "reason": "Identity envelope rewrapped to the current iOS content key.",
        })

    if memory_plans:
        for idx, env in memory_plans:
            if 0 <= idx < len(moments) and isinstance(moments[idx], dict):
                _apply_envelope_fields(moments[idx], env)
                moments[idx]["rewrapped_at"] = now
        _save_moments(store, moments)

    swapped_ids: set[str] = set()
    for item_id, env in chat_plans:
        # _swap_chat persists the swapped envelope fields to the DB itself.
        if _swap_chat(store, item_id, env) == "ok":
            swapped_ids.add(item_id)
    if swapped_ids:
        # Stamp rewrapped_at on the affected in-memory messages and persist the
        # full updated rows (chat is row-per-message in PostgreSQL now).
        with store.chat_lock:
            for msg in store.chat_messages:
                if isinstance(msg, dict) and msg.get("id") in swapped_ids:
                    msg["rewrapped_at"] = now
                    db.chat_append(store.user_id, msg["id"], msg["ts"], msg, MAX_CHAT_MESSAGES)

    if not _set_user_public_key(store.user_id, requested_public_key):
        return jsonify({"error": "user not found"}), 404

    print(f"[content-rewrap:{store.user_id}] ok rewrapped={summary['total_rewrapped']} skipped={summary['total_skipped']} fpr={response['public_key_fpr']}")
    return jsonify(response)


# ---------------------------------------------------------------------------
# Phase B — user-initiated data export + account reset.
#
# These power the "Export my data" + "Delete my data" + "Reset & re-import"
# rows in the new Settings → Privacy page. Both are user-initiated, both
# are auth-gated, and the reset path requires an explicit confirmation
# token in the body to prevent accidental wipes from a buggy client that
# holds the api_key but misbehaves.
# ---------------------------------------------------------------------------


# Cap single-shot export response size. With frames bounded to MAX_FRAMES
# (200) and each body_ct at ~200 KiB, worst-case frame payload is ~40 MiB —
# so the 80 MiB ceiling covers frames + chat + memory + identity with
# headroom. If this ever trips, switch to a streaming multipart response.
_EXPORT_MAX_BYTES = 80 * 1024 * 1024  # 80 MiB


@app.route("/v1/content/export", methods=["GET"])
def content_export():
    """Return the caller's chat, memory, identity, and frames as one JSON blob.

    Ciphertext is returned verbatim — iOS decrypts client-side using
    the user's content_sk from Keychain. No decryption happens server-
    side, so there is no additional trust boundary crossed by this
    endpoint beyond the existing auth check.

    Frames are included as v1 envelopes (same shape as chat/memory) with
    their stored body_ct inline, so the user can walk away with the full
    screen-recording dataset decryptable only on their devices.
    """
    store = require_user()
    hist = store.chat_messages
    moments = _load_moments(store)
    identity = _load_identity(store)

    # Inline each frame's stored envelope. frames_meta is the index; the
    # ciphertext lives in its frame_envelopes row. A missing row just means the
    # frame was evicted mid-read — skip it rather than 500.
    frames_out: list[dict] = []
    with store.frames_lock:
        frame_index = [f.copy() for f in store.frames_meta]
    for meta in frame_index:
        fid = meta.get("id")
        envelope = db.frame_get(store.user_id, fid) if fid else None
        if not isinstance(envelope, dict):
            continue
        frames_out.append({
            "id": fid,
            "ts": meta.get("ts"),
            "w": meta.get("w", 0),
            "h": meta.get("h", 0),
            "envelope": envelope,
        })

    exported_at = datetime.now().isoformat()
    enclave_info = _get_enclave_info() or {}

    export = {
        "schema_version": 2,
        "user_id": store.user_id,
        "exported_at": exported_at,
        "attestation_snapshot": {
            "enclave_content_public_key_hex": enclave_info.get("content_pk_hex", ""),
            "compose_hash": enclave_info.get("compose_hash", ""),
        },
        "chat": hist,
        "memory": moments,
        "identity": identity,
        "frames": frames_out,
        "notes": (
            "Ciphertext included verbatim; decrypt client-side using your"
            " content private key (iCloud Keychain). The attestation_snapshot"
            " records which enclave version was live at export time so you"
            " can verify origin later. Frames are v1 envelopes — their JPEG"
            " + OCR live inside body_ct."
        ),
    }

    body = json.dumps(export, ensure_ascii=False, indent=2)
    if len(body.encode("utf-8")) > _EXPORT_MAX_BYTES:
        return jsonify({
            "error": "export_too_large",
            "detail": "One-shot export exceeds the 80 MiB budget. Streaming"
                      " export is planned (TODO). Contact support / open an issue."
        }), 413

    resp = Response(body, mimetype="application/json")
    # Suggest a filename when clients save to disk.
    safe_name = f"feedling-export-{store.user_id}-{exported_at.replace(':', '').split('.')[0]}.json"
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return resp


@app.route("/v1/account/reset", methods=["POST"])
def account_reset():
    """Hard-delete the caller's account: wipe the user dir, revoke the
    api_key, remove the user record.

    Requires an explicit confirmation token in the body to prevent
    accidental wipes from a buggy client that holds the api_key but
    sends the wrong request. Two steps of intent (correct key + correct
    confirmation body) are needed.

    Idempotent in the safe-to-retry sense: a second call with the same
    api_key fails auth (user no longer exists) and returns 401. So
    retries are harmless; spurious wipes require a fresh registration.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    confirm = (payload.get("confirm") or "").strip()
    if confirm != "delete-all-data":
        return jsonify({
            "error": "confirmation_required",
            "detail": "POST body must include {\"confirm\": \"delete-all-data\"}."
                      " This prevents accidental resets from misbehaving clients."
        }), 400

    user_id = store.user_id

    # Remove the user record FIRST so any in-flight requests carrying the old
    # api_key fail auth immediately.
    with _users_lock:
        before = len(_users)
        _users[:] = [u for u in _users if u.get("user_id") != user_id]
        removed = before - len(_users)
        # Evict all cached (hash → user_id) entries pointing at this user.
        to_evict = [h for h, uid in _key_to_user.items() if uid == user_id]
        for h in to_evict:
            _key_to_user.pop(h, None)
        db.delete_user(user_id)

    # Then hard-delete all of the user's data rows (chat / memory / frames /
    # logs / blobs) and evict the cached in-memory store.
    db.delete_user_data(user_id)
    with _stores_lock:
        _stores.pop(user_id, None)

    # Best-effort cleanup of any residual on-disk dir (pre-migration leftovers).
    try:
        import shutil
        if (
            store.dir.exists()
            and store.dir != FEEDLING_DIR
            and store.dir.parent == FEEDLING_DIR
        ):
            shutil.rmtree(store.dir)
    except Exception as e:
        print(f"[reset:{user_id}] residual dir cleanup failed: {e}")

    print(f"[reset:{user_id}] deleted (user_record={removed})")
    return jsonify({"deleted": True, "user_id": user_id})


@app.route("/healthz", methods=["GET"])
def healthz():
    """Liveness + readiness probe. Public, no auth — used by Docker/compose."""
    return jsonify({"ok": True, "mode": "multi_tenant"})


# Hosted Model API proactive heartbeat. Single gunicorn worker (see the
# UserStore cache comment) so exactly one scheduler runs. Set
# FEEDLING_HOSTED_TICK_ENABLED=0 to disable (tests / one-off scripts).
if os.environ.get("FEEDLING_HOSTED_TICK_ENABLED", "1") == "1":
    threading.Thread(target=_hosted_tick_loop, daemon=True, name="hosted-tick").start()


@app.errorhandler(401)
def _unauthorized(e):
    return jsonify({"error": "unauthorized"}), 401


@app.errorhandler(403)
def _forbidden(e):
    return jsonify({"error": "forbidden"}), 403


@app.errorhandler(503)
def _unavailable(e):
    return jsonify({"error": "service_unavailable", "detail": "admin token is not configured"}), 503


if __name__ == "__main__":
    # PORT is read so isolation/load tests can spin up a hermetic backend on
    # a random free port without colliding with a developer's local dev
    # server on 5001 (or with another test running in parallel). Production
    # deploys can leave it unset — the 5001 default matches the published
    # compose/Dockerfile contract.
    port = int(os.environ.get("FEEDLING_PORT", os.environ.get("PORT", "5001")))
    # Production and all containers run gunicorn (see deploy/Dockerfile CMD
    # and the compose files): the Werkzeug dev server below is single-process
    # and stalls on large responses under the TDX CVM. Keep this path for
    # local dev and the hermetic test harness ONLY — never for real traffic.
    print(f"Feedling DEV server running at http://0.0.0.0:{port} (mode=multi-tenant, auth=api-key; prod uses gunicorn)")
    app.run(host="0.0.0.0", port=port, debug=False)
