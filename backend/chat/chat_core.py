"""Framework-neutral chat write/read core (ASGI-migration plan §7.4 / §9.1).

The message / response / history / history-clear / message-body / verify-loop
logic for the resident chat line, lifted out of the Flask routes so the FastAPI
async routes reuse **identical** semantics — same envelope validation, same
append / claim, the SAME wake calls (``store.notify_chat_waiters`` /
``wake_bus.notify``) fired at the SAME points, the same ``debug_trace`` events.
Only the ``/poll`` wait primitive stays framework-specific (see ``poll_core`` /
``routes_asgi``).

E2E boundary: chat messages are v1 ciphertext envelopes; the server NEVER
decrypts. Every function here takes a ``UserStore`` + already-parsed params and
returns a plain ``(body_dict, status)`` — there is no ``flask.request`` in this
module, so the Flask adapter (``routes.py``) and the ASGI adapter
(``routes_asgi.py``) both delegate here and the wakes fire identically on every
write path.

Wake calls preserved (byte-for-byte with the old Flask routes):
- ``write_message``  → ``store.notify_chat_waiters()`` after the append.
- ``clear_history``  → ``store.notify_chat_waiters()`` + ``wake_bus.notify``.
- ``verify_loop``    → ``store.notify_chat_waiters()`` after the synthetic ping.
- ``write_response`` → NO explicit notify (matches Flask; the resident consumer
  reply is picked up via /history, and ``append_chat`` still fires its own
  cross-worker ``wake_bus.notify`` internally, exactly as before).
"""

from __future__ import annotations

import base64
import logging
import time
import uuid

import db
import debug_trace
from accounts import onboarding as accounts_onboarding
from bootstrap import gates as boot_gates
from chat import consumer as chat_consumer
from chat import idempotency as chat_idempotency
from chat import service as chat_service
from core import envelope as core_envelope
from core import wake_bus
from core.store import UserStore
from notices import catalog as notices_catalog
from notices import core as notices_core
from proactive import service as proactive_service
from push import service as push_service

log = logging.getLogger(__name__)

_ENVELOPE_REQUIRED = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]


def _stale_key_conflict(store: UserStore, envelope: dict) -> tuple[dict, int] | None:
    """409 when a LABELED envelope was sealed to a key that is not the user's
    current registered content key.

    ``content_pk_fpr`` is written by ``build_envelope`` at seal time, so a
    mismatch means the writer's key cache is stale (e.g. a resident consumer
    whose whoami refresh has been failing since before a key rotation —
    usr_f13f 2026-07-16). Storing such a row is worse than bouncing it: the
    device can never open it, and only a later client-triggered rewrap can
    repair it. The writer is expected to re-fetch whoami and retry.

    Unlabeled envelopes pass (older clients), as does everything when the user
    has no registered key to compare against.
    """
    labeled = str(envelope.get("content_pk_fpr") or "").strip()
    if not labeled:
        return None
    registered = core_envelope.get_user_public_key(store.user_id)
    if not registered:
        return None
    current = core_envelope._content_public_key_fingerprint(registered)
    if labeled == current:
        return None
    return {
        "error": "content_pk_fpr_mismatch",
        "message": "Envelope was sealed to a key that is no longer the user's "
                   "registered content key. Re-fetch whoami and re-seal.",
        "current_public_key_fpr": current,
        "envelope_content_pk_fpr": labeled,
    }, 409


# --------------------------------------------------------------------------- #
# proactive push-delivery decision (moved verbatim from the Flask route; pure
# store logic, no flask.request)
# --------------------------------------------------------------------------- #

def _settings_v2_for_store(store: UserStore):
    try:
        from proactive.store_v2 import DBProactiveSettingsStoreV2

        return DBProactiveSettingsStoreV2().load(store.user_id)
    except Exception:
        return store.load_proactive_settings()


def _proactive_job_for_response(store: UserStore, job_id: str) -> dict | None:
    if not job_id:
        return None
    try:
        for job in store.list_proactive_jobs(since_epoch=0, limit=0):
            if str(job.get("job_id") or "") == str(job_id):
                return job
    except Exception:
        return None
    return None


def _proactive_delivery_decision_v2(store: UserStore, payload: dict):
    from proactive.controls_v2 import evaluate_delivery_v2

    source = "heartbeat"
    manual = False
    job = _proactive_job_for_response(store, str(payload.get("proactive_job_id") or ""))
    if job:
        try:
            from proactive.adapters_v2 import wake_event_v2_from_legacy_job

            event = wake_event_v2_from_legacy_job(store.user_id, job)
            source = event.source
            manual = event.manual
        except Exception:
            manual = bool(job.get("manual"))
    manual = manual or bool(payload.get("manual") or payload.get("manual_wake") or payload.get("user_initiated"))
    return evaluate_delivery_v2(_settings_v2_for_store(store), source=source, manual=manual)


# --------------------------------------------------------------------------- #
# debug-trace helpers (moved verbatim from the Flask route; pure store logic)
# --------------------------------------------------------------------------- #

