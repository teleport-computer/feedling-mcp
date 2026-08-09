"""Unified provider-native tool loop (spec C2). Dependency-clean: no hosted/agent_runtime/db;
all side effects injected. One loop for every model — no is_official branch."""

from __future__ import annotations
from dataclasses import dataclass
import json
import posixpath
import re
from provider_types import ProviderResponse, ToolExchange, ToolResult
from capabilities import registry as cap_registry
from capabilities import result_budget
from capabilities import tool_schema
from model_api_runtime.v2 import prompt_frontier
from model_api_runtime.v2 import provenance
import provider_client

_CATALOG = None  # built lazily/once
_SEARCH_RESULT_URL_RE = re.compile(r'"url"\s*:\s*("(?:\\.|[^"\\])*")')

# Provider output is untrusted even after its tool names/arguments validate.  These
# defaults bound both fan-out and how much observation text one native exchange can
# add to the next prompt. Production passes env-validated values from worker.py;
# direct callers/tests inherit these safe defaults.
DEFAULT_MAX_TOOL_CALLS_PER_ROUND = 8
DEFAULT_MAX_TOOL_CALLS_PER_TURN = 24
DEFAULT_TOOL_RESULT_CHAR_CAP = 2000
DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP = 8000
DEFAULT_MAX_TOOL_ARGS_CHARS = 16000
DEFAULT_MAX_TOOL_BATCH_ARGS_CHARS = 64000
DEFAULT_MAX_NATIVE_ASSISTANT_TURN_CHARS = 65536
DEFAULT_MAX_ASSISTANT_TOOL_TEXT_CHARS = 8192
MIN_TOOL_RESULT_ERROR_QUOTA = 64
_RESULT_TRUNCATION_MARKER = "...[truncated]"
MCP_MUTATION_OUTCOME_UNKNOWN_ERROR = "error: mcp_mutation_outcome_unknown"
MUTATION_BLOCKED_AFTER_UNKNOWN_OUTCOME_ERROR = (
    "error: mutation_blocked_after_unknown_outcome"
)
_MEMORY_DISCOVERY_TOOLS = frozenset({"memory_index", "memory_search"})
_PLATFORM_MUTATION_TOOLS = frozenset(
    set(cap_registry.WRITE_ACTIONS) | {tool_schema.MEMORY_ORGANIZE_TOOL}
)
_FILE_DELIVERY_TOOLS = frozenset(
    {
        "memory_index",
        "memory_search",
        "memory_fetch",
        "workspace_list",
        "workspace_read",
        "workspace_write",
        tool_schema.FILE_REPLY_TOOL,
    }
)


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = tool_schema.build_tool_specs()
    return _CATALOG


def _is_probably_tool_schema_rejection(exc: provider_client.ProviderError) -> bool:
    """Should a tools-enabled 400/422 be retried once WITHOUT tools?

    Only when the provider's error text actually implicates the tool/function
    schema. A 400 can equally come from the message content (e.g. an OpenAI
    Responses assistant history part sent as input_text instead of output_text).
    Dropping tools then re-sends the identical bad history — a second billed
    call that 400s again and masks the real error as 'tool_schema_rejected'. The
    provider surfaces its error body in the ProviderError message
    (``provider_http_400: <detail>``), and content errors don't mention
    tools/functions, so this gate keeps the genuine tool-schema fallback while
    letting a content error propagate on its first call."""
    detail = str(exc).lower()
    return "tool" in detail or "function" in detail


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


