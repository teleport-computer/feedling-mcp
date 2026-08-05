"""Memory-capture trigger coordinator.

This module owns only the trigger layer: chat/window bookkeeping and enqueueing
typed capture jobs. It must not run the capture handler and must not consult
proactive reach-out gates.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import db
from psycopg.types.json import Jsonb
from proactive import capture_daily, capture_jobs
from memory import migration as memory_migration

CAPTURE_STATE_KIND = "capture_state"
CAPTURE_LIVE_SOURCES = frozenset({
    "chat",
    "model_api",
    "live_activity",
    "agent_initiated_proactive",
    # The single post-call summary written at voice-call hangup (the call's
    # per-turn rows are deleted then); this row is the call's memory input.
    "voice_call_summary",
})
CAPTURE_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped"})


def _env_float(name: str, default: float, *, lo: float = 0.0, hi: float = 86400.0) -> float:
    try:
        raw = float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        raw = default
    return max(lo, min(hi, raw))


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 1000) -> int:
    try:
        raw = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        raw = default
    return max(lo, min(hi, raw))


def quiet_sec() -> float:
    return _env_float("FEEDLING_CAPTURE_QUIET_SEC", 1200.0, hi=86400.0)


def turn_backstop() -> int:
    return _env_int("FEEDLING_CAPTURE_TURN_BACKSTOP", 24, hi=500)


def min_interval_sec() -> float:
    return _env_float("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", 600.0, hi=86400.0)


def migrate_window_sec() -> float:
    # One legacy-migration batch per user per this window (default 1h). Lower to
    # drain a backlog faster in test / quiet windows.
    return _env_float("FEEDLING_MIGRATE_WINDOW_SEC", 3600.0, lo=60.0, hi=86400.0)


def migrate_reaudit_sec() -> float:
    return _env_float("FEEDLING_MIGRATE_REAUDIT_SEC", memory_migration.DEFAULT_REAUDIT_SEC, hi=2592000.0)


def _now_iso(now: float | None = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _state_doc(raw: Any) -> dict[str, Any]:
    doc = dict(raw) if isinstance(raw, dict) else {}
    seq_initialized = (
        bool(doc.get("capture_seq_initialized"))
        if "capture_seq_initialized" in doc
        else "last_captured_until_seq" in doc
    )
    return {
        "last_captured_until_message_id": str(doc.get("last_captured_until_message_id") or "")[:160],
        "last_captured_until_ts": _safe_float(doc.get("last_captured_until_ts"), 0.0),
        "last_captured_until_seq": max(
            0, int(_safe_float(doc.get("last_captured_until_seq"), 0.0))
        ),
        "capture_seq_initialized": seq_initialized,
        "pending_capture_key": str(doc.get("pending_capture_key") or "")[:240],
        "last_capture_completed_at": _safe_float(doc.get("last_capture_completed_at"), 0.0),
        "last_capture_cards_added_at": _safe_float(
            doc.get("last_capture_cards_added_at"), 0.0
        ),
        "last_capture_cards_added": max(
            0, int(_safe_float(doc.get("last_capture_cards_added"), 0.0))
        ),
        "capture_fail_streak": max(0, int(_safe_float(doc.get("capture_fail_streak"), 0.0))),
        "last_capture_failed_at": _safe_float(doc.get("last_capture_failed_at"), 0.0),
        "migrate_fail_streak": max(0, int(_safe_float(doc.get("migrate_fail_streak"), 0.0))),
        "last_migrate_failed_at": _safe_float(doc.get("last_migrate_failed_at"), 0.0),
        "last_seen_message_id": str(doc.get("last_seen_message_id") or "")[:160],
        "last_seen_ts": _safe_float(doc.get("last_seen_ts"), 0.0),
        "turns_since_capture": max(0, int(_safe_float(doc.get("turns_since_capture"), 0.0))),
        "message_count": max(0, int(_safe_float(doc.get("message_count"), 0.0))),
        "updated_at": str(doc.get("updated_at") or "")[:80],
    }


def load_capture_state(store) -> dict[str, Any]:
    return _state_doc(db.get_blob(store.user_id, CAPTURE_STATE_KIND))


def load_capture_state_strict(store) -> dict[str, Any]:
    """Runner-facing state read: a DB failure must not look like frontier zero."""
    return _state_doc(db.get_blob_strict(store.user_id, CAPTURE_STATE_KIND))


def save_capture_state(store, state: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    doc = _state_doc(state)
    doc["updated_at"] = _now_iso(now)
    db.set_blob(store.user_id, CAPTURE_STATE_KIND, doc)
    return doc


def _patch_capture_state(
    store,
    patch: Mapping[str, Any],
    *,
    now: float | None = None,
    expected_frontier_id: str | None = None,
    source_message_id: str | None = None,
) -> dict[str, Any]:
    """Atomically merge selected fields without clobbering another process.

    Chat appends and runner completion happen in different backend processes.
    Rewriting a previously read whole blob lets a late chat refresh restore an
    old capture frontier. This SQL merge updates only the caller-owned fields;
    completion additionally CAS-fences the frontier it processed.
    """
    normalized = _state_doc(patch)
    update = {
        key: normalized[key]
        for key in patch
        if key in normalized
    }
    update["updated_at"] = _now_iso(now)
    persisted: dict[str, Any]
    wrote = False
    mirrored_under_fence = False
    with db.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                source_id = str(source_message_id or "")
                source_valid = True
                if source_id:
                    # A chat-derived refresh uses the same shared fence as
                    # ordinary writers. If Clear linearized first, the stale
                    # in-process store row no longer exists and must not
                    # recreate capture_state metadata.
                    db._lock_chat_user_fence_on_cursor(cur, store.user_id)
                    cur.execute(
                        "SELECT 1 FROM chat_messages WHERE user_id=%s "
                        "AND msg_id=%s AND doc->>'role' IN ('user','openclaw') "
                        "AND COALESCE(doc->>'source','')=ANY(%s::text[])",
                        (
                            store.user_id,
                            source_id,
                            list(CAPTURE_LIVE_SOURCES),
                        ),
                    )
                    source_valid = cur.fetchone() is not None
                if source_valid:
                    cur.execute(
                        "INSERT INTO user_blobs (user_id,kind,doc) "
                        "VALUES (%s,%s,%s) ON CONFLICT (user_id,kind) DO NOTHING",
                        (store.user_id, CAPTURE_STATE_KIND, Jsonb({})),
                    )
                    sql = (
                        "UPDATE user_blobs SET doc=doc || %s "
                        "WHERE user_id=%s AND kind=%s"
                    )
                    params: list[Any] = [
                        Jsonb(update),
                        store.user_id,
                        CAPTURE_STATE_KIND,
                    ]
                    if expected_frontier_id is not None:
                        sql += (
                            " AND COALESCE(doc->>'last_captured_until_message_id','')=%s"
                        )
                        params.append(str(expected_frontier_id))
                    cur.execute(sql + " RETURNING doc", tuple(params))
                    row = cur.fetchone()
                    wrote = row is not None
                else:
                    row = None
                if row is None:
                    cur.execute(
                        "SELECT doc FROM user_blobs "
                        "WHERE user_id=%s AND kind=%s",
                        (store.user_id, CAPTURE_STATE_KIND),
                    )
                    row = cur.fetchone()
                persisted = _state_doc(row[0] if row is not None else {})
                if wrote and source_id:
                    # Keep the shared Chat Clear fence until the mirror lands.
                    # Clear's primary delete + mirror delete therefore order
                    # after every successful pre-clear refresh; a delayed
                    # postcommit mirror cannot resurrect the row in TEE.
                    db._mirror_persisted_blob(
                        store.user_id, CAPTURE_STATE_KIND, persisted
                    )
                    mirrored_under_fence = True
    if wrote and not mirrored_under_fence:
        db._mirror_persisted_blob(store.user_id, CAPTURE_STATE_KIND, persisted)
    return persisted


def _is_live_capture_message(message: Mapping[str, Any] | None) -> bool:
    if not isinstance(message, Mapping):
        return False
    role = str(message.get("role") or "").strip()
    source = str(message.get("source") or "").strip()
    if role not in {"user", "openclaw"}:
        return False
    return source in CAPTURE_LIVE_SOURCES


def _live_messages_after_capture(store, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    if bool(state.get("capture_seq_initialized")):
        # Runtime V2's raw frontier may end on a synthetic/import row whose ID
        # is intentionally absent from the live-message subset. Seq is the
        # only safe discovery cursor: an out-of-order later live row can carry
        # an older timestamp and must still trigger Capture.
        rows = db.chat_capture_messages_after_seq(
            store.user_id,
            max(0, int(_safe_float(state.get("last_captured_until_seq"), 0.0))),
            # Trigger discovery needs the newest live identity plus a capped
            # turn-count backstop, not the entire uncaptured transcript. The
            # worker independently pages exact oldest batches of 60. Keeping
            # this synchronous append-path read bounded avoids O(backlog) work
            # on every message (especially after a user opts out).
            sources=tuple(CAPTURE_LIVE_SOURCES),
            limit=max(64, min(1000, turn_backstop() * 2)),
        )
        return [dict(row) for row in rows if _is_live_capture_message(row)]
    after_id = str(state.get("last_captured_until_message_id") or "")
    after_ts = _safe_float(state.get("last_captured_until_ts"), 0.0)
    chat_messages = getattr(store, "chat_messages", None)
    if not isinstance(chat_messages, list):
        return []
    chat_lock = getattr(store, "chat_lock", None)
    if chat_lock is not None:
        with chat_lock:
            live = [dict(msg) for msg in chat_messages if _is_live_capture_message(msg)]
    else:
        live = [dict(msg) for msg in chat_messages if _is_live_capture_message(msg)]
    if not after_id and not after_ts:
        return live
    out: list[dict[str, Any]] = []
    found_after_id = False
    for msg in live:
        msg_id = str(msg.get("id") or "")
        msg_ts = _safe_float(msg.get("ts"), 0.0)
        if after_id:
            if found_after_id:
                out.append(msg)
            elif msg_id == after_id:
                found_after_id = True
            elif after_ts and msg_ts > after_ts:
                # If the boundary message was pruned, use the timestamp fallback.
                out.append(msg)
        elif msg_ts > after_ts:
            out.append(msg)
    return out


def refresh_capture_state_from_chat(store, *, now: float | None = None) -> dict[str, Any]:
    state = load_capture_state(store)
    window_messages = _live_messages_after_capture(store, state)
    patch: dict[str, Any] = {}
    if window_messages:
        last = window_messages[-1]
        patch["last_seen_message_id"] = str(last.get("id") or "")[:160]
        patch["last_seen_ts"] = _safe_float(last.get("ts"), 0.0)
        patch["message_count"] = len(window_messages)
        patch["turns_since_capture"] = sum(
            1
            for msg in window_messages
            if str(msg.get("role") or "") == "user"
        )
    else:
        patch["message_count"] = 0
        patch["turns_since_capture"] = 0
    return _patch_capture_state(
        store,
        patch,
        now=now,
        source_message_id=(
            str(window_messages[-1].get("id") or "") if window_messages else None
        ),
    )


def _current_window(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "after_message_id": str(state.get("last_captured_until_message_id") or "")[:160],
        "after_seq": max(
            0, int(_safe_float(state.get("last_captured_until_seq"), 0.0))
        ),
        "until_message_id": str(state.get("last_seen_message_id") or "")[:160],
        "until_ts": _safe_float(state.get("last_seen_ts"), 0.0),
        "message_count": max(0, int(_safe_float(state.get("message_count"), 0.0))),
    }


def capture_key_for_window(window: Mapping[str, Any]) -> str:
    material = "|".join(
        str(window.get(key) or "")
        for key in ("after_message_id", "until_message_id", "until_ts")
    )
    return "capture:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _enqueue_window(
    store,
    *,
    trigger: str,
    now: float | None = None,
    submit: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply every capture gate, then submit to exactly one job substrate.

    ``submit=None`` is the unchanged resident path and therefore consults the
    legacy ``proactive_jobs`` stream for active-job recovery.  A supplied
    submitter is the Runtime V2 path: it must never inspect or wait behind that
    abandoned stream.  V2's ``agent_jobs`` single-flight/coalescing result is
    the authority for whether work is already active.
    """
    now_ts = time.time() if now is None else float(now)
    state = refresh_capture_state_from_chat(store, now=now_ts)
    window = _current_window(state)
    until_id = str(window.get("until_message_id") or "")
    if not until_id or int(window.get("message_count") or 0) <= 0:
        return {"enqueued": False, "reason": "no_new_messages", "state": state, "job": None}
    if until_id == str(state.get("last_captured_until_message_id") or ""):
        return {"enqueued": False, "reason": "already_captured", "state": state, "job": None}
    pending_key = str(state.get("pending_capture_key") or "")
    if pending_key and submit is None:
        if capture_jobs._find_active_capture(store) is not None:
            return {"enqueued": False, "reason": "capture_already_pending", "state": state, "job": None}
        # Stale flag: the job it pointed to is terminal/gone (e.g. a failed capture
        # whose key got re-armed). Self-heal so a stuck user isn't blocked forever,
        # then fall through and re-evaluate this window.
        state["pending_capture_key"] = ""
        state = save_capture_state(store, state, now=now_ts)
    last_completed = _safe_float(state.get("last_capture_completed_at"), 0.0)
    if last_completed and now_ts - last_completed < min_interval_sec():
        return {"enqueued": False, "reason": "min_interval", "state": state, "job": None}
    # 失败退避：min_interval 只看上次成功，对「永远失败的窗口」（坏 BYOK key）
    # 不生效，会退化成每 tick 重试。手动 force（debug 面板）不受限。
    if trigger != "manual_force" and capture_jobs.in_failure_backoff(
        int(state.get("capture_fail_streak") or 0),
        _safe_float(state.get("last_capture_failed_at"), 0.0),
        now_ts,
    ):
        return {"enqueued": False, "reason": "failure_backoff", "state": state, "job": None}

    key = capture_key_for_window(window)
    if submit is None:
        job, enqueued, reason = capture_jobs.enqueue_memory_capture_job(
            store,
            trigger=trigger,
            capture_key=key,
            window=window,
            now=now_ts,
        )
    else:
        submitted = submit(
            store,
            trigger=trigger,
            now=now_ts,
            window=window,
            capture_key=key,
        )
        job = submitted.get("job")
        enqueued = bool(submitted.get("enqueued"))
        reason = submitted.get("reason")
    # Only arm pending for a genuinely in-flight job. Arming it on a terminal
    # (completed/failed) duplicate was the root cause of the permanent
    # capture_already_pending lock — a terminal job never re-fires a status event
    # to clear it.
    if job is not None and (enqueued or capture_jobs._active_capture_job(job)):
        state["pending_capture_key"] = str(job.get("capture_key") or key)[:240]
        if submit is None:
            state = save_capture_state(store, state, now=now_ts)
        # Runtime V2's durable single-flight authority is agent_jobs. Do not
        # create a second, generation-unfenced pending marker that a delayed
        # post-Clear submit callback could resurrect.
    return {"enqueued": bool(enqueued), "reason": reason, "state": state, "job": job}


