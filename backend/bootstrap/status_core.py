"""Framework-neutral /v1/bootstrap/status payload (ASGI-migration plan §7 / §9.4).

The onboarding progress signal, lifted out of the Flask route so the native
ASGI route reuses the exact same payload. Pure store/service reads — no
Flask/FastAPI request object. Reads several services (identity/memory/chat) so
ASGI callers run it on the threadpool.
"""

from __future__ import annotations

from datetime import datetime

from accounts import onboarding as accounts_onboarding
from bootstrap import gates as boot_gates
from chat import consumer as chat_consumer
from core.store import UserStore
from identity import service as identity_service
from memory import service as memory_service

# /v1/chat/response historically stamps role="openclaw" (legacy from when the
# only supported agent was OpenClaw). Treat both as agent-authored — see
# test_bootstrap_status_role_schema.
_AGENT_ROLES = ("agent", "openclaw")


def bootstrap_status_payload(store: UserStore) -> dict:
    """Server-observed bootstrap progress (no decryption / MCP heartbeat)."""
    identity = identity_service._load_identity(store)
    has_identity = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    identity_updated_at = (identity or {}).get("updated_at", "")

    moments = memory_service._load_moments(store)
    memory_count = len(moments) if isinstance(moments, list) else 0
    # Days-scaled TOTAL memory floor (consistency signal, NOT a gate — see
    # memory_service._memory_floor_for_days). A long relationship should have
    # a proportional number of memory cards; memory_below_floor just surfaces
    # the gap so the App/agent can see it. A fresh 0-day account sitting below
    # the trivial floor (2) is expected and fine — nothing here blocks
    # onboarding or chat.
    relationship_age_days = identity_service._relationship_age_days(store)
    memory_floor = memory_service._memory_floor_for_days(relationship_age_days)
    memory_aspiration = memory_service._memory_aspiration_for_days(relationship_age_days)
    memory_below_floor = bool(memory_count < memory_floor)
    last_moment_ts = ""
    if memory_count > 0:
        try:
            last_moment_ts = max(
                (m.get("created_at") or "") for m in moments if isinstance(m, dict)
            )
        except Exception:
            last_moment_ts = ""

    # chat_messages is mutated under chat_lock elsewhere; copy under the same
    # lock so we don't race with /v1/chat/response writes.
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    # Exclude synthetic verify-loop liveness replies (source="verify_ping",
    # role=agent/openclaw). /v1/chat/history hides these rows from the visible
    # transcript (_hide_verify_ping_from_feed), so counting them here as real
    # agent messages made bootstrap_status report agent_messages_count>=1 while
    # /chat/history returned total=0 — the App showed a "new message" bubble that
    # opened onto an empty chat. Every other real-message count in the codebase
    # (gates.py / chat.service / db.py) already excludes source=="verify_ping";
    # this line was the outlier. See test_bootstrap_status_ignores_verify_ping.
    agent_msgs = [
        m
        for m in chat_msgs
        if isinstance(m, dict)
        and m.get("role") in _AGENT_ROLES
        and m.get("source") != "verify_ping"
    ]
    agent_msg_count = len(agent_msgs)
    last_agent_msg_ts = ""
    if agent_msg_count > 0:
        # Chat ts is unix epoch float; identity/memory timestamps are ISO
        # strings. Normalise to ISO so the lexicographic max() picks the actual
        # latest event across all three signals.
        try:
            latest_unix = max(
                float(m.get("ts") or m.get("timestamp") or 0) for m in agent_msgs
            )
            last_agent_msg_ts = datetime.fromtimestamp(latest_unix).isoformat() if latest_unix > 0 else ""
        except Exception:
            last_agent_msg_ts = ""

    # chat_loop_verified — reply pipeline explicitly verified by
    # /v1/chat/verify_loop, or the agent responded to a real user message at
    # least once (agent_messages_count>=1 only proves the agent SPOKE).
    chat_loop_verified = boot_gates._chat_loop_verified_by_server(store)
    resident_consumer = chat_consumer._consumer_validation_state(store)

    agent_connected = has_identity or memory_count > 0 or agent_msg_count > 0
    candidate_ts = [t for t in (identity_updated_at, last_moment_ts, last_agent_msg_ts) if t]
    last_activity = max(candidate_ts) if (agent_connected and candidate_ts) else ""

    # Hosted model_api accounts have no independent resident consumer, so their
    # onboarding completion cannot require a resident heartbeat or verify_loop.
    # This mirrors the route exemption in gates._gate_bootstrap_for_chat
    # (gates.py:137-159). Other and legacy/missing routes retain the resident
    # completion contract. Memory is informational for every route.
    route = accounts_onboarding._load_onboarding_route(store)
    live_loop_complete = route == "model_api" or (
        resident_consumer["passing"] and chat_loop_verified
    )
    is_complete = (
        has_identity
        and agent_msg_count >= 1
        and live_loop_complete
    )

    return {
        "agent_connected": agent_connected,
        "last_agent_activity": last_activity,
        "identity_written": has_identity,
        "relationship_anchored": relationship_anchored,
        "memories_count": memory_count,
        "memory_floor": memory_floor,
        "memory_aspiration": memory_aspiration,
        "memory_below_floor": memory_below_floor,
        "agent_messages_count": agent_msg_count,
        "chat_loop_verified": chat_loop_verified,
        "resident_consumer_connected": resident_consumer["passing"],
        "resident_consumer": resident_consumer,
        "is_complete": is_complete,
    }
