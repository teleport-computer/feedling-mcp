"""Per-user state store (write-through cache over PostgreSQL).

The module-level ``_stores`` dict is this worker's cache. Under ``-w N`` each
worker has its own; writes persist immediately (write-through) and the
cross-worker wake bus (``core/wake_bus.py``) refreshes the other workers' cached
store in place via ``_evict_store`` when a genuine write fires a NOTIFY. Object
identity of ``_stores`` and ``UserStore`` instances matters: tests and the
eviction path mutate them in place — never rebind them.
"""

import os
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db
from core import config
from core import wake_bus

MAX_FRAMES = 200
# Per-process hot chat window per user. The PostgreSQL ``chat_messages`` table
# is the immutable encrypted source transcript and is never trimmed by this
# value. History pages and single-message body reads go directly to bounded DB
# APIs, while resident polling/cache scans keep only this newest working set.
MAX_CHAT_MESSAGES = 5000
PUSH_COOLDOWN_SECONDS = int(os.environ.get("FEEDLING_PUSH_COOLDOWN_SEC", 300))
LIVE_ACTIVITY_DEDUPE_SEC = int(os.environ.get("FEEDLING_LIVE_ACTIVITY_DEDUPE_SEC", 900))
LIVE_ACTIVITY_START_COOLDOWN_SEC = int(os.environ.get("FEEDLING_LIVE_ACTIVITY_START_COOLDOWN_SEC", 1800))
DEVICE_EVENT_RETENTION_DAYS = int(os.environ.get("FEEDLING_DEVICE_EVENT_RETENTION_DAYS", 30))
TRACK_EVENT_RETENTION_DAYS = int(os.environ.get("FEEDLING_TRACK_EVENT_RETENTION_DAYS", 90))
TRACK_EVENT_MAX = int(os.environ.get("FEEDLING_TRACK_EVENT_MAX", 2000))
PROACTIVE_JOB_MAX = int(os.environ.get("FEEDLING_PROACTIVE_JOB_MAX", 500))
# Proactive gate audit trails: one append per gate evaluation (high frequency,
# background-paced). Kept above the dashboard read caps so debug views stay full.
GATE_DECISION_MAX = int(os.environ.get("FEEDLING_GATE_DECISION_MAX", 2000))
GATE_REVIEW_MAX = int(os.environ.get("FEEDLING_GATE_REVIEW_MAX", 1000))
PROACTIVE_USER_STATES = {"default", "focused", "social", "resting", "away"}
PROACTIVE_AI_STATES = {"present", "watching", "thinking", "curious", "waiting"}
PROACTIVE_BROADCAST_STATES = {"unknown", "on", "off", "paused"}
# Web-search toggle. Blob-backed like proactive_settings, so no migration.
WEB_SETTINGS_BLOB = "web_settings"
PROACTIVE_DEFAULT_TIMEZONE = os.environ.get("FEEDLING_DEFAULT_TIMEZONE", "Asia/Shanghai").strip() or "UTC"
PROACTIVE_WAKE_INTERVAL_DEFAULT_SEC = 7200
PROACTIVE_WAKE_INTERVAL_MIN_SEC = 900
PROACTIVE_WAKE_INTERVAL_MAX_SEC = 43200
HEARTBEAT_NEXT_TICK_AT_KEY = "heartbeat_next_tick_at"
_HEARTBEAT_NEXT_TICK_CAS_ATTEMPTS = 4

# Per-thread "currently loading from the DB" flag. The blob-backed loaders
# (_load_tokens / _load_frames_meta) re-persist normalized state on read, so a
# reload triggered by a cross-worker NOTIFY would itself write + re-broadcast →
# a NOTIFY storm across workers. While this flag is set on the loading thread,
# _broadcast_store_change suppresses the wake; genuine writes (on other threads /
# outside a load) still broadcast. Thread-local so a load on one thread can't
# mute a concurrent genuine write on another.
_reload_guard = threading.local()


def normalize_proactive_wake_interval_sec(value) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return PROACTIVE_WAKE_INTERVAL_DEFAULT_SEC
    return max(PROACTIVE_WAKE_INTERVAL_MIN_SEC, min(PROACTIVE_WAKE_INTERVAL_MAX_SEC, interval))