def _positive_limit(value, *, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _serialized_chars(value) -> int | None:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _truncate_result_content(content: str, cap: int, *, marker: str = _RESULT_TRUNCATION_MARKER) -> str:
    """Deterministically cap one result, including the truncation marker."""
    text = str(content or "")
    if len(text) <= cap:
        return text
    if cap <= len(marker):
        return marker[:cap]
    return text[: cap - len(marker)] + marker


def _result_quotas(lengths: list[int], batch_cap: int) -> list[int]:
    """Water-fill a batch budget fairly, redistributing space unused by short results.

    Remainder characters go to earlier provider calls, making the output stable while
    guaranteeing that a later call is never starved merely because an earlier result
    was large.
    """
    quotas = [0] * len(lengths)
    active = list(range(len(lengths)))
    remaining = batch_cap
    while active:
        share = remaining // len(active)
        short = [index for index in active if lengths[index] <= share]
        if short:
            for index in short:
                quotas[index] = lengths[index]
                remaining -= lengths[index]
            short_set = set(short)
            active = [index for index in active if index not in short_set]
            continue
        for index in active:
            quotas[index] = share
        remainder = remaining - (share * len(active))
        for index in active[:remainder]:
            quotas[index] += 1
        break
    return quotas


def _batch_quotas(contents: list[str], policies: list, batch_cap: int) -> list[int]:
    """Water-fill the batch budget after reserving every atomic result in full.

    With no atomic result in the batch this is exactly ``_result_quotas`` over
    the unmodified ``batch_cap`` — the pre-existing behavior, unchanged.

    With one present: the batch budget rises by its ``extra_batch_budget``
    (otherwise a 4500-char fetch would leave seven siblings ~500 each), the
    atomic result keeps its full length, and only the remainder is water-filled
    among the siblings.  Reserving can exhaust the remainder in a pathological
    batch; siblings then get 0, which is the same starvation the plain
    water-fill already produces — never a sliced atomic result.
    """
    atomic = [
        index
        for index, policy in enumerate(policies)
        if policy is not None and policy.atomic_json
    ]
    if not atomic:
        return _result_quotas([len(content) for content in contents], batch_cap)
    budget = batch_cap + sum(
        policies[index].extra_batch_budget for index in atomic
    )
    reserved = sum(len(contents[index]) for index in atomic)
    shared = [index for index in range(len(contents)) if index not in set(atomic)]
    shared_quotas = _result_quotas(
        [len(contents[index]) for index in shared], max(0, budget - reserved)
    )
    quotas = [0] * len(contents)
    for index in atomic:
        quotas[index] = len(contents[index])
    for index, quota in zip(shared, shared_quotas):
        quotas[index] = quota
    return quotas


def _normalize_tool_results(
    results: list[ToolResult],
    *,
    per_result_cap: int,
    batch_cap: int,
) -> list[ToolResult]:
    """Apply provider-neutral per-call and aggregate prompt budgets in call order.

    Results whose trusted metadata declares an ``atomic_json`` budget policy
    (``capabilities/result_budget.py``) are reserved at full length before the
    water-fill runs, and the batch budget rises by that policy's
    ``extra_batch_budget``.  They are never string-truncated: their producer
    already shrank them structurally, and cutting a JSON payload mid-string
    makes the whole result unparseable for the model.

    A batch with no such result takes exactly the path it always took — same
    per-result cap, same batch cap, same quotas, byte for byte.
    """
    policies = [result_budget.for_metadata(result.metadata) for result in results]
    markers = []
    for result in results:
        metadata = result.metadata or {}
        if metadata.get("memory_query_kind") in {"memory_index", "memory_search"}:
            total = metadata.get("memory_total")
            returned = metadata.get("memory_returned")
            markers.append(
                "...[memory result truncated; this query returned "
                f"{returned if isinstance(returned, int) else '?'} of "
                f"{total if isinstance(total, int) else '?'} total cards. "
                "Use memory_index with bucket or thread filters to browse partitions.]"
            )
        else:
            markers.append(_RESULT_TRUNCATION_MARKER)
    individually_capped = [
        _truncate_result_content(
            result.content,
            policy.result_cap if policy is not None else per_result_cap,
            marker=marker,
        )
        for result, policy, marker in zip(results, policies, markers)
    ]
    quotas = _batch_quotas(individually_capped, policies, batch_cap)
    return [
        ToolResult(
            call_id=result.call_id,
            content=_truncate_result_content(content, quota, marker=marker),
            metadata=result.metadata,
        )
        for result, content, quota, marker in zip(results, individually_capped, quotas, markers)
    ]


@dataclass
class LoopOutcome:
    final_text: str
    rounds: int
    stop_reason: str
    replied_intermediate: bool


class FinalReplySuperseded(RuntimeError):
    """The final reply lost the atomic late-input fence before publication.

    ``on_reply`` raises this only after the candidate reply effect has been
    terminally discarded without reaching its sink.  It is therefore safe for
    the loop to fold the newly arrived input and ask the provider again.  An
    intermediate ``reply{}`` bubble never uses this signal: those bubbles are
    intentionally immediate and remain visible while the turn continues.
    """


async def run_tool_loop(
    *,
    provider_config,
    build_messages,
    dispatch_tools,
    on_reply,
    fold_new_messages,
    add_usage,
    max_calls: int,
    before_provider_call=None,
    on_provider_success=None,
    on_provider_failure=None,
    fold_before_first: bool = False,
    on_progress=None,
    on_trajectory_event=None,
    extra_tool_specs=None,
    extra_mutating_tool_names=None,
    disabled_tool_names=None,
    allow_reply_tool: bool = True,
    include_reasoning: bool = False,
    # Self-authored thinking: when True, NEVER request provider-native reasoning —
    # not via include_reasoning, and NOT via reasoning_effort either. The model then
    # has no separate native-CoT channel and emits its thinking in the reply's
    # <think> block instead (which the seal surfaces). This is what aligns V2 with
    # the V1 resident: without it a reasoning-capable model (e.g. sonnet) puts its
    # thought in the native channel — shown raw, often in the wrong language — and
    # skips the <think>. Default False → other lanes unchanged.
    suppress_native_reasoning: bool = False,
    # Whether a text-free provider reply is an ERROR. Defaults to True, which
    # is the chat lane's rule (a terminal turn with no text is the no-filler
    # failure). The wake lane is the opposite — "weak wake sleeps": a model
    # that chooses to stay silent is a legitimate outcome, not a failure — and
    # must pass False, otherwise provider_client raises
    # ProviderError("provider response had no usable reply text") on the very
    # response that means "nothing to say" (`required = require_reply and not
    # tool_calls`), and the wake fails silently.
    require_reply: bool = True,
    on_file_reply=None,
    on_tool_event=None,
    required_file_suffixes: tuple[str, ...] | None = None,
    file_requirement_messages=(),
    resolve_required_file_suffixes=None,
    on_file_requirement_changed=None,
    outbound_blocking_read_tool_names=None,
    outbound_blocking_read_tool_predicate=None,
    max_tool_calls_per_round: int = DEFAULT_MAX_TOOL_CALLS_PER_ROUND,
    max_tool_calls_per_turn: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    tool_result_char_cap: int = DEFAULT_TOOL_RESULT_CHAR_CAP,
    tool_batch_result_char_cap: int = DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP,
    max_tool_args_chars: int = DEFAULT_MAX_TOOL_ARGS_CHARS,
    max_tool_batch_args_chars: int = DEFAULT_MAX_TOOL_BATCH_ARGS_CHARS,
    max_native_assistant_turn_chars: int = DEFAULT_MAX_NATIVE_ASSISTANT_TURN_CHARS,
    max_assistant_tool_text_chars: int = DEFAULT_MAX_ASSISTANT_TOOL_TEXT_CHARS,
    prompt_context_window_overrides=None,
    prompt_output_reserve_tokens: int = prompt_frontier.DEFAULT_OUTPUT_RESERVE_TOKENS,
    prompt_safety_margin_tokens: int | None = None,
    prompt_estimator_utf8_bytes_per_token: float = prompt_frontier.DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
    prompt_image_reserve_tokens: int = prompt_frontier.DEFAULT_IMAGE_RESERVE_TOKENS,
    on_tail_window=None,
    on_prompt_frontier_exhaustion=None,
) -> LoopOutcome:
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
    reintroduce the event-loop-blocking write the offload is meant to avoid).

    ``on_file_reply`` is an optional async chat-lane callback. When absent, the
    ``send_file`` tool is removed from the offered catalog. Production resolves
    only encrypted ``/workspace`` entries through this callback; the loop never
    interprets a model string as a host filesystem path.

    ``on_tool_event`` receives the same started/result/error boundary events
    for protocol-level ``reply`` and ``send_file`` calls that the worker's
    dispatcher emits for platform and MCP tools.

    ``required_file_suffixes`` is a completion guard for explicit output formats.
    ``None`` and ``()`` disable the hard guard; a non-empty tuple gets one bounded
    completion retry before the provider's terminal text is published normally.
    Chat callers may also provide ``file_requirement_messages`` plus a resolver;
    newly folded user messages then update or cancel the requirement before each
    later provider round."""
    max_tool_calls_per_round = _positive_limit(
        max_tool_calls_per_round, name="max_tool_calls_per_round"
    )
    max_tool_calls_per_turn = _positive_limit(
        max_tool_calls_per_turn, name="max_tool_calls_per_turn"
    )
    tool_result_char_cap = _positive_limit(
        tool_result_char_cap, name="tool_result_char_cap"
    )
    tool_batch_result_char_cap = _positive_limit(
        tool_batch_result_char_cap, name="tool_batch_result_char_cap"
    )
    max_tool_args_chars = _positive_limit(
        max_tool_args_chars, name="max_tool_args_chars"
    )
    max_tool_batch_args_chars = _positive_limit(
        max_tool_batch_args_chars, name="max_tool_batch_args_chars"
    )
    max_native_assistant_turn_chars = _positive_limit(
        max_native_assistant_turn_chars,
        name="max_native_assistant_turn_chars",
    )
    max_assistant_tool_text_chars = _positive_limit(
        max_assistant_tool_text_chars,
        name="max_assistant_tool_text_chars",
    )
    if tool_result_char_cap < MIN_TOOL_RESULT_ERROR_QUOTA:
        raise ValueError("tool_result_char_cap is too small for stable error results")
    if (
        tool_batch_result_char_cap
        < max_tool_calls_per_round * MIN_TOOL_RESULT_ERROR_QUOTA
    ):
        raise ValueError(
            "tool_batch_result_char_cap is too small for stable batch errors"
        )
    model_prompt_limit = prompt_frontier.resolve_model_limit_from_config(
        provider_config,
        deployment_overrides=prompt_context_window_overrides,
    )
    # Validate the static frontier configuration before any fold/decrypt work.
    prompt_frontier.build_prompt_budget(
        model_prompt_limit.context_window_tokens,
        output_reserve_tokens=prompt_output_reserve_tokens,
        safety_margin_tokens=prompt_safety_margin_tokens,
    )
    prompt_frontier.estimate_utf8_tokens(
        "",
        utf8_bytes_per_token=prompt_estimator_utf8_bytes_per_token,
    )

    transcript: list = []
    replied_intermediate = False
    attempts = 0
    tool_calls_used = 0
    reasoning_fragments: list[str] = []
    seen_reasoning_fragments: set[str] = set()
    force_text_fallback = False
    external_content_seen = False
    mutation_outcome_unknown = False
    outbound_tools_blocked = False
    file_requirement_message_state = [
        dict(message)
        for message in file_requirement_messages
        if isinstance(message, dict)
    ]

    def _normalize_file_requirement(value) -> tuple[bool, frozenset[str]]:
        suffixes = frozenset(
            str(suffix).strip().casefold() for suffix in (value or ())
        )
        if any(
            re.fullmatch(r"\.[a-z0-9]{1,16}", suffix) is None
            for suffix in suffixes
        ):
            raise ValueError("required_file_suffixes contains an invalid suffix")
        return value is not None, suffixes

    resolved_file_requirement = required_file_suffixes
    if resolve_required_file_suffixes is not None:
        resolved_file_requirement = resolve_required_file_suffixes(
            file_requirement_message_state
        )
    file_delivery_required, normalized_required_suffixes = (
        _normalize_file_requirement(resolved_file_requirement)
    )
    delivered_file_suffixes: set[str] = set()
    delivery_retry_instruction = ""
    file_delivery_retry_used = False
    required_file_missing_recorded = False
    file_delivery_fallback_text = ""
    file_delivery_fallback_reasoning = ""
    file_delivery_recovery_needed = False
    workspace_write_applied = False
    completed_memory_discovery_tools: set[str] = set()
    outbound_blocking_reads = {
        str(name) for name in (outbound_blocking_read_tool_names or ()) if str(name)
    }

    def _read_blocks_later_outbound(tool_call) -> bool:
        """Classify a completed local read before the next provider round.

        Name-only callers remain supported for the workspace/memory boundary.
        Runtime V2 also supplies an argument-aware predicate because a numeric
        health snapshot is safe to combine with later outbound tools while a
        calendar/title/screen read is not.  Classification failures are
        fail-closed: malformed model arguments must never reopen an exfiltration
        channel.
        """
        if tool_call.name in outbound_blocking_reads:
            return True
        if outbound_blocking_read_tool_predicate is None:
            return False
        try:
            return bool(outbound_blocking_read_tool_predicate(tool_call))
        except Exception:  # noqa: BLE001 - security classifier fails closed
            return True
    allowed_fetch_urls: set[str] = set()
    # Per-turn tool surface = the static platform catalog plus any user-MCP tools
    # injected for this turn (chat lane only). New list, never mutates the memoized
    # `_catalog()`. `mcp_names` marks the injected tools so their args skip the
    # platform PARAMS validation (the MCP server validates its own schema). MCP
    # result content is nevertheless untrusted external input: after the model
    # observes it, every later outbound MCP call is stripped, including a tool
    # whose exact catalog fingerprint the user approved as read-only.  Read-only
    # controls remote mutation semantics; it does not make sending model-chosen
    # arguments to that remote server safe after prompt-injected external text.
    disabled_names = {str(name) for name in (disabled_tool_names or ()) if str(name)}
    # ``reply`` is part of the parent loop protocol rather than a side-effect
    # capability. Recovery callers may remove every mutation schema, but must
    # always leave the model a way to produce an intermediate bubble. Isolated
    # child loops explicitly disable it and return only terminal plain text.
    if allow_reply_tool:
        disabled_names.discard(tool_schema.REPLY_TOOL)
    else:
        disabled_names.add(tool_schema.REPLY_TOOL)
    if on_file_reply is None:
        disabled_names.add(tool_schema.FILE_REPLY_TOOL)
    turn_catalog = [
        spec
        for spec in (_catalog() + list(extra_tool_specs or []))
        if spec.name not in disabled_names
    ]
    mcp_names = {spec.name for spec in (extra_tool_specs or [])}
    # Only offered MCP tools can gain mutating semantics through this injected
    # set. Intersecting avoids a stale/buggy loader accidentally reclassifying a
    # platform read or a tool that was never shown to the provider.
    mutating_mcp_names = set(extra_mutating_tool_names or ()) & mcp_names

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

    async def _trajectory(event_kind: str, payload: dict) -> None:
        # Unlike cheap progress telemetry this callback is the encrypted flight
        # recorder. Production awaits its durable append at causal boundaries;
        # a pre-action append failure therefore prevents the action from running.
        if on_trajectory_event is not None:
            await on_trajectory_event(event_kind, payload)

    async def _record_required_file_missing(round_number: int) -> None:
        nonlocal required_file_missing_recorded
        if required_file_missing_recorded:
            return
        await _trajectory(
            "required_file_missing",
            {
                "round": round_number,
                "required_suffixes": sorted(normalized_required_suffixes),
                "delivered_suffixes": sorted(delivered_file_suffixes),
            },
        )
        required_file_missing_recorded = True

    def _capture_reasoning(result: dict) -> None:
        reasoning = str(result.get("reasoning") or "").strip()
        if not reasoning:
            return
        key = " ".join(reasoning.split()).casefold()
        if key in seen_reasoning_fragments:
            return
        seen_reasoning_fragments.add(key)
        reasoning_fragments.append(reasoning)

    def _merged_reasoning() -> str:
        return "\n\n".join(reasoning_fragments)

    async def _tool_event(tc, event_kind: str, payload: dict) -> None:
        if on_tool_event is not None:
            await on_tool_event(tc, event_kind, payload)

    while attempts < max_calls:
        _progress("round_boundary")
        if attempts > 0 or fold_before_first:
            # Seq-native production callers also fold at the first boundary to
            # close the prompt-assembly race. Legacy fixtures keep the historical
            # after-first behavior until their timestamp seam is removed.
            folded = await fold_new_messages()
            if folded:
                # A newly arrived user message changes the answer target. Do not
                # attach reasoning produced for the superseded prompt to the
                # revised final reply.
                reasoning_fragments.clear()
                seen_reasoning_fragments.clear()
                await _trajectory(
                    "late_input_fold",
                    {"round": attempts + 1, "messages": folded},
                )
                transcript.extend(folded)
                if resolve_required_file_suffixes is not None:
                    file_requirement_message_state.extend(
                        dict(message)
                        for message in folded
                        if isinstance(message, dict)
                    )
                    (
                        next_file_delivery_required,
                        next_required_suffixes,
                    ) = _normalize_file_requirement(
                        resolve_required_file_suffixes(
                            file_requirement_message_state
                        )
                    )
                    if (
                        next_file_delivery_required != file_delivery_required
                        or next_required_suffixes
                        != normalized_required_suffixes
                    ):
                        file_delivery_required = next_file_delivery_required
                        normalized_required_suffixes = next_required_suffixes
                        delivered_file_suffixes.clear()
                        delivery_retry_instruction = ""
                        file_delivery_retry_used = False
                        required_file_missing_recorded = False
                        file_delivery_fallback_text = ""
                        file_delivery_fallback_reasoning = ""
                        if on_file_requirement_changed is not None:
                            await on_file_requirement_changed()

        messages = build_messages(list(transcript))
        if delivery_retry_instruction:
            # Keep provider-native tool exchanges intact. They are dataclasses,
            # not mappings, and must survive unchanged so the next provider
            # round can replay the exact assistant/tool-result transcript.
            messages = [
                dict(message) if isinstance(message, dict) else message
                for message in messages
            ]
            if (
                messages
                and isinstance(messages[0], dict)
                and messages[0].get("role") == "system"
            ):
                messages[0]["content"] = (
                    str(messages[0].get("content") or "").rstrip()
                    + "\n\n"
                    + delivery_retry_instruction
                )
            else:
                messages.insert(
                    0,
                    {"role": "system", "content": delivery_retry_instruction},
                )
        # Reserve the final provider attempt for a tools-disabled terminal reply.
        # A 400/422 tool-schema rejection or repeated malformed call also forces
        # this same one-shot fallback; the fallback itself is never retried.
        terminal_text_round = force_text_fallback or attempts == max_calls - 1
        if terminal_text_round:
            tools = None
        elif (
            external_content_seen or mutation_outcome_unknown or outbound_tools_blocked
        ):
            # Web results are untrusted model input. Once one is present, page
            # text cannot spend the original write authorization or choose a
            # fresh outbound query/URL. Preserve the useful search -> fetch flow
            # only when search returned at least one exact allowlisted URL; the
            # call is checked again below before dispatch.
            blocked_tools: set[str] = set()
            if external_content_seen:
                blocked_tools.update(_PLATFORM_MUTATION_TOOLS)
                # Every MCP call crosses an outbound data boundary.  A matching
                # read-only approval permits parallel execution before external
                # content is observed, but cannot permit a later data-dependent
                # request which could exfiltrate private conversation history.
                blocked_tools.update(mcp_names)
                blocked_tools.add("web_search")
                blocked_tools.add(tool_schema.TASK_TOOL)
                if not allowed_fetch_urls:
                    blocked_tools.add("web_fetch")
            # A timed-out MCP mutation may already have committed remotely.
            # With no server-wide idempotency contract, allowing the model to
            # try any later mutation can duplicate or compound unknown state.
            if mutation_outcome_unknown:
                blocked_tools.update(_PLATFORM_MUTATION_TOOLS)
                blocked_tools.update(mutating_mcp_names)
            # A private read may expose persona, workspace/artifact, or memory
            # content. That observation cannot influence a later outbound
            # query/URL/MCP/task call. Local durable edits remain available:
            # read-then-edit is a core workspace/working-memory workflow and
            # still carries the original user/wake seed plus generation fence.
            if outbound_tools_blocked:
                blocked_tools.update({"web_search", "web_fetch"})
                blocked_tools.update(mcp_names)
                # A parent task can hand private workspace/memory-derived text
                # to a child which still has outbound web access.  Treat that
                # delegation as another outbound channel, not as a local read.
                blocked_tools.add(tool_schema.TASK_TOOL)
            tools = [spec for spec in turn_catalog if spec.name not in blocked_tools]
        else:
            tools = turn_catalog
        if tools is not None and completed_memory_discovery_tools:
            # Each discovery mode may run once. A keyword search can legitimately
            # miss while memory_index still finds the user's saved cards, so do
            # not suppress both after only one observation.
            tools = [
                spec
                for spec in tools
                if spec.name not in completed_memory_discovery_tools
            ]
        requirement_already_met = (
            not file_delivery_required
            or (
                bool(delivered_file_suffixes)
                if not normalized_required_suffixes
                else normalized_required_suffixes.issubset(delivered_file_suffixes)
            )
        )
        forced_delivery_tool = ""
        if tools is not None and file_delivery_required and not requirement_already_met:
            # Keep the recovery path narrow enough for weaker tool-using models.
            tools = [spec for spec in tools if spec.name in _FILE_DELIVERY_TOOLS]
            if file_delivery_recovery_needed:
                forced_delivery_tool = (
                    tool_schema.FILE_REPLY_TOOL
                    if workspace_write_applied
                    else "workspace_write"
                )
                tools = [spec for spec in tools if spec.name == forced_delivery_tool]
        tail_window = None
        adaptive_planner = getattr(build_messages, "plan_provider_round", None)
        try:
            if callable(adaptive_planner):
                messages, frontier_plan, tail_window = adaptive_planner(
                    transcript=list(transcript),
                    tools=tools,
                    model_limit=model_prompt_limit,
                    output_reserve_tokens=prompt_output_reserve_tokens,
                    safety_margin_tokens=prompt_safety_margin_tokens,
                    utf8_bytes_per_token=prompt_estimator_utf8_bytes_per_token,
                    image_reserve_tokens=prompt_image_reserve_tokens,
                )
            else:
                frontier_plan = prompt_frontier.plan_provider_round(
                    model_limit=model_prompt_limit,
                    messages=messages,
                    tools=tools,
                    output_reserve_tokens=prompt_output_reserve_tokens,
                    safety_margin_tokens=prompt_safety_margin_tokens,
                    utf8_bytes_per_token=prompt_estimator_utf8_bytes_per_token,
                    image_reserve_tokens=prompt_image_reserve_tokens,
                )
        except prompt_frontier.PromptFrontierExhausted:
            if on_prompt_frontier_exhaustion is not None:
                try:
                    on_prompt_frontier_exhaustion()
                except Exception:
                    pass
            raise
        if tail_window is not None and on_tail_window is not None:
            try:
                on_tail_window(dict(tail_window))
            except Exception:
                pass
        if "tool_schemas" in frontier_plan.omitted_optional_components:
            # Schemas are atomic and optional: omitting the whole catalog is a
            # valid weak-model/text-only degradation. Required conversation and
            # native tool exchanges are never clipped to make room.
            tools = None
        if before_provider_call is not None:
            before_provider_call()
        await _trajectory(
            "provider_request",
            {
                "round": attempts + 1,
                "messages": messages,
                "tools": tools,
                "forced_tool": forced_delivery_tool,
                "prompt_frontier": frontier_plan,
                "tail_window": tail_window,
            },
        )
        attempts += 1
        _progress("provider_start")
        try:
            provider_kwargs = {"tools": tools}
            if not require_reply:
                # Only send the non-default so every existing caller's wire
                # stays byte-identical.
                provider_kwargs["require_reply"] = False
            if (
                forced_delivery_tool
                and tools is not None
                and str(getattr(provider_config, "provider", "")).strip().lower()
                == "openrouter"
            ):
                provider_kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": forced_delivery_tool},
                }
            reasoning_effort = str(
                getattr(provider_config, "reasoning_effort", "") or ""
            ).strip().lower()
            # A protocol fallback must produce usable plain text. Some reasoning
            # relays can spend the whole fallback budget on hidden reasoning and
            # return no reply body, which turns a safe degradation into another
            # provider failure. Earlier rounds already captured reasoning, so the
            # tools-disabled correction round intentionally asks for text only.
            if (
                (
                    include_reasoning
                    or (
                        reasoning_effort
                        and reasoning_effort not in {"off", "none"}
                    )
                )
                and not terminal_text_round
                and not suppress_native_reasoning
            ):
                provider_kwargs["include_reasoning"] = True
            if on_file_reply is not None:
                # File-capable foreground chat may need to place an entire
                # generated document inside workspace_write arguments. The
                # provider client's historical 700-token default predates V2
                # tools and truncates even modest documents into malformed
                # JSON. Use the output budget already reserved by V2's prompt
                # frontier; wake/child/screen lanes omit on_file_reply and keep
                # their existing limits unchanged.
                provider_kwargs["max_tokens"] = prompt_output_reserve_tokens
            result = await provider_client.reliable_chat_completion_async(
                provider_config,
                messages,
                max_attempts=(1 if forced_delivery_tool else 2),
                base_delay_sec=0.2,
                max_delay_sec=1.0,
                **provider_kwargs,
            )
        except Exception as exc:
            attempt_trace = provider_client.runtime_provider_attempt_trace(exc)
            await _trajectory(
                "provider_error",
                {
                    "round": attempts,
                    "error_class": type(exc).__name__,
                    "tools_enabled": tools is not None,
                    "provider_attempt_trace": attempt_trace,
                },
            )
            # TurnMetrics' docstring promises failed provider calls ARE counted
            # (model_calls bumped, just with no token usage) — add_usage(None)
            # before either falling back or propagating.
            add_usage(None)
            if on_provider_failure is not None:
                try:
                    await on_provider_failure(exc)
                except Exception:
                    pass
            if (
                file_delivery_retry_used
                and not normalized_required_suffixes.issubset(
                    delivered_file_suffixes
                )
            ):
                # The extra completion attempt is best-effort. A provider error
                # here must not retroactively discard the usable text from the
                # original terminal response that triggered the retry.
                await _record_required_file_missing(attempts)
                await _trajectory(
                    "reply_planned",
                    {
                        "round": attempts,
                        "final": True,
                        "text": file_delivery_fallback_text,
                        "reason": "required_file_missing",
                    },
                )
                if file_delivery_fallback_text.strip():
                    try:
                        await on_reply(
                            file_delivery_fallback_text,
                            final=True,
                            reasoning=file_delivery_fallback_reasoning,
                        )
                    except FinalReplySuperseded:
                        _progress("final_reply_superseded")
                        await _trajectory(
                            "final_reply_superseded",
                            {"round": attempts},
                        )
                        if attempts < max_calls:
                            delivery_retry_instruction = ""
                            file_delivery_retry_used = False
                            required_file_missing_recorded = False
                            file_delivery_fallback_text = ""
                            file_delivery_fallback_reasoning = ""
                            continue
                        return LoopOutcome(
                            "", attempts, "input_advanced", replied_intermediate
                        )
                return LoopOutcome(
                    file_delivery_fallback_text,
                    attempts,
                    "required_file_missing",
                    replied_intermediate,
                )
            if (
                tools is not None
                and isinstance(exc, provider_client.ProviderError)
                and exc.status_code in {400, 422}
                and attempts < max_calls
                and _is_probably_tool_schema_rejection(exc)
            ):
                force_text_fallback = True
                await _trajectory(
                    "protocol_fallback",
                    {"round": attempts, "reason": "tool_schema_rejected"},
                )
                _progress("provider_retry_boundary")
                continue
            raise
        _progress("provider_complete")
        add_usage(result.get("usage"))
        if on_provider_success is not None:
            try:
                await on_provider_success()
            except Exception:
                pass
        await _trajectory(
            "provider_response",
            {"round": attempts, "response": result},
        )
        # Exact wire attempts are now durably encrypted. Do not retain large
        # prompt/image bodies through the following tool batch merely because
        # ProviderResponse.raw keeps its input mapping alive.
        result = provider_client.without_runtime_provider_attempt_trace(result)
        pr = ProviderResponse.from_result(result)
        _capture_reasoning(pr.raw)

        # A tools-disabled request is terminal even if a broken relay invents a
        # tool call anyway.  Never execute an undeclared call; use only its text.
        if tools is None or not pr.tool_calls:
            requirement_met = (
                not file_delivery_required
                or (
                    bool(delivered_file_suffixes)
                    if not normalized_required_suffixes
                    else normalized_required_suffixes.issubset(
                        delivered_file_suffixes
                    )
                )
            )
            if not requirement_met:
                missing_suffixes = sorted(
                    normalized_required_suffixes - delivered_file_suffixes
                )
                if pr.text.strip() and not file_delivery_fallback_text:
                    file_delivery_fallback_text = pr.text
                    file_delivery_fallback_reasoning = str(
                        pr.raw.get("reasoning") or ""
                    )
                if missing_suffixes:
                    target = ", ".join(missing_suffixes)
                    delivery_retry_instruction = (
                        "REQUIRED FILE DELIVERY: The user explicitly requested "
                        f"downloadable output in {target}. Do not finish with "
                        "plain text. Create editable source with workspace_write, "
                        "then call send_file for every missing format before the "
                        "terminal reply."
                    )
                else:
                    delivery_retry_instruction = (
                        "REQUIRED FILE DELIVERY: The user explicitly requested a "
                        "downloadable file. Do not finish with plain text. Create "
                        "it with workspace_write, then call send_file before the "
                        "terminal reply."
                    )
                force_text_fallback = False
                # One guard-triggered recovery is enough. Tool calls emitted by
                # that recovery may still take later rounds to write, deliver,
                # and finish; the guard itself must not keep re-arming or consume
                # the tools-disabled final round when delivery is impossible.
                file_delivery_recovery_needed = True
                if not file_delivery_retry_used and attempts < max_calls - 1:
                    file_delivery_retry_used = True
                    _progress("required_file_retry_boundary")
                    continue
                await _record_required_file_missing(attempts)
                terminal_text = file_delivery_fallback_text or pr.text
                terminal_reasoning = (
                    file_delivery_fallback_reasoning
                    if file_delivery_fallback_text
                    else str(pr.raw.get("reasoning") or "")
                )
                await _trajectory(
                    "reply_planned",
                    {
                        "round": attempts,
                        "final": True,
                        "text": terminal_text,
                        "reason": "required_file_missing",
                    },
                )
                if terminal_text.strip():
                    try:
                        await on_reply(
                            terminal_text,
                            final=True,
                            reasoning=terminal_reasoning,
                        )
                    except FinalReplySuperseded:
                        _progress("final_reply_superseded")
                        await _trajectory(
                            "final_reply_superseded",
                            {"round": attempts},
                        )
                        if attempts < max_calls:
                            delivery_retry_instruction = ""
                            file_delivery_retry_used = False
                            required_file_missing_recorded = False
                            file_delivery_fallback_text = ""
                            file_delivery_fallback_reasoning = ""
                            continue
                        return LoopOutcome(
                            "", attempts, "input_advanced", replied_intermediate
                        )
                return LoopOutcome(
                    terminal_text,
                    attempts,
                    "required_file_missing",
                    replied_intermediate,
                )
            await _trajectory(
                "reply_planned",
                {
                    "round": attempts,
                    "final": True,
                    "text": pr.text,
                    "reason": "terminal_provider_text",
                },
            )
            try:
                # Plain text IS the final reply (no responder).  The worker's
                # final-effect callback may reject it atomically when user input
                # arrived while this provider request was in flight.  Never put
                # the rejected assistant text in the transcript: the next round
                # must answer the newly folded conversation, not critique a
                # response the user never saw.
                await on_reply(
                    pr.text,
                    final=True,
                    reasoning=_merged_reasoning(),
                )
            except FinalReplySuperseded:
                reasoning_fragments.clear()
                seen_reasoning_fragments.clear()
                _progress("final_reply_superseded")
                await _trajectory(
                    "final_reply_superseded",
                    {"round": attempts},
                )
                if attempts < max_calls:
                    continue
                return LoopOutcome("", attempts, "input_advanced", replied_intermediate)
            return LoopOutcome(pr.text, attempts, "final_text", replied_intermediate)

        call_ids = [tc.id for tc in pr.tool_calls]
        argument_sizes = [
            (_serialized_chars(tc.args) if tc.args_ok else len(str(tc.args_raw or "")))
            for tc in pr.tool_calls
        ]
        native_turn_size = (
            _serialized_chars(pr.assistant_turn.payload)
            if pr.assistant_turn is not None
            else 0
        )
        oversized_tool_exchange = (
            any(size is None or size > max_tool_args_chars for size in argument_sizes)
            or sum(size or 0 for size in argument_sizes) > max_tool_batch_args_chars
            or native_turn_size is None
            or native_turn_size > max_native_assistant_turn_chars
            or len(pr.text) > max_assistant_tool_text_chars
        )
        over_tool_call_budget = (
            len(pr.tool_calls) > max_tool_calls_per_round
            or tool_calls_used + len(pr.tool_calls) > max_tool_calls_per_turn
        )
        offered_names = {spec.name for spec in tools}
        malformed = any(
            not tc.id
            or not tc.name
            or not tc.args_ok
            or (
                tc.name not in offered_names
                and tc.name not in completed_memory_discovery_tools
            )
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
            tc.name in {tool_schema.REPLY_TOOL, tool_schema.FILE_REPLY_TOOL}
            for tc in pr.tool_calls
        ) and any(
            tc.name in _PLATFORM_MUTATION_TOOLS or tc.name in mutating_mcp_names
            for tc in pr.tool_calls
        )
        if (
            malformed
            or len(set(call_ids)) != len(call_ids)
            or mixed_reply_write
            or over_tool_call_budget
            or oversized_tool_exchange
        ):
            # Invalid, over-budget, and duplicate-id batches are all-or-nothing:
            # executing a valid subset and then asking for a correction can
            # duplicate durable writes on the corrected round. Missing/duplicate
            # ids also cannot form a provider-native result exchange. Make exactly
            # one tools-disabled fallback from the original prompt instead.
            # A reply plus either a platform or MCP mutation is rejected for the
            # same reason: the bubble cannot truthfully claim success before the
            # later sink commits. The model may mutate in one round and reply only
            # after observing its result in the next.
            if attempts >= max_calls:
                break
            await _trajectory(
                "protocol_fallback",
                {
                    "round": attempts,
                    "reason": "invalid_or_over_budget_tool_exchange",
                    "malformed": malformed,
                    "mixed_reply_write": mixed_reply_write,
                    "over_tool_call_budget": over_tool_call_budget,
                    "oversized_tool_exchange": oversized_tool_exchange,
                },
            )
            force_text_fallback = True
            continue

        tool_calls_used += len(pr.tool_calls)

        # text accompanying tool_calls = preamble/thinking, NOT a bubble.
        reply_calls = [tc for tc in pr.tool_calls if tc.name == tool_schema.REPLY_TOOL]
        file_reply_calls = [
            tc for tc in pr.tool_calls if tc.name == tool_schema.FILE_REPLY_TOOL
        ]
        loop_reply_tools = {tool_schema.REPLY_TOOL, tool_schema.FILE_REPLY_TOOL}
        other_calls = [tc for tc in pr.tool_calls if tc.name not in loop_reply_tools]
        reply_results: dict[str, ToolResult] = {}
        repeated_memory_calls = []
        dispatch_calls = []
        discovery_names_in_batch: set[str] = set()
        for tc in other_calls:
            repeated_discovery = (
                tc.name in completed_memory_discovery_tools
                or tc.name in discovery_names_in_batch
            )
            if repeated_discovery:
                repeated_memory_calls.append(tc)
                continue
            dispatch_calls.append(tc)
            if tc.name in _MEMORY_DISCOVERY_TOOLS:
                discovery_names_in_batch.add(tc.name)
        for tc in repeated_memory_calls:
            await _tool_event(tc, "tool_call_started", {})
            repeated_result = ToolResult(
                call_id=tc.id,
                content=(
                    "ok: this memory discovery was already completed; use its "
                    "prior result and continue without calling it again"
                ),
            )
            reply_results[tc.id] = repeated_result
            await _tool_event(
                tc, "tool_call_result", {"result": repeated_result}
            )
        for tc in reply_calls:
            reply_text = str(tc.args.get("text") or "")
            await _tool_event(tc, "tool_call_started", {})
            await _trajectory(
                "reply_planned",
                {
                    "round": attempts,
                    "final": False,
                    "call_id": tc.id,
                    "text": reply_text,
                },
            )
            # Pass this round's reasoning so the sink can detect a cross-channel
            # torn-protocol leak on the intermediate reply too (Codex code-review
            # #1): without it a chat-lane tail is only ever weak evidence and
            # would be delivered. NOTE: the delivery-confirmation contract below
            # still marks a non-empty reply as delivered even when the sink
            # suppresses it — a pre-existing gap (degenerate replies hit it too);
            # tightening on_reply to report published/suppressed is a separate,
            # wider change left for review.
            try:
                await on_reply(
                    reply_text, final=False, reasoning=str(pr.raw.get("reasoning") or "")
                )  # immediate intermediate bubble
            except Exception as exc:
                await _tool_event(
                    tc, "tool_call_error", {"error": type(exc).__name__}
                )
                raise
            if reply_text.strip():
                replied_intermediate = True
                content = "ok: reply delivered"
            else:
                content = "ok: empty reply ignored"
            reply_result = ToolResult(call_id=tc.id, content=content)
            reply_results[tc.id] = reply_result
            await _tool_event(
                tc, "tool_call_result", {"result": reply_result}
            )

        for tc in file_reply_calls:
            workspace_path = str(tc.args.get("path") or "").strip()
            workspace_revision = int(tc.args["revision"])
            await _tool_event(tc, "tool_call_started", {})
            await _trajectory(
                "file_reply_planned",
                {"round": attempts, "call_id": tc.id},
            )
            # Presence is guaranteed by the offer gate above. Keep the explicit
            # check as a defense for direct/internal callers.
            if on_file_reply is None:
                raise RuntimeError("send_file callback is unavailable")
            file_suffix = posixpath.splitext(workspace_path)[1].casefold()
            if (
                normalized_required_suffixes
                and file_suffix not in normalized_required_suffixes
            ):
                file_result = ToolResult(
                    call_id=tc.id,
                    content=(
                        "error: wrong file format; required suffixes: "
                        + ", ".join(sorted(normalized_required_suffixes))
                    ),
                )
                reply_results[tc.id] = file_result
                await _tool_event(
                    tc, "tool_call_result", {"result": file_result}
                )
                continue
            try:
                await on_file_reply(workspace_path, workspace_revision)
            except Exception as exc:
                await _tool_event(
                    tc, "tool_call_error", {"error": type(exc).__name__}
                )
                raise
            delivered_file_suffixes.add(file_suffix)
            replied_intermediate = True
            file_result = ToolResult(
                call_id=tc.id,
                content="ok: file delivered",
            )
            reply_results[tc.id] = file_result
            await _tool_event(
                tc, "tool_call_result", {"result": file_result}
            )

        if other_calls:
            await _trajectory(
                "tool_batch_planned",
                {"round": attempts, "calls": other_calls},
            )
        dispatched = list(await dispatch_tools(dispatch_calls)) if dispatch_calls else []
        _progress("tool_batch_complete")
        dispatched_by_id = {result.call_id: result for result in dispatched}
        if len(dispatched_by_id) != len(dispatched):
            raise RuntimeError("tool dispatcher returned duplicate call ids")
        ordered_results: list[ToolResult] = []
        for tc in pr.tool_calls:
            result_for_call = reply_results.get(tc.id) or dispatched_by_id.get(tc.id)
            if result_for_call is None:
                raise RuntimeError(f"tool dispatcher omitted result for call {tc.id!r}")
            ordered_results.append(result_for_call)
        if set(dispatched_by_id) != {tc.id for tc in dispatch_calls}:
            raise RuntimeError("tool dispatcher returned mismatched call ids")

        if any(
            tc.name in mutating_mcp_names
            and str(ordered_results[index].content)
            == MCP_MUTATION_OUTCOME_UNKNOWN_ERROR
            for index, tc in enumerate(pr.tool_calls)
        ):
            mutation_outcome_unknown = True

        # This is the provider-neutral trust boundary: platform, MCP, reply, and
        # any future injected dispatcher all receive identical prompt budgets.
        # Normalize before deriving the web-fetch allowlist so a URL the model did
        # not actually receive can never become fetch-authorized.
        ordered_results = _normalize_tool_results(
            ordered_results,
            per_result_cap=tool_result_char_cap,
            batch_cap=tool_batch_result_char_cap,
        )
        ordered_results_by_id = {result.call_id: result for result in ordered_results}
        if other_calls:
            # Capture only the same bounded results the next provider round
            # receives. A malicious/buggy MCP response must not make the flight
            # recorder serialize an unbounded pre-normalization object.
            await _trajectory(
                "tool_batch_result",
                {
                    "round": attempts,
                    "calls": other_calls,
                    "results": [ordered_results_by_id[tc.id] for tc in other_calls],
                },
            )
        for tc in other_calls:
            result = ordered_results_by_id[tc.id]
            if tc.name == "web_search":
                allowed_fetch_urls.update(_search_result_urls(result.content))
            elif tc.name == "web_fetch":
                allowed_fetch_urls.discard(str(tc.args.get("url") or "").strip())
        completed_memory_discovery_tools.update(
            tc.name for tc in dispatch_calls if tc.name in _MEMORY_DISCOVERY_TOOLS
        )
        if any(
            tc.name == "workspace_write"
            and str(ordered_results_by_id[tc.id].content).strip().lower().startswith("ok")
            for tc in dispatch_calls
        ):
            workspace_write_applied = True
        if any(
            tc.name in provenance.EXTERNAL_READS or tc.name in mcp_names
            for tc in dispatch_calls
        ):
            # Set only after dispatch: a write selected in the SAME batch was
            # chosen before the model saw the external result.  Only later
            # rounds are influenced by that result and therefore lose writes
            # and all outbound MCP tools.  ``task`` is deliberately included
            # without inspecting its child transcript: a child summary may
            # contain web data, private workspace data, or both, so provenance
            # propagates conservatively across the subagent boundary.
            external_content_seen = True
        if any(_read_blocks_later_outbound(tc) for tc in dispatch_calls):
            # Same-batch outbound calls were selected before the model observed
            # this result. Only subsequent rounds are data-dependent and fenced.
            outbound_tools_blocked = True

        transcript.append(
            ToolExchange(
                calls=tuple(pr.tool_calls),
                results=tuple(ordered_results),
                assistant_text=pr.text,
                assistant_turn=pr.assistant_turn,
            )
        )
    # Only reachable for max_calls == 0, or when a malformed response consumed the
    # last reserved attempt before a fallback could be made.
    await _trajectory(
        "loop_exhausted",
        {"rounds": attempts, "tool_calls_used": tool_calls_used},
    )
    return LoopOutcome("", attempts, "budget_exhausted", replied_intermediate)
