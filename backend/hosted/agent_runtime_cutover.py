"""Hosted model_api → agent-runtime cutover (plan §P3).

Behind a per-user flag (``agent_runtime_driver`` in the model_api config:
``legacy`` | ``claude`` | ``codex``), ``/v1/model_api/chat/send`` can hand the
turn to the out-of-process agent runtime instead of doing the inline LLM call.
The external contract stays stable: the user message is written to the chat
store as today; we then wait briefly for the agent-runner's reply and return it
synchronously, or return ``processing`` for a slow turn (the client already
reads replies via chat poll). ``legacy`` (default) is unchanged — flipping the
flag back is the rollback.

This module is pure/IO-light and unit-tested; the route does the thin delegation.
The wait helper takes injected ``now``/``sleep`` so it is deterministic in tests.
"""

from __future__ import annotations

import logging
import time

import provider_client

log = logging.getLogger("feedling.hosted.agent_runtime_cutover")

# The agent driver is DERIVED from the provider, never user-chosen: each CLI is
# locked to a wire format (Claude Code = Anthropic Messages, Codex = OpenAI
# Responses). Empirically (2026-06-25): anthropic + deepseek (its /anthropic
# endpoint) work with Claude Code; openai works with Codex; gemini/openrouter/
# openai_compatible have no native fit for either CLI today → stay legacy.
# Keep this map in sync with the SQL CASE in db.list_agent_runtime_enabled_users.
_PROVIDER_DRIVER = {
    "anthropic": "claude",
    "deepseek": "claude",
    "openai": "codex",
}
# Values of ``agent_runtime_driver`` that mean "hosted runtime OFF" (legacy
# inline path). Anything else is the enable flag — the WHICH-agent decision is
# then derived from the provider, so a stale "claude"/"codex" still resolves
# correctly for the configured key.
_OFF_FLAGS = {"", "legacy", "off", "false", "0", "no", "disabled"}


def driver_for_provider(provider: str) -> str:
    """The agent driver for a provider key — auto-derived, NOT user-chosen.

    anthropic / deepseek → ``claude`` (Anthropic-wire CLI); openai → ``codex``;
    anything else (gemini, openrouter, openai_compatible, …) → ``legacy`` (no
    native hosted-agent fit yet)."""
    return _PROVIDER_DRIVER.get(provider_client.normalize_provider(provider), "legacy")


def hosting_enabled(config: dict | None) -> bool:
    """Whether the hosted agent runtime is turned on for this user (the gradual
    -rollout gate). The agent itself is still derived from the provider."""
    if not isinstance(config, dict):
        return False
    return str(config.get("agent_runtime_driver") or "").strip().lower() not in _OFF_FLAGS


def resolve_driver(config: dict | None, *, default: str = "legacy") -> str:
    """The driver for this user's turn: ``legacy`` unless hosting is enabled, then
    the agent derived from the configured provider (``claude``/``codex``), or
    ``legacy`` if the provider has no hosted-agent fit."""
    if not hosting_enabled(config):
        return default
    return driver_for_provider(str((config or {}).get("provider") or ""))


def is_enabled(driver: str) -> bool:
    """True when the agent runtime (not legacy inline) should handle the turn."""
    return driver in ("claude", "codex")


def should_route(driver: str, *, has_image: bool) -> bool:
    """Whether this send should go to the agent runtime.

    Image turns stay on the legacy multimodal path: the consumer decrypts each
    polled envelope as UTF-8 text, so an image envelope would fail to process.
    Route only enabled, text-only turns until the runtime handles images.
    """
    return is_enabled(driver) and not has_image


def _is_assistant(row: dict) -> bool:
    return str(row.get("role") or "") in ("openclaw", "assistant", "agent")


def find_reply_row(store, user_message_id: str) -> dict | None:
    """The agent's reply to ``user_message_id``, or None if not answered yet.

    Prefers the precise link (the user row gains ``reply_message_id`` when a
    reply with ``reply_to_message_id`` is posted); falls back to scanning for an
    assistant row that points back at the user message.
    """
    messages = list(getattr(store, "chat_messages", []) or [])
    by_id = {str(m.get("id")): m for m in messages}
    user_row = by_id.get(str(user_message_id))
    if user_row:
        reply_id = str(user_row.get("reply_message_id") or "")
        if reply_id and reply_id in by_id:
            return by_id[reply_id]
    for m in messages:
        if _is_assistant(m) and str(m.get("reply_to_message_id") or "") == str(user_message_id):
            return m
    return None


def wait_for_reply(
    store,
    user_message_id: str,
    *,
    timeout: float = 8.0,
    poll_interval: float = 0.5,
    now=time.time,
    sleep=time.sleep,
) -> dict | None:
    """Poll the store for the agent's reply up to ``timeout`` seconds."""
    deadline = now() + timeout
    while True:
        row = find_reply_row(store, user_message_id)
        if row is not None:
            return row
        if now() >= deadline:
            return None
        sleep(poll_interval)


def _runtime_block(driver: str) -> dict:
    return {"engine": "feedling_agent_runtime", "mode": "hosted_agent", "driver": driver, "version": 1}


def build_processing_response(user_row: dict, *, driver: str) -> tuple[dict, int]:
    """Reply not ready within the wait window — the client reads it via chat
    poll once the agent-runner posts it. Always 202; never a `reply` field
    (the server holds only ciphertext under E2E)."""
    return (
        {
            "status": "processing",
            "reply_ready": False,
            "user_message": {"id": user_row.get("id"), "ts": user_row.get("ts")},
            "runtime": _runtime_block(driver),
        },
        202,
    )


def build_ready_response(user_row: dict, reply_row: dict, *, driver: str) -> tuple[dict, int]:
    """The reply landed within the wait window. Still 202 (not 200/`ok`) and
    still no plaintext `reply`: we hand back the assistant message ref so the
    client can fetch+decrypt it immediately, but the text never transits the
    server. Avoids the misleading `200 ok` with no reply text."""
    return (
        {
            "status": "processing",
            "reply_ready": True,
            "user_message": {"id": user_row.get("id"), "ts": user_row.get("ts")},
            "assistant_message": {"id": reply_row.get("id"), "ts": reply_row.get("ts")},
            "runtime": _runtime_block(driver),
        },
        202,
    )


def handle_send(store, user_row: dict, driver: str, *, timeout: float = 8.0) -> tuple[dict, int]:
    """Delegate a flagged send to the agent runtime: the user message is already
    in the store; wait briefly and report whether the reply is ready (the client
    fetches the ciphertext reply via chat poll either way)."""
    reply = wait_for_reply(store, str(user_row.get("id") or ""), timeout=timeout)
    if reply is not None:
        return build_ready_response(user_row, reply, driver=driver)
    return build_processing_response(user_row, driver=driver)