def _turn_failure_attribution(error_class: str, payload: dict) -> tuple[str, str]:
    """回合失败的归责与用户文案 —— **服务端按 error_class 查表，不信 payload**。

    归责红线（docs/FRONTEND_ERROR_CONTRACT.md §二）：provider 的错要抛给用户，
    但不是他的错**绝不能赖给他**。透传 payload 的 blame 意味着一个写错/被改的
    consumer 能把我们自己的故障标成 user_provider，让用户白跑一趟改配置——这
    正是红线要防的那件事。catalog.blame_for 对未知 error_class 安全落 system，
    与另外两条通道（notices.core 丢弃坏枚举、asgi.responses 直接 raise）同纪律。

    user_text 同理：注释和 OpenAPI 都宣称「服务端组好、绝不含原始 provider
    detail」，那就必须真由服务端组——payload 里的任意 500 字撑不起这个保证
    （截断不是脱敏）。catalog 的文案与 consumer 分类器由
    tests/test_catalog_consumer_parity.py 锁住不漂移。

    payload 里的同名字段只在 catalog 未收录该 error_class 时作降级兜底，且
    blame 仍强制落在合法枚举内。
    """
    blame = notices_catalog.blame_for(error_class)
    user_text = notices_catalog.user_text_for(error_class)
    if error_class not in notices_catalog.ERROR_CLASSES:
        # 未收录的新 slug：文案退回 poster 提供的（老后端 + 新 consumer 的过渡期），
        # 但 blame 绝不退让——非法值一律 system，宁可我们背锅也不误导用户。
        fallback_text = str(payload.get("turn_failure_user_text") or "")[:500]
        if fallback_text:
            user_text = fallback_text
        raw_blame = str(payload.get("turn_failure_blame") or "")
        if raw_blame in notices_core.VALID_BLAME:
            blame = raw_blame
    return blame, user_text


def _reply_to_message_id(payload: dict) -> str:
    """The reply target id from any of the accepted payload aliases (trimmed)."""
    return str(
        payload.get("reply_to_message_id")
        or payload.get("reply_to_id")
        or payload.get("in_reply_to")
        or ""
    ).strip()


def _chat_message_by_id(store: UserStore, msg_id: str) -> dict | None:
    msg_id = str(msg_id or "").strip()
    if not msg_id:
        return None
    with store.chat_lock:
        for msg in store.chat_messages:
            if str(msg.get("id") or "") == msg_id:
                return dict(msg)
    return None


def _is_resident_maintenance_reply(store: UserStore, payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("source") or "").strip() != "resident_maintenance":
        return False
    parent = _chat_message_by_id(store, _reply_to_message_id(payload))
    return bool(
        parent
        and str(parent.get("role") or "") == "user"
        and str(parent.get("source") or "") == "resident_maintenance"
    )


FIRST_CHAT_OK_USER_SOURCES = {"chat", "model_api"}


def _maybe_mark_first_chat_ok(store: UserStore, reply_to_message_id: str) -> None:
    user_msg = _chat_message_by_id(store, reply_to_message_id)
    if not user_msg:
        return
    if (
        str(user_msg.get("role") or "") == "user"
        and str(user_msg.get("source") or "") in FIRST_CHAT_OK_USER_SOURCES
    ):
        store.mark_first_chat_ok()


def _maybe_enqueue_resident_introduction(store) -> None:
    """Trigger the resident one-shot greeting on a verified chat loop.

    Called from ``verify_loop`` when a synthetic ping got an agent reply
    (``chat_loop_verified``). This is what breaks the fresh-resident deadlock:
    the greeting is enqueued HERE, NOT on ``first_chat_ok_at`` — a brand-new
    resident user cannot send a real message until the greeting opens Chat, and
    the verify ping deliberately does not count as their First message.

    ONLY the resident route triggers here (model-API / official-app greetings
    come from their own paths). The supervisor spawn/autoverify path stays a
    recovery fallback; BOTH route through the SAME atomic enqueue-once
    (``store.claim_and_enqueue_introduction``) so a user who later has a real
    conversation never gets a second introduction. Best-effort: a failed enqueue
    must never fail the verify response — the DB transaction rolls itself back."""
    try:
        from accounts import onboarding as accounts_onboarding
        from agent_runtime import introduction as agent_introduction
        if accounts_onboarding._load_onboarding_route(store) != "resident":
            return
        agent_introduction.enqueue_introduction_once(store, now=time.time())
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("feedling.chat_core").warning(
            "chat_loop_verified introduction enqueue failed for %s: %s",
            getattr(store, "user_id", ""), exc)


def _plaintext_for_trace(payload: dict, envelope: dict) -> str:
    """Best-effort plaintext for the debug excerpt ONLY. The server never
    decrypts; use a client-provided preview if present, else empty."""
    return str(payload.get("debug_preview") or envelope.get("synthetic_marker") or "")[:1000]


# --------------------------------------------------------------------------- #
# GET /v1/chat/history
# --------------------------------------------------------------------------- #

