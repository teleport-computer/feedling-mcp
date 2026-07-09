"""Framework-neutral chat long-poll core (ASGI-migration plan §7.4).

The "advertise runtime/release, check for pending chat, claim it, and shape the
response" logic for `/v1/chat/poll`, lifted out of the Flask route so the
forthcoming FastAPI async poll route (plan §9.1) reuses **identical** payload /
claim semantics. Only the *waiting* primitive stays in the route (Flask
`threading.Event` today, an asyncio waiter under ASGI); everything here is pure
store access with no Flask/FastAPI request object.

The pending computation already lives in `chat.service`; this module wraps it
with the delivery trace and locks the response contract.
"""

from __future__ import annotations

import db  # lowest layer — chat may depend downward on it. Do NOT import
# model_api_runtime.v2.jobs_store here: that's the write side owned by Plan
# B/C; poll_core's read side goes through db.list_agent_status_events, the
# single shared read primitive (spec §9 / dependency direction).
import debug_trace
from chat import consumer as chat_consumer
from chat import service as chat_service
from core.store import UserStore


def poll_context(store: UserStore) -> dict:
    """The runtime/release fields advertised on every poll response.

    ``runtime_v2`` tells the consumer which resident runtime profile is active;
    ``client_release`` tells a self-hosted consumer which commit to self-update
    to. Neither depends on whether there is pending chat.
    """
    from proactive import resident_runtime_v2  # lazy: chat poll must not own proactive startup
    from hosted import mcp_core  # lazy: chat poll must not own hosted startup

    return {
        "runtime_v2": resident_runtime_v2.resident_runtime_v2_public_profile(store),
        "client_release": {"expected_consumer_commit": chat_consumer.expected_consumer_commit()},
        "user_mcp": {"fingerprint": mcp_core.fingerprint_for_store(store)},
    }


def _trace_poll_delivered(store: UserStore, pending: list, *, consumer_id: str, claim: bool) -> None:
    if not pending:
        return
    debug_trace.trace_event(
        store,
        subsystem="route",
        type="chat.poll.delivered",
        actor="consumer",
        summary=f"delivered {len(pending)} message(s) to consumer",
        detail={
            "count": len(pending),
            "consumer_id": consumer_id,
            "claimed": bool(claim),
        },
    )


def pending_messages(store: UserStore, *, since: float, consumer_id: str, claim: bool) -> list:
    """Pending chat messages for this consumer (claiming them when ``claim``).

    Records the delivery trace when non-empty — the exact behavior of the legacy
    route's ``_trace_chat_poll_delivered`` at each delivery point.
    """
    pending = chat_service._pending_chat_messages_for_poll(
        store, since=since, consumer_id=consumer_id, claim=claim
    )
    _trace_poll_delivered(store, pending, consumer_id=consumer_id, claim=claim)
    return pending


def pending_status_events(store: UserStore, *, after_id: int, limit: int = 50) -> list:
    """Agent tool-call status events for this user since ``after_id`` (spec §9:
    foreground-only tool-call progress, never part of chat history).

    Read side only — goes through the ``db`` primitive shared with the write
    side (``model_api_runtime.v2.jobs_store.append_status_event`` writes the
    same table; ``jobs_store.list_status_events`` delegates to this same
    ``db`` read so there is exactly one SQL statement for the read path). The
    `chat` package must not import upward into `model_api_runtime.v2` — see
    tests/test_v2_dependency_direction.py and CONTRIBUTING's dependency
    direction rule.
    """
    return db.list_agent_status_events(store.user_id, after_id=after_id, limit=limit)


def build_response(
    *,
    messages: list,
    context: dict,
    consumer_id: str,
    claim: bool,
    timed_out: bool,
    status_events: list | None = None,
    status_cursor: int = 0,
) -> dict:
    """The `/v1/chat/poll` response contract (locked for parity, + §9 status).

    ``status_events`` / ``status_cursor`` are additive with safe defaults —
    an existing call site that never passes them gets an empty list / a zero
    cursor, byte-identical to the pre-§9 contract in every other field.
    """
    events = status_events or []
    return {
        "messages": messages,
        "runtime_v2": context["runtime_v2"],
        "client_release": context["client_release"],
        "user_mcp": context["user_mcp"],
        "timed_out": timed_out,
        "consumer_id": consumer_id,
        "claimed": claim,
        "agent_status_events": events,
        "status_cursor": int(status_cursor or (events[-1]["id"] if events else 0)),
    }
