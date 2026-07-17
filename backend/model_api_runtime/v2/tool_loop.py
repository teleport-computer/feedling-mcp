"""Unified provider-native tool loop (spec C2). Dependency-clean: no hosted/agent_runtime/db;
all side effects injected. One loop for every model — no is_official branch."""
from __future__ import annotations
from dataclasses import dataclass
import json
import re
from provider_types import ProviderResponse, ToolExchange, ToolResult
from capabilities import registry as cap_registry
from capabilities import tool_schema
from model_api_runtime.v2 import provenance
import provider_client

_CATALOG = None  # built lazily/once
_SEARCH_RESULT_URL_RE = re.compile(r'"url"\s*:\s*("(?:\\.|[^"\\])*")')


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = tool_schema.build_tool_specs()
    return _CATALOG


def _search_result_urls(content: str) -> set[str]:
    """Extract exact result URLs even when the capped JSON tail is truncated."""
    urls: set[str] = set()
    for match in _SEARCH_RESULT_URL_RE.finditer(str(content or "")):
        try:
            value = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, str) and value.strip():
            urls.add(value.strip())
    return urls


@dataclass
class LoopOutcome:
    final_text: str
    rounds: int
    stop_reason: str
    replied_intermediate: bool


async def run_tool_loop(*, provider_config, build_messages, dispatch_tools, on_reply,
                        fold_new_messages, add_usage, max_calls: int,
                        fold_before_first: bool = False,
                        on_progress=None, extra_tool_specs=None) -> LoopOutcome:
    """Run one chronological, provider-native tool transcript.

    ``build_messages`` receives a single chronological list containing newly folded
    user-message dicts and :class:`provider_types.ToolExchange` objects.  Keeping the
    assistant's native tool-call turn adjacent to its call-id-matched results is not
    optional protocol decoration: OpenAI, Anthropic, and Gemini all require that
    exchange on the next provider request (and Gemini additionally carries opaque
    thought signatures which cannot be reconstructed from normalized calls).

    ``fold_new_messages`` is an ASYNC callable (``async def fold_new_messages() ->
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
    transcript: list = []
    replied_intermediate = False
    attempts = 0
    force_text_fallback = False
    external_content_seen = False
    allowed_fetch_urls: set[str] = set()
    # Per-turn tool surface = the static platform catalog plus any user-MCP tools
    # injected for this turn (chat lane only). New list, never mutates the memoized
    # `_catalog()`. `mcp_names` marks the injected tools so their args skip the
    # platform PARAMS validation (the MCP server validates its own schema); MCP
    # tools are deliberately NOT external reads, so a call to one does not lock
    # writes for the rest of the turn.
    turn_catalog = _catalog() + list(extra_tool_specs or [])
    mcp_names = {spec.name for spec in (extra_tool_specs or [])}

    def _progress(stage: str) -> None:
        # Parent-process wedge detection is observability, never turn logic.
        # Production passes a cheap synchronous pipe callback; tests/legacy
        # callers omit it.  A telemetry failure must not alter model behavior.
        if on_progress is None:
            return
        try:
            on_progress(stage)
        except Exception:  # noqa: BLE001
            pass

    while attempts < max_calls:
        _progress("round_boundary")
        if attempts > 0 or fold_before_first:
            # Seq-native production callers also fold at the first boundary to
            # close the prompt-assembly race. Legacy fixtures keep the historical
            # after-first behavior until their timestamp seam is removed.
            transcript.extend(await fold_new_messages())

        messages = build_messages(list(transcript))
        # Reserve the final provider attempt for a tools-disabled terminal reply.
        # A 400/422 tool-schema rejection or repeated malformed call also forces
        # this same one-shot fallback; the fallback itself is never retried.
        if force_text_fallback or attempts == max_calls - 1:
            tools = None
        elif external_content_seen:
            # Web results are untrusted model input. Once one is present, page
            # text cannot spend the original write authorization or choose a
            # fresh outbound query/URL. Preserve the useful search -> fetch flow
            # only when search returned at least one exact allowlisted URL; the
            # call is checked again below before dispatch.
            blocked_after_external = set(cap_registry.WRITE_ACTIONS) | {"web_search"}
            if not allowed_fetch_urls:
                blocked_after_external.add("web_fetch")
            tools = [
                spec for spec in turn_catalog
                if spec.name not in blocked_after_external
            ]
        else:
            tools = turn_catalog
        attempts += 1
        _progress("provider_start")
        try:
            result = await provider_client.chat_completion_async(provider_config, messages, tools=tools)
        except Exception as exc:
            # TurnMetrics' docstring promises failed provider calls ARE counted
            # (model_calls bumped, just with no token usage) — add_usage(None)
            # before either falling back or propagating.
            add_usage(None)
            if (
                tools is not None
                and isinstance(exc, provider_client.ProviderError)
                and exc.status_code in {400, 422}
                and attempts < max_calls
            ):
                force_text_fallback = True
                _progress("provider_retry_boundary")
                continue
            raise
        _progress("provider_complete")
        add_usage(result.get("usage"))
        pr = ProviderResponse.from_result(result)

        # A tools-disabled request is terminal even if a broken relay invents a
        # tool call anyway.  Never execute an undeclared call; use only its text.
        if tools is None or not pr.tool_calls:
            await on_reply(pr.text, final=True)      # plain text IS the final reply (no responder)
            return LoopOutcome(pr.text, attempts, "final_text", replied_intermediate)

        call_ids = [tc.id for tc in pr.tool_calls]
        offered_names = {spec.name for spec in tools}
        malformed = any(
            not tc.id
            or not tc.name
            or not tc.args_ok
            or tc.name not in offered_names
            or (
                external_content_seen
                and tc.name == "web_fetch"
                and str(tc.args.get("url") or "").strip() not in allowed_fetch_urls
            )
            or (
                tc.name not in mcp_names
                and tool_schema.validate_tool_args(tc.name, tc.args) is not None
            )
            for tc in pr.tool_calls
        )
        mixed_reply_write = any(
            tc.name == tool_schema.REPLY_TOOL for tc in pr.tool_calls
        ) and any(
            tc.name in cap_registry.WRITE_ACTIONS for tc in pr.tool_calls
        )
        if malformed or len(set(call_ids)) != len(call_ids) or mixed_reply_write:
            # A malformed or duplicate-id batch is all-or-nothing: executing its
            # valid subset and then asking for a correction can duplicate durable
            # writes on the corrected round.  Missing/duplicate ids also cannot be
            # represented as a valid provider-native tool-result exchange.  Make
            # exactly one tools-disabled fallback from the original prompt instead.
            # A reply+write batch is rejected for the same reason: executing the
            # bubble first can falsely tell the user "saved" before the write is
            # even enqueued, while executing it last still cannot prove the later
            # sink will commit. The model may enqueue the write in one round and
            # reply only after observing its result in the next.
            if attempts >= max_calls:
                break
            force_text_fallback = True
            continue

        # text accompanying tool_calls = preamble/thinking, NOT a bubble.
        reply_calls = [tc for tc in pr.tool_calls if tc.name == tool_schema.REPLY_TOOL]
        other_calls = [tc for tc in pr.tool_calls if tc.name != tool_schema.REPLY_TOOL]
        reply_results: dict[str, ToolResult] = {}
        for tc in reply_calls:
            reply_text = str(tc.args.get("text") or "")
            await on_reply(reply_text, final=False)   # immediate intermediate bubble
            if reply_text.strip():
                replied_intermediate = True
                content = "ok: reply delivered"
            else:
                content = "ok: empty reply ignored"
            reply_results[tc.id] = ToolResult(call_id=tc.id, content=content)

        dispatched = list(await dispatch_tools(other_calls)) if other_calls else []
        _progress("tool_batch_complete")
        dispatched_by_id = {result.call_id: result for result in dispatched}
        if len(dispatched_by_id) != len(dispatched):
            raise RuntimeError("tool dispatcher returned duplicate call ids")
        for tc in other_calls:
            result = dispatched_by_id.get(tc.id)
            if result is None:
                continue
            if tc.name == "web_search":
                allowed_fetch_urls.update(_search_result_urls(result.content))
            elif tc.name == "web_fetch":
                allowed_fetch_urls.discard(str(tc.args.get("url") or "").strip())
        if any(tc.name in provenance.EXTERNAL_READS for tc in other_calls):
            # Set only after dispatch: a write selected in the SAME batch was
            # chosen before the model saw the external result.  Only later
            # rounds are influenced by that result and therefore lose writes.
            external_content_seen = True
        ordered_results: list[ToolResult] = []
        for tc in pr.tool_calls:
            result_for_call = reply_results.get(tc.id) or dispatched_by_id.get(tc.id)
            if result_for_call is None:
                raise RuntimeError(f"tool dispatcher omitted result for call {tc.id!r}")
            ordered_results.append(result_for_call)
        if set(dispatched_by_id) != {tc.id for tc in other_calls}:
            raise RuntimeError("tool dispatcher returned mismatched call ids")

        transcript.append(ToolExchange(
            calls=tuple(pr.tool_calls), results=tuple(ordered_results),
            assistant_text=pr.text, assistant_turn=pr.assistant_turn,
        ))
    # Only reachable for max_calls == 0, or when a malformed response consumed the
    # last reserved attempt before a fallback could be made.
    return LoopOutcome("", attempts, "budget_exhausted", replied_intermediate)
