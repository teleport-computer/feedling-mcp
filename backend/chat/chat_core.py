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
import time
import uuid

import db
import debug_trace
from bootstrap import gates as boot_gates
from chat import consumer as chat_consumer
from chat import service as chat_service
from core import wake_bus
from core.store import UserStore
from proactive import service as proactive_service
from push import service as push_service

_ENVELOPE_REQUIRED = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]


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
    with store.chat_lock:
        # Scrub verify-loop liveness rows from the visible transcript:
        #   - the REPLY (agent/openclaw) is always hidden — a reply that outlives
        #     verify_loop's GC window would otherwise leak as a stray "__verify_ack__".
        #   - the PING (user-role) is KEPT WHILE FRESH: the enclave decrypt proxy
        #     reuses this very route (enclave/routes/chat.py -> backend_client
        #     .backend_get("/v1/chat/history")) to deliver the ping to the resident
        #     consumer, which detects it by source. It is hidden ONLY once stale
        #     (see _hide_verify_ping_from_feed), so a ping whose verify_loop GC was
        #     skipped (mid-run SIGTERM) self-heals out of the feed instead of
        #     lingering as a visible "__VERIFY_PING__:..." bubble.
        all_msgs = [
            m for m in store.chat_messages
            if not _hide_verify_ping_from_feed(m, now)
        ]
        total = len(all_msgs)

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
    msg = store.append_chat(
        "user", "chat", envelope,
        content_type=content_type,
        extra=file_extra or None,
    )
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


def gate_response_dict(store: UserStore, allow_verify_reply: bool):
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
        thinking_extra.update(chat_service._chat_thinking_metadata_from_payload(payload))
    else:
        thinking_extra.update(chat_service._chat_plaintext_thinking_extra_for_store(store, payload))
    source = str(payload.get("source") or "chat").strip() or "chat"
    # "verify_ping": the resident consumer stamps its synthetic liveness reply
    # with this source so the visible /v1/chat/history feed can filter it out
    # (and verify_loop's GC can match it) regardless of GC timing. It is never a
    # user-visible message.
    if source not in {"chat", "live_activity", "heartbeat", "verify_ping", proactive_service.PROACTIVE_JOB_SOURCE}:
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
    reply_to_message_id = _reply_to_message_id(payload)
    if reply_to_message_id and role != "system":
        _parent = _chat_message_by_id(store, reply_to_message_id)
        if _parent is not None and (
            _parent.get("reply_status") == "replied" or _parent.get("reply_message_id")
        ):
            # Reply-exclusivity guard (delivery exclusivity is the claim CAS's job).
            # If this turn was ALREADY answered — e.g. THIS consumer's claim expired
            # mid-turn (>CHAT_POLL_CLAIM_TTL_SEC), the lease failed over, and the new
            # consumer already replied — don't append a duplicate reply and
            # double-burn the user's model key. Drop it with 409. (Guarding only on
            # already-replied, not claim ownership, so a legit reply that omits its
            # consumer_id is never rejected.)
            return {"error": "already_answered", "reply_status": "replied"}, 409
    msg = store.append_chat(
        role,
        source,
        envelope,
        content_type=content_type,
        extra=extra,
    )
    if reply_to_message_id and role != "system":
        store.update_chat_message_metadata(reply_to_message_id, {
            "reply_status": "replied",
            "reply_message_id": str(msg.get("id") or ""),
            "replied_by": consumer_id,
            "replied_at": f"{time.time():.3f}",
        })
        _maybe_mark_first_chat_ok(store, reply_to_message_id)
    delivery_fields: dict = {}
    visible_push_body = (push_body or alert_body).strip()
    # Defense-in-depth: a synthetic verify-loop liveness reply must NEVER surface
    # as a push / Live Activity, no matter what the caller passed. The resident
    # consumer already sends suppress_push=True, so this changes nothing today —
    # it just closes the gap if a future caller regression posts source=verify_ping
    # with a body.
    if source == "verify_ping":
        visible_push_body = ""
    # Any plaintext AI reply supplied by the caller enters the same app-state
    # policy: background/unknown app state gets Live Activity + APNs alert;
    # foreground app state records a suppression instead of interrupting.
    if source != "verify_ping" and (visible_push_body or payload.get("push_live_activity")):
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
        boot_gates._log_bootstrap_event(store, "chat_loop_verified", success=True)

    # Cleanup: remove synthetic ping from history regardless of outcome. If a
    # reply landed, also remove the matching agent response. The verify exchange
    # is a private liveness test; it must not open Chat as the user's visible
    # "First message."
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

    return {
        "loop_alive": found_reply,
        "response_time_sec": response_time,
        "ping_id": ping_uuid,
        "timeout_sec": timeout_sec,
        "suggestions": suggestions,
        "passing": found_reply,
    }, 200
