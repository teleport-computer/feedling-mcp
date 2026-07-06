"""Memory-dream trigger coordinator.

Dream is background memory maintenance: it periodically consolidates existing
memory cards by enqueueing typed ``memory_dream`` jobs. It does not run the
agent, write chat, or consult proactive reach-out gates.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import db
import memory_readside_core
from memory import service as memory_service
from proactive import capture_jobs

DREAM_STATE_KIND = "dream_state"
DREAM_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped"})


def _env_float(name: str, default: float, *, lo: float = 0.0, hi: float = 7 * 86400.0) -> float:
    try:
        raw = float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        raw = default
    return max(lo, min(hi, raw))


def _env_int(name: str, default: int, *, lo: int = 0, hi: int = 10000) -> int:
    try:
        raw = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        raw = default
    return max(lo, min(hi, raw))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def min_new_cards() -> int:
    return _env_int("FEEDLING_DREAM_MIN_NEW_CARDS", 3, lo=1, hi=1000)


def min_new_turns() -> int:
    return _env_int("FEEDLING_DREAM_MIN_NEW_TURNS", 24, lo=0, hi=10000)


def min_interval_sec() -> float:
    return _env_float("FEEDLING_DREAM_MIN_INTERVAL_SEC", 23 * 3600.0, hi=7 * 86400.0)


def night_only() -> bool:
    return _env_bool("FEEDLING_DREAM_NIGHT_ONLY", True)


def night_start_hour() -> int:
    return _env_int("FEEDLING_DREAM_NIGHT_START_HOUR", 2, lo=0, hi=23)


def night_end_hour() -> int:
    return _env_int("FEEDLING_DREAM_NIGHT_END_HOUR", 5, lo=0, hi=23)


def _now_iso(now: float | None = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _state_doc(raw: Any) -> dict[str, Any]:
    doc = dict(raw) if isinstance(raw, dict) else {}
    return {
        "last_dream_completed_at": _safe_float(doc.get("last_dream_completed_at"), 0.0),
        "last_dreamed_until": str(doc.get("last_dreamed_until") or "")[:240],
        "last_dreamed_card_count": max(0, _safe_int(doc.get("last_dreamed_card_count"), 0)),
        "last_dreamed_turn_count": max(0, _safe_int(doc.get("last_dreamed_turn_count"), 0)),
        "last_dream_signature": str(doc.get("last_dream_signature") or "")[:240],
        "pending_dream_key": str(doc.get("pending_dream_key") or "")[:240],
        "dream_fail_streak": max(0, _safe_int(doc.get("dream_fail_streak"), 0)),
        "last_dream_failed_at": _safe_float(doc.get("last_dream_failed_at"), 0.0),
        "updated_at": str(doc.get("updated_at") or "")[:80],
    }


def load_dream_state(store) -> dict[str, Any]:
    return _state_doc(db.get_blob(store.user_id, DREAM_STATE_KIND))


def save_dream_state(store, state: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    doc = _state_doc(state)
    doc["updated_at"] = _now_iso(now)
    db.set_blob(store.user_id, DREAM_STATE_KIND, doc)
    return doc


def _timezone_for_store(store) -> ZoneInfo:
    settings = {}
    try:
        settings = store.load_proactive_settings()
    except Exception:
        settings = {}
    tz_name = str((settings or {}).get("timezone") or os.environ.get("FEEDLING_DREAM_TIMEZONE") or "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _within_night_window(store, *, now: float) -> bool:
    local_dt = datetime.fromtimestamp(now, timezone.utc).astimezone(_timezone_for_store(store))
    start = night_start_hour()
    end = night_end_hour()
    hour = local_dt.hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _dream_available_moments(store) -> list[dict[str, Any]]:
    moments = memory_service._load_moments(store)
    return [
        dict(moment)
        for moment in moments
        if memory_readside_core.memory_available(moment, store.user_id)
    ]


def _live_user_turn_count(store) -> int:
    chat_messages = getattr(store, "chat_messages", None)
    if not isinstance(chat_messages, list):
        return 0
    chat_lock = getattr(store, "chat_lock", None)
    if chat_lock is not None:
        with chat_lock:
            messages = [dict(msg) for msg in chat_messages if isinstance(msg, Mapping)]
    else:
        messages = [dict(msg) for msg in chat_messages if isinstance(msg, Mapping)]
    return sum(1 for msg in messages if str(msg.get("role") or "").strip() == "user")


def _moment_signature(moment: Mapping[str, Any]) -> str:
    return "|".join(
        str(moment.get(key) or "")
        for key in ("id", "updated_at", "last_referenced_at", "occurred_at", "status")
    )


def _dream_snapshot(store) -> dict[str, Any]:
    moments = sorted(
        _dream_available_moments(store),
        key=lambda item: str(item.get("id") or ""),
    )
    digest = hashlib.sha256(
        "\n".join(_moment_signature(moment) for moment in moments).encode("utf-8")
    ).hexdigest()[:32]
    last = moments[-1] if moments else {}
    last_until = str(
        last.get("updated_at")
        or last.get("last_referenced_at")
        or last.get("occurred_at")
        or ""
    )[:240]
    return {
        "card_count": len(moments),
        "turn_count": _live_user_turn_count(store),
        "signature": digest,
        "last_until": last_until,
    }


def dream_key_for_snapshot(state: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
    material = "|".join(
        [
            str(state.get("last_dream_signature") or ""),
            str(snapshot.get("signature") or ""),
            str(snapshot.get("card_count") or 0),
            str(snapshot.get("turn_count") or 0),
        ]
    )
    return "dream:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _dream_enabled(store) -> bool:
    try:
        return bool(store.load_proactive_settings().get("dream_enabled", True))
    except Exception:
        return True


def tick_memory_dream(store, *, now: float | None = None, force: bool = False) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    state = load_dream_state(store)
    if not _dream_enabled(store):
        return {"enqueued": False, "reason": "dream_disabled", "state": state, "job": None, "snapshot": {}}
    snapshot = _dream_snapshot(store)
    card_count = int(snapshot.get("card_count") or 0)
    if card_count <= 0:
        return {"enqueued": False, "reason": "no_memory_cards", "state": state, "job": None, "snapshot": snapshot}
    pending_key = str(state.get("pending_dream_key") or "")
    if pending_key:
        if capture_jobs._find_active_dream(store) is not None:
            return {"enqueued": False, "reason": "dream_already_pending", "state": state, "job": None, "snapshot": snapshot}
        # Stale flag: the job it pointed to is terminal/gone — self-heal so a stuck
        # user isn't blocked forever, then fall through and re-evaluate.
        state["pending_dream_key"] = ""
        state = save_dream_state(store, state, now=now_ts)
    if not force and night_only() and not _within_night_window(store, now=now_ts):
        return {"enqueued": False, "reason": "night_not_due", "state": state, "job": None, "snapshot": snapshot}
    # 失败退避（同 capture）：min_interval 只看上次成功，对永远失败的 dream
    # （坏 BYOK key）不生效，会退化成每 tick 重试。force 绕过。
    if not force and capture_jobs.in_failure_backoff(
        int(state.get("dream_fail_streak") or 0),
        _safe_float(state.get("last_dream_failed_at"), 0.0),
        now_ts,
    ):
        return {"enqueued": False, "reason": "failure_backoff", "state": state, "job": None, "snapshot": snapshot}
    last_count = max(0, int(state.get("last_dreamed_card_count") or 0))
    new_cards = max(0, card_count - last_count)
    turn_count = max(0, int(snapshot.get("turn_count") or 0))
    last_turn_count = max(0, int(state.get("last_dreamed_turn_count") or 0))
    new_turns = max(0, turn_count - last_turn_count)
    if str(state.get("last_dream_signature") or "") == str(snapshot.get("signature") or "") and new_turns <= 0:
        return {"enqueued": False, "reason": "already_dreamed", "state": state, "job": None, "snapshot": snapshot}
    last_completed = _safe_float(state.get("last_dream_completed_at"), 0.0)
    if last_completed and not force and now_ts - last_completed < min_interval_sec():
        return {"enqueued": False, "reason": "min_interval", "state": state, "job": None, "snapshot": snapshot}
    turn_threshold = min_new_turns()
    turn_due = turn_threshold > 0 and new_turns >= turn_threshold
    if not force and new_cards < min_new_cards() and not turn_due:
        return {
            "enqueued": False,
            "reason": "not_enough_new_cards",
            "state": state,
            "job": None,
            "snapshot": snapshot,
            "new_cards": new_cards,
            "new_turns": new_turns,
        }

    key = dream_key_for_snapshot(state, snapshot)
    stats = {
        "card_count": card_count,
        "new_cards": new_cards,
        "new_turns": new_turns,
        "last_dreamed_card_count": last_count,
        "last_dreamed_turn_count": last_turn_count,
        "turn_count": turn_count,
        "signature": snapshot.get("signature") or "",
    }
    job, enqueued, reason = capture_jobs.enqueue_memory_dream_job(
        store,
        trigger="force_dream" if force else "nightly_dream",
        dream_key=key,
        dream_until={
            "signature": snapshot.get("signature") or "",
            "last_until": snapshot.get("last_until") or "",
        },
        dream_stats=stats,
        now=now_ts,
    )
    # Only arm pending for a genuinely in-flight job (mirror capture fix): arming on
    # a terminal duplicate is what caused the permanent dream_already_pending lock.
    if job is not None and (enqueued or capture_jobs._active_dream_job(job)):
        state["pending_dream_key"] = str(job.get("dream_key") or key)[:240]
        state = save_dream_state(store, state, now=now_ts)
    return {
        "enqueued": bool(enqueued),
        "reason": reason,
        "state": state,
        "job": job,
        "snapshot": snapshot,
        "new_cards": new_cards,
        "new_turns": new_turns,
    }


def record_dream_job_status(store, job: Mapping[str, Any], *, status: str, now: float | None = None) -> dict[str, Any]:
    if not capture_jobs.is_memory_dream_job(job):
        return load_dream_state(store)
    status_text = str(status or job.get("status") or "").strip().lower()
    if status_text not in DREAM_TERMINAL_STATUSES:
        return load_dream_state(store)
    now_ts = time.time() if now is None else float(now)
    state = load_dream_state(store)
    dream_key = str(job.get("dream_key") or "")
    if dream_key and str(state.get("pending_dream_key") or "") == dream_key:
        state["pending_dream_key"] = ""
    elif not dream_key:
        state["pending_dream_key"] = ""
    if status_text == "completed":
        stats = job.get("dream_stats") if isinstance(job.get("dream_stats"), Mapping) else {}
        until = job.get("dream_until") if isinstance(job.get("dream_until"), Mapping) else {}
        state["last_dream_completed_at"] = now_ts
        state["last_dreamed_card_count"] = max(0, _safe_int(stats.get("card_count"), 0))
        state["last_dreamed_turn_count"] = max(0, _safe_int(stats.get("turn_count"), 0))
        state["last_dream_signature"] = str(stats.get("signature") or until.get("signature") or "")[:240]
        state["last_dreamed_until"] = str(until.get("last_until") or "")[:240]
        state["dream_fail_streak"] = 0
        state["last_dream_failed_at"] = 0.0
    elif status_text == "failed":
        # skipped 是调度器主动暂缓、不算失败；只有真失败累计退避 streak。
        state["dream_fail_streak"] = int(state.get("dream_fail_streak") or 0) + 1
        state["last_dream_failed_at"] = now_ts
    return save_dream_state(store, state, now=now_ts)
