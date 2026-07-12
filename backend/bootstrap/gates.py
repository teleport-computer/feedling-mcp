"""Bootstrap completion gates for chat / identity writes."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

import db
from core import util as core_util
from core.store import UserStore

from accounts import onboarding as accounts_onboarding
from chat import consumer as chat_consumer
from identity import service as identity_service
from memory import service as memory_service

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




_SKILL_URL = core_util.io_onboarding_skill_url("skill.md")


def _bootstrap_state(store) -> dict:
    """Snapshot of bootstrap completion for `store`. Read-only; safe to call
    on every write path. Identity and memory state are informational only.

    Returns:
        {
          memory_count: int,                # total across tabs
          memory_floor: int,                # total floor (back-compat)
          counts: {story, about_me, ta_thinking, total},
          floors: {story, about_me, ta_thinking, total},
          identity_written: bool,
          stage: "main_loop",
          missing_tabs: []                  # always empty (memory no longer gates)
        }

    Gate semantics (2026-07): neither Memory Garden nor Identity Card content
    gates whether the agent may speak. ``identity_written`` remains available
    to status/diagnostic consumers, but never drives ``stage``.
    """
    moments = memory_service._load_moments(store)
    counts = memory_service._count_by_tab(moments)
    identity_written = identity_service._load_identity(store) is not None
    floors = memory_service._per_tab_floors_for_days(identity_service._relationship_age_days(store))

    # Memory and identity stay visible for status display only. Neither drives
    # ``stage``; ``missing_tabs`` is retained for response-shape compatibility.
    missing_tabs: list[str] = []

    return {
        "memory_count": counts["total"],
        "memory_floor": floors["total"],
        "counts": counts,
        "floors": floors,
        "identity_written": identity_written,
        "stage": "main_loop",
        "missing_tabs": missing_tabs,
    }


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
        # Host (model_api) accounts don't run an independent resident consumer —
        # their chat loop is the supervisor-managed agent-runner, which posts via
        # /v1/model_api/chat/send (gate-free) and never stamps the official
        # `feedling-chat-resident` heartbeat. Requiring that heartbeat here is a
        # route mix-up: it 409s host accounts (e.g. an onboarding-validate job
        # posting its result through /v1/chat/response) with the resident-consumer
        # text even though their bootstrap is complete. Identity is already
        # written at main_loop, so let host accounts through.
        if accounts_onboarding._load_onboarding_route(store) == "model_api":
            return None
        consumer_state = chat_consumer._consumer_validation_state(store)
        if not consumer_state["passing"]:
            print(
                f"[gate:{store.user_id}] chat_response blocked stage=needs_resident_consumer "
                f"consumer={consumer_state.get('consumer_name')} recent={consumer_state.get('age_sec')}"
            )
            return ({
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
            return ({
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
    return None


def _gate_bootstrap_for_identity_init(store):
    """A' (2026-06): identity init is NO LONGER gated on memory floor.

    0 memory cards is a valid state — identity is the baseline that comes
    first; the Memory Garden grows naturally afterwards. Envelope /
    days_with_user / relationship_anchor_evidence validation still happens in
    the identity route itself (not here). Kept as a hook (always allows) so
    call sites stay stable.
    """
    return None
