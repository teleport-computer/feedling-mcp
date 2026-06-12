"""Bootstrap completion gates for chat / identity writes."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

import db
from core.store import UserStore

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




_SKILL_URL = "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md"


def _bootstrap_state(store) -> dict:
    """Snapshot of bootstrap completion for `store`. Read-only; safe to call
    on every write path. Source of truth: on-disk identity + memory files.

    Returns:
        {
          memory_count: int,                # total across tabs
          memory_floor: int,                # total floor (back-compat)
          counts: {story, about_me, ta_thinking, total},
          floors: {story, about_me, ta_thinking, total},
          identity_written: bool,
          stage: str ∈ {"needs_memory", "needs_identity", "main_loop"},
          missing_tabs: [tab_name, ...]     # Which tab floors are unmet
        }

    Gate semantics (post-typed-memory rewrite):
      - "needs_memory" means Story floor OR About me floor not yet met.
        TA 在想 (insight/reflection) is encouraged but not blocking —
        reflections need substrate from the other two tabs first, so
        gating on it would create a deadlock at low-density tiers.
    """
    moments = memory_service._load_moments(store)
    counts = memory_service._count_by_tab(moments)
    identity_written = identity_service._load_identity(store) is not None
    floors = memory_service._per_tab_floors_for_days(identity_service._relationship_age_days(store))

    missing_tabs = []
    if counts["story"] < floors["story"]:
        missing_tabs.append("story")
    if counts["about_me"] < floors["about_me"]:
        missing_tabs.append("about_me")

    if missing_tabs:
        stage = "needs_memory"
    elif not identity_written:
        stage = "needs_identity"
    else:
        stage = "main_loop"

    return {
        "memory_count": counts["total"],
        "memory_floor": floors["total"],
        "counts": counts,
        "floors": floors,
        "identity_written": identity_written,
        "stage": stage,
        "missing_tabs": missing_tabs,
    }


def _gate_required_for_missing_tabs(state) -> str:
    """Human-readable instruction string for the missing tabs in `state`."""
    c = state["counts"]
    f = state["floors"]
    parts = []
    if "story" in state["missing_tabs"]:
        parts.append(
            f"Story tab {c['story']}/{f['story']} — write more moment/quote memories"
        )
    if "about_me" in state["missing_tabs"]:
        parts.append(
            f"About me tab {c['about_me']}/{f['about_me']} — write more fact/event memories "
            f"(this is the density layer — preferences, relationships, dates, habits)"
        )
    return (
        "Per-tab memory floors are below threshold: "
        + "; ".join(parts)
        + ". Use feedling_memory_add_moment(type=...) for each. Then call "
        "feedling_identity_init. Do not fabricate Pass 4 summaries — the cards "
        "must actually exist."
    )


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
        consumer_state = chat_consumer._consumer_validation_state(store)
        if not consumer_state["passing"]:
            print(
                f"[gate:{store.user_id}] chat_response blocked stage=needs_resident_consumer "
                f"consumer={consumer_state.get('consumer_name')} recent={consumer_state.get('age_sec')}"
            )
            return jsonify({
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
            return jsonify({
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
    if state["stage"] == "needs_memory":
        required = _gate_required_for_missing_tabs(state)
    else:  # needs_identity
        required = (
            "Call feedling_identity_init with the derived identity card "
            "(7 dimensions + days_with_user) BEFORE you can post chat."
        )
    print(f"[gate:{store.user_id}] chat_response blocked stage={state['stage']} "
          f"missing={state['missing_tabs']} id={state['identity_written']}")
    return jsonify({
        "error": "bootstrap_incomplete",
        "stage": state["stage"],
        "memory_count": state["memory_count"],
        "memory_floor": state["memory_floor"],
        "counts": state["counts"],
        "floors": state["floors"],
        "missing_tabs": state["missing_tabs"],
        "identity_written": state["identity_written"],
        "required": required,
        "skill_url": _SKILL_URL,
    }), 409


def _gate_bootstrap_for_identity_init(store):
    """Refuse /v1/identity/init when Story or About me tab floors are unmet.

    Identity must be DERIVED from memory substrate — writing identity in the
    30+ day tier with only 2 cards means the Agent skipped the depth pass.
    TA 在想 floor is advisory at this gate (reflections need other-tab
    substrate first, gating on it would deadlock low-density users).
    """
    state = _bootstrap_state(store)
    if not state["missing_tabs"]:
        return None
    print(f"[gate:{store.user_id}] identity_init blocked missing={state['missing_tabs']} "
          f"counts={state['counts']} floors={state['floors']}")
    return jsonify({
        "error": "bootstrap_incomplete",
        "stage": "needs_memory",
        "memory_count": state["memory_count"],
        "memory_floor": state["memory_floor"],
        "counts": state["counts"],
        "floors": state["floors"],
        "missing_tabs": state["missing_tabs"],
        "required": _gate_required_for_missing_tabs(state)
                    + " Identity dimensions must be derived from real cards, not invented.",
        "skill_url": _SKILL_URL,
    }), 409
