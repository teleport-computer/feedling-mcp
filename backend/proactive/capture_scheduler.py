"""Memory-capture trigger coordinator.

This module owns only the trigger layer: chat/window bookkeeping and enqueueing
typed capture jobs. It must not run the capture handler and must not consult
proactive reach-out gates.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import db
from psycopg.types.json import Jsonb
from notices import status_reason as notices_status_reason
from proactive import capture_daily, capture_jobs
from memory.capture_failure import capture_failure_patch as _capture_failure_patch
from memory.capture_failure import frontier_seq as _frontier_seq
from memory.capture_failure import SUCCESS_RESET_PATCH as _SUCCESS_RESET_PATCH

log = logging.getLogger(__name__)

CAPTURE_STATE_KIND = "capture_state"
CAPTURE_LIVE_SOURCES = frozenset({
    "chat",
    "model_api",
    "live_activity",
    "agent_initiated_proactive",
    # The one card written at voice-call hangup (the call's per-turn rows are
    # deleted then). Capture swaps it for the archived FULL transcript when it
    # renders the window — the card itself is only a bounded preview.
    # NOTE: keep this set in sync with worker._CAPTURE_PROMPT_SOURCES; a source
    # that triggers capture but is filtered out of the prompt makes capture spin
    # on an empty window and advance the cursor anyway (that is exactly what
    # voice_call_summary did on V2 from 2026-08-05 until this change).
    "voice_call_transcript",
})
CAPTURE_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped"})

#: 自托管 / 托管 V1 的一批落卡最多几条（只数会触发落卡的 user/openclaw 消息）。
#: 和 V2 worker 的 ``_CAPTURE_BATCH_LIMIT`` 同一个数，两条 runtime 一批一样大
#: （tests/test_v1_capture_backlog_batches.py 锁住两边相等）。
#: 🔴 必须小于 ``_live_messages_after_capture`` 的发现上限（至少 64），
#: 否则 ``message_count >= 一批`` 这个「还有积压」的判断永远不成立。
CAPTURE_V1_BATCH_LIMIT = 60
#: consumer 在 ``X-Feedling-Consumer-Capabilities`` 里声明「我能按 seq 精确取一批」。
#: 老 consumer 只会拿最新 160 行再截尾，给它最早一批它看不到（见 ``_enqueue_window``）。
CAPTURE_BATCH_WINDOW_CAPABILITY = "capture_batch_window_v1"
#: chat/consumer.py 记录的 consumer 状态 blob。proactive 在 chat 下层，不能 import
#: chat.consumer，这里直接读同一个 blob 的同一个字段（poll 时写入）。
_CONSUMER_STATE_BLOB = "consumer_state"


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


def append_refresh_deferred() -> bool:
    """Whether foreground chat writes defer Capture discovery to the tick.

    ``sync`` is an emergency rollback mode that restores the pre-change
    request-path refresh behavior without reverting the durable-seq fixes.
    """
    mode = os.environ.get(
        "FEEDLING_CAPTURE_APPEND_REFRESH_MODE", "deferred"
    )
    return str(mode or "deferred").strip().lower() != "sync"


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
        #: 当前 streak 属于哪个窗口。**不带这个的话 streak 会跨窗口累加** ——
        #: 三次互不相干的偶发失败会被当成"同一条毒消息卡住了"，误跳过一批好数据。
        "capture_fail_window_key": str(doc.get("capture_fail_window_key") or "")[:340],
        #: 同一窗口**连续**解析类失败几次（到 3 快速跳过）；总失败数仍看 capture_fail_streak。
        "capture_parse_fail_streak": max(
            0, int(_safe_float(doc.get("capture_parse_fail_streak"), 0.0))
        ),
        #: 同一窗口里**非账号类**失败的次数（到 6 兜底跳过）；账号类失败不计。
        "capture_window_fail_count": max(
            0, int(_safe_float(doc.get("capture_window_fail_count"), 0.0))
        ),
        #: 同一窗口第一次账号/服务类失败的时间；持续 7 天仍失败才跳过。
        "capture_account_fail_since": _safe_float(doc.get("capture_account_fail_since"), 0.0),
        #: 最近一次失败若是账号/服务问题，对照表里的类别（如 quota_insufficient），给用户提示说清原因。
        "capture_account_error_code": str(doc.get("capture_account_error_code") or "")[:80],
        #: 最近一次亲手累计失败的 V2 任务 id —— 提示只认它（见 worker._notify_capture_backoff）。
        #: 🔴 必须在这个白名单里：生产读状态走本函数归一化，漏了字段提示就永远不发（Codex 第 7 轮）。
        "last_capture_failed_job_id": str(doc.get("last_capture_failed_job_id") or "")[:80],
        #: 一共跳过了几批、最近一次跳的是什么时候。只记数字和游标，不记原文。
        "capture_skipped_windows": max(
            0, int(_safe_float(doc.get("capture_skipped_windows"), 0.0))
        ),
        "last_capture_skipped_at": _safe_float(doc.get("last_capture_skipped_at"), 0.0),
        "last_seen_message_id": str(doc.get("last_seen_message_id") or "")[:160],
        "last_seen_ts": _safe_float(doc.get("last_seen_ts"), 0.0),
        "turns_since_capture": max(0, int(_safe_float(doc.get("turns_since_capture"), 0.0))),
        "message_count": max(0, int(_safe_float(doc.get("message_count"), 0.0))),
        "updated_at": str(doc.get("updated_at") or "")[:80],
    }


def _failure_reason_of(job) -> str:
    """从任务上取失败原因。两个位置都可能有，优先用更具体的那个。"""
    src = job if isinstance(job, Mapping) else {}
    result = src.get("capture_result")
    if isinstance(result, Mapping) and result.get("reason"):
        return str(result.get("reason"))
    return str(src.get("status_reason") or "")


def _record_skipped_window(store, *, window: Mapping | None, streak: int,
                           job_id: str = "") -> None:
    """把"跳过了一批"记成一条可查的事件。

    🔴 这一步不能省。跳过是**永久丢掉那一批对话的记忆**，只改状态不留痕迹的话，
    它就是又一处"每一步都成功、只是什么都没记住"。

    只记游标和条数，**不记任何对话原文** —— 这条事件会进诊断面。
    """
    w = window if isinstance(window, Mapping) else {}
    try:
        import debug_trace

        debug_trace.trace_event(
            store,
            subsystem="memory",
            type="memory.capture.window_skipped",
            actor="backend",
            job_id=str(job_id or ""),
            summary=(
                f"skipped a capture window after {streak} consecutive failures; "
                "those messages will never be captured"
            ),
            detail={
                "after_message_id": str(w.get("after_message_id") or "")[:160],
                "until_message_id": str(w.get("until_message_id") or "")[:160],
                "through_seq": int(_safe_float(w.get("through_seq"), 0.0)),
                "message_count": int(_safe_float(w.get("message_count"), 0.0)),
                "fail_streak": int(streak),
            },
        )
    except Exception:  # noqa: BLE001 —— 记不上痕迹不该让跳过本身失败
        log.exception("[capture] 记录跳过窗口失败")


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
    require_existing: bool = False,
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
                if source_id or require_existing:
                    # A chat-derived refresh uses the same shared fence as
                    # ordinary writers. If Clear linearized first, the stale
                    # refresh must not recreate capture_state metadata.
                    db._lock_chat_user_fence_on_cursor(cur, store.user_id)
                if source_id:
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
                    if not require_existing:
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
                if wrote and (source_id or require_existing):
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
    # Runtime V2's raw frontier may end on a synthetic/import row whose ID is
    # intentionally absent from the live-message subset. Seq is the only safe
    # discovery cursor: an out-of-order later live row can carry an older
    # timestamp and must still trigger Capture. Translate a legacy ID once per
    # read; if it was pruned, restart safely from zero just like V2 extraction.
    # 数字和消息 id 两份进度取靠后的 —— 和 V2 worker / 提交路径同一个规则。
    after_seq = _frontier_seq(
        state, lambda message_id: db.chat_seq_for_msg_id(store.user_id, message_id)
    )
    rows = db.chat_capture_messages_after_seq(
        store.user_id,
        after_seq,
        # Trigger discovery needs the newest live identity plus a capped
        # turn-count backstop, not the entire uncaptured transcript. The worker
        # independently pages exact oldest batches of 60.
        sources=tuple(CAPTURE_LIVE_SOURCES),
        limit=max(64, min(1000, turn_backstop() * 2)),
    )
    return [dict(row) for row in rows if _is_live_capture_message(row)]


def refresh_capture_state_from_chat(store, *, now: float | None = None) -> dict[str, Any]:
    raw_state = db.get_blob(store.user_id, CAPTURE_STATE_KIND)
    state = _state_doc(raw_state)
    window_messages = _live_messages_after_capture(store, state)
    # Clear deletes both the transcript and this derived frontier. A delayed
    # scheduler tick that observes neither must remain a no-op instead of
    # recreating an empty capture_state row after Clear linearized.
    if raw_state is None and not window_messages:
        return state
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
        # An empty refresh may update an existing frontier, but it must never
        # create one. Clear can delete the row between the read above and this
        # fenced write.
        require_existing=not bool(window_messages),
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


def _consumer_supports_batch_window(store) -> bool:
    """这个用户最近一次 poll 的 consumer 是否声明能处理按 seq 划定的一批。

    不看新鲜度：consumer 离线期间 iOS 切后台照样会入队，那时读到的是它离线前
    最后一次声明。读失败 / 没声明 → False，退回老窗口（今天的行为），不冒险给
    老 consumer 一个它取不到的批次。
    """
    try:
        doc = db.get_blob(store.user_id, _CONSUMER_STATE_BLOB)
    except Exception:  # noqa: BLE001 —— 读不到就按老 consumer 处理
        return False
    if not isinstance(doc, Mapping):
        return False
    caps = doc.get("consumer_capabilities")
    if not isinstance(caps, (list, tuple)):
        return False
    return CAPTURE_BATCH_WINDOW_CAPABILITY in {
        str(item).strip().lower() for item in caps
    }


def _v1_oldest_batch_window(store, state: Mapping[str, Any]) -> dict[str, Any] | None:
    """游标之后**最早**的一批（最多 CAPTURE_V1_BATCH_LIMIT 条）连续消息的精确边界。

    和 V2 worker 一个思路：一批一批从旧往新推，完成只把游标推到**这批**的末尾。
    多取一条只为知道「这批后面还有没有」，不读整段积压。
    """
    after_seq = _frontier_seq(
        state, lambda message_id: db.chat_seq_for_msg_id(store.user_id, message_id)
    )
    rows = db.chat_capture_messages_oldest_after_seq(
        store.user_id,
        after_seq,
        sources=tuple(CAPTURE_LIVE_SOURCES),
        limit=CAPTURE_V1_BATCH_LIMIT + 1,
    )
    live = [dict(row) for row in rows if _is_live_capture_message(row)]
    batch = live[:CAPTURE_V1_BATCH_LIMIT]
    if not batch:
        return None
    last = batch[-1]
    through_seq = int(_safe_float(last.get("seq"), 0.0))
    last_id = str(last.get("id") or "")[:160]
    if through_seq <= after_seq or not last_id:
        return None
    # 🔴 键的顺序有讲究：consumer 回报时 capture_window 只保留前 12 个键
    # （proactive_core._safe_capture_doc），边界字段必须排在指纹字段前面。
    return {
        "after_message_id": str(state.get("last_captured_until_message_id") or "")[:160],
        "after_seq": int(after_seq),
        "until_message_id": last_id,
        "until_ts": _safe_float(last.get("ts"), 0.0),
        "message_count": len(batch),
        "through_seq": through_seq,
        "backlog_remaining": len(live) > CAPTURE_V1_BATCH_LIMIT,
    }


def _trace_legacy_window_backlog(store, window: Mapping[str, Any]) -> None:
    """老 consumer 拿老窗口、而积压已超过一批：如实留痕，不假装都记住了。

    老 consumer 只看得到最新的一段对话，游标却会推到最新 —— 更早的积压不会被记住，
    直到它自更新到声明 capture_batch_window_v1 的版本。只记计数，不记原文。
    """
    try:
        import debug_trace

        debug_trace.trace_event(
            store,
            subsystem="memory",
            type="memory.capture.legacy_window_backlog",
            actor="backend",
            summary=(
                "resident consumer does not support batch capture windows; "
                "older backlog beyond the newest messages will not be captured"
            ),
            explain="这个 consumer 版本太旧，积压超过一批时只能记最新的一段，更早的聊天不会补记；更新 consumer 后恢复逐批补记。",
            detail={
                "message_count": int(_safe_float(window.get("message_count"), 0.0)),
                "batch_limit": CAPTURE_V1_BATCH_LIMIT,
            },
        )
    except Exception:  # noqa: BLE001 —— 留痕失败不挡入队
        log.exception("[capture] 记录老窗口积压痕迹失败")


def capture_key_for_window(window: Mapping[str, Any]) -> str:
    material = "|".join(
        str(window.get(key) or "")
        for key in ("after_message_id", "until_message_id", "until_ts")
    )
    return "capture:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def v2_deferred_submission(disposition: str) -> dict[str, Any] | None:
    """V2 落卡入队这一轮**没有**任务时，submit 回调该返回什么（Codex 第 13 轮 M1）。

    ``disposition`` 取 ``jobs_store.CaptureEnqueueResult.disposition``。旧任务租约过期被终结
    （``expired_deferred``），或刚记下的失败让本轮落在退避里（``backoff_deferred``）：
    都不是「合并进了一个排队中的任务」，不能回一个假的 pending 任务 —— 下面
    ``_enqueue_window`` 会拿它标 pending，接口也会报成 v2_coalesced。有任务时返回 None。
    """
    if disposition == "expired_deferred":
        return {"enqueued": False, "reason": "v2_expired_deferred", "job": None}
    if disposition == "backoff_deferred":
        return {"enqueued": False, "reason": "failure_backoff", "job": None}
    return None


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
    # 游标之后已攒满至少一整批 = 积压。发现上限（>=64）大于一批（60），所以
    # message_count >= 60 与「最早那批是满的」等价，不用多查一次库。
    full_batch_pending = int(window.get("message_count") or 0) >= CAPTURE_V1_BATCH_LIMIT
    batch_capable = submit is None and _consumer_supports_batch_window(store)
    last_completed = _safe_float(state.get("last_capture_completed_at"), 0.0)
    # Resident V1 补积压不等 min_interval（默认 10 分钟一批，几千条积压要补一整天）；
    # 失败退避照旧生效，坏账号不会因此被高频重试。V2 与老 consumer 行为不变。
    if (last_completed and now_ts - last_completed < min_interval_sec()
            and not (batch_capable and full_batch_pending)):
        return {"enqueued": False, "reason": "min_interval", "state": state, "job": None}
    # 失败退避：min_interval 只看上次成功，对「永远失败的窗口」（坏 BYOK key）
    # 不生效，会退化成每 tick 重试。手动 force（debug 面板）不受限。
    if trigger != "manual_force" and capture_jobs.in_failure_backoff(
        int(state.get("capture_fail_streak") or 0),
        _safe_float(state.get("last_capture_failed_at"), 0.0),
        now_ts,
    ):
        return {"enqueued": False, "reason": "failure_backoff", "state": state, "job": None}

    legacy_backlog = False
    if batch_capable:
        # Resident V1 + 能按 seq 取批的 consumer：窗口是游标之后**最早**的一批，
        # 完成只推到这批末尾，积压逐批补记（和 V2 worker 同一个思路）。
        batch_window = _v1_oldest_batch_window(store, state)
        if batch_window is None:
            return {"enqueued": False, "reason": "no_new_messages", "state": state, "job": None}
        window = batch_window
    elif submit is None and full_batch_pending:
        # 老 consumer 只看得到最新 160 行，给它最早一批会反复「窗口取不到」失败、
        # 最后被逃生阀跳过 → 仍给老窗口（今天的行为），入队后如实留痕。
        legacy_backlog = True

    key = capture_key_for_window(window)
    if submit is None:
        job, enqueued, reason = capture_jobs.enqueue_memory_capture_job(
            store,
            trigger=trigger,
            capture_key=key,
            window=window,
            now=now_ts,
        )
        if enqueued and legacy_backlog:
            _trace_legacy_window_backlog(store, window)
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


def record_chat_append(
    store,
    message: Mapping[str, Any],
    *,
    defer_to_tick: bool = False,
) -> dict[str, Any]:
    if defer_to_tick:
        # Latency-sensitive chat writes are already durable before this hook.
        # The resident/V2 scheduler tick rebuilds the authoritative frontier
        # from PostgreSQL, so the request path does not need to repeat those
        # reads synchronously.
        return {
            "enqueued": False,
            "reason": "deferred_to_tick",
            "state": {},
            "job": None,
        }
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
    # Foreground chat writes defer capture discovery to this durable sweep.
    # Preserve the turn-count backstop for both resident and V2 with at most
    # one scheduler cadence of delay.
    if int(state.get("turns_since_capture") or 0) >= turn_backstop():
        return _enqueue_window(
            store,
            trigger="turn_backstop",
            now=now_ts,
            submit=submit,
        )
    # Resident V1 积压补记：游标后还攒着至少一整批时不等用户安静下来，下一个 tick
    # 就排下一批（批次本身有界，成本随消息量线性，不会整段积压一次塞给模型）。
    # 消息里 user 轮数不够回合兜底（比如大量 AI 主动消息）时也靠这条排空。
    if (submit is None
            and int(state.get("message_count") or 0) >= CAPTURE_V1_BATCH_LIMIT
            and _consumer_supports_batch_window(store)):
        return _enqueue_window(store, trigger="backlog_drain", now=now_ts)
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
    # completed / failed 两个分支都要用 —— 以前只在 completed 里赋值，
    # failed 分支一走到就 UnboundLocalError（tests/test_v2_capture_lifecycle.py 覆盖）。
    processed = window if isinstance(window, Mapping) else {}
    skipped = False
    if status_text == "completed":
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
            **_SUCCESS_RESET_PATCH,
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
        # ⚠️ 这个函数签名里**没有 job**。之前这里写了 `_failure_reason_of(job)` 和
        # `_capture_trace_job_id(job)` —— pyflakes 报 undefined name。
        #
        # 它没在 prod 上炸，只是因为 V2 的 capture lane 根本不走到这里：
        # worker 的 `_record_extraction_status` 对 lane == "capture" 直接 return，
        # V2 落卡的失败状态走 jobs_store 的持久批次协议（_capture_fail_on_cursor）。
        # 但这里一旦被接上，第一次跳过就会 NameError。
        #
        # 失败原因没有就留空 —— 空原因会走保守的 6 次档，不会误把会自己好的
        # 失败按 3 次跳掉。
        reason = str(processed.get("failure_reason") or "")
        patch, streak, skipped = _capture_failure_patch(
            state, processed, now_ts=now_ts, reason=reason)
        if skipped:
            _record_skipped_window(store, window=processed, streak=streak,
                                   job_id="")
        state = _patch_capture_state(
            store, {"pending_capture_key": "", **patch}, now=now_ts)
    capture_jobs.notify_backoff(
        store,
        lane="capture",
        status=status_text,
        account_code=str(state.get("capture_account_error_code") or ""),
        streak=int(state.get("capture_fail_streak") or 0),
        skipped=skipped,
    )
    return refresh_capture_state_from_chat(store, now=now_ts)


def _capture_trace_job_id(job: Mapping[str, Any]) -> str:
    return str(job.get("job_id") or "")[:120]


def _capture_trace_card_titles(job: Mapping[str, Any]) -> str:
    result = job.get("capture_result") if isinstance(job.get("capture_result"), Mapping) else {}
    titles = result.get("titles") if isinstance(result.get("titles"), list) else []
    if not titles:
        cards = result.get("cards") if isinstance(result.get("cards"), list) else []
        titles = [c.get("title", "") for c in cards if isinstance(c, Mapping)]
    return " | ".join(str(t) for t in titles if t)[:1000]


def _trace_safe_reason(job: Mapping[str, Any]) -> str:
    """Reason for a trace event, redacted before it leaves the process.

    Both fields are externally supplied free text — ``_job_status_patch`` stores
    ``payload["reason"][:500]`` and ``payload["noop_reason"][:500]`` straight
    from the request body — and ``GET /v1/debug/trace`` hands trace details back
    on the user's own auth. ``debug_trace._safe_detail`` bounds length but does
    not judge content, so redaction has to happen here, at the writer.
    """
    raw = str(job.get("status_reason") or job.get("noop_reason") or "").strip()
    return notices_status_reason.sanitize_status_reason(raw)


def _record_legacy_window_failure(store, job: Mapping[str, Any], failed_window: Mapping[str, Any], *,
                                  capture_key: str, now_ts: float):
    """老 V1 任务（修复前入队、窗口没带 after_seq）回报失败：判断 + 写入在同一个 fence 事务里。

    游标确实从未推进过（id、seq 都没有）= 首次落卡，起点就是 0，补上 ``after_seq=0``；
    额外要求窗口终点那条消息**还在**：Chat Clear 会删掉落卡状态，但不会作废清空前入队的
    V1 任务，那种旧任务回报失败时不能并进清空后的新窗口（Codex 第 11 轮）。

    以前「终点消息还在吗」和「写状态」是两个事务：检查通过 → Chat Clear 提交（删消息、删状态）
    → 旧任务把清空前读到的整份状态写回去，落卡状态被复活（Codex 第 12 轮 I3）。现在：

    - 持 Chat Clear 的共享 fence（Clear 拿独占）→ ``FOR UPDATE`` 读状态行 → 查终点消息 → 写回，
      Clear 要么整个在前、要么整个在后。
    - 窗口终点消息已不在（Clear 在同一个事务里删消息和状态；消息 id 唯一，清空后不会再出现）
      → 不写、返回 None，调用方不发提示、不记跳过痕迹。以前这种情况仍会把失败写进状态
      （只是不补 0），清空后的新状态会被旧窗口的失败冲掉计数。
    - 失败补丁基于**加锁后**读到的状态算，不用调用方事务外读的快照。

    返回 ``(写入后的状态, 实际用的窗口, streak, 是否跳过)`` 或 None。
    """
    until_id = str(failed_window.get("until_message_id") or "")
    reason = _failure_reason_of(job)
    with db.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                db._lock_chat_user_fence_on_cursor(cur, store.user_id)
                cur.execute(
                    "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s FOR UPDATE",
                    (store.user_id, CAPTURE_STATE_KIND),
                )
                row = cur.fetchone()
                until_exists: bool | None = None
                if until_id:
                    cur.execute(
                        "SELECT 1 FROM chat_messages WHERE user_id=%s AND msg_id=%s",
                        (store.user_id, until_id),
                    )
                    until_exists = cur.fetchone() is not None
                if until_exists is False:
                    # 窗口终点消息已经不在 = 这是 Chat Clear 之前的旧任务，它说的那批对话已经没了：
                    # 不复活被删的状态，也不把旧失败并进清空后的新状态。
                    return None
                if row is None:
                    # 状态行不在、终点消息还在 = 没清空过（只是状态行还没建），照旧建一行记失败。
                    # 连终点都说不清的旧任务无从判断，不建。
                    if not until_exists:
                        return None
                    cur.execute(
                        "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,%s,%s) "
                        "ON CONFLICT (user_id,kind) DO NOTHING",
                        (store.user_id, CAPTURE_STATE_KIND, Jsonb({})),
                    )
                    cur.execute(
                        "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s FOR UPDATE",
                        (store.user_id, CAPTURE_STATE_KIND),
                    )
                    row = cur.fetchone()
                state = _state_doc(row[0])
                if not capture_key or str(state.get("pending_capture_key") or "") == capture_key:
                    state["pending_capture_key"] = ""
                window = failed_window
                if (until_exists
                        and not str(state.get("last_captured_until_message_id") or "")
                        and not bool(state.get("capture_seq_initialized"))
                        and int(_safe_float(state.get("last_captured_until_seq"), 0.0)) == 0):
                    window = {**failed_window, "after_seq": 0}
                patch, streak, skipped = _capture_failure_patch(
                    state, window, now_ts=now_ts, reason=reason)
                state.update(patch)
                state = _state_doc(state)
                state["updated_at"] = _now_iso(now_ts)
                cur.execute(
                    "UPDATE user_blobs SET doc=%s WHERE user_id=%s AND kind=%s",
                    (Jsonb(state), store.user_id, CAPTURE_STATE_KIND),
                )
                # 和 _patch_capture_state 一样在 fence 内镜像：Clear 的主库删除 + 镜像删除
                # 一定排在这次写之后，迟到的镜像不会在 TEE 里复活这一行。
                db._mirror_persisted_blob(store.user_id, CAPTURE_STATE_KIND, state)
    return state, window, streak, skipped


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
    skipped = False
    if status_text == "completed":
        window = job.get("capture_window") if isinstance(job.get("capture_window"), Mapping) else job.get("window")
        window = window if isinstance(window, Mapping) else {}
        issued = job.get("window") if isinstance(job.get("window"), Mapping) else {}
        batch_through_seq = max(0, int(_safe_float(issued.get("through_seq"), 0.0)))
        if batch_through_seq > 0:
            # 按批次发的窗口：边界以后端入队时发出的 window 为准（consumer 回报的
            # capture_window 会被截键、也可能被改写），游标只推到**这批**末尾。
            # 迟到/重放的完成回报不能把已经更靠后的游标拉回来（否则下一批会重复落卡）。
            until_id = str(issued.get("until_message_id") or "")[:160]
            current_seq = _frontier_seq(
                state, lambda message_id: db.chat_seq_for_msg_id(store.user_id, message_id)
            )
            if until_id and batch_through_seq > current_seq:
                state["last_captured_until_message_id"] = until_id
                state["last_captured_until_ts"] = _safe_float(issued.get("until_ts"), 0.0)
                state["last_captured_until_seq"] = batch_through_seq
                state["capture_seq_initialized"] = True
                state["last_capture_completed_at"] = now_ts
        else:
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
        state.update(_SUCCESS_RESET_PATCH)
    elif status_text == "failed":
        # skipped 是调度器主动暂缓、不算失败；只有真失败累计退避 streak。
        failed_window = (job.get("capture_window")
                         if isinstance(job.get("capture_window"), Mapping)
                         else job.get("window"))
        failed_window = failed_window if isinstance(failed_window, Mapping) else None
        if (failed_window is not None
                and not str(failed_window.get("after_message_id") or "")
                and failed_window.get("after_seq") in (None, "")):
            # 老任务（修复前入队）没带 after_seq：要不要补 0 取决于游标和聊天记录，
            # 判断和写入必须在同一个 Chat Clear fence 事务里做完（Codex 第 12 轮 I3）。
            recorded = _record_legacy_window_failure(
                store, job, failed_window, capture_key=capture_key, now_ts=now_ts)
            if recorded is None:
                # Chat Clear 已经删掉落卡状态：这是清空前的旧任务，什么都不写、不提示、不留痕迹。
                return load_capture_state(store)
            state, failed_window, streak, skipped = recorded
            if skipped:
                _record_skipped_window(store, window=failed_window, streak=streak,
                                       job_id=_capture_trace_job_id(job))
            return _after_capture_status_saved(
                store, job, state, status_text=status_text, skipped=skipped,
                cards_added=cards_added, now_ts=now_ts)
        patch, streak, skipped = _capture_failure_patch(
            state, failed_window, now_ts=now_ts, reason=_failure_reason_of(job))
        if skipped:
            # 🔴 同一批消息连续失败到阈值 —— 推过它。那批记忆就此丢掉，
            # 但这个用户后面还能继续记。见 CAPTURE_POISON_SKIP_AFTER。
            _record_skipped_window(store, window=failed_window, streak=streak,
                                   job_id=_capture_trace_job_id(job))
        state.update(patch)
    state = save_capture_state(store, state, now=now_ts)
    return _after_capture_status_saved(
        store, job, state, status_text=status_text, skipped=skipped,
        cards_added=cards_added, now_ts=now_ts)


def _after_capture_status_saved(store, job: Mapping[str, Any], state: Mapping[str, Any], *,
                                status_text: str, skipped: bool, cards_added: int,
                                now_ts: float) -> dict[str, Any]:
    """V1 落卡终态写进状态**之后**：提示、刷新游标、调试痕迹。"""
    capture_jobs.notify_backoff(store, lane="capture", status=status_text,
                                account_code=str(state.get("capture_account_error_code") or ""),
                                streak=int(state.get("capture_fail_streak") or 0),
                                skipped=skipped)
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
        reason = _trace_safe_reason(job)
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
        reason = _trace_safe_reason(job)
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