def proactive_heartbeat_next_tick_at(settings: dict | None) -> float:
    try:
        return max(0.0, float((settings or {}).get(HEARTBEAT_NEXT_TICK_AT_KEY) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _cas_heartbeat_next_tick_at(user_id: str, transform) -> float:
    """CAS one scheduler field without clobbering concurrent settings writes."""
    for _attempt in range(_HEARTBEAT_NEXT_TICK_CAS_ATTEMPTS):
        raw = db.get_blob(user_id, "proactive_settings")
        expected = dict(raw) if isinstance(raw, dict) else {}
        current = proactive_heartbeat_next_tick_at(expected)
        updated = max(0.0, float(transform(current)))
        if updated == current:
            return current
        replacement = dict(expected)
        replacement[HEARTBEAT_NEXT_TICK_AT_KEY] = updated
        if db.set_blob_if_unchanged(
            user_id,
            "proactive_settings",
            expected,
            replacement,
            insert_if_missing=not isinstance(raw, dict),
        ):
            return updated
    return proactive_heartbeat_next_tick_at(db.get_blob(user_id, "proactive_settings"))


def advance_proactive_heartbeat_tick(
    user_id: str,
    *,
    now: float,
    wake_interval_sec,
) -> float:
    target = float(now) + normalize_proactive_wake_interval_sec(wake_interval_sec)
    return _cas_heartbeat_next_tick_at(user_id, lambda current: max(current, target))


def shrink_proactive_heartbeat_tick(
    user_id: str,
    *,
    now: float,
    wake_interval_sec,
) -> float:
    target = float(now) + normalize_proactive_wake_interval_sec(wake_interval_sec)
    return _cas_heartbeat_next_tick_at(
        user_id,
        lambda current: min(current, target) if current > 0 else current,
    )


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
        # Reentrant: memory mutations hold this across load→mutate→save (see
        # memory_service.mutate) so same-user concurrent writes can't lost-update,
        # and _save_moments re-acquires it inside that hold. RLock makes the
        # nested acquire a no-op instead of a deadlock. Plain Lock previously
        # only guarded the final write, which under the ASGI threadpool (same-user
        # requests now truly overlap) let a stale-snapshot save delete a
        # concurrently-added moment.
        self.memory_lock = threading.RLock()
        self.world_books: list[dict] = []
        self.world_books_lock = threading.Lock()
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

        # load persistent state (write-on-read normalization must not broadcast)
        _prev_guard = getattr(_reload_guard, "active", False)
        _reload_guard.active = True
        try:
            self._load_tokens()
            self._load_push_state()
            self._load_live_activity_state()
            self._load_chat()
            self._load_frames_meta()
            self._load_world_books()
        finally:
            _reload_guard.active = _prev_guard

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
        self._broadcast_store_change("frames")

    # ------- tokens -------
    def _load_tokens(self):
        data = db.get_blob(self.user_id, "tokens")
        self.tokens = data if isinstance(data, list) else []
        self.tokens[:] = [_normalize_token_entry(t) for t in self.tokens]
        self._save_tokens()

    def _save_tokens(self):
        db.set_blob(self.user_id, "tokens", self.tokens)
        self._broadcast_store_change("blob")

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
        self._broadcast_store_change("blob")  # other workers' push cooldown must see this

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
        self._broadcast_store_change("blob")

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
        self.chat_messages = db.chat_load_recent(
            self.user_id, MAX_CHAT_MESSAGES)

    def reload_chat_strict(self) -> list[dict]:
        """Refresh chat state without converting a DB failure into emptiness."""
        with self.chat_lock:
            rows = db.chat_load_recent_strict(
                self.user_id, MAX_CHAT_MESSAGES)
            self.chat_messages = rows
            return list(rows)

    def reload(self):
        """Re-read this store's cached state from PostgreSQL IN PLACE, keeping
        the same object identity (and the same waiter lists). Used by the cache
        TTL / admin eviction so out-of-band DB writes surface without a swap.

        Each collection is reassigned under its own lock. chat_load + a
        concurrent append() are both serialized on chat_lock, so no append is
        lost: either reload reads it from the DB, or append re-adds it to the
        freshly-loaded list.

        Guarded so the loaders' write-on-read normalization doesn't re-broadcast
        a blob/frames wake (this reload is often itself the result of one)."""
        _prev_guard = getattr(_reload_guard, "active", False)
        _reload_guard.active = True
        try:
            with self.chat_lock:
                self.chat_messages = db.chat_load_recent(
                    self.user_id, MAX_CHAT_MESSAGES)
            with self.frames_lock:
                self._load_frames_meta()
            with self.world_books_lock:
                self._load_world_books()
            self._load_tokens()
            self._load_live_activity_state()
            self._load_push_state()
        finally:
            _reload_guard.active = _prev_guard

    def _broadcast_store_change(self, channel: str) -> None:
        """Tell other workers to refresh this user's cached blob-backed state
        (``tokens`` / ``push_state`` / ``live_activity_state`` / ``frames_meta``)
        so -w N can't serve a stale copy until the 15-min TTL. Suppressed while
        this thread is loading from the DB — see ``_reload_guard``."""
        if getattr(_reload_guard, "active", False):
            return
        wake_bus.notify(channel, self.user_id)

    def _build_chat_message(
        self,
        role: str,
        source: str,
        envelope: dict,
        content_type: str = "text",
        extra: dict | None = None,
    ) -> dict:
        """Build the stored form of a v1 ciphertext chat message.

        The client supplies the envelope's `id`, which becomes the stored
        message id so the AEAD additional-data the client baked in
        (owner||v||id) stays verifiable by the enclave on read-back.

        `content_type` is plaintext metadata: "text" (default), "image", or
        "file". Used by clients/enclave to render the decrypted bytes
        correctly — the envelope itself only carries opaque bytes; the type
        tag tells the renderer to show a string, decode JPEG, or offer a
        file download (with `file_name`/`file_mime`/`file_byte_count` extras,
        see below).
        """
        msg_id = envelope.get("id") if isinstance(envelope.get("id"), str) and envelope["id"] else uuid.uuid4().hex
        ct = content_type if content_type in ("text", "image", "file") else "text"

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
            # Seal-time label of the user pk K_user was wrapped to; rewrap's
            # skip logic reads it off the stored row (empty for old clients).
            "content_pk_fpr": envelope.get("content_pk_fpr", ""),
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
        # Server-authored maintenance prompts must also be readable on the
        # direct /v1/chat/poll fallback path. Unlike user chat, this prompt is
        # operational copy, not private user content.
        if source == "resident_maintenance":
            # Store the server-authored prompt verbatim (no strip) — the poll
            # fallback compares it byte-for-byte against the injected plaintext.
            content = str((extra or {}).get("content") or "")
            if content:
                msg["content"] = content
        if envelope.get("K_enclave") is not None:
            msg["K_enclave"] = envelope["K_enclave"]
        if extra:
            for key in (
                "gate_decision_id",
                "proactive_job_id",
                "notice_kind",
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
                # V2 wake-lane marker: tells an agent-initiated wake reply apart
                # from a reply to the user's own message. Fixed vocabulary, not
                # user content -- see worker._build_encrypted_reply_effect_payload.
                # Values are the V2 lane name: heartbeat/scheduled/manual_wake/
                # screen_watch (`worker._WAKE_LANES`). This is NOT the same
                # vocabulary as V1's `proactive_jobs` log `wake_kind` field
                # (presence/screen/screen_watch/scheduled_wake/background_result,
                # `proactive/gate.py:_proactive_v2_wake_kind`) -- only
                # "screen_watch" overlaps. Do not join across the two as if they
                # were one column.
                "wake_kind",
                # Comma-joined memory ids the user explicitly referenced for
                # this turn (Garden「talk in chat」). Plaintext ids only; the
                # enclave expands them into decrypted memory context on read.
                "quoted_memory_ids",
                "image_mime",
                "file_name",
                "file_mime",
                # Optional client operation UUID. Plaintext routing metadata
                # only: it identifies a logical send retry but carries no
                # message content and is not part of the E2EE envelope.
                "client_msg_id",
                "caption_v",
                "caption_id",
                "caption_body_ct",
                "caption_nonce",
                "caption_K_user",
                "caption_K_enclave",
                "caption_enclave_pk_fpr",
                "caption_content_pk_fpr",
                "caption_visibility",
                "caption_owner_user_id",
                "thinking_v",
                "thinking_id",
                "thinking_body_ct",
                "thinking_nonce",
                "thinking_K_user",
                "thinking_K_enclave",
                "thinking_enclave_pk_fpr",
                "thinking_content_pk_fpr",
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
                # Turn-failure metadata (spec 2026-07-18 §2): carried on the
                # fallback reply doc itself so it survives /v1/chat/history's
                # `since` incremental filter — see chat_core.write_response.
                "turn_failure_error_class",
                "turn_failure_blame",
                "turn_failure_user_text",
                "reply_to_message_id",
            ):
                value = extra.get(key)
                if isinstance(value, str) and value.strip():
                    msg[key] = value.strip()
                elif isinstance(value, bool):
                    msg[key] = value
        return msg

    def append_chat(
        self,
        role: str,
        source: str,
        envelope: dict,
        content_type: str = "text",
        extra: dict | None = None,
        *,
        strict: bool = False,
        enqueue: dict | None = None,
        reply_through_seq: int | None = None,
        resident_runtime_fenced: bool = False,
        resident_reply_to: str | None = None,
        resident_replied_by: str = "",
    ) -> dict:
        """Build the stored form of a v1 ciphertext chat message.

        The client supplies the envelope's `id`, which becomes the stored
        message id so the AEAD additional-data the client baked in
        (owner||v||id) stays verifiable by the enclave on read-back.

        `content_type` is plaintext metadata: "text" (default), "image", or
        "file". Used by clients/enclave to render the decrypted bytes
        correctly — the envelope itself only carries opaque bytes; the type
        tag tells the renderer to show a string, decode JPEG, or offer a
        file download (with `file_name`/`file_mime` extras, see below).

        `enqueue` (v2 send path only, requires `strict=True`): when provided,
        the message INSERT and its V2 job enqueue/coalesce happen in ONE DB
        transaction via `db.chat_append_and_enqueue` (spec A7) instead of
        `db.chat_append_strict` — closing the crash window where the message
        persists but the process dies before the job is queued, orphaning it.
        Shape: `{"lane": str, "reason": str | None, "trace_id": str | None,
        "expected_generation": int | None, "expected_runtime_state": str | None,
        "expected_runtime_mode": str | None}`. The three expected runtime
        values form the send-time ownership CAS. Optional ``client_msg_id`` and
        ``idempotency_window_sec`` preserve logical-send idempotency inside the
        same atomic transaction. `None` (the default) preserves
        today's `chat_append_strict`/`chat_append` behavior byte-for-byte —
        the in-memory cache append, trim, `wake_bus.notify`, and
        Capture bookkeeping still runs after a genuine write. Exact Runtime V2
        sends only refresh that state; their runner-owned scheduler is the sole
        capture producer and therefore never appends a legacy proactive job.

        `reply_through_seq` is the V2 final-reply path. It requires
        `strict=True` and no `enqueue`, and commits the deterministic reply row
        plus the durable reply cursor in one transaction. Replays do not
        rebroadcast or reschedule capture.

        `resident_runtime_fenced` is the resident response path. Every response
        verifies resident ownership under the DB cutover lock; when
        `resident_reply_to` is present, the same transaction also CAS-marks that
        parent answered. It cannot be combined with the V2/strict enqueue paths.
        """
        if reply_through_seq is not None and (not strict or enqueue is not None):
            raise ValueError("reply_through_seq requires strict=True and no enqueue")
        if (resident_runtime_fenced or resident_reply_to is not None) and (
            strict or enqueue is not None or reply_through_seq is not None
        ):
            raise ValueError(
                "resident response cannot combine with strict/enqueue/V2 reply")
        resident_runtime_fenced = bool(
            resident_runtime_fenced or resident_reply_to is not None)
        msg_id = envelope.get("id") if isinstance(envelope.get("id"), str) and envelope["id"] else uuid.uuid4().hex
        ct = content_type if content_type in ("text", "image", "file") else "text"

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
            # Seal-time label of the user pk K_user was wrapped to; rewrap's
            # skip logic reads it off the stored row (empty for old clients).
            "content_pk_fpr": envelope.get("content_pk_fpr", ""),
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
        # Server-authored maintenance prompts must also be readable on the
        # direct /v1/chat/poll fallback path (parity with _build_chat_message).
        # Unlike user chat, this prompt is operational copy, not private content.
        if source == "resident_maintenance":
            # Store the server-authored prompt verbatim (no strip) — the poll
            # fallback compares it byte-for-byte against the injected plaintext.
            content = str((extra or {}).get("content") or "")
            if content:
                msg["content"] = content
        if envelope.get("K_enclave") is not None:
            msg["K_enclave"] = envelope["K_enclave"]
        if extra:
            for key in (
                "gate_decision_id",
                "proactive_job_id",
                "notice_kind",
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
                # Comma-joined memory ids the user explicitly referenced for
                # this turn (Garden「talk in chat」). Plaintext ids only; the
                # enclave expands them into decrypted memory context on read.
                "quoted_memory_ids",
                "image_mime",
                "file_name",
                "file_mime",
                "file_byte_count",
                # Optional client operation UUID. Plaintext routing metadata
                # only: it identifies a logical send retry but carries no
                # message content and is not part of the E2EE envelope.
                "client_msg_id",
                "caption_v",
                "caption_id",
                "caption_body_ct",
                "caption_nonce",
                "caption_K_user",
                "caption_K_enclave",
                "caption_enclave_pk_fpr",
                "caption_content_pk_fpr",
                "caption_visibility",
                "caption_owner_user_id",
                "thinking_v",
                "thinking_id",
                "thinking_body_ct",
                "thinking_nonce",
                "thinking_K_user",
                "thinking_K_enclave",
                "thinking_enclave_pk_fpr",
                "thinking_content_pk_fpr",
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
                "resident_delivery_id",
            ):
                value = extra.get(key)
                if isinstance(value, str) and value.strip():
                    msg[key] = value.strip()
                elif isinstance(value, bool):
                    msg[key] = value
                elif key == "file_byte_count" and isinstance(value, int) and value > 0:
                    msg[key] = value
        if resident_reply_to is not None:
            msg["reply_to_message_id"] = str(resident_reply_to)

        persisted_new = True
        with self.chat_lock:
            # The legacy route is deliberately best-effort for compatibility.
            # V2 terminal replies opt into strict ordering: commit first, then
            # expose the row in the process cache.  A failed DB write therefore
            # cannot leave a phantom reply that makes the job look successful.
            resident_parent_doc = None
            if resident_reply_to is not None:
                (
                    seq,
                    persisted_new,
                    resident_parent_doc,
                    persisted_reply_doc,
                ) = db.chat_append_resident_reply(
                    self.user_id,
                    msg_id,
                    msg["ts"],
                    msg,
                    MAX_CHAT_MESSAGES,
                    parent_msg_id=str(resident_reply_to),
                    replied_by=resident_replied_by,
                )
                msg = dict(persisted_reply_doc)
                msg["seq"] = seq
            elif resident_runtime_fenced:
                seq, persisted_new, persisted_reply_doc = db.chat_append_resident_message(
                    self.user_id,
                    msg_id,
                    msg["ts"],
                    msg,
                    MAX_CHAT_MESSAGES,
                )
                msg = dict(persisted_reply_doc)
                msg["seq"] = seq
            elif strict and reply_through_seq is not None:
                seq, persisted_new = db.chat_append_effect_with_cursor(
                    self.user_id, msg_id, msg["ts"], msg, MAX_CHAT_MESSAGES,
                    int(reply_through_seq),
                )
                msg["seq"] = seq
            elif strict and enqueue is not None:
                # A7: message persist + job enqueue/coalesce in one transaction
                # so a crash between them can never orphan the message.
                _seq, _job_id = db.chat_append_and_enqueue(
                    self.user_id, msg_id, msg["ts"], msg, MAX_CHAT_MESSAGES,
                    enqueue["lane"],
                    reason=enqueue.get("reason"),
                    trace_id=enqueue.get("trace_id"),
                    expected_generation=enqueue.get("expected_generation"),
                    expected_runtime_state=enqueue.get("expected_runtime_state"),
                    expected_runtime_mode=enqueue.get("expected_runtime_mode"),
                    client_msg_id=enqueue.get("client_msg_id"),
                    idempotency_window_sec=enqueue.get(
                        "idempotency_window_sec"
                    ),
                )
                if _job_id is None:
                    winner = db.chat_doc_for_seq(self.user_id, _seq)
                    if not isinstance(winner, dict):
                        raise RuntimeError(
                            "idempotent V2 chat winner disappeared after commit"
                        )
                    msg = dict(winner)
                    persisted_new = False
            elif strict:
                db.chat_append_strict(
                    self.user_id, msg_id, msg["ts"], msg, MAX_CHAT_MESSAGES)
            persisted_msg_id = str(msg.get("id") or msg_id)
            if not any(
                str(existing.get("id") or "") == persisted_msg_id
                for existing in self.chat_messages
            ):
                self.chat_messages.append(msg)
            if len(self.chat_messages) > MAX_CHAT_MESSAGES:
                self.chat_messages[:] = self.chat_messages[-MAX_CHAT_MESSAGES:]
            if resident_parent_doc is not None:
                for existing in self.chat_messages:
                    if str(existing.get("id") or "") == str(resident_reply_to):
                        existing.update(resident_parent_doc)
                        break
            if not strict:
                if not resident_runtime_fenced:
                    db.chat_append(
                        self.user_id, msg_id, msg["ts"], msg, MAX_CHAT_MESSAGES)
        # Cross-worker wake: other workers' pollers for this user park on their
        # own threading.Events, which our notify_chat_waiters can't reach. The
        # local fast path (the caller's notify_chat_waiters) stays; this only
        # broadcasts the genuine write. Emitted here (the sole new-message
        # chokepoint), never from the wake/reload path, so it can't loop.
        if not persisted_new:
            if resident_runtime_fenced:
                replayed = dict(msg)
                replayed["_resident_replayed"] = True
                return replayed
            if strict and enqueue is not None and enqueue.get("client_msg_id"):
                replayed = dict(msg)
                replayed["_client_msg_replayed"] = True
                return replayed
            return msg
        wake_bus.notify("chat", self.user_id)
        try:
            from proactive import capture_scheduler

            scheduler_owned_capture = bool(
                strict
                and (
                    reply_through_seq is not None
                    or (
                        enqueue is not None
                        and str(enqueue.get("expected_runtime_mode") or "")
                        == "db_action_v2"
                    )
                )
            )
            if scheduler_owned_capture:
                capture_scheduler.refresh_capture_state_from_chat(self, now=msg["ts"])
            else:
                capture_scheduler.record_chat_append(self, msg)
        except Exception as e:
            print(f"[{self.user_id}/capture] chat_append coordinator failed: {e}")
        return msg

    def apply_finalized_chat_reply(
        self,
        parent_msg_id: str,
        parent_doc: dict,
        reply_doc: dict,
    ) -> dict:
        """Reconcile an atomic reply winner into this worker's cache.

        PostgreSQL has already committed both rows.  This method is therefore
        deliberately memory-only: it must never call chat_update_metadata (or
        any other persistence helper) for the parent.  It emits the same genuine
        append wake/capture side effects as append_chat, and callers invoke it
        only for the CAS winner.
        """
        cached_reply = dict(reply_doc)
        with self.chat_lock:
            for index, existing in enumerate(self.chat_messages):
                if str(existing.get("id") or "") == parent_msg_id:
                    self.chat_messages[index] = dict(parent_doc)
                    break

            replaced_reply = False
            for index, existing in enumerate(self.chat_messages):
                if str(existing.get("id") or "") == str(cached_reply.get("id") or ""):
                    self.chat_messages[index] = cached_reply
                    replaced_reply = True
                    break
            if not replaced_reply:
                self.chat_messages.append(cached_reply)
            if len(self.chat_messages) > MAX_CHAT_MESSAGES:
                self.chat_messages[:] = self.chat_messages[-MAX_CHAT_MESSAGES:]

        wake_bus.notify("chat", self.user_id)
        try:
            from proactive import capture_scheduler

            capture_scheduler.record_chat_append(self, cached_reply)
        except Exception as e:
            print(f"[{self.user_id}/capture] chat_append coordinator failed: {e}")
        return cached_reply

    def finalize_chat_reply_once(
        self,
        parent_msg_id: str,
        candidate: dict,
        replied_fields: dict,
    ) -> tuple[dict, dict] | None:
        """Persist one reply atomically, then apply winner-only side effects."""
        finalized = db.chat_finalize_reply_once(
            self.user_id,
            parent_msg_id,
            str(candidate.get("id") or ""),
            float(candidate.get("ts") or 0),
            candidate,
            replied_fields,
        )
        if finalized is None:
            return None
        parent_doc, reply_doc = finalized
        db.chat_finalize_reply_post_commit(
            self.user_id, reply_doc, MAX_CHAT_MESSAGES
        )
        cached_reply = self.apply_finalized_chat_reply(
            parent_msg_id, parent_doc, reply_doc
        )
        return parent_doc, cached_reply

    def append_chat_idempotent(
        self,
        role: str,
        source: str,
        envelope: dict,
        *,
        client_msg_id: str,
        window_sec: int,
        content_type: str = "text",
        extra: dict | None = None,
    ) -> tuple[dict, bool]:
        """Append one logical client send, atomically across backend workers.

        Returns ``(winner, inserted)``. A duplicate reconciles the authoritative
        database winner into this worker's cache but deliberately emits none of
        append_chat's wake/capture side effects. Database failures propagate:
        failing closed is required because treating an unavailable lookup as a
        miss could start a duplicate turn.
        """
        metadata = dict(extra or {})
        metadata["client_msg_id"] = client_msg_id
        candidate = self._build_chat_message(
            role, source, envelope, content_type, metadata
        )
        winner, inserted = db.chat_append_idempotent(
            self.user_id,
            str(candidate["id"]),
            float(candidate["ts"]),
            candidate,
            MAX_CHAT_MESSAGES,
            client_msg_id=client_msg_id,
            window_sec=window_sec,
        )

        with self.chat_lock:
            replaced = False
            for index, existing in enumerate(self.chat_messages):
                if str(existing.get("id") or "") == str(winner.get("id") or ""):
                    self.chat_messages[index] = dict(winner)
                    replaced = True
                    break
            if not replaced:
                self.chat_messages.append(dict(winner))
            if len(self.chat_messages) > MAX_CHAT_MESSAGES:
                self.chat_messages[:] = self.chat_messages[-MAX_CHAT_MESSAGES:]

        if not inserted:
            return winner, False

        wake_bus.notify("chat", self.user_id)
        try:
            from proactive import capture_scheduler

            capture_scheduler.record_chat_append(self, winner)
        except Exception as e:
            print(f"[{self.user_id}/capture] chat_append coordinator failed: {e}")
        return winner, True

    # ------- world book -------
    def _load_world_books(self):
        self.world_books = db.world_book_load(self.user_id)

    def upsert_world_book(self, record: dict) -> dict:
        entry_id = str(record.get("id") or "").strip()
        if not entry_id:
            raise ValueError("world book record id is required")
        stored = dict(record)
        stored["id"] = entry_id
        stored.setdefault("owner_user_id", self.user_id)
        stored.setdefault("updated_at", datetime.now().isoformat())
        with self.world_books_lock:
            replaced = False
            for i, existing in enumerate(self.world_books):
                if str(existing.get("id") or "") == entry_id:
                    self.world_books[i] = stored
                    replaced = True
                    break
            if not replaced:
                self.world_books.append(stored)
            db.world_book_upsert(self.user_id, entry_id, str(stored.get("updated_at") or ""), stored)
        return stored

    def delete_world_book(self, entry_id: str) -> bool:
        entry_id = str(entry_id or "").strip()
        if not entry_id:
            return False
        with self.world_books_lock:
            before = len(self.world_books)
            self.world_books[:] = [
                item for item in self.world_books
                if str(item.get("id") or "") != entry_id
            ]
            removed_local = len(self.world_books) != before
            removed_db = db.world_book_delete(self.user_id, entry_id)
        return removed_local or removed_db

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
            # 回合失败冗余持久化（spec 2026-07-18 §2.2）。权威载体是兜底回复消息；
            # 这里是全量 history / 重启后的恢复路径。
            "reply_error_class",
            "reply_blame",
            "reply_user_text",
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
        _fire_async_wake("chat", self.user_id)

    # ------- proactive presence -------
    def load_proactive_settings(self) -> dict:
        default = {
            "version": 2,
            "enabled": True,
            "dnd": False,
            "scheduled": True,
            "timezone": PROACTIVE_DEFAULT_TIMEZONE,
            "permission_states": {},
            "user_state": "default",
            "manual_user_state": "default",
            "ai_state": "present",
            "broadcast_state": "unknown",
            # User-authored proactive directive (D2 power-user): the user's own
            # natural-language "when should you reach out to me" instruction,
            # injected into the wake prompt (see model_api_runtime/wake.py). The
            # agent weighs it when deciding to message or sleep. Empty = no
            # preference.
            "wake_directive": "",
            "wake_interval_sec": PROACTIVE_WAKE_INTERVAL_DEFAULT_SEC,
            HEARTBEAT_NEXT_TICK_AT_KEY: 0.0,
            "dream_enabled": True,
            "capture_enabled": True,
            "screen_watch_enabled": True,
            "photo_wake_enabled": True,
            "arrival_wake_enabled": True,
            "unlock_wake_enabled": True,
            "first_chat_ok_at": "",
            # Durable "agent has done its first proactive self-introduction"
            # marker, INDEPENDENT of the identity card (identity-card-never-gates,
            # 2026-07). A no-card / empty-card user has no `self_introduction`
            # field to write, so the intro's one-shot dedup can't live in the card
            # — it lives here. Set once the introduction job is atomically
            # enqueued by agent_runtime.introduction.
            "introduced_at": "",
            "updated_at": datetime.now().isoformat(),
        }
        try:
            data = db.get_blob(self.user_id, "proactive_settings")
            if isinstance(data, dict):
                merged = dict(default)
                merged.update(data)
                merged["scheduled"] = bool(merged.get("scheduled", True))
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
                merged["wake_interval_sec"] = normalize_proactive_wake_interval_sec(
                    merged.get("wake_interval_sec")
                )
                for key in (
                    "dream_enabled",
                    "capture_enabled",
                    "screen_watch_enabled",
                    "photo_wake_enabled",
                    "arrival_wake_enabled",
                    "unlock_wake_enabled",
                ):
                    merged[key] = bool(merged.get(key, True))
                return merged
        except Exception as e:
            print(f"[{self.user_id}/proactive] settings load failed: {e}")
        return default

    def save_proactive_settings(self, patch: dict) -> dict:
        allowed = {
            "enabled",
            "dnd",
            "ambient",
            "scheduled",
            "reminders_delivery",
            "timezone",
            "permission_states",
            "user_state",
            "manual_user_state",
            "ai_state",
            "broadcast_state",
            "wake_directive",
            "wake_interval_sec",
            "dream_enabled",
            "capture_enabled",
            "screen_watch_enabled",
            "photo_wake_enabled",
            "arrival_wake_enabled",
            "unlock_wake_enabled",
        }
        patch_doc = dict(patch or {})
        if "ambient" in patch_doc:
            patch_doc["enabled"] = patch_doc["ambient"]
        if "reminders_delivery" in patch_doc:
            patch_doc["dnd"] = not bool(patch_doc["reminders_delivery"])
        # ``cur`` supplies defaults only when the row does not exist. Persist
        # just the validated patch: writing this pre-lock snapshot back as a
        # whole document lets an unrelated process restore a stale Capture
        # consent value.
        cur = self.load_proactive_settings()
        update: dict = {}
        for key, value in patch_doc.items():
            if key not in allowed:
                continue
            if key in {
                "enabled",
                "dnd",
                "scheduled",
                "dream_enabled",
                "capture_enabled",
                "screen_watch_enabled",
                "photo_wake_enabled",
                "arrival_wake_enabled",
                "unlock_wake_enabled",
            }:
                update[key] = bool(value)
            elif key == "ambient":
                update["enabled"] = bool(value)
            elif key == "reminders_delivery":
                update["dnd"] = not bool(value)
            elif key == "timezone":
                tz_name = str(value or "").strip()
                try:
                    ZoneInfo(tz_name)
                except ZoneInfoNotFoundError:
                    continue
                update[key] = tz_name
            elif key == "permission_states" and isinstance(value, dict):
                update["permission_states"] = {
                    str(pname): str(pstate)
                    for pname, pstate in value.items()
                }
            elif key in {"user_state", "manual_user_state"}:
                state = str(value or "").strip().lower()
                if state in PROACTIVE_USER_STATES:
                    update[key] = state
            elif key == "ai_state":
                state = str(value or "").strip().lower()
                if state in PROACTIVE_AI_STATES:
                    update[key] = state
            elif key == "broadcast_state":
                state = str(value or "").strip().lower()
                if state in PROACTIVE_BROADCAST_STATES:
                    update[key] = state
            elif key == "wake_directive":
                update[key] = str(value or "").strip()[:1000]
            elif key == "wake_interval_sec":
                try:
                    interval = int(value)
                except (TypeError, ValueError):
                    continue
                update[key] = max(
                    PROACTIVE_WAKE_INTERVAL_MIN_SEC,
                    min(PROACTIVE_WAKE_INTERVAL_MAX_SEC, interval),
                )
        update["version"] = 2
        update["updated_at"] = datetime.now().isoformat()
        with self.proactive_lock:
            persisted = db.patch_proactive_settings_strict(
                self.user_id,
                update,
                seed_doc=cur,
            )
        if "wake_interval_sec" in update:
            persisted[HEARTBEAT_NEXT_TICK_AT_KEY] = shrink_proactive_heartbeat_tick(
                self.user_id,
                now=time.time(),
                wake_interval_sec=update["wake_interval_sec"],
            )
        cur.update(persisted)
        return cur

    # ------- web search -------
    # USER PREFERENCE ONLY. An operator kill switch must never rewrite this —
    # otherwise restoring the feature would force every user to re-enable it by
    # hand. Whether the web tools are actually offered on a given turn is
    # derived (preference + lane + halted flags) in
    # model_api_runtime/v2/web_gate.py, not here.
    #
    # Strict booleans throughout: bool("no") is True, so a coercing
    # implementation would read {"enabled": "no"} as web access switched ON.
    def load_web_settings(self) -> dict:
        """Web-search toggle. Defaults to OFF, with no migration for existing
        users: a missing blob means off."""
        default = {"version": 1, "enabled": False}
        try:
            doc = db.get_blob(self.user_id, WEB_SETTINGS_BLOB)
            if isinstance(doc, dict):
                # Rebuild the contract rather than spreading the stored doc —
                # a historic blob's unknown fields would otherwise leak into
                # every response built from this.
                return {"version": 1, "enabled": doc.get("enabled") is True}
        except Exception as e:
            print(f"[{self.user_id}/web_settings] load failed: {e}")
        return default

    def save_web_settings_strict(self, patch: dict) -> dict:
        """Accepts only a real ``bool`` under ``enabled`` — an allowlist, not a
        denylist. Raises ValueError on any other type; nothing is written.

        Uses ``set_blob_strict`` deliberately. ``db.set_blob`` logs and swallows
        write failures, which for a user-initiated setting would mean the UI
        reports "switched on" while the next turn still reads the old value.
        A failed write has to surface as an error.
        """
        cur = self.load_web_settings()
        if isinstance(patch, dict) and "enabled" in patch:
            if not isinstance(patch["enabled"], bool):
                raise ValueError("enabled must be boolean")
            cur["enabled"] = patch["enabled"]
        db.set_blob_strict(self.user_id, WEB_SETTINGS_BLOB, cur)
        return cur

    def first_chat_ok_at(self) -> str:
        settings = self.load_proactive_settings()
        return str(settings.get("first_chat_ok_at") or "").strip()

    def proactive_activation_ready(self) -> bool:
        return bool(self.first_chat_ok_at())

    def mark_first_chat_ok(self, *, at_iso: str | None = None) -> dict:
        with self.proactive_lock:
            cur = self.load_proactive_settings()
            if str(cur.get("first_chat_ok_at") or "").strip():
                return cur
            updated_at = datetime.now().isoformat()
            persisted = db.patch_proactive_settings_strict(
                self.user_id,
                {
                    "first_chat_ok_at": str(at_iso or updated_at),
                    "version": 2,
                    "updated_at": updated_at,
                },
                seed_doc=cur,
            )
            cur.update(persisted)
        return cur

    def introduced_at(self) -> str:
        settings = self.load_proactive_settings()
        return str(settings.get("introduced_at") or "").strip()

    def introduction_done(self) -> bool:
        """True once the agent's first proactive self-introduction has been
        enqueued for this user. Card-independent (see `introduced_at` default)
        so no-card / empty-card users get exactly one introduction."""
        return bool(self.introduced_at())

    def mark_introduced(self, *, at_iso: str | None = None) -> dict:
        with self.proactive_lock:
            cur = self.load_proactive_settings()
            if str(cur.get("introduced_at") or "").strip():
                return cur
            updated_at = datetime.now().isoformat()
            persisted = db.patch_proactive_settings_strict(
                self.user_id,
                {
                    "introduced_at": str(at_iso or updated_at),
                    "version": 2,
                    "updated_at": updated_at,
                },
                seed_doc=cur,
            )
            cur.update(persisted)
        return cur

    def claim_and_enqueue_introduction(self, job: dict) -> dict | None:
        """Cross-process exactly-once enqueue of the one-shot introduction.

        The durable ``introduced_at`` marker and the proactive job are written
        in ONE PostgreSQL transaction (db.claim_and_enqueue_introduction), so
        two processes that don't share a Python lock (backend workers + the
        standalone runner) cannot both enqueue, and a job-write failure rolls the
        marker back automatically — the marker never persists without a job.

        Returns the ``job`` iff THIS caller won the claim and it persisted, else
        ``None`` (already introduced, lost the race, or a DB failure rolled it
        back). Post-commit side effects (trim + waiter/worker wake) mirror
        ``append_proactive_job``."""
        with self.proactive_lock:
            settings = dict(self.load_proactive_settings())
        at_iso = datetime.now().isoformat()
        settings["introduced_at"] = at_iso
        settings["version"] = 2
        settings["updated_at"] = at_iso
        result = db.claim_and_enqueue_introduction(
            self.user_id, settings, job, at_iso=at_iso,
            ts=self._entry_epoch(job),
            item_key=(str(job.get("job_id") or "") or None),
        )
        if result is None:
            return None
        db.log_trim(self.user_id, "proactive_jobs", PROACTIVE_JOB_MAX)
        self.notify_proactive_job_waiters()
        wake_bus.notify("proactive", self.user_id)
        return job

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
        db.log_trim(self.user_id, "gate_decisions", GATE_DECISION_MAX)
        return decision

    def list_gate_decisions(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return db.log_read(self.user_id, "gate_decisions", limit=limit, since_epoch=since_epoch)

    def append_gate_review(self, review: dict) -> dict:
        db.log_append(self.user_id, "gate_reviews", review, ts=self._entry_epoch(review))
        db.log_trim(self.user_id, "gate_reviews", GATE_REVIEW_MAX)
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
        wake_bus.notify("proactive", self.user_id)  # wake other workers' pollers
        return job

    def append_skipped_proactive_job(self, job: dict) -> dict:
        """Record a gate rejection without waking consumers or trimming pending work."""
        db.log_append(
            self.user_id,
            "proactive_jobs",
            job,
            ts=self._entry_epoch(job),
            item_key=(str(job.get("job_id") or "") or None),
        )
        db.log_trim(
            self.user_id,
            "proactive_jobs",
            PROACTIVE_JOB_MAX,
            only_statuses=["skipped", "failed", "completed", "delivered", "posted"],
        )
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
            "recovered_at",
            "updated_at",
            "chat_message_id",
            "agent_action",
            "agent_action_status",
            "agent_actions",
            "ai_state",
            "broadcast_state",
            "request_broadcast",
            "wake_result",
            "capture_result",
            "dream_result",
            "capture_window",
            "memory_action_status",
            "memory_results",
            "cards_added",
            "cards_merged",
            "cards_superseded",
            "questions",
            "noop_reason",
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
            wake_bus.notify("proactive", self.user_id)  # wake other workers
        return changed

    def notify_proactive_job_waiters(self):
        with self.proactive_job_waiters_lock:
            for ev in self.proactive_job_waiters:
                ev.set()
            self.proactive_job_waiters.clear()
        _fire_async_wake("proactive", self.user_id)


# Registry of per-user stores
# In-process per-user store cache, one per worker process: under gunicorn
# -w N there are N independent copies, and cross-worker consistency relies on
# wake-bus broadcasts driving eviction/reload. A UserStore is a write-through
# cache over PostgreSQL (every mutation persists immediately), so dropping and
# rebuilding from the DB is always safe. The TTL bounds staleness from
# out-of-band DB writes (e.g. admin data surgery / the orphan-account recovery
# tool) so they surface without a backend redeploy; `_evict_store` is the
# targeted, immediate counterpart.
STORE_CACHE_TTL_SECONDS = 900  # 15 min

_stores: dict[str, UserStore] = {}
_stores_lock = threading.Lock()


# Async long-poll wake hook (ASGI-migration plan §9.3 / §19.2). Injected by the
# ASGI lifespan as `runtime.waiters.registry.wake`; None under legacy Flask (the
# threading.Event waiters above are the only waiters then). Called from BOTH the
# same-worker write path (notify_*_waiters, directly — NOT via the self-origin-
# filtered wake bus, which is what closes the §19.2 gap) and the cross-worker
# LISTEN path (_wake_store_waiters). The hook must be thread-safe.
_async_wake_hook = None


def set_async_wake_hook(fn) -> None:
    global _async_wake_hook
    _async_wake_hook = fn


def _fire_async_wake(channel: str, user_id: str) -> None:
    hook = _async_wake_hook
    if hook is None:
        return
    try:
        hook(channel, user_id)
    except Exception:
        pass


def _wake_store_waiters(store: "UserStore") -> None:
    """Release threads parked on a store's long-poll waiters (chat / proactive)
    so they return promptly and re-evaluate against the refreshed state."""
    try:
        store.notify_chat_waiters()  # also fires the async "chat" hook
    except Exception:
        pass
    try:
        with store.proactive_job_waiters_lock:
            for ev in store.proactive_job_waiters:
                ev.set()
    except Exception:
        pass
    # notify_chat_waiters already fired the chat hook; the proactive branch above
    # is inline (not the notify method) so fire the async proactive hook here.
    _fire_async_wake("proactive", store.user_id)


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
