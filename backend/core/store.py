"""Per-user state store (write-through cache over PostgreSQL).

gunicorn runs a single worker, so the module-level ``_stores`` dict is the
one shared cache for the whole backend. Object identity of ``_stores`` and
``UserStore`` instances matters: tests and the eviction path mutate them
in place — never rebind them.
"""

import os
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db
from core import config

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
DEVICE_EVENT_RETENTION_DAYS = int(os.environ.get("FEEDLING_DEVICE_EVENT_RETENTION_DAYS", 30))
TRACK_EVENT_RETENTION_DAYS = int(os.environ.get("FEEDLING_TRACK_EVENT_RETENTION_DAYS", 90))
TRACK_EVENT_MAX = int(os.environ.get("FEEDLING_TRACK_EVENT_MAX", 2000))
PROACTIVE_JOB_MAX = int(os.environ.get("FEEDLING_PROACTIVE_JOB_MAX", 500))
PROACTIVE_USER_STATES = {"default", "focused", "social", "resting", "away"}
PROACTIVE_AI_STATES = {"present", "watching", "thinking", "curious", "waiting"}
PROACTIVE_BROADCAST_STATES = {"unknown", "on", "off", "paused"}
PROACTIVE_DEFAULT_TIMEZONE = os.environ.get("FEEDLING_DEFAULT_TIMEZONE", "Asia/Shanghai").strip() or "UTC"

# Hooks run after UserStore.append_proactive_job persists a job. The assembly
# layer (app.py) registers the hosted wake consumer; when nothing is
# registered (e.g. unit tests constructing UserStore directly without
# importing app) appends behave exactly as before.
on_proactive_job_appended: list = []


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
        self.dir = config.FEEDLING_DIR / user_id

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
        # Post-append hooks: the assembly layer registers the hosted wake
        # consumer here (hosted sits above core, so core must not import it).
        # No-op for resident users (route gate inside the hosted hook).
        for _hook in on_proactive_job_appended:
            _hook(self, job)
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