def record_chat_append(store, message: Mapping[str, Any]) -> dict[str, Any]:
    if not _is_live_capture_message(message):
        return {"enqueued": False, "reason": "ignored_message", "state": load_capture_state(store), "job": None}
    now_ts = _safe_float(message.get("ts"), time.time())
    state = refresh_capture_state_from_chat(store, now=now_ts)
    if str(message.get("role") or "") == "user" and int(state.get("turns_since_capture") or 0) >= turn_backstop():
        return _enqueue_window(store, trigger="turn_backstop", now=now_ts)
    return {"enqueued": False, "reason": "turn_backstop_not_due", "state": state, "job": None}


def is_capture_boundary_event(event: Mapping[str, Any] | None) -> bool:
    if not isinstance(event, Mapping):
        return False
    event_type = str(event.get("type") or "").strip().lower()
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    phase = str(
        payload.get("scene_phase")
        or payload.get("phase")
        or payload.get("app_state")
        or ""
    ).strip().lower()
    if event_type == "app_presence" and phase in {"background", "inactive"}:
        return True
    return event_type in {
        "app_background",
        "screen_lock",
        "explicit_close",
        "session_end",
        "unlock_after_absence",
        "good_night",
    }


def handle_device_event(
    store,
    event: Mapping[str, Any],
    *,
    submit: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not is_capture_boundary_event(event):
        return {"enqueued": False, "reason": "not_capture_boundary", "state": load_capture_state(store), "job": None}
    if not _capture_enabled(store):
        return {
            "enqueued": False,
            "reason": "capture_disabled",
            "state": load_capture_state(store),
            "job": None,
        }
    trigger = str(event.get("type") or "device_boundary").strip().lower() or "device_boundary"
    if trigger == "app_presence":
        trigger = "app_background"
    return _enqueue_window(
        store,
        trigger=trigger,
        now=_safe_float(event.get("ts"), time.time()),
        submit=submit,
    )


def _capture_enabled(store) -> bool:
    try:
        settings = db.get_blob_strict(store.user_id, "proactive_settings")
        if settings is None:
            return True
        if not isinstance(settings, dict):
            return False
        return bool(settings.get("capture_enabled", True))
    except Exception:
        # Capture is background content processing.  A broken consent/settings
        # read must never opt the user in.
        return False


def tick_quiet_capture(
    store, *, now: float | None = None,
    submit: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    state = refresh_capture_state_from_chat(store, now=now_ts)
    if not _capture_enabled(store):
        return {"enqueued": False, "reason": "capture_disabled", "state": state, "job": None}
    until_id = str(state.get("last_seen_message_id") or "")
    if not until_id or int(state.get("message_count") or 0) <= 0:
        return {"enqueued": False, "reason": "no_new_messages", "state": state, "job": None}
    if until_id == str(state.get("last_captured_until_message_id") or ""):
        return {"enqueued": False, "reason": "already_captured", "state": state, "job": None}
    # V2 chat writes only refresh capture state; the runner-owned sweep is the
    # sole producer. Preserve the turn-count backstop with at most one scheduler
    # cadence of delay, while resident/import paths keep their immediate legacy
    # ``record_chat_append`` behavior.
    if submit is not None and int(state.get("turns_since_capture") or 0) >= turn_backstop():
        return _enqueue_window(
            store,
            trigger="turn_backstop",
            now=now_ts,
            submit=submit,
        )
    quiet_for = now_ts - _safe_float(state.get("last_seen_ts"), 0.0)
    if quiet_for < quiet_sec():
        return {"enqueued": False, "reason": "quiet_not_due", "quiet_for_sec": quiet_for, "state": state, "job": None}
    if submit is None:
        result = _enqueue_window(store, trigger="quiet_timeout", now=now_ts)
    else:
        result = _enqueue_window(
            store,
            trigger="quiet_timeout",
            now=now_ts,
            submit=submit,
        )
    result["quiet_for_sec"] = quiet_for
    return result


def force_capture(
    store, *, now: float | None = None,
    submit: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Debug: enqueue a capture job for the current window NOW, skipping the quiet
    window (still needs new messages since the last capture). For the test panel's
    'capture now' button so you don't wait 20 min."""
    now_ts = time.time() if now is None else float(now)
    state = refresh_capture_state_from_chat(store, now=now_ts)
    until_id = str(state.get("last_seen_message_id") or "")
    if not until_id or int(state.get("message_count") or 0) <= 0:
        return {"enqueued": False, "reason": "no_new_messages", "state": state, "job": None}
    if until_id == str(state.get("last_captured_until_message_id") or ""):
        return {"enqueued": False, "reason": "already_captured", "state": state, "job": None}
    if submit is None:
        return _enqueue_window(store, trigger="manual_force", now=now_ts)
    return _enqueue_window(
        store, trigger="manual_force", now=now_ts, submit=submit
    )


def record_v2_capture_status(
    store,
    *,
    status: str,
    window: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply a Runtime V2 capture terminal result to the shared frontier.

    V2 jobs intentionally do not masquerade as legacy ``memory_capture`` rows.
    The runner reports the exact oldest-contiguous batch it actually processed;
    a successful no-card result advances the same frontier as a successful
    memory write, while a provider/write failure only arms exponential backoff.
    """
    status_text = str(status or "").strip().lower()
    if status_text not in CAPTURE_TERMINAL_STATUSES:
        return load_capture_state(store)
    now_ts = time.time() if now is None else float(now)
    state = load_capture_state_strict(store)
    if status_text == "completed":
        processed = window if isinstance(window, Mapping) else {}
        until_id = str(processed.get("until_message_id") or "")[:160]
        until_ts = _safe_float(processed.get("until_ts"), 0.0)
        until_seq = max(0, int(_safe_float(processed.get("through_seq"), 0.0)))
        after_id = str(processed.get("after_message_id") or "")[:160]
        current_id = str(state.get("last_captured_until_message_id") or "")
        # Single-flight normally makes this equality tautological. The fence is
        # still important for delayed/replayed callbacks: never move an already
        # newer capture frontier backwards.
        patch = {
            "pending_capture_key": "",
            "capture_fail_streak": 0,
            "last_capture_failed_at": 0.0,
        }
        if until_id and current_id == after_id:
            patch.update(
                {
                    "last_captured_until_message_id": until_id,
                    "last_captured_until_ts": until_ts,
                    "last_captured_until_seq": until_seq,
                    "capture_seq_initialized": True,
                    "last_capture_completed_at": now_ts,
                }
            )
        state = _patch_capture_state(
            store,
            patch,
            now=now_ts,
            expected_frontier_id=after_id,
        )
    elif status_text == "failed":
        state = _patch_capture_state(
            store,
            {
                "pending_capture_key": "",
                "capture_fail_streak": int(
                    state.get("capture_fail_streak") or 0
                )
                + 1,
                "last_capture_failed_at": now_ts,
            },
            now=now_ts,
        )
    capture_jobs.notify_backoff(
        store,
        lane="capture",
        status=status_text,
        streak=int(state.get("capture_fail_streak") or 0),
    )
    return refresh_capture_state_from_chat(store, now=now_ts)


def tick_quiet_migrate(store, *, now: float | None = None) -> dict[str, Any]:
    """Legacy→v1 migration trigger — rides the same quiet window as capture.

    Enqueues one migration batch job when the user is quiet AND the migration-state
    cache isn't 'done'. Card SHAPE stays the source of truth (handler re-scans), so
    the state blob is only a cheap gate. Single-flight + active-maintenance guard
    live in enqueue_memory_migrate_job, so this never runs alongside capture/dream."""
    if not memory_migration.migration_enabled():
        return {"enqueued": False, "reason": "migration_disabled", "job": None}
    now_ts = time.time() if now is None else float(now)
    state = load_capture_state(store)
    quiet_for = now_ts - _safe_float(state.get("last_seen_ts"), 0.0)
    if quiet_for < quiet_sec():
        return {"enqueued": False, "reason": "quiet_not_due", "quiet_for_sec": quiet_for, "job": None}
    # 失败退避（同 _enqueue_window）：migrate 的每小时新 window key 不挡同 key
    # 重试，坏钥用户会在窗口内每 tick 重建 job。
    if capture_jobs.in_failure_backoff(
        int(state.get("migrate_fail_streak") or 0),
        _safe_float(state.get("last_migrate_failed_at"), 0.0),
        now_ts,
    ):
        return {"enqueued": False, "reason": "failure_backoff", "job": None}
    mig_state = db.get_blob(store.user_id, memory_migration.MIGRATION_STATE_BLOB)
    if not memory_migration.should_enqueue(mig_state):
        # 'done' — but periodically re-scan once (a card may have reverted to old
        # shape via some legacy path); handler re-confirms done if nothing legacy.
        if not memory_migration.reaudit_due(mig_state, now=now_ts, reaudit_sec=migrate_reaudit_sec()):
            return {"enqueued": False, "reason": "migration_done", "job": None}
    # One batch per user per window (single-flight serializes anyway); a new window
    # = a new key = the next batch. Default 1h; FEEDLING_MIGRATE_WINDOW_SEC to tune.
    window_id = str(int(now_ts // max(1.0, migrate_window_sec())))
    migrate_key = memory_migration.migrate_key_for_window(store.user_id, window_id)
    job, enqueued, reason = capture_jobs.enqueue_memory_migrate_job(
        store, trigger="quiet_window_migrate", migrate_key=migrate_key, now=now_ts)
    return {"enqueued": enqueued, "reason": reason, "job": job}


def _capture_trace_job_id(job: Mapping[str, Any]) -> str:
    return str(job.get("job_id") or "")[:120]


def _capture_trace_card_titles(job: Mapping[str, Any]) -> str:
    result = job.get("capture_result") if isinstance(job.get("capture_result"), Mapping) else {}
    titles = result.get("titles") if isinstance(result.get("titles"), list) else []
    if not titles:
        cards = result.get("cards") if isinstance(result.get("cards"), list) else []
        titles = [c.get("title", "") for c in cards if isinstance(c, Mapping)]
    return " | ".join(str(t) for t in titles if t)[:1000]


def record_migrate_job_status(store, job: Mapping[str, Any], *, status: str, now: float | None = None) -> dict[str, Any]:
    """migrate 终态只维护失败退避 streak——window 游标由 handler 自己重扫，
    这里不像 capture 那样推进 last_captured_*。"""
    if not capture_jobs.is_memory_migrate_job(job):
        return load_capture_state(store)
    status_text = str(status or job.get("status") or "").strip().lower()
    now_ts = time.time() if now is None else float(now)
    state = load_capture_state(store)
    if status_text == "completed":
        state["migrate_fail_streak"] = 0
        state["last_migrate_failed_at"] = 0.0
    elif status_text == "failed":
        state["migrate_fail_streak"] = int(state.get("migrate_fail_streak") or 0) + 1
        state["last_migrate_failed_at"] = now_ts
    else:
        return state
    state = save_capture_state(store, state, now=now_ts)
    capture_jobs.notify_backoff(store, lane="migrate", status=status_text,
                                streak=int(state.get("migrate_fail_streak") or 0))
    return state


def record_capture_job_status(store, job: Mapping[str, Any], *, status: str, now: float | None = None) -> dict[str, Any]:
    if not capture_jobs.is_memory_capture_job(job):
        return load_capture_state(store)
    status_text = str(status or job.get("status") or "").strip().lower()
    if status_text not in CAPTURE_TERMINAL_STATUSES:
        return load_capture_state(store)
    now_ts = time.time() if now is None else float(now)
    state = load_capture_state(store)
    try:
        cards_added = max(0, int(job.get("cards_added") or 0))
    except (TypeError, ValueError):
        cards_added = 0
    capture_key = str(job.get("capture_key") or "")
    if capture_key and str(state.get("pending_capture_key") or "") == capture_key:
        state["pending_capture_key"] = ""
    elif not capture_key:
        state["pending_capture_key"] = ""
    if status_text == "completed":
        window = job.get("capture_window") if isinstance(job.get("capture_window"), Mapping) else job.get("window")
        window = window if isinstance(window, Mapping) else {}
        until_id = str(window.get("until_message_id") or "")[:160]
        until_ts = _safe_float(window.get("until_ts"), 0.0)
        if until_id:
            state["last_captured_until_message_id"] = until_id
            state["last_captured_until_ts"] = until_ts
            state["last_capture_completed_at"] = now_ts
        state.update(capture_daily.daily_capture_patch(
            state,
            cards_added=cards_added,
            completed_at=now_ts,
        ))
        state["capture_fail_streak"] = 0
        state["last_capture_failed_at"] = 0.0
    elif status_text == "failed":
        # skipped 是调度器主动暂缓、不算失败；只有真失败累计退避 streak。
        state["capture_fail_streak"] = int(state.get("capture_fail_streak") or 0) + 1
        state["last_capture_failed_at"] = now_ts
    state = save_capture_state(store, state, now=now_ts)
    capture_jobs.notify_backoff(store, lane="capture", status=status_text,
                                streak=int(state.get("capture_fail_streak") or 0))
    result = refresh_capture_state_from_chat(store, now=now_ts)

    import debug_trace  # local import avoids load-order cycle

    job_id = _capture_trace_job_id(job)
    if status_text == "completed":
        titles = _capture_trace_card_titles(job)
        debug_trace.trace_event(
            store,
            subsystem="memory",
            type="memory.capture.done",
            actor="backend",
            job_id=job_id,
            summary=f"captured {cards_added} card(s)",
            explain=(f"记忆抓取完成：新增 {cards_added} 条" if cards_added else "本轮没有可抓取的新记忆（合法）"),
            detail={"cards_added": cards_added},
            content_excerpt={"titles": titles} if titles else None,
        )
    elif status_text == "skipped":
        # Scheduler declined/deferred the job (e.g. throttled / wake-gate) — this is
        # NOT a failure. Keep it distinct from "failed" so the dashboard doesn't
        # render it red (see CAPTURE_RETRYABLE_TERMINAL comment in capture_jobs.py:
        # "failed = error; skipped = abnormal terminal — noop is reported as
        # completed, not skipped").
        reason = str(job.get("status_reason") or job.get("noop_reason") or "").strip()
        detail = {"status": status_text}
        if reason:
            detail["reason"] = reason[:200]
        debug_trace.trace_event(
            store,
            subsystem="memory",
            type="memory.capture.done",
            actor="backend",
            status="ok",
            job_id=job_id,
            summary="capture job skipped",
            explain="记忆抓取跳过：调度器暂缓执行（未失败）",
            detail=detail,
        )
    else:
        reason = str(job.get("status_reason") or job.get("noop_reason") or "").strip()
        debug_trace.trace_event(
            store,
            subsystem="memory",
            type="memory.capture.error",
            actor="backend",
            status="error",
            job_id=job_id,
            summary=f"capture job {status_text}",
            explain="记忆抓取失败",
            detail={"status": status_text, "reason": reason[:200]} if reason else {"status": status_text},
        )
    return result
