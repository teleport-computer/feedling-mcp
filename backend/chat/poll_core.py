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


def _self_heal_if_stale(store: UserStore, since: float) -> bool:
    """Read-time staleness probe for the poll path (2026-07-22 prod incident).

    ``/v1/chat/history`` has probed the DB before serving a stale since-window
    since 2026-07-15 ("push arrives, chat page doesn't"); ``/v1/chat/poll`` did
    not, and it serves from the same per-worker cache. When a cross-worker
    NOTIFY is missed, the consumer's polls then truthfully report "nothing" for
    a message that IS committed — prod 2026-07-22: four consecutive 33s polls
    empty, the message served three minutes late. Shared ``chat_core`` probe:
    a capped per-window COUNT (catches a missing middle row, not just a newer
    one), same fail-open contract. Run on EVERY poll, not only empty ones — a
    single stale cached row is enough to make a partially-missing window look
    like a real answer.
    """
    from chat import chat_core  # lazy: keeps poll_core's import surface flat

    return chat_core._self_heal_if_stale(store, since, label="chat/poll")


def pending_messages(store: UserStore, *, since: float, consumer_id: str, claim: bool) -> list:
    """Pending chat messages for this consumer (claiming them when ``claim``).

    Records the delivery trace when non-empty — the exact behavior of the legacy
    route's ``_trace_chat_poll_delivered`` at each delivery point.

    The DB is probed for rows this worker's cache is missing in the window
    (see ``_self_heal_if_stale``) and, if any are found, the pending set is
    recomputed and unioned in: "nothing pending" must be a fact, not this
    worker's cache being behind.
    """
    pending = chat_service._pending_chat_messages_for_poll(
        store, since=since, consumer_id=consumer_id, claim=claim
    )
    if _self_heal_if_stale(store, since):
        # UNION, never replace. The re-check is not idempotent for redelivery
        # (ts <= since) claims: chat_try_claim_reply(redelivery=True) refuses a
        # row that already carries a live claim — including the one pass 1 just
        # stamped — so a replacing re-check would drop a message it had already
        # claimed and hand back nothing for it. Pass 1's rows keep their order;
        # the healed rows append.
        seen = {str(m.get("id") or "") for m in pending}
        pending = list(pending) + [
            m
            for m in chat_service._pending_chat_messages_for_poll(
                store, since=since, consumer_id=consumer_id, claim=claim
            )
            if str(m.get("id") or "") not in seen
        ]
    _trace_poll_delivered(store, pending, consumer_id=consumer_id, claim=claim)
    return pending


def build_response(
    *, messages: list, context: dict, consumer_id: str, claim: bool, timed_out: bool
) -> dict:
    """The `/v1/chat/poll` response contract (locked for parity)."""
    return {
        "messages": messages,
        "runtime_v2": context["runtime_v2"],
        "client_release": context["client_release"],
        "user_mcp": context["user_mcp"],
        "timed_out": timed_out,
        "consumer_id": consumer_id,
        "claimed": claim,
    }
