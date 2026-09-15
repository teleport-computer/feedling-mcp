"""Memory-dream trigger coordinator.

Dream is background memory maintenance: it periodically consolidates existing
memory cards by enqueueing typed ``memory_dream`` jobs. It does not run the
agent, write chat, or consult proactive reach-out gates.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import db
import debug_trace
import memory_readside_core
from memgarden import dreaming as mg_dreaming
from memory import service as memory_service
from proactive import capture_jobs

log = logging.getLogger(__name__)

DREAM_STATE_KIND = "dream_state"
DREAM_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped"})
# Worker-side "nothing to consolidate" verdicts that may arm the skip ledger.
# Same content-free vocabulary as the tick reasons below (the kernel's
# ``needs_dream``); anything else a status patch calls "skipped" leaves the
# ledger untouched so it can never silence Dream for a day by accident.
DREAM_SKIP_REASONS = frozenset({"not_enough_new_cards"})
# One hour keeps a stalled scheduler visible within a bounded diagnostic window
# while collapsing a stable 45-second client poll to at most 24 traces/user/day.
DREAM_TRACE_HEARTBEAT_SEC = 3600.0


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


def min_interval_sec() -> float:
    return _env_float("FEEDLING_DREAM_MIN_INTERVAL_SEC", 23 * 3600.0, hi=7 * 86400.0)


def night_only() -> bool:
    return _env_bool("FEEDLING_DREAM_NIGHT_ONLY", True)


def night_start_hour() -> int:
    return _env_int("FEEDLING_DREAM_NIGHT_START_HOUR", 2, lo=0, hi=23)


def night_end_hour() -> int:
    return _env_int("FEEDLING_DREAM_NIGHT_END_HOUR", 5, lo=0, hi=23)


# ---------------------------------------------------------------------------
# Night-burst protection (prod 09-10 / 09-13): every user's Dream used to become
# due at the same second (window start), and the burst of whole-garden card
# reads coincided with fleet-wide enclave decrypt timeouts. Two independent
# guards, both kill switches rather than feature gates:
#
# 1. Stagger — each user gets an offset into the night window, derived from the
#    user id and tonight's local date (same value every tick, process and restart
#    within one night; a different slot next night, so under saturation the users
#    stuck in late slots rotate instead of the same ones losing every night).
# 2. Admission ceiling — no new Dream is enqueued while the fleet already has
#    ``dream_max_concurrent()`` Dream jobs queued or running (V1 queued/running +
#    V2 recently pending/claimed/running; see ``active_dream_job_count``), counted
#    and enqueued under one fleet-wide lock (``_admission_slot``).
#
# ``force`` (a user-requested organize) bypasses both, like every other gate.
# ---------------------------------------------------------------------------

#: The tail of the window kept free of first attempts, so a first run that
#: fails still has room for the failure-backoff retries (600s base, doubling:
#: 10 + 20 + 40 min = 70 min) plus a run, before the window closes. Never more
#: than half the window, so short custom windows still stagger.
DREAM_STAGGER_TAIL_MARGIN_SEC = 5400
#: A V1 Dream job created longer ago than this no longer counts toward the
#: admission ceiling. A resident Dream is at most a couple of 300s agent turns;
#: an hour-old active row is an orphan (consumer gone), not load.
#: It also bounds a *pending* V2 Dream's slot: V2 has no pending expiry for this
#: lane (no queue deadline; the reaper's pending TTL is chat-only). Trade-off: a
#: stalled V2 queue blocks admission for up to this long.
DREAM_ADMISSION_LEGACY_HORIZON_SEC = 3600.0
#: Default ceiling. The enclave serves decrypts from 4 GIL-bound worker
#: processes (FEEDLING_ENCLAVE_WORKERS=4 in the prod compose), and a Dream's
#: card read decrypts the whole garden in one burst; Runtime V2 likewise bounds
#: one worker instance's enclave requests at 4. More than 4 simultaneous Dreams
#: can therefore occupy every decrypt worker at once and starve foreground reads.
DREAM_MAX_CONCURRENT_DEFAULT = 4
#: Plus ``pending`` V2 Dreams within the horizon: the pool claims them as soon as
#: it has room, so leaving them out let a burst far past the ceiling (Codex review
#: 2026-09-15); counting them forever let a stalled queue block Dream fleet-wide.
_V2_ADMISSION_JOB_STATUSES = ("claimed", "running")


def stagger_enabled() -> bool:
    return _env_bool("FEEDLING_DREAM_STAGGER", True)


def dream_max_concurrent() -> int:
    """0 disables the admission ceiling (kill switch)."""
    return _env_int("FEEDLING_DREAM_MAX_CONCURRENT", DREAM_MAX_CONCURRENT_DEFAULT, lo=0, hi=10000)


def _night_window_len_sec() -> int:
    start = night_start_hour()
    end = night_end_hour()
    if start == end:
        return 0
    return ((end - start) % 24) * 3600


def dream_stagger_span_sec() -> int:
    """Seconds from window start over which first attempts are spread."""
    window = _night_window_len_sec()
    return max(0, window - min(DREAM_STAGGER_TAIL_MARGIN_SEC, window // 2))


def dream_stagger_offset_sec(user_id: str, night: str = "") -> int:
    """Per-user offset into the night window, in ``[0, span)``.

    A hash of the user id and the night's local date (``night``, ``YYYY-MM-DD``
    of the evening the window opened) — never the clock within a night or a
    random draw — so every tick, process and restart agrees on tonight's slot,
    while the slot rotates from night to night. A fixed per-user slot would let
    the same late-slot users lose to the admission ceiling every night.
    """
    span = dream_stagger_span_sec()
    if span <= 0:
        return 0
    digest = hashlib.sha256(f"feedling-dream-stagger:{user_id}:{night}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % span


def _seconds_into_night_window(store, *, now: float) -> int:
    """Local wall-clock seconds since tonight's window opened (caller has
    already established that ``now`` is inside the window)."""
    local_dt = datetime.fromtimestamp(now, timezone.utc).astimezone(_timezone_for_store(store))
    since_midnight = local_dt.hour * 3600 + local_dt.minute * 60 + local_dt.second
    return (since_midnight - night_start_hour() * 3600) % 86400


def _night_key(store, *, now: float) -> str:
    """Local date on which tonight's window opened (``now`` inside the window).

    A 23:00–02:00 window keeps one key across midnight.
    """
    local_dt = datetime.fromtimestamp(now, timezone.utc).astimezone(_timezone_for_store(store))
    opened = local_dt - timedelta(seconds=_seconds_into_night_window(store, now=now))
    return opened.date().isoformat()


def _stagger_not_due(store, *, now: float) -> bool:
    if not stagger_enabled():
        return False
    offset = dream_stagger_offset_sec(str(store.user_id), _night_key(store, now=now))
    return _seconds_into_night_window(store, now=now) < offset


def active_dream_job_count() -> int:
    """Fleet Dream jobs that hold an admission slot.

    - V1: active ``memory_dream`` rows created within the orphan horizon. A V1
      job is only ever claimed by a live consumer shortly after it is queued, so
      pending and claimed both count; the horizon retires orphans.
    - V2: claimed/running + in-horizon pending — see ``_V2_ADMISSION_JOB_STATUSES``.

    The orphan horizon is measured on server time, and scheduler-enqueued V1
    rows carry server time too (``_tick_memory_dream``): the decision ``now`` of
    a tick may come from a client and must not be able to hide or pin load.
    """
    return db.memory_dream_active_job_count(
        legacy_since_epoch=time.time() - DREAM_ADMISSION_LEGACY_HORIZON_SEC,
        legacy_active_statuses=sorted(capture_jobs.CAPTURE_ACTIVE_STATUSES),
        v2_active_statuses=list(_V2_ADMISSION_JOB_STATUSES),
        v2_pending_horizon_sec=DREAM_ADMISSION_LEGACY_HORIZON_SEC,
    )


def _admission_ceiling_reached() -> bool:
    cap = dream_max_concurrent()
    if cap <= 0:
        return False
    try:
        active = active_dream_job_count()
    except Exception as exc:  # noqa: BLE001 — a failed count must not stop Dream
        log.warning("dream admission count failed; admitting: %s", type(exc).__name__)
        return False
    return active >= cap


@contextlib.contextmanager
def _admission_slot(*, force: bool):
    """Yield ``None`` (may enqueue, body runs under the fleet lock — see
    ``db.memory_dream_admission_lock``) or the skip reason. ``force`` / the ``0``
    kill switch take no lock; a DB error on the lock admits (like a failed count);
    a lock held elsewhere answers ``dream_admission_busy`` (retry next tick)."""
    if force or dream_max_concurrent() <= 0:
        yield None
        return
    with contextlib.ExitStack() as stack:
        try:
            held = stack.enter_context(db.memory_dream_admission_lock())
        except Exception as exc:  # noqa: BLE001 — a failed lock must not stop Dream
            log.warning("dream admission lock unavailable; admitting: %s", type(exc).__name__)
            held = None
        if held is False:
            yield "dream_admission_busy"
        elif _admission_ceiling_reached():
            yield "dream_concurrency_cap"
        else:
            yield None


def _now_iso(now: float | None = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _trace_now() -> float:
    """Use server time; the scheduler's decision ``now`` may come from a client."""
    return time.time()


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
    last_card_count = max(0, _safe_int(doc.get("last_dreamed_card_count"), 0))
    return {
        "last_dream_completed_at": _safe_float(doc.get("last_dream_completed_at"), 0.0),
        "last_dream_organized_count": max(0, _safe_int(doc.get("last_dream_organized_count"), 0)),
        "last_dream_merged_count": max(0, _safe_int(doc.get("last_dream_merged_count"), 0)),
        "last_dreamed_until": str(doc.get("last_dreamed_until") or "")[:240],
        "last_dreamed_card_count": last_card_count,
        # Backward-compatible migration: using the old all-active count as the
        # initial seed floor suppresses an unsafe one-time replay after deploy.
        # Subsequent completions persist the exact historical non-Dream count.
        "last_dreamed_seed_card_count": max(
            0,
            _safe_int(doc.get("last_dreamed_seed_card_count"), last_card_count),
        ),
        "last_dreamed_turn_count": max(0, _safe_int(doc.get("last_dreamed_turn_count"), 0)),
        "last_dream_signature": str(doc.get("last_dream_signature") or "")[:240],
        "pending_dream_key": str(doc.get("pending_dream_key") or "")[:240],
        "dream_fail_streak": max(0, _safe_int(doc.get("dream_fail_streak"), 0)),
        "last_dream_failed_at": _safe_float(doc.get("last_dream_failed_at"), 0.0),
        # A Dream job that ran but had nothing to consolidate (garden below the
        # kernel minimum). Neither a success (the consolidation ledger above is
        # untouched) nor a failure (no backoff streak); it only spaces retries.
        "last_dream_skipped_at": _safe_float(doc.get("last_dream_skipped_at"), 0.0),
        "last_dream_skip_reason": (
            str(doc.get("last_dream_skip_reason") or "")
            if str(doc.get("last_dream_skip_reason") or "") in DREAM_SKIP_REASONS
            else ""
        ),
        "last_dream_trace_reason": str(doc.get("last_dream_trace_reason") or "")[:120],
        "last_dream_trace_at": _safe_float(doc.get("last_dream_trace_at"), 0.0),
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


def _live_user_turn_count(store) -> int:
    return db.chat_user_turn_count_strict(store.user_id)


def _dream_snapshot(store) -> dict[str, Any]:
    """取花园形状。**「什么算种子卡」「签名怎么算」已搬进内核** —— 那是 Garden
    内部结构的知识；这里只负责取数（查库、按归属和可见性过滤）。对话轮数只在
    真正准备入队时再查，避免稳定用户每个 scheduler tick 都扫描聊天历史。
    见 memgarden/dreaming.py 的模块说明。
    """
    all_moments = [
        dict(moment)
        for moment in memory_service._load_moments(store)
        if isinstance(moment, dict)
        and str(moment.get("owner_user_id") or "") == str(store.user_id)
    ]
    moments = sorted(
        [
            moment
            for moment in all_moments
            if memory_readside_core.memory_available(moment, store.user_id)
        ],
        key=lambda item: str(item.get("id") or ""),
    )
    snap = mg_dreaming.dream_snapshot(available_cards=moments, all_cards=all_moments)
    last = moments[-1] if moments else {}
    last_until = str(
        last.get("updated_at")
        or last.get("last_referenced_at")
        or last.get("occurred_at")
        or ""
    )[:240]
    return {
        "card_count": snap.card_count,
        "seed_card_count": snap.seed_card_count,
        "signature": snap.signature,
        "last_until": last_until,
    }


def dream_key_for_snapshot(state: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
    """幂等键。键的算法在内核，io 只把自己那侧的材料（对话轮数）拌进去。"""
    return mg_dreaming.dream_idempotency_key(
        mg_dreaming.DreamLedger(
            last_seed_card_count=int(state.get("last_dreamed_seed_card_count") or 0),
            last_signature=str(state.get("last_dream_signature") or ""),
        ),
        mg_dreaming.DreamSnapshot(
            card_count=int(snapshot.get("card_count") or 0),
            seed_card_count=int(snapshot.get("seed_card_count") or 0),
            signature=str(snapshot.get("signature") or ""),
        ),
        extra=(int(snapshot.get("turn_count") or 0),),
    )


def _dream_enabled(store) -> bool:
    try:
        return bool(store.load_proactive_settings().get("dream_enabled", True))
    except Exception:
        return True


def tick_memory_dream(
    store, *, now: float | None = None, force: bool = False,
    submit: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """做梦判定 —— 外层只负责有界留痕，判定逻辑一行没动（见 `_tick_memory_dream`）。

    2026-08-17 补：此前 7 种早退理由全是裸 return，服务端查不到「为什么没做梦」。
    实测代价：某用户六天没做过梦，从日志里完全看不出是没攒够卡、还是被夜间
    窗口挡住、还是失败退避 —— 只能去猜。
    """
    now_ts = time.time() if now is None else float(now)
    started = time.monotonic()
    outcome = _tick_memory_dream(store, now=now_ts, force=force, submit=submit)
    trace_now = _trace_now()
    try:
        _emit_window_missed_at_cap(store, outcome)
    except Exception:  # noqa: BLE001 — 观测失败绝不能挡住做梦
        pass
    try:
        _emit_dream_trace(store, outcome, duration_ms=(time.monotonic() - started) * 1000.0,
                          forced=bool(force), now=trace_now)
    except Exception:  # noqa: BLE001 — 观测失败绝不能挡住做梦
        pass
    return outcome


def _emit_window_missed_at_cap(store, outcome: Mapping[str, Any]) -> None:
    """A due user was still held by the admission ceiling when the window closed.

    The trace cursor always holds the previous tick's reason (a reason change is
    always traced), so ``dream_concurrency_cap`` → ``night_not_due`` means the
    user was due and capped until the window ended. Fires once per such
    transition; content-free (reason codes only).
    """
    if str(outcome.get("reason") or "") != "night_not_due":
        return
    state = _state_doc(outcome.get("state"))
    if state.get("last_dream_trace_reason") != "dream_concurrency_cap":
        return
    log.warning("dream window closed while user was still capped: user=%s", store.user_id)
    debug_trace.trace_event(
        store, subsystem="memory", type="memory.dream.window_missed", actor="backend",
        status="warning",
        summary="夜间窗口结束时仍被做梦并发上限挡住，今晚未做梦",
        explain="上一次判定是 dream_concurrency_cap、这一次窗口已关。只记理由码，不含卡片内容。",
        detail={"reason": "dream_concurrency_cap_at_window_end",
                "max_concurrent": dream_max_concurrent()},
    )


def _emit_dream_trace(store, outcome: Mapping[str, Any], *, duration_ms: float,
                      forced: bool, now: float) -> None:
    """状态变化、实际入队或每小时心跳时记录；卡片正文、摘要一律不进。"""
    snapshot = outcome.get("snapshot") if isinstance(outcome.get("snapshot"), Mapping) else {}
    enqueued = bool(outcome.get("enqueued"))
    reason = str(outcome.get("reason") or ("enqueued" if enqueued else "unknown"))
    state = _state_doc(outcome.get("state"))
    last_reason = str(state.get("last_dream_trace_reason") or "")
    last_emitted_at = _safe_float(state.get("last_dream_trace_at"), 0.0)
    heartbeat_due = (
        last_emitted_at <= 0.0
        or float(now) - last_emitted_at >= DREAM_TRACE_HEARTBEAT_SEC
    )
    if not (enqueued or reason != last_reason or heartbeat_due):
        return
    emission = (
        "enqueued"
        if enqueued
        else "reason_changed"
        if reason != last_reason
        else "heartbeat"
    )
    detail = {
        "enqueued": enqueued,
        "reason": reason,
        "emission": emission,
        "forced": forced,
        "counts": {
            # 「攒够没」这个判据的两个输入 —— 没有它们就说不清为什么没触发
            "seed_cards": _safe_int(snapshot.get("seed_card_count")),
            "new_since_last": _safe_int(snapshot.get("new_since_last")),
            "min_new_cards": min_new_cards(),
            "user_turns": _safe_int(snapshot.get("user_turn_count")),
        },
        "signature_changed": bool(snapshot.get("signature")
                                  and snapshot.get("signature") != outcome.get("last_signature")),
        "dur_ms": round(float(duration_ms), 1),
    }
    debug_trace.trace_event(
        store, subsystem="memory", type="memory.dream.tick", actor="backend",
        status="ok" if (enqueued or reason in _EXPECTED_SKIP_REASONS) else "warning",
        summary=("已排入做梦" if enqueued else f"未做梦：{reason}"),
        explain="实际入队、状态变化或每小时心跳时留一条。计数与理由落库，卡片内容不落库。",
        detail=detail,
    )
    # Merge only the trace cursor. A full blob rewrite here could resurrect a
    # stale sibling field (for example pending_dream_key) from this tick's read.
    db.patch_blob_strict(
        store.user_id,
        DREAM_STATE_KIND,
        {
            "last_dream_trace_reason": reason,
            "last_dream_trace_at": float(now),
        },
    )


#: 这些「没做梦」是正常的，不该在面板上显示成告警。
_EXPECTED_SKIP_REASONS = frozenset({
    "dream_disabled", "no_memory_cards", "dream_already_pending",
    "night_not_due", "min_interval", "not_enough_new_cards", "already_dreamed",
    "dream_stagger_not_due", "dream_concurrency_cap", "dream_admission_busy",
})


def _tick_memory_dream(
    store, *, now: float | None = None, force: bool = False,
    submit: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
    # Stagger refines the night window (it has no meaning without one): inside the
    # window, this user's first attempt waits for its stable per-user offset.
    if not force and night_only() and _stagger_not_due(store, now=now_ts):
        return {"enqueued": False, "reason": "dream_stagger_not_due", "state": state, "job": None, "snapshot": snapshot}
    # 失败退避（同 capture）：min_interval 只看上次成功，对永远失败的 dream
    # （坏 BYOK key）不生效，会退化成每 tick 重试。force 绕过。
    if not force and capture_jobs.in_failure_backoff(
        int(state.get("dream_fail_streak") or 0),
        _safe_float(state.get("last_dream_failed_at"), 0.0),
        now_ts,
    ):
        return {"enqueued": False, "reason": "failure_backoff", "state": state, "job": None, "snapshot": snapshot}
    # A recent worker skip ("garden too small to consolidate") spaces the next
    # attempt like a completion would, without pretending a dream happened:
    # the consolidation ledger stays untouched, so once the garden grows the
    # next night's run is a real one. force bypasses.
    skip_reason = str(state.get("last_dream_skip_reason") or "")
    last_skipped = _safe_float(state.get("last_dream_skipped_at"), 0.0)
    if (
        not force
        and skip_reason
        and last_skipped
        and now_ts - last_skipped < min_interval_sec()
    ):
        return {"enqueued": False, "reason": skip_reason, "state": state, "job": None, "snapshot": snapshot}
    last_turn_count = max(0, int(state.get("last_dreamed_turn_count") or 0))

    # 「值不值得整理」的判据在内核 —— 只数种子卡、比指纹，不看时间不看内容。
    # 这里保留的是「能不能 / 什么时候」那半（上面的开关/防重/夜间窗口/失败退避，
    # 以及下面的 min_interval），它们跟哪个记忆库无关。
    verdict = mg_dreaming.needs_dream(
        mg_dreaming.DreamSnapshot(
            card_count=int(snapshot.get("card_count") or 0),
            seed_card_count=max(0, int(snapshot.get("seed_card_count") or 0)),
            signature=str(snapshot.get("signature") or ""),
        ),
        mg_dreaming.DreamLedger(
            last_seed_card_count=max(0, int(state.get("last_dreamed_seed_card_count") or 0)),
            last_signature=str(state.get("last_dream_signature") or ""),
        ),
        min_new_cards=min_new_cards(),
    )
    new_cards = verdict.new_cards

    if verdict.reason == "already_dreamed":
        return {
            "enqueued": False,
            "reason": "already_dreamed",
            "state": state,
            "job": None,
            "snapshot": snapshot,
            "new_cards": new_cards,
            "new_turns": 0,
        }
    last_completed = _safe_float(state.get("last_dream_completed_at"), 0.0)
    if last_completed and not force and now_ts - last_completed < min_interval_sec():
        return {"enqueued": False, "reason": "min_interval", "state": state, "job": None, "snapshot": snapshot}
    # force 绕过内核判据（人工触发不受「攒够没」限制），与原实现一致。
    if not force and not verdict.needed:
        return {
            "enqueued": False,
            "reason": verdict.reason,
            "state": state,
            "job": None,
            "snapshot": snapshot,
            "new_cards": new_cards,
            "new_turns": 0,
        }

    # Fleet admission ceiling — last, so only genuine enqueue candidates pay for
    # the count. Count + enqueue are one decision under a fleet-wide lock, else
    # concurrent producers (V1 ticks, the V2 scheduler) all take one free slot.
    with _admission_slot(force=force) as admission_skip:
        if admission_skip is not None:
            return {
                "enqueued": False,
                "reason": admission_skip,
                "state": state,
                "job": None,
                "snapshot": snapshot,
                "new_cards": new_cards,
                "new_turns": 0,
            }

        # Chat turns do not decide whether a dream is needed. Count them only for
        # a real enqueue candidate, where they remain part of the legacy job stats
        # and idempotency material without creating a periodic history-sized read.
        turn_count = max(0, _live_user_turn_count(store))
        snapshot = dict(snapshot)
        snapshot["turn_count"] = turn_count
        new_turns = max(0, turn_count - last_turn_count)
        key = dream_key_for_snapshot(state, snapshot)
        trigger = "force_dream" if force else "nightly_dream"
        # V2 seam（同 capture_scheduler.tick_quiet_capture）：默认 None = 今天的行为
        # （append 进 legacy proactive_jobs 流）。V2 的 scheduler 传入一个把 job 塞进
        # agent_jobs 的 submitter —— 上面的所有早退（disabled / no_memory_cards /
        # dream_already_pending / night_not_due / failure_backoff / already_dreamed /
        # min_interval / not_enough_new_cards）原样复用，零漂移。submit 的返回值
        # 直接就是这个函数的 enqueue 结果，形状与 legacy 分支一致。
        if submit is not None:
            submitted = submit(store, trigger=trigger, now=now_ts)
            job = submitted.get("job")
            enqueued = bool(submitted.get("enqueued"))
            reason = submitted.get("reason")
        else:
            stats = {
                "card_count": card_count,
                "new_cards": new_cards,
                "new_turns": new_turns,
                "last_dreamed_card_count": max(0, int(state.get("last_dreamed_card_count") or 0)),
                "last_dreamed_seed_card_count": max(
                    0, int(state.get("last_dreamed_seed_card_count") or 0)
                ),
                "seed_card_count": max(0, int(snapshot.get("seed_card_count") or 0)),
                "last_dreamed_turn_count": last_turn_count,
                "turn_count": turn_count,
                "signature": snapshot.get("signature") or "",
            }
            job, enqueued, reason = capture_jobs.enqueue_memory_dream_job(
                store,
                trigger=trigger,
                dream_key=key,
                dream_until={
                    "signature": snapshot.get("signature") or "",
                    "last_until": snapshot.get("last_until") or "",
                },
                dream_stats=stats,
                # Server time, not the (possibly client-supplied) decision ``now``:
                # the row's ts is what the admission ceiling's orphan horizon reads.
                now=_trace_now(),
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


#: Resident "no cards" completion. Consumers before the strict Dream read also
#: sent it when the card read itself failed (timeout/5xx swallowed into ``{}``).
LEGACY_NO_CARDS_REASON = "dream_no_cards_available"
#: Content-free failure code for a Dream whose card read failed (V1 and V2).
CONTEXT_UNAVAILABLE_REASON = "dream_context_unavailable"


def reclassify_unverified_no_cards_completion(
    store, job: Mapping[str, Any] | None, patch: dict[str, Any]
) -> dict[str, Any]:
    """Turn an old consumer's "no cards" completion into a read failure when the
    garden provably has cards.

    Before the strict read, a resident consumer answered a timed-out card read
    with ``completed`` + ``dream_no_cards_available``. Recording that as a
    completion advances the Dream ledger, and the scheduler then answers
    ``already_dreamed`` until enough new cards arrive (prod, 09-10 / 09-13).
    Current consumers mark a genuinely empty read with
    ``dream_result.cards_read == "empty"``; only unmarked reports are checked,
    against the same live, owner-scoped card count the scheduler enqueues on
    (a Dream is only ever enqueued with ``card_count > 0``). Any doubt — marker
    present, zero live cards, or the count itself failing — keeps the patch
    exactly as sent.
    """
    if not capture_jobs.is_memory_dream_job(job):
        return patch
    if str(patch.get("status") or "") != "completed":
        return patch
    dream_result = patch.get("dream_result") if isinstance(patch.get("dream_result"), Mapping) else {}
    reasons = {
        str(patch.get("status_reason") or ""),
        str(patch.get("noop_reason") or ""),
        str(dream_result.get("reason") or ""),
    }
    if LEGACY_NO_CARDS_REASON not in reasons or dream_result.get("cards_read") == "empty":
        return patch
    try:
        card_count = int(_dream_snapshot(store).get("card_count") or 0)
    except Exception:  # noqa: BLE001 — a failed count must not change what the consumer said
        return patch
    if card_count <= 0:
        return patch
    rewritten = {
        key: value for key, value in patch.items() if key != "completed_at"
    }
    rewritten["status"] = "failed"
    rewritten["failed_at"] = patch.get("completed_at") or datetime.now().isoformat()
    rewritten["status_reason"] = CONTEXT_UNAVAILABLE_REASON
    rewritten["noop_reason"] = CONTEXT_UNAVAILABLE_REASON
    rewritten["dream_result"] = {
        **dict(dream_result),
        "status": "failed",
        "reason": CONTEXT_UNAVAILABLE_REASON,
    }
    return rewritten


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
        dream_result = job.get("dream_result") if isinstance(job.get("dream_result"), Mapping) else {}
        organized_count = _safe_int(
            job.get("organized_count")
            or dream_result.get("organized_count")
            or job.get("cards_superseded"),
            0,
        )
        merged_count = _safe_int(
            job.get("merged_count")
            or dream_result.get("merged_count")
            or job.get("cards_merged"),
            0,
        )
        state["last_dream_completed_at"] = now_ts
        state["last_dream_organized_count"] = max(0, organized_count)
        state["last_dream_merged_count"] = max(0, merged_count)
        state["last_dreamed_card_count"] = max(0, _safe_int(stats.get("card_count"), 0))
        state["last_dreamed_seed_card_count"] = max(
            0,
            _safe_int(
                stats.get("seed_card_count"),
                _safe_int(stats.get("card_count"), 0),
            ),
        )
        state["last_dreamed_turn_count"] = max(0, _safe_int(stats.get("turn_count"), 0))
        state["last_dream_signature"] = str(stats.get("signature") or until.get("signature") or "")[:240]
        state["last_dreamed_until"] = str(until.get("last_until") or "")[:240]
        state["dream_fail_streak"] = 0
        state["last_dream_failed_at"] = 0.0
        state["last_dream_skipped_at"] = 0.0
        state["last_dream_skip_reason"] = ""
    elif status_text == "skipped":
        skip_reason = str(job.get("dream_skip_reason") or "").strip()
        if skip_reason in DREAM_SKIP_REASONS:
            state["last_dream_skipped_at"] = now_ts
            state["last_dream_skip_reason"] = skip_reason
    elif status_text == "failed":
        # skipped 是调度器主动暂缓、不算失败；只有真失败累计退避 streak。
        state["dream_fail_streak"] = int(state.get("dream_fail_streak") or 0) + 1
        state["last_dream_failed_at"] = now_ts
    state = save_dream_state(store, state, now=now_ts)
    capture_jobs.notify_backoff(store, lane="dream", status=status_text,
                                streak=int(state.get("dream_fail_streak") or 0))
    return state