# A verify-loop PING is delivered to the enclave-backed resident consumer via
# /v1/chat/history while it is FRESH (the consumer detects it by source). Once
# older than this it is a dead probe — verify_loop capped its own wait at ≤60s
# and then GC's the ping, so anything past this window is a leaked row whose GC
# was skipped (e.g. a mid-run SIGTERM). Hiding stale pings makes a leaked
# `__VERIFY_PING__:...` self-heal out of the visible transcript without starving
# a live consumer of a fresh ping. 2× the 60s verify cap gives clock-skew margin.
VERIFY_PING_VISIBLE_TTL_SEC = 120.0


def _hide_verify_ping_from_feed(m: dict, now: float) -> bool:
    """Whether a ``source='verify_ping'`` row must be hidden from the visible
    (iOS-facing) chat feed. The synthetic liveness REPLY (agent/openclaw) is
    always hidden. The PING (user-role) is kept only while fresh (see
    ``VERIFY_PING_VISIBLE_TTL_SEC``), so a fresh ping still reaches enclave-backed
    consumers but a stale/leaked one drops out of the transcript."""
    if m.get("source") != "verify_ping":
        return False
    if m.get("role") in ("agent", "openclaw"):
        return True
    try:
        ts = float(m.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return (now - ts) > VERIFY_PING_VISIBLE_TTL_SEC


def _visible_msgs_and_raw_max(store: UserStore, now: float) -> tuple[list, float]:
    """Snapshot the visible transcript + the RAW newest ts under chat_lock.

    Visible list scrubs verify-loop liveness rows:
      - the REPLY (agent/openclaw) is always hidden — a reply that outlives
        verify_loop's GC window would otherwise leak as a stray "__verify_ack__".
      - the PING (user-role) is KEPT WHILE FRESH: the enclave decrypt proxy
        reuses this very route (enclave/routes/chat.py -> backend_client
        .backend_get("/v1/chat/history")) to deliver the ping to the resident
        consumer, which detects it by source. It is hidden ONLY once stale
        (see _hide_verify_ping_from_feed), so a ping whose verify_loop GC was
        skipped (mid-run SIGTERM) self-heals out of the feed instead of
        lingering as a visible "__VERIFY_PING__:..." bubble.

    ``raw_max_ts`` is computed over the UNFILTERED ring — it feeds the staleness
    probe (_self_heal_if_stale), which must compare against everything the store
    actually holds: comparing against the visible list would see a hidden verify
    row as "missing" and re-reload on every empty poll."""
    with store.chat_lock:
        raw_max_ts = max(
            (float(m.get("ts", 0) or 0) for m in store.chat_messages), default=0.0
        )
        visible = [
            m for m in store.chat_messages
            if not _hide_verify_ping_from_feed(m, now)
        ]
    return visible, raw_max_ts


def _self_heal_if_stale(store: UserStore, since: float, *, label: str = "chat/history") -> bool:
    """Read-time staleness probe + in-place heal (2026-07-15 延迟诊断报告 P1).

    Multi-worker reads serve from each worker's in-memory store; cross-worker
    freshness rides on LISTEN/NOTIFY. A missed broadcast (listener down for a
    blip, worker recycled before its LISTEN was up) previously meant this worker
    served stale chat for up to STORE_CACHE_TTL_SECONDS (15 min) — push arrives,
    chat page doesn't. Reload in place only when the DB genuinely holds rows this
    worker's ring is missing in the ``ts > since`` window.

    Counts, not max-ts: an earlier version compared the DB's newest ts against
    the ring's newest ts, which is blind to a *missing middle* row — a dropped
    broadcast for a message that is NOT the newest (user sends two, the first's
    NOTIFY is lost, this worker sees the second and replies, so both sides' newest
    ts are that reply and look equal). Comparing the per-window COUNT catches a
    gap anywhere in the window. Both counts include hidden rows (verify_ping):
    comparing raw-to-raw, so a hidden row is not mistaken for a missing one.
    Still cheap: a capped COUNT on the hot since-poll path. Fail-open: a probe
    error returns the (possibly stale) answer rather than 500."""
    from core import store as core_store  # lazy: chat_core imports UserStore only

    with store.chat_lock:
        mem_count = sum(
            1 for m in store.chat_messages if float(m.get("ts", 0) or 0) > since
        )
    try:
        db_count = db.chat_count_since(
            store.user_id, since, cap=core_store.MAX_CHAT_MESSAGES
        )
    except Exception as e:  # noqa: BLE001 — probe is best-effort by contract
        print(f"[{label}:{store.user_id}] stale probe failed (fail-open): {e}")
        return False
    if db_count <= mem_count:
        return False

    core_store._evict_store(store.user_id)
    print(
        f"[{label}:{store.user_id}] stale store self-healed "
        f"(db rows since={db_count} > mem rows since={mem_count})"
    )
    return True


def history(store: UserStore, *, query, user_agent: str, remote_addr: str) -> tuple[dict, int]:
    try:
        limit = int(query.get("limit", 200))
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    limit = max(1, min(limit, 200))

    try:
        since = float(query.get("since", 0))
    except (TypeError, ValueError):
        return {"error": "invalid since"}, 400

    before_raw = query.get("before", "")
    before = 0.0
    if before_raw not in ("", None):
        try:
            before = float(before_raw)
        except (TypeError, ValueError):
            return {"error": "invalid before"}, 400

    include_image_body = str(
        query.get("include_image_body", query.get("include_image_bodies", "true"))
    ).lower() not in {"0", "false", "no", "off"}

    now = time.time()
    all_msgs, raw_max_ts = _visible_msgs_and_raw_max(store, now)
    total = len(all_msgs)

    if before > 0:
        filtered = [m for m in all_msgs if float(m.get("ts", 0)) < before]
        msgs = filtered[-limit:]
        has_more_older = len(filtered) > len(msgs)
        has_more_newer = False
        page_mode = "before"
    elif since > 0:
        filtered = [m for m in all_msgs if float(m.get("ts", 0)) > since]
        # Probe on EVERY since-window, not only the empty ones: one cached row
        # is enough to make a partially-stale window look like a real answer,
        # and the missing rows then wait out the 15-min TTL (2026-07-22). The
        # probe is already the cheap side — it only reloads when the DB holds
        # rows this ring is missing in the window, so a fresh cache pays a single
        # capped COUNT and nothing else. Pure read: unlike the poll path there is
        # no claim to be non-idempotent about, so re-answering from the healed
        # ring is safe.
        if _self_heal_if_stale(store, since):
            # Cross-worker staleness healed in place — re-read and re-answer so
            # THIS response already carries the recovered rows (the whole point:
            # the user must not wait for the next poll, let alone the 15-min TTL).
            all_msgs, raw_max_ts = _visible_msgs_and_raw_max(store, now)
            total = len(all_msgs)
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

    # Pull this page's R2-offloaded bodies concurrently before rendering; without
    # it each one costs a serial round-trip inside _chat_history_item.
    msgs = chat_service.hydrate_history_page(msgs, include_image_body=include_image_body)
    out = [chat_service._chat_history_item(m, include_image_body=include_image_body) for m in msgs]
    omitted_bodies = sum(1 for m in out if m.get("body_omitted"))
    omitted_image_bodies = sum(
        1
        for m in out
        if m.get("body_omitted") and m.get("content_type", "text") == "image"
    )
    oldest_ts = float(out[0].get("ts", 0)) if out else 0
    latest_ts = float(out[-1].get("ts", 0)) if out else 0

    print(
        f"[chat/history:{store.user_id}] ip={remote_addr} mode={page_mode} "
        f"since={since} before={before} limit={limit} returned={len(out)} total={total} "
        f"include_image_body={include_image_body} omitted_bodies={omitted_bodies} "
        f"omitted_images={omitted_image_bodies} ua={user_agent[:80]}"
    )

    return {
        "messages": out,
        "total": total,
        "oldest_ts": oldest_ts,
        "latest_ts": latest_ts,
        "has_more_older": has_more_older,
        "has_more_newer": has_more_newer,
        "bodies_omitted": omitted_bodies,
        "image_bodies_omitted": omitted_image_bodies,
        "body_omit_inline_max": chat_service.CHAT_HISTORY_INLINE_BODY_CT_MAX,
    }, 200


# --------------------------------------------------------------------------- #
# DELETE /v1/chat/history
# --------------------------------------------------------------------------- #

def clear_history(store: UserStore, payload: dict) -> tuple[dict, int]:
    """Clear only the caller's chat transcript.

    This intentionally does not touch memory, identity, frames, API keys, or
    onboarding route state. The destructive account reset endpoint remains the
    only path that wipes the whole user record.
    """
    confirm = (payload.get("confirm") or "").strip()
    if confirm != "clear-chat-history":
        return {
            "error": "confirmation_required",
            "detail": "DELETE body must include {\"confirm\": \"clear-chat-history\"}."
        }, 400

    deleted = db.chat_clear(store.user_id)
    if deleted is None:
        return {"error": "chat_clear_failed"}, 500

    with store.chat_lock:
        store.chat_messages = []

    store.notify_chat_waiters()
    # Cross-worker: other workers still hold the now-cleared messages in cache —
    # refresh them (a delete isn't a new-message append, so it won't route
    # through append_chat's notify).
    wake_bus.notify("chat", store.user_id)
    print(f"[chat/clear:{store.user_id}] deleted={deleted}")
    return {"cleared": True, "deleted": deleted}, 200


# --------------------------------------------------------------------------- #
# GET /v1/chat/messages/<message_id>/body
# --------------------------------------------------------------------------- #

def message_body(store: UserStore, message_id: str) -> tuple[dict, int]:
    with store.chat_lock:
        msg = next((m for m in store.chat_messages if str(m.get("id") or "") == str(message_id)), None)
    # A verify-loop synthetic row is never a legitimate single-body fetch target;
    # refuse it here too so a leaked ping id can't be re-fetched out-of-band.
    if not msg or msg.get("source") == "verify_ping":
        return {"error": "message_not_found"}, 404
    return {"message": chat_service._chat_history_item(msg, include_image_body=True)}, 200


# --------------------------------------------------------------------------- #
# POST /v1/chat/message  (user sends a v1 ciphertext envelope)
# --------------------------------------------------------------------------- #

def write_message(store: UserStore, payload: dict) -> tuple[dict, int]:
    """User sends a chat message as a v1 ciphertext envelope.

    See docs/DESIGN_E2E.md §3.2 for envelope field definitions. The server never
    decrypts the envelope — it is stored verbatim and later surfaced by the
    enclave's /v1/* handlers.
    """
    client_msg_id, client_msg_id_err = chat_idempotency.parse_client_msg_id(payload)
    if client_msg_id_err is not None:
        return client_msg_id_err
    envelope = payload.get("envelope")
    if envelope is None:
        return {"error": "envelope required"}, 400
    missing = [f for f in _ENVELOPE_REQUIRED if not envelope.get(f)]
    if missing:
        return {"error": "envelope_missing_fields", "detail": missing}, 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return {"error": "envelope.visibility must be 'shared' or 'local_only'"}, 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return {"error": "envelope with visibility=shared requires K_enclave"}, 400
    conflict = _stale_key_conflict(store, envelope)
    if conflict is not None:
        return conflict
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image", "file"):
        return {"error": "content_type must be 'text', 'image', or 'file'"}, 400
    file_extra: dict = {}
    if content_type == "file":
        fname = str(payload.get("file_name") or "").strip()
        fmime = str(payload.get("file_mime") or "").strip()
        if fname:
            file_extra["file_name"] = fname[:120]
        if fmime:
            file_extra["file_mime"] = fmime[:120]
    inserted = True
    if client_msg_id is not None:
        msg, inserted = store.append_chat_idempotent(
            "user",
            "chat",
            envelope,
            client_msg_id=client_msg_id,
            window_sec=chat_idempotency.CLIENT_MSG_ID_WINDOW_SEC,
            content_type=content_type,
            extra=file_extra or None,
        )
    else:
        msg = store.append_chat(
            "user", "chat", envelope,
            content_type=content_type,
            extra=file_extra or None,
        )
    if inserted:
        store.notify_chat_waiters()
        debug_trace.trace_event(
            store,
            subsystem="route",
            type="chat.message",
            actor="ios",
            trace_id=msg["id"],
            turn_id=msg["id"],
            summary=f"user message stored id={msg['id']}",
            explain="收到用户消息，已入库并唤醒 resident consumer",
            detail={"content_type": content_type, "msg_id": msg["id"]},
            content_excerpt={"user_message": _plaintext_for_trace(payload, envelope)} if content_type == "text" else None,
        )
        print(f"[chat:{store.user_id}] user(v1, visibility={envelope['visibility']}, type={content_type}) id={msg['id']}")
    return {"id": msg["id"], "ts": msg["ts"], "v": msg["v"]}, 200


# --------------------------------------------------------------------------- #
# POST /v1/chat/response  (agent posts a reply as a v1 ciphertext envelope)
# --------------------------------------------------------------------------- #

def trace_response_gated(store: UserStore, payload: dict, allow_verify_reply: bool) -> None:
    """The ``chat.response.gated`` debug-trace event (shared by both adapters)."""
    reply_to_message_id = _reply_to_message_id(payload)
    debug_trace.trace_event(
        store,
        subsystem="route",
        type="chat.response.gated",
        actor="agent",
        status="blocked",
        trace_id=reply_to_message_id,
        turn_id=reply_to_message_id,
        summary="bootstrap_incomplete gate fired",
        detail={"allow_verify_reply": bool(allow_verify_reply)},
    )


def gate_response_dict(store: UserStore, allow_verify_reply: bool, payload: dict | None = None):
    """Bridge to the shared bootstrap gate.

    ``boot_gates._gate_bootstrap_for_chat`` returns a framework-neutral
    ``(body_dict, status)`` — or ``None`` when the call may proceed — so no flask
    application context is needed. Looked up on ``boot_gates`` at call time so test
    monkeypatches of ``_gate_bootstrap_for_chat`` are honored.

    Identity Card presence/content is not part of this gate. The remaining VPS
    checks prove that a resident consumer and live chat loop are available.
    """
    gated = boot_gates._gate_bootstrap_for_chat(
        store, allow_verify_reply=allow_verify_reply
    )
    if gated is None:
        return None
    if _is_resident_maintenance_reply(store, payload):
        return None
    body, status = gated
    return body, status


def write_response(
    store: UserStore,
    payload: dict,
    *,
    consumer_id: str,
    consumer_info: dict,
    allow_verify_reply: bool,
) -> tuple[dict, int]:
    """Agent posts a reply as a v1 ciphertext envelope. Shape matches
    /v1/chat/message. Caller (the adapter) has already evaluated the bootstrap
    gate; this handles consumer bookkeeping, envelope/thinking validation, the
    append, and the plaintext push-policy delivery.

    ``consumer_id`` is the stable responder id (``replied_by`` on a reply-claim);
    ``consumer_info`` is the X-Feedling-Consumer identity for the liveness state.
    Both are parsed framework-neutrally by the adapter.
    """
    chat_consumer._record_consumer_event(store, "response", info=consumer_info)
    envelope = payload.get("envelope")
    if envelope is None:
        return {"error": "envelope required"}, 400
    missing = [f for f in _ENVELOPE_REQUIRED if not envelope.get(f)]
    if missing:
        return {"error": "envelope_missing_fields", "detail": missing}, 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return {"error": "envelope.visibility must be 'shared' or 'local_only'"}, 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return {"error": "envelope with visibility=shared requires K_enclave"}, 400
    conflict = _stale_key_conflict(store, envelope)
    if conflict is not None:
        return conflict
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image"):
        return {"error": "content_type must be 'text' or 'image'"}, 400
    thinking_envelope = payload.get("thinking_envelope")
    thinking_extra: dict = {}
    if thinking_envelope is not None:
        if not isinstance(thinking_envelope, dict):
            return {"error": "thinking_envelope must be an object"}, 400
        missing = [f for f in _ENVELOPE_REQUIRED if not thinking_envelope.get(f)]
        if missing:
            return {"error": "thinking_envelope_missing_fields", "detail": missing}, 400
        if thinking_envelope["visibility"] not in ("shared", "local_only"):
            return {"error": "thinking_envelope.visibility must be 'shared' or 'local_only'"}, 400
        if thinking_envelope["visibility"] == "shared" and not thinking_envelope.get("K_enclave"):
            return {"error": "thinking_envelope with visibility=shared requires K_enclave"}, 400
        conflict = _stale_key_conflict(store, thinking_envelope)
        if conflict is not None:
            return conflict
        thinking_extra = {
            "thinking_v": str(thinking_envelope.get("v", 1)),
            "thinking_id": str(thinking_envelope.get("id") or ""),
            "thinking_body_ct": str(thinking_envelope["body_ct"]),
            "thinking_nonce": str(thinking_envelope["nonce"]),
            "thinking_K_user": str(thinking_envelope["K_user"]),
            "thinking_visibility": str(thinking_envelope["visibility"]),
            "thinking_owner_user_id": str(thinking_envelope["owner_user_id"]),
            "thinking_enclave_pk_fpr": str(thinking_envelope.get("enclave_pk_fpr") or ""),
            "thinking_content_pk_fpr": str(thinking_envelope.get("content_pk_fpr") or ""),
        }
        if thinking_envelope.get("K_enclave"):
            thinking_extra["thinking_K_enclave"] = str(thinking_envelope["K_enclave"])
        thinking_extra.update(chat_service._chat_thinking_metadata_from_payload(payload))
    else:
        thinking_extra.update(chat_service._chat_plaintext_thinking_extra_for_store(store, payload))
    source = str(payload.get("source") or "chat").strip() or "chat"
    # "verify_ping": synthetic liveness reply, hidden from visible history.
    # "resident_maintenance": reply to a server-authored maintenance prompt; it
    # is visible in history but must not trigger push or bootstrap success.
    if source not in {
        "chat",
        "live_activity",
        "heartbeat",
        "verify_ping",
        "resident_maintenance",
        proactive_service.PROACTIVE_JOB_SOURCE,
    }:
        return {"error": "invalid source"}, 400
    # role: 消费者可声明 "system"（技术通知气泡，spec 2026-07-06-upstream-error-
    # surfacing）。白名单外一律落 openclaw——新增 role 前先过 spec 的 role 审计表。
    role = str(payload.get("role") or "openclaw").strip()
    if role not in ("openclaw", "system"):
        role = "openclaw"
    notice_kind = ""
    if role == "system":
        notice_kind = str(payload.get("notice_kind") or "")[:64]
    # Gate the hidden "verify_ping" source to an actual pending probe. Because
    # source="verify_ping" rows are scrubbed from the visible transcript, an
    # ordinary reply that (mis)used this source would silently vanish while still
    # touching push/metadata. Accept it ONLY as the answer to an outstanding
    # verify ping (allow_verify_reply, computed by the adapter). A late reply that
    # lands after verify_loop already GC'd its ping is correctly rejected here —
    # that round's verify has already concluded and the reply is unwanted.
    if source == "verify_ping" and not allow_verify_reply:
        return {"error": "verify_ping reply without a pending verify ping"}, 409
    alert_body = str(payload.get("alert_body") or "")
    push_body = str(payload.get("push_body") or "")
    extra = {
        "gate_decision_id": str(payload.get("gate_decision_id") or ""),
        "proactive_job_id": str(payload.get("proactive_job_id") or ""),
        **thinking_extra,
    }
    if notice_kind:
        extra["notice_kind"] = notice_kind
    if source == proactive_service.PROACTIVE_JOB_SOURCE:
        preview = (alert_body or push_body).strip()
        if preview:
            extra["alert_preview"] = preview[:240]
        if push_body.strip():
            extra["push_body_preview"] = push_body.strip()[:240]
        extra["push_live_activity_requested"] = bool(payload.get("push_live_activity"))
    turn_failure_error_class = str(payload.get("turn_failure_error_class") or "")[:64]
    reply_to_message_id = _reply_to_message_id(payload)
    if reply_to_message_id and role != "system":
        # Verify acks are hidden, but their exact parent link is load-bearing:
        # verify_loop must not accept an unrelated concurrent agent reply.
        if source == "verify_ping":
            extra["reply_to_message_id"] = reply_to_message_id
        # Turn-failure metadata（spec 2026-07-18 §2）：兜底回复是【实时载体】——它是
        # 新消息、有新 ts，能通过 /v1/chat/history 的 `since` 增量过滤；而对用户那条
        # 旧消息就地更新 metadata 不产生新 ts，永远进不了增量流。reply_to_message_id
        # 必须一并落在回复消息上，否则客户端在增量流里拿到失败事件却无法配对回它
        # 失败的那条用户消息。只做加法：不携带这些字段时本段完全不执行。
        #
        # 必须在 _build_chat_message 之前写进 extra —— 那一行之后 candidate 已定型，
        # 再改 extra 不会进入原子 CAS 的那条 INSERT（3d160bf9 的新结构）。
        turn_failure_blame = ""
        turn_failure_user_text = ""
        if turn_failure_error_class and source == "chat":
            turn_failure_blame, turn_failure_user_text = _turn_failure_attribution(
                turn_failure_error_class, payload
            )
            extra["turn_failure_error_class"] = turn_failure_error_class
            extra["turn_failure_blame"] = turn_failure_blame
            extra["turn_failure_user_text"] = turn_failure_user_text
            extra["reply_to_message_id"] = reply_to_message_id
        # Build the exact append_chat row immediately before the one-statement
        # parent-CAS + reply-INSERT.  No slow work belongs in this gap: two workers
        # may arrive together, and PostgreSQL decides the sole winner.
        candidate = store._build_chat_message(
            role,
            source,
            envelope,
            content_type=content_type,
            extra=extra,
        )
        replied_fields = {
            "reply_status": "replied",
            "reply_message_id": str(candidate.get("id") or ""),
            "replied_by": consumer_id,
            "replied_at": f"{float(candidate['ts']):.3f}",
        }
        # 冗余持久化到用户消息上（供全量 history / 重启后恢复）。权威载体仍是兜底
        # 回复消息本身；这里搭 finalize 的原子 CAS 顺风车，比旧结构的事后 metadata
        # 更新更可靠——旧写法在 parent 不在本 worker 内存时会静默丢失。
        if turn_failure_error_class and source == "chat":
            replied_fields["reply_error_class"] = turn_failure_error_class
            replied_fields["reply_blame"] = turn_failure_blame
            replied_fields["reply_user_text"] = turn_failure_user_text
        finalized = store.finalize_chat_reply_once(
            reply_to_message_id, candidate, replied_fields
        )
        if finalized is None:
            return {"error": "already_answered", "reply_status": "replied"}, 409
        _parent_doc, msg = finalized
        _maybe_mark_first_chat_ok(store, reply_to_message_id)
    else:
        # System notices bypass reply exclusivity by design, as do ordinary
        # response writes with no reply target.
        msg = store.append_chat(
            role,
            source,
            envelope,
            content_type=content_type,
            extra=extra,
        )
    delivery_fields: dict = {}
    visible_push_body = (push_body or alert_body).strip()
    # Defense-in-depth: synthetic/maintenance replies must NEVER surface as push
    # or Live Activity, no matter what the caller passed.
    if source in {"verify_ping", "resident_maintenance"}:
        visible_push_body = ""
    # Any plaintext AI reply supplied by the caller enters the same app-state
    # policy: background/unknown app state gets Live Activity + APNs alert;
    # foreground app state records a suppression instead of interrupting.
    if source not in {"verify_ping", "resident_maintenance"} and (
        visible_push_body or payload.get("push_live_activity")
    ):
        delivery = None
        if source == proactive_service.PROACTIVE_JOB_SOURCE:
            delivery = _proactive_delivery_decision_v2(store, payload)
        if delivery is not None and not delivery.allow_visible_delivery:
            delivery_fields.update({
                "push_decision": "suppressed",
                "push_reason": delivery.reason,
                "alert_status": "suppressed",
                "alert_reason": delivery.reason,
                "live_activity_status": "suppressed",
                "live_activity_reason": delivery.reason,
            })
        else:
            delivery_fields.update(push_service._deliver_ai_message_push_if_background(
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
    debug_trace.trace_event(
        store,
        subsystem="route",
        type="chat.response",
        actor="agent",
        trace_id=(reply_to_message_id or msg["id"]),
        turn_id=(reply_to_message_id or msg["id"]),
        summary=f"agent reply stored id={msg['id']} source={source}",
        explain=f"agent 回复已入库（source={source}）",
        detail={"source": source, "content_type": content_type, "msg_id": msg["id"]},
    )
    print(f"[chat:{store.user_id}] openclaw(v1, source={source}, type={content_type}) id={msg['id']}")
    return {"id": msg["id"], "ts": msg["ts"], "v": msg["v"]}, 200


# --------------------------------------------------------------------------- #
# POST /v1/chat/verify_loop
# --------------------------------------------------------------------------- #

def _verify_synthetic_ids_to_gc(messages) -> list[str]:
    """Ids of the synthetic verify-loop rows (ping + ack) safe to delete.

    ONLY ``source == 'verify_ping'`` rows qualify. A real reply that merely
    landed after the ping must NEVER be collected here: deleting it orphans its
    parent's ``reply_message_id`` (the hosted dangling-pointer lost-reply bug,
    2026-07-20). Every genuine ack carries ``source='verify_ping'``, so this
    loses no coverage.
    """
    return [
        str(m.get("id"))
        for m in messages
        if isinstance(m, dict)
        and m.get("source") == "verify_ping"
        and m.get("id")
    ]


def _verify_reply_matches_ping(message: dict, *, ping_id: str, ping_ts: float) -> bool:
    """Whether ``message`` is the hidden ack for this exact verify probe."""
    if not isinstance(message, dict):
        return False
    if message.get("role") not in ("agent", "openclaw"):
        return False
    if message.get("source") != "verify_ping":
        return False
    if str(message.get("reply_to_message_id") or "") != ping_id:
        return False
    try:
        return float(message.get("ts") or 0) > ping_ts
    except (TypeError, ValueError):
        return False


def verify_loop(store: UserStore, payload: dict) -> tuple[dict, int]:
    """Synthetic ping: insert a marker user message, wait up to ``timeout_sec``
    for an agent-role reply, return whether a reply pipeline is alive.

    Blocking (``time.sleep`` poll loop) by design — the adapter runs it off the
    event loop via the threadpool. See the original Flask docstring for the
    marker/GC semantics.
    """
    timeout_sec = min(int(payload.get("timeout_sec", 30)), 60)

    ping_uuid = uuid.uuid4().hex[:12]
    ping_marker = f"__VERIFY_PING__:{ping_uuid}"

    # Build a synthetic v1 envelope. Content is sentinel plaintext — not visible
    # to agent decryption pipelines (they see plaintext ping_marker via the normal
    # chat history endpoint). Visibility is local_only so we don't pollute the
    # enclave's shared store.
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

    # append_chat acquires chat_lock internally — don't hold it here or we'd
    # deadlock on the non-reentrant lock.
    ping_msg = store.append_chat("user", "verify_ping", synthetic_env)
    store.notify_chat_waiters()
    ping_ts = ping_msg["ts"]
    ping_id = str(ping_msg.get("id") or "")

    print(f"[verify_loop:{store.user_id}] posted synthetic ping {ping_uuid} at ts={ping_ts}")

    # Wait for the hidden reply tied to this exact ping. A concurrent ordinary
    # agent reply is not evidence that the synthetic probe was handled.
    deadline = time.time() + timeout_sec
    response_time = None
    found_reply = False
    while time.time() < deadline:
        time.sleep(2)
        with store.chat_lock:
            chat_msgs = list(store.chat_messages)
        for m in chat_msgs:
            if _verify_reply_matches_ping(m, ping_id=ping_id, ping_ts=ping_ts):
                response_time = float(m["ts"]) - ping_ts
                found_reply = True
                break
        if found_reply:
            break

    decrypt_health = None
    decrypt_policy = None
    route = accounts_onboarding._load_onboarding_route(store)
    if route not in {"model_api", "official_import"}:
        consumer_state = chat_consumer._consumer_validation_state(store)
        decrypt_health = consumer_state.get("decrypt_health") or (
            chat_consumer._decrypt_health_from_state({})
        )
        decrypt_policy = chat_consumer._decrypt_health_enforcement_state(
            store, consumer_state
        )
    decrypt_ready = not (
        decrypt_policy and decrypt_policy["blocks_verify"]
    )
    passing = found_reply and decrypt_ready

    if passing:
        boot_gates._log_bootstrap_event(store, "chat_loop_verified", success=True)
        _maybe_enqueue_resident_introduction(store)

    # Cleanup: remove the synthetic ping AND its ack from history regardless of
    # outcome. The verify exchange is a private liveness test; it must not open
    # Chat as the user's visible "First message."
    #
    # GC keys ONLY off source="verify_ping". Older code deleted "the first agent
    # reply after the ping", so a concurrent REAL reply could be removed and
    # leave its parent pointing at a missing reply row
    # (DIAGNOSIS_hosted_reply_dangling_pointer_2026-07-20). The success matcher
    # above is now stricter still (source + exact reply_to), while source remains
    # the safe collection boundary for both the ping and its hidden ack.
    with store.chat_lock:
        removed_ids = _verify_synthetic_ids_to_gc(store.chat_messages)
        removed_set = set(removed_ids)
        store.chat_messages = [
            m for m in store.chat_messages
            if not (isinstance(m, dict) and str(m.get("id") or "") in removed_set)
        ]
        for rid in removed_ids:
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
    if found_reply and not decrypt_ready and decrypt_health:
        suggestions.append(decrypt_health["required"])

    return {
        "loop_alive": found_reply,
        "response_time_sec": response_time,
        "ping_id": ping_uuid,
        "timeout_sec": timeout_sec,
        "suggestions": suggestions,
        "passing": passing,
        "reason": (
            decrypt_health["reason"]
            if found_reply and not decrypt_ready and decrypt_health
            else ""
        ),
        "decrypt_health": decrypt_health,
        "decrypt_health_policy": decrypt_policy,
    }, 200
