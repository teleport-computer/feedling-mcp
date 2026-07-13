"""Unified provider-native tool loop (spec C2). Dependency-clean: no hosted/agent_runtime/db;
all side effects injected. One loop for every model — no is_official branch."""
from __future__ import annotations
from dataclasses import dataclass
from provider_types import ProviderResponse
from capabilities import tool_schema
import provider_client

_CATALOG = None  # built lazily/once


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = tool_schema.build_tool_specs()
    return _CATALOG


@dataclass
class LoopOutcome:
    final_text: str
    rounds: int
    stop_reason: str
    replied_intermediate: bool


async def run_tool_loop(*, provider_config, build_messages, dispatch_tools, on_reply,
                        fold_new_messages, add_usage, max_calls: int) -> LoopOutcome:
    """``fold_new_messages`` is an ASYNC callable (``async def fold_new_messages() ->
    list[dict]``) — it wraps an enclave-bound decrypt read (spec §11 R3), same as
    ``dispatch_tools``, so it must be awaited, never called synchronously (a sync
    call would block the event loop thread for the HTTP round-trip and bypass the
    shared enclave semaphore the initial-turn coalesce already goes through).

    ``on_reply`` is likewise an ASYNC callable (``async def on_reply(text, *, final) ->
    None``) — every production caller enqueues a reply effect and then drains it via
    `deps.apply_pending_effects`, whose reply sink does an enclave-bound encrypted
    write; the drain itself is `asyncio.to_thread`-offloaded on the caller's side, so
    ``on_reply`` must be awaited here, never called synchronously (a sync call would
    reintroduce the event-loop-blocking write the offload is meant to avoid)."""
    folded: list = []
    prior_results: list = []
    replied_intermediate = False
    rounds = 0
    for call_idx in range(max_calls):
        if call_idx > 0:
            folded.extend(await fold_new_messages())   # per-round fold, no restart, no debounce
        messages = build_messages(folded, prior_results)
        last_call = call_idx == max_calls - 1
        tools = None if last_call else _catalog()
        try:
            result = await provider_client.chat_completion_async(provider_config, messages, tools=tools)
        except Exception:
            # TurnMetrics' docstring promises failed provider calls ARE counted
            # (model_calls bumped, just with no token usage) — add_usage(None)
            # before letting the exception propagate so a turn that fails on its
            # very first provider call doesn't flush model_calls=0.
            add_usage(None)
            raise
        add_usage(result.get("usage"))
        rounds += 1
        pr = ProviderResponse.from_result(result)
        if not pr.tool_calls:
            await on_reply(pr.text, final=True)      # plain text IS the final reply (no responder)
            return LoopOutcome(pr.text, rounds, "final_text", replied_intermediate)
        # text accompanying tool_calls = preamble/thinking, NOT a bubble.
        reply_calls = [tc for tc in pr.tool_calls if tc.name == tool_schema.REPLY_TOOL]
        other_calls = [tc for tc in pr.tool_calls if tc.name != tool_schema.REPLY_TOOL]
        for tc in reply_calls:
            await on_reply(str(tc.args.get("text") or ""), final=False)   # immediate intermediate bubble
            replied_intermediate = True
        if other_calls:
            prior_results = prior_results + list(await dispatch_tools(other_calls))
    # budget exhausted without a terminal: the last call had tools=None so pr had no tool_calls
    # and returned above; this line is only reached if max_calls==0.
    return LoopOutcome("", rounds, "budget_exhausted", replied_intermediate)
