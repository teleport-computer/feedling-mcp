"""Chat live-loop verification: /v1/chat/verify_loop."""

import base64
import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

import db
from core.store import UserStore
from flask import Blueprint, Response
import threading

from accounts import auth
from bootstrap import gates as boot_gates

bp = Blueprint("chat_verify", __name__)

@bp.route("/v1/chat/verify_loop", methods=["POST"])
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
    store = auth.require_user()
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
        boot_gates._log_bootstrap_event(store, "chat_loop_verified", success=True)

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

