"""Unified provider-native tool loop (spec C2). Dependency-clean: no hosted/agent_runtime/db;
all side effects injected. One loop for every model — no is_official branch."""

from __future__ import annotations
from dataclasses import dataclass
import inspect
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


def _has_tagged_image_message(messages, tag_key: str) -> bool:
    return bool(tag_key) and any(
        isinstance(message, dict) and message.get(tag_key) is True
        for message in messages
    )


def _without_tagged_image_messages(messages, tag_key: str) -> list:
    if not tag_key:
        return list(messages)
    return [
        message
        for message in messages
        if not (isinstance(message, dict) and message.get(tag_key) is True)
    ]

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
_EMPTY_RESPONSE_CORRECTION = (
    "The previous response completed without visible text or a client tool call. "
    "Complete the user's request now. Return either non-empty visible answer text "
    "or a valid call to one of the offered client tools. Do not return a "
    "thinking-only response."
)
_CONTENT_FREE_STOP_REASONS = frozenset(
    {
        "blocklist",
        "content_filter",
        "end_turn",
        "function_call",
        "image_safety",
        "language",
        "length",
        "malformed_function_call",
        "max_tokens",
        "other",
        "pause_turn",
        "prohibited_content",
        "recitation",
        "refusal",
        "safety",
        "spii",
        "stop",
        "stop_sequence",
        "tool_calls",
        "tool_use",
    }
)


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = tool_schema.build_tool_specs()
    return _CATALOG


def _memory_discovery_call_key(tool_call) -> tuple[str, str] | None:
    """Return the per-turn deduplication key for a memory discovery call.

    ``memory_index`` remains a once-per-turn overview regardless of filters.
    Other discovery tools (currently ``memory_search``) may legitimately run more
    than once for different subjects, so only an exact canonical-JSON argument
    match is repeated. Query whitespace and case stay significant; normalizing
    either could merge distinct searches.
    """
    if tool_call.name not in _MEMORY_DISCOVERY_TOOLS:
        return None
    if tool_call.name == "memory_index":
        return (tool_call.name, "")
    return (
        tool_call.name,
        json.dumps(
            tool_call.args,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


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


# 判断按**小句**做,不按整段。整段判断有三个治不好的毛病(codex 两轮审出):
#   1. 一句里的「失败」会赦免另一句里的谎报(「图生成失败了。不过图已经画好了」);
#   2. 表达不了「完成的不是图,是提示词/思路」——「已经为你做好了图片生成提示词」;
#   3. 表达不了引用与假设——「如果我说『图片已经生成』,那是在骗你」。
# 小句级判断天然解决:每个小句自己带着自己的否定、引用、宾语。
# ⚠️ 切分必须**保留结尾标点**。第一版用 [。！？!?] 当分隔符,切完小句里根本没有
# 问号 —— 于是任何"疑问句检测"都不可能生效,「图片已经生成了吗?」被判成谎报。
# codex 第三轮审出;根因是切分,不是词表不够。
_CLAUSE_RE = re.compile(r"[^。！？!?;；\n,，]+[。！？!?;；\n,，]?")

# 引号里的内容是**被谈论的话**,不是此刻的断言:
#   「你想让我说"图片已经生成"吗?」「"图片已经生成"只是一个示例。」
_QUOTED_RE = re.compile(r"[「『\"'“”‘’]([^「』\"'“”‘’]{0,60})[」』\"'“”‘’]")
_META_RE = re.compile(
    r"(示例|例子|举例|比如|原话|引用|假装|placeholder|example|for instance)",
    re.IGNORECASE,
)
# 疑问不是断言。中文靠句末语气词或问号,英文靠助动词/疑问词开头。
_QUESTION_RE = re.compile(
    r"[?？]|(吗|呢|么)\s*[。.]?\s*$"
    r"|^\s*(are|is|do|does|did|would|will|can|could|shall|should|have|has"
    r"|why|what|how)\b",
    re.IGNORECASE,
)

# 这个小句在谈**别的产物**,不是图本身。命中即整句放行。
_NOT_AN_IMAGE_RE = re.compile(
    r"(提示词|prompt|思路|构思|方案|描述|计划|草案|框架|步骤|流程"
    r"|教程|指南|guide|说明|文档|功能|设置|选项|配置|模型|服务|接口"
    r"|description|plan|idea|outline|draft|tutorial|instruction)",
    re.IGNORECASE,
)
# 否定 / 未完成 / 假设 —— 这些小句不是在断言「图已经在这儿」。
_NOT_A_CLAIM_RE = re.compile(
    r"(失败|没有|没能|未能|不能|无法|出错|报错|需要|如果|要是|假如|假设"
    r"|并没|尚未|还没|不是|别|骗|谎"
    r"|fail|error|cannot|can['’]?t|could ?n['’]?t|unable|do(es)?\s+not"
    r"|don['’]?t|did ?n['’]?t|have ?n['’]?t|has ?n['’]?t|not\s+yet|yet\b"
    r"|if\s+i|suppose|would|unsupported|not\s+(be\s+)?(able|support))",
    re.IGNORECASE,
)
_IMAGE_NOUN = r"(?:自画像|画像|插画|图片|图像|图)"
_IMAGE_CLAIM_RE = re.compile(
    r"("
    # 图 + (已经) + 完成动词:「图片已经生成」「图片生成好了」
    rf"{_IMAGE_NOUN}\s*(?:也|都)?\s*(?:已经|已)\s*(?:生成|画|做|完成|发|给)"
    rf"|{_IMAGE_NOUN}\s*(?:生成好|生成完|画好|画完|做好|做完|完成)了"
    # (已经)(为你) + 完成动词 + 了 + 图:「已经为你生成了一张图片」
    rf"|(?:已经|已)\s*(?:为你|给你)?\s*(?:生成|画|做)(?:好|完)?了\s*"
    rf"(?:一|这|那)?\s*[张幅副]?\s*{_IMAGE_NOUN}"
    # 无宾语完成态「已经为你画好了」,**只在小句末尾**命中。不在末尾就说明后面
    # 跟着别的宾语(「已经为你做好了准备」)——那类宾语是开集,靠词表挡不住,
    # 只有位置挡得住。
    r"|(?:已经|已)\s*(?:为你|给你)?\s*(?:生成|画|做)(?:好|完)了\s*[。.]?\s*$"
    # 英文。连字符要一起挡("image-generation guide"),所以边界用 (?![\w-])。
    r"|here\s*(?:['’]?s|\s+is|\s+are)\s+(?:the|your|a|an)?\s*"
    r"(?:images?|pictures?|illustrations?|artworks?|drawings?)(?![\w-])"
    r"|i\s*['’]?\s*(?:ve|have)\s+(?:just\s+)?(?:created|generated|drawn|made)\s+"
    r"(?:the|an?|your)?\s*(?:images?|pictures?|illustrations?|drawings?)(?![\w-])"
    r")",
    re.IGNORECASE,
)


def _claims_image_delivered(text: str) -> bool:
    """这段话是不是在断言「图已经存在」?

    只用来识别**谎报**(说了却没有 media),不用来判断用户想不想要图 ——
    那个判断权归伴侣自己。

    宁可漏判也不要误判:漏判只是少一次纠正;误判会把伴侣一句诚实的失败说明
    ("图片生成失败了")当成谎话打回去,**等于逼它把真话改成假话**。

    按**小句**判,不按整段 —— 整段判断有三个治不好的毛病:
      1. 一句里的「失败」会赦免另一句里的谎报;
      2. 表达不了「完成的不是图,是提示词/思路」;
      3. 表达不了引用与假设。
    """
    for raw_clause in _CLAUSE_RE.findall(str(text or "")):
        clause = raw_clause.strip()
        if not clause:
            continue
        if _QUESTION_RE.search(clause) or _META_RE.search(clause):
            continue
        if _NOT_A_CLAIM_RE.search(clause) or _NOT_AN_IMAGE_RE.search(clause):
            continue
        # 引号内容剥掉再判:引号里的是被谈论的话,不是此刻的断言。
        if _IMAGE_CLAIM_RE.search(_QUOTED_RE.sub(" ", clause)):
            return True
    return False


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
    makes the whole result unparseable for the model.  An atomic result that
    arrives *over* its own cap is a producer contract violation, not something
    to slice: it is replaced wholesale by one short, valid error result before
    the water-fill sees it, so nothing downstream can reserve or cut a broken
    payload.  (``worker`` rejects the configurations that would make this
    reachable at start-up; this is the defence-in-depth half.)

    A batch with no such result takes exactly the path it always took — same
    per-result cap, same batch cap, same quotas, byte for byte.
    """
    policies = [result_budget.for_metadata(result.metadata) for result in results]
    results = [
        ToolResult(
            call_id=result.call_id,
            content=result_budget.OVERFLOW_RESULT_CONTENT,
            metadata=result.metadata,
        )
        if (
            policy is not None
            and policy.atomic_json
            and len(result.content) > policy.result_cap
        )
        else result
        for result, policy in zip(results, policies)
    ]
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
    delivered_media_count: int = 0


class FinalReplySuperseded(RuntimeError):
    """The final reply lost the atomic late-input fence before publication.

    ``on_reply`` raises this only after the candidate reply effect has been
    terminally discarded without reaching its sink.  It is therefore safe for
    the loop to fold the newly arrived input and ask the provider again.  An
    intermediate ``reply{}`` bubble never uses this signal: those bubbles are
    intentionally immediate and remain visible while the turn continues.
    """


class ProviderEmptyReply(RuntimeError):
    """A structurally valid provider success had no foreground-usable output."""


def _empty_response_shape(pr: ProviderResponse) -> dict[str, object]:
    """Return content-free diagnostics for a provider success with no output."""
    raw_stop_reason = str(pr.raw.get("stop_reason") or "").strip().lower()
    return {
        "stop_reason": (
            raw_stop_reason
            if raw_stop_reason in _CONTENT_FREE_STOP_REASONS
            else ("other" if raw_stop_reason else "")
        ),
        "has_visible_text": bool(pr.text.strip()),
        "reasoning_present": bool(str(pr.raw.get("reasoning") or "").strip()),
        "tool_call_count": len(pr.tool_calls),
        "completion_tokens": pr.usage.completion_tokens,
    }


def _with_system_suffix(messages: list, suffix: str) -> list:
    """Append a transient system suffix while preserving native exchanges."""
    instruction = str(suffix or "").strip()
    if not instruction:
        return messages
    updated = [
        dict(message) if isinstance(message, dict) else message
        for message in messages
    ]
    if (
        updated
        and isinstance(updated[0], dict)
        and updated[0].get("role") == "system"
    ):
        updated[0]["content"] = (
            str(updated[0].get("content") or "").rstrip()
            + "\n\n"
            + instruction
        )
    else:
        updated.insert(0, {"role": "system", "content": instruction})
    return updated


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
    on_provider_tool_surface=None,
    on_provider_success=None,
    on_provider_failure=None,
    fold_before_first: bool = False,
    on_progress=None,
    on_trajectory_event=None,
    extra_tool_specs=None,
    refresh_extra_tool_specs=None,
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
    allow_image_output: bool = False,
    on_file_reply=None,
    on_image_reply=None,
    on_tool_event=None,
    required_file_suffixes: tuple[str, ...] | None = None,
    file_requirement_messages=(),
    resolve_required_file_suffixes=None,
    on_file_requirement_changed=None,
    outbound_blocking_read_tool_names=None,
    outbound_blocking_read_tool_predicate=None,
    initial_outbound_tools_blocked: bool = False,
    tagged_image_message_key: str = "",
    on_tagged_images_rejected=None,
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
    later provider round.

    Image generation belongs to the companion: it decides when to call
    ``generate_image`` and writes the prompt itself. The loop only carries its
    words alongside the picture, hands generation failures back as tool results,
    and bounces an unbacked "I made the image" claim exactly once."""
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
    force_text_fallback_reason = ""
    empty_response_recovery_used = False
    empty_response_retry_instruction = ""
    external_content_seen = False
    mutation_outcome_unknown = False
    outbound_tools_blocked = bool(initial_outbound_tools_blocked)
    tagged_image_fallback_active = False
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
    image_claim_bounces = 0
    image_claim_retry_instruction = ""
    file_delivery_fallback_reasoning = ""
    file_delivery_recovery_needed = False
    workspace_write_applied = False
    # Names keep required schemas visible and completed discovery calls valid in
    # native history. Exact call keys independently decide whether dispatch would
    # repeat work; do not collapse these sets back into one name-only concept.
    completed_memory_discovery_tools: set[str] = set()
    completed_memory_discovery_calls: set[tuple[str, str]] = set()
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
    # injected for this turn (chat lane only). `mcp_names` is frozen from the
    # entry snapshot so a dynamic schema refresh can replace definitions but can
    # never grant a new tool name mid-turn. The MCP server validates tool args.
    # MCP result content remains external model input, but the user explicitly
    # chose each MCP endpoint. Under the accepted trust boundary, observing that
    # content does not remove later user-MCP calls; platform web/task fences and
    # the unknown-mutation idempotency gate still apply below.
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
    if on_image_reply is None:
        disabled_names.add(tool_schema.IMAGE_REPLY_TOOL)
    initial_extra_specs = list(extra_tool_specs or [])
    mcp_names = {spec.name for spec in initial_extra_specs}

    def _turn_catalog() -> list:
        current_extra_specs = initial_extra_specs
        if refresh_extra_tool_specs is not None:
            try:
                refreshed = list(refresh_extra_tool_specs() or [])
            except Exception:  # noqa: BLE001 - optional refresh fails stable
                refreshed = initial_extra_specs
            current_extra_specs = [
                spec for spec in refreshed if spec.name in mcp_names
            ]
        return [
            spec
            for spec in (_catalog() + current_extra_specs)
            if spec.name not in disabled_names
        ]
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
        turn_catalog = _turn_catalog()
        # 三道有界纠正共用同一条临时 system suffix 注入通道：文件投递缺失、
        # 生图谎报，以及语义空回复。各自最多打回一次，不写入 transcript。
        retry_instructions = "\n\n".join(
            instruction
            for instruction in (
                delivery_retry_instruction,
                image_claim_retry_instruction,
                empty_response_retry_instruction,
            )
            if instruction
        )
        if retry_instructions:
            messages = _with_system_suffix(messages, retry_instructions)
        # Reserve the final provider attempt for a terminal reply. Providers with
        # a real tool_choice=none keep schemas referenced by their native history;
        # other wires omit tools as before. A 400/422 schema rejection or repeated
        # malformed call also forces this bounded fallback.
        terminal_text_round = force_text_fallback or attempts == max_calls - 1
        historical_tool_names = {
            call.name
            for item in transcript
            if isinstance(item, ToolExchange)
            for call in item.calls
            if call.name
        }
        provider_name = str(
            getattr(provider_config, "provider", "") or ""
        ).strip().lower()
        terminal_schema_guard = (
            terminal_text_round
            and bool(historical_tool_names)
            and provider_name
            in {"anthropic", "openrouter", "openai_compatible", "deepseek"}
        )
        surface_candidate_tools = list(turn_catalog)
        surface_reason = ""
        if terminal_schema_guard:
            tools = [
                spec for spec in turn_catalog if spec.name in historical_tool_names
            ] or None
            surface_reason = (
                force_text_fallback_reason or "terminal_text_round"
            )
        elif terminal_text_round:
            tools = None
            surface_reason = (
                force_text_fallback_reason or "terminal_text_round"
            )
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
                # ⚠️ 2026-08-12 Seven 拍板:user-MCP 不再因为「看过外部内容」下架。
                #
                # 原规则:任何一次 MCP 调用之后,本轮**所有** MCP 工具从工具面消失
                # ——理由是每次 MCP 调用都跨出站边界,后一次请求可能被前一次的返回
                # 内容操纵去带走隐私。代价是**一轮只能调一次 MCP**,而记忆型服务器
                # 天生要「先取后存」:两位用户报的「MCP 只能读不能写」正是这条。
                #
                # 对齐 Claude Code —— 它对 MCP 没有这类围栏。服务器是用户自己挑的、
                # 自己填的地址与鉴权头,和「模型自己搜到的网页」不是同一个威胁模型。
                # 被接受的风险面:模型读过私密内容后可以把它发给用户配置的任意 MCP
                # 服务器(含论坛这类别人可写内容的)。**这是有意放宽,不是疏漏** ——
                # 谁想加回来,请先去看 docs/MCP_TRUST_BOUNDARY.md 里的决策记录。
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
            # content. That observation cannot influence a later platform-owned
            # query/URL/task call. User-selected MCP endpoints remain available.
            # Local durable edits remain available:
            # read-then-edit is a core workspace/working-memory workflow and
            # still carries the original user/wake seed plus generation fence.
            if outbound_tools_blocked:
                blocked_tools.update({"web_search", "web_fetch"})
                # ⚠️ 同一次放宽(2026-08-12):读过私密内容之后,user-MCP 也不再下架。
                #
                # 这条比上面那条更常触发,而且几乎没人意识到:_PRIVATE_READ_TOOLS 里
                # 有 memory_index / memory_search / memory_fetch,而模型**几乎每轮
                # 都要读记忆**(usr_dd0b 的 trace 里 memory.index.called ×5)。
                # 于是 MCP 往往在第一次读记忆之后就没了,连第一次调用都轮不上 ——
                # 用户看到的是「工具明明连着,AI 说用不了」。
                #
                # web/task 仍然拦着:那是模型自己选的目的地,MCP 是用户选的。
                blocked_tools.add(tool_schema.TASK_TOOL)
            tools = [spec for spec in turn_catalog if spec.name not in blocked_tools]
            surface_candidate_tools = list(tools)
        else:
            tools = turn_catalog
            surface_candidate_tools = list(tools)
        requirement_already_met = (
            not file_delivery_required
            or (
                bool(delivered_file_suffixes)
                if not normalized_required_suffixes
                else normalized_required_suffixes.issubset(delivered_file_suffixes)
            )
        )
        forced_delivery_tool = ""
        if (
            not terminal_text_round
            and tools is not None
            and file_delivery_required
            and not requirement_already_met
        ):
            # Keep the recovery path narrow enough for weaker tool-using models.
            tools = [spec for spec in tools if spec.name in _FILE_DELIVERY_TOOLS]
            surface_reason = "file_delivery_forced"
            if file_delivery_recovery_needed:
                forced_delivery_tool = (
                    tool_schema.FILE_REPLY_TOOL
                    if workspace_write_applied
                    else "workspace_write"
                )
                tools = [spec for spec in tools if spec.name == forced_delivery_tool]
        tail_window = None
        required_schema_names = (
            historical_tool_names
            if terminal_schema_guard
            else completed_memory_discovery_tools
        )
        adaptive_planner = getattr(build_messages, "plan_provider_round", None)
        try:
            if callable(adaptive_planner):
                messages, frontier_plan, tail_window = adaptive_planner(
                    transcript=list(transcript),
                    tools=tools,
                    required_tool_names=required_schema_names,
                    model_limit=model_prompt_limit,
                    output_reserve_tokens=prompt_output_reserve_tokens,
                    safety_margin_tokens=prompt_safety_margin_tokens,
                    utf8_bytes_per_token=prompt_estimator_utf8_bytes_per_token,
                    image_reserve_tokens=prompt_image_reserve_tokens,
                    system_suffix=retry_instructions,
                )
            else:
                frontier_plan = prompt_frontier.plan_provider_round(
                    model_limit=model_prompt_limit,
                    messages=messages,
                    tools=tools,
                    required_tool_names=required_schema_names,
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
        planned_tool_names = tuple(
            getattr(frontier_plan, "included_tool_names", ()) or ()
        )
        planned_offered_tool_names = tuple(
            getattr(frontier_plan, "offered_tool_names", ()) or ()
        )
        if planned_offered_tool_names:
            included_tool_names = set(planned_tool_names)
            tools = [
                spec
                for spec in (tools or [])
                if spec.name in included_tool_names
            ] or None
            if (
                not surface_reason
                and len(tools or []) < len(surface_candidate_tools)
            ):
                surface_reason = "frontier_omitted"
        elif "tool_schemas" in frontier_plan.omitted_optional_components:
            # Once a memory discovery result is present in the required native
            # transcript, keep that matching schema even when the remaining
            # optional catalog no longer fits. This compatibility branch also
            # supports injected/legacy planners that predate per-tool frontier
            # decisions.
            tools = [
                spec
                for spec in (tools or [])
                if spec.name in required_schema_names
            ] or None
            if (
                not surface_reason
                and len(tools or []) < len(surface_candidate_tools)
            ):
                surface_reason = "frontier_omitted"
        if before_provider_call is not None:
            before_provider_call()
        if tagged_image_fallback_active:
            messages = _without_tagged_image_messages(
                messages, tagged_image_message_key
            )
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
        provider_error: BaseException | None = None
        try:
            # V2 owns the lane-specific empty-response policy. Let the provider
            # parser return any structurally valid success so an abnormal HTTP
            # 200 is not retried as though it were a transient network failure.
            provider_kwargs = {"tools": tools, "require_reply": False}
            if terminal_schema_guard and tools is not None:
                provider_kwargs["tool_choice"] = "none"
            if allow_image_output and not terminal_text_round:
                provider_kwargs["allow_image_output"] = True
            if (
                forced_delivery_tool
                and not terminal_text_round
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
            if on_provider_tool_surface is not None:
                candidate_names = {
                    str(spec.name) for spec in surface_candidate_tools
                }
                sent_names = {str(spec.name) for spec in (tools or [])}
                mcp_candidate_names = candidate_names & mcp_names
                mcp_sent_names = sent_names & mcp_names
                try:
                    await on_provider_tool_surface(
                        {
                            "round": attempts,
                            "candidate_tool_count": len(candidate_names),
                            "sent_tool_count": len(sent_names),
                            "dropped_tool_count": len(candidate_names - sent_names),
                            "mcp_candidate_tool_count": len(mcp_candidate_names),
                            "mcp_sent_tool_count": len(mcp_sent_names),
                            "mcp_dropped_tool_count": len(
                                mcp_candidate_names - mcp_sent_names
                            ),
                            "reason": surface_reason or "none",
                        }
                    )
                except Exception:
                    pass
            result = await provider_client.reliable_chat_completion_async(
                provider_config,
                messages,
                max_attempts=(1 if forced_delivery_tool else 2),
                base_delay_sec=0.2,
                max_delay_sec=1.0,
                **provider_kwargs,
            )
        except Exception as exc:
            tagged_image_rejected = (
                not tagged_image_fallback_active
                and _has_tagged_image_message(messages, tagged_image_message_key)
                and getattr(exc, "status_code", None) in {400, 404, 415, 422}
            )
            if tagged_image_rejected:
                await _trajectory(
                    "provider_error",
                    {
                        "round": attempts,
                        "error_class": type(exc).__name__,
                        "tools_enabled": tools is not None,
                        "tagged_images_rejected": True,
                    },
                )
                add_usage(None)
                tagged_image_fallback_active = True
                messages = _without_tagged_image_messages(
                    messages, tagged_image_message_key
                )
                await _trajectory(
                    "provider_request",
                    {
                        "round": attempts,
                        "messages": messages,
                        "tools": tools,
                        "forced_tool": forced_delivery_tool,
                        "retry_without_tagged_images": True,
                    },
                )
                try:
                    result = await provider_client.reliable_chat_completion_async(
                        provider_config,
                        messages,
                        max_attempts=1,
                        base_delay_sec=0.2,
                        max_delay_sec=1.0,
                        **provider_kwargs,
                    )
                    # A successful text-only retry confirms that the rejected
                    # part was our tagged image block (important for ambiguous
                    # provider statuses such as OpenRouter's HTTP 404).
                    if on_tagged_images_rejected is not None:
                        callback_result = on_tagged_images_rejected(exc)
                        if inspect.isawaitable(callback_result):
                            await callback_result
                except Exception as retry_exc:
                    provider_error = retry_exc
            else:
                provider_error = exc
        if provider_error is not None:
            exc = provider_error
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
                force_text_fallback_reason = "tool_schema_rejected"
                await _trajectory(
                    "protocol_fallback",
                    {"round": attempts, "reason": "tool_schema_rejected"},
                )
                _progress("provider_retry_boundary")
                continue
            raise provider_error
        _progress("provider_complete")
        add_usage(result.get("usage"))
        raw_has_usable_output = bool(
            str(result.get("reply") or "").strip()
            or result.get("tool_calls")
            or result.get("media")
        )
        if (not require_reply or raw_has_usable_output) and on_provider_success is not None:
            try:
                await on_provider_success()
            except Exception:
                pass
        trajectory_result = result
        if result.get("media"):
            trajectory_result = dict(result)
            trajectory_result["media"] = [
                {
                    "mime_type": str(item.get("mime_type") or ""),
                    "encoded_chars": len(str(item.get("data_base64") or "")),
                }
                for item in result.get("media") or []
                if isinstance(item, dict)
            ]
            raw_turn = result.get("assistant_turn")
            if isinstance(raw_turn, dict):
                trajectory_result["assistant_turn"] = {
                    "wire": str(raw_turn.get("wire") or ""),
                    "payload": "[generated image omitted]",
                }
        await _trajectory(
            "provider_response",
            {"round": attempts, "response": trajectory_result},
        )
        # Exact wire attempts are now durably encrypted. Do not retain large
        # prompt/image bodies through the following tool batch merely because
        # ProviderResponse.raw keeps its input mapping alive.
        result = provider_client.without_runtime_provider_attempt_trace(result)
        pr = ProviderResponse.from_result(result)

        if (
            require_reply
            and not pr.text.strip()
            and not pr.tool_calls
            and not pr.media
        ):
            semantic_empty = bool(
                str(pr.raw.get("reasoning") or "").strip()
                or str(pr.raw.get("stop_reason") or "").strip()
            )
            can_correct = (
                semantic_empty
                and not empty_response_recovery_used
                and attempts < max_calls - 1
            )
            await _trajectory(
                "empty_provider_response",
                {
                    "round": attempts,
                    "reason": "empty_provider_success",
                    "response_shape": _empty_response_shape(pr),
                    "action": (
                        "semantic_correction"
                        if can_correct
                        else "fail_provider_empty_reply"
                    ),
                },
            )
            if can_correct:
                empty_response_recovery_used = True
                empty_response_retry_instruction = _EMPTY_RESPONSE_CORRECTION
                reasoning_fragments.clear()
                seen_reasoning_fragments.clear()
                _progress("empty_response_retry_boundary")
                continue
            exc = ProviderEmptyReply("empty_reply")
            if on_provider_failure is not None:
                try:
                    await on_provider_failure(exc)
                except Exception:
                    pass
            raise exc

        # The correction is a one-round system suffix, not transcript. Once the
        # provider returns usable text or a tool call, later native tool rounds
        # proceed with their original safety-filtered catalog and transcript.
        empty_response_retry_instruction = ""
        _capture_reasoning(pr.raw)

        # A tools-disabled request is terminal even if a broken relay invents a
        # tool call anyway.  Never execute an undeclared call; use only its text.
        if tools is None or not pr.tool_calls:
            # 伴侣声称画好了却一张图都没有 —— 打回去让它自己纠正。
            #
            # 这里原本是一道**正则意图闸**:用关键词判断「用户是不是在要图」,
            # 模型没调工具就拿**用户原话**去生图,并把模型那轮文字整个丢掉。
            # 拆掉的理由是它替伴侣做了本该由伴侣做的判断:「这时候有张图就更好了」
            # 这类含蓄请求正则一定判否(想画也画不成);拿用户原话当 prompt,画出来
            # 的东西不带伴侣的理解(不知道「自画像」里的「自」是谁);伴侣自己突然
            # 想画一张更是完全没有入口。**决定权归伴侣。**
            #
            # 但「说了没做」必须处理 —— 处理方式是**退回给它重答**,而不是我们替它
            # 补一张图或把话吞掉:它自己可以选择真去调工具,或者老实说没画成。
            # 只退一次(和 capture 的格式打回同一个预算观),再撒谎就是模型的问题,
            # 照原样发出并留痕,不再纠缠。
            if (
                pr.text
                and not pr.media
                and image_claim_bounces < 1
                and _claims_image_delivered(pr.text)
            ):
                image_claim_bounces += 1
                await _trajectory(
                    "image_claim_without_media_bounced",
                    {"round": attempts, "text_chars": len(pr.text)},
                )
                image_claim_retry_instruction = (
                    "上一轮你说图已经生成/画好了,但这一轮没有任何图片真的被生成。"
                    "请二选一,不要再声称已生成:"
                    "(1) 你确实想给出这张图 —— 调用 generate_image 工具,把完整的"
                    "画面描述写进 prompt;"
                    "(2) 你并不打算画 —— 照实说,不要用文字假装图已经存在。"
                )
                # 只要还有下一轮就纠正 —— 哪怕那是留给「工具禁用终局回复」的
                # 最后一轮:让它把话说诚实,比让谎话原样发出去重要。
                # (原来是 attempts < max_calls - 1:max_calls=2 时首轮谎报根本
                # 不打回,谎话直接发给用户 —— codex 审出。)
                if attempts < max_calls:
                    _progress("image_claim_retry_boundary")
                    continue

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
                force_text_fallback_reason = ""
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
                reply_kwargs = {
                    "final": True,
                    "reasoning": _merged_reasoning(),
                }
                if pr.media:
                    reply_kwargs["media"] = pr.media
                await on_reply(pr.text, **reply_kwargs)
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
            return LoopOutcome(
                pr.text,
                attempts,
                "final_media" if pr.media else "final_text",
                replied_intermediate,
                delivered_media_count=len(pr.media),
            )

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
        # Provider media is terminal output. Do not silently discard or retain
        # its large inline payload when a broken relay also invents function
        # calls in the same turn; fall back once with every tool disabled.
        malformed = (
            (terminal_text_round and bool(pr.tool_calls))
            or bool(pr.media)
            or any(
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
                    and str(tc.args.get("url") or "").strip()
                    not in allowed_fetch_urls
                )
                or (
                    tc.name not in mcp_names
                    and tool_schema.validate_tool_args(tc.name, tc.args) is not None
                )
                for tc in pr.tool_calls
            )
        )
        image_reply_calls = [
            tc for tc in pr.tool_calls if tc.name == tool_schema.IMAGE_REPLY_TOOL
        ]
        mixed_reply_write = any(
            tc.name in {
                tool_schema.REPLY_TOOL,
                tool_schema.FILE_REPLY_TOOL,
                tool_schema.IMAGE_REPLY_TOOL,
            }
            for tc in pr.tool_calls
        ) and any(
            tc.name in _PLATFORM_MUTATION_TOOLS or tc.name in mutating_mcp_names
            for tc in pr.tool_calls
        )
        invalid_image_batch = bool(image_reply_calls) and (
            len(image_reply_calls) != 1 or len(pr.tool_calls) != 1
        )
        if (
            malformed
            or len(set(call_ids)) != len(call_ids)
            or mixed_reply_write
            or invalid_image_batch
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
            force_text_fallback_reason = ""
            continue

        tool_calls_used += len(pr.tool_calls)

        # text accompanying tool_calls = preamble/thinking, NOT a bubble.
        reply_calls = [tc for tc in pr.tool_calls if tc.name == tool_schema.REPLY_TOOL]
        file_reply_calls = [
            tc for tc in pr.tool_calls if tc.name == tool_schema.FILE_REPLY_TOOL
        ]
        loop_reply_tools = {
            tool_schema.REPLY_TOOL,
            tool_schema.FILE_REPLY_TOOL,
            tool_schema.IMAGE_REPLY_TOOL,
        }
        other_calls = [tc for tc in pr.tool_calls if tc.name not in loop_reply_tools]
        reply_results: dict[str, ToolResult] = {}
        repeated_memory_calls = []
        dispatch_calls = []
        discovery_calls_in_batch: set[tuple[str, str]] = set()
        for tc in other_calls:
            discovery_call_key = _memory_discovery_call_key(tc)
            repeated_discovery = discovery_call_key is not None and (
                discovery_call_key in completed_memory_discovery_calls
                or discovery_call_key in discovery_calls_in_batch
            )
            if repeated_discovery:
                repeated_memory_calls.append(tc)
                continue
            dispatch_calls.append(tc)
            if discovery_call_key is not None:
                discovery_calls_in_batch.add(discovery_call_key)
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

        image_final_superseded = False
        for tc in image_reply_calls:
            await _tool_event(tc, "tool_call_started", {})
            await _trajectory(
                "image_reply_planned",
                {"round": attempts, "call_id": tc.id},
            )
            if on_image_reply is None:
                raise RuntimeError("generate_image callback is unavailable")
            # 生成与发布必须分开捕获。合在一个 try 里会把「图画好了但发布出问题」
            # (FinalReplySuperseded / 丢租约 / 落库失败)误报成「生图失败」写进
            # transcript —— 伴侣下一轮会以为没画成而**再画一次、再付一次钱**。
            try:
                media = tuple(await on_image_reply(dict(tc.args)))
                if not media:
                    raise RuntimeError("generate_image returned no media")
            except Exception as exc:
                # 生图失败**不打断这一轮** —— 把结构化失败交回给伴侣,让它自己
                # 决定怎么跟用户说(换个描述再试、或者老实讲这次没画成)。
                # 原来这里直接 raise,整轮炸掉:用户既没有图、也没有一句解释,
                # 而伴侣根本不知道发生过什么。工具失败是它该知道的事实,不是
                # runtime 替它隐藏的意外。
                image_error_code = str(
                    getattr(exc, "error_code", "") or "image_generation_failed"
                )[:64]
                image_result = ToolResult(
                    call_id=tc.id,
                    content=(
                        "error: image generation failed ("
                        + str(getattr(exc, "error_code", "") or type(exc).__name__)
                        + "). 图没有生成。请如实告诉用户,或换一个更清楚的画面"
                        "描述再调一次;不要声称图已经生成。"
                    ),
                    metadata={"image_generation_result_code": image_error_code},
                )
                reply_results[tc.id] = image_result
                await _tool_event(
                    tc, "tool_call_result", {"result": image_result}
                )
                continue
            # 发布阶段:图已经存在,异常走既有的发布语义(不能当成生图失败)。
            # FinalReplySuperseded 必须和**文本终局同款**处理(见 :1118):用户在
            # 我们生图期间又说话了,这一轮的回复已经不该发。若放它冒泡,worker 会
            # 当成一次通用失败 —— mark_failed + 给用户一个报错气泡,而文本终局
            # 在同样情形下是安静地折进下一轮。生图耗时长,撞上 late input 的概率
            # 比文本高得多,这条差异会被真实用户高频撞到。codex 审出。
            try:
                await on_reply(pr.text or "", final=True, media=media)
            except FinalReplySuperseded:
                reasoning_fragments.clear()
                seen_reasoning_fragments.clear()
                _progress("final_reply_superseded")
                await _trajectory(
                    "final_reply_superseded",
                    {"round": attempts, "had_media": True},
                )
                if attempts < max_calls:
                    # 注意:这里在 `for tc in image_reply_calls` 里,单纯 break 会
                    # 掉进下面的 tool dispatch 继续跑这一轮。要的是**外层重来**,
                    # 所以置标志位,退出图片循环后立刻 continue 外层。
                    image_final_superseded = True
                    break
                return LoopOutcome("", attempts, "input_advanced", replied_intermediate)
            image_result = ToolResult(
                call_id=tc.id,
                content="ok: image delivered",
            )
            await _tool_event(
                tc, "tool_call_result", {"result": image_result}
            )
            return LoopOutcome(
                "",
                attempts,
                "final_media",
                replied_intermediate,
                delivered_media_count=len(media),
            )

        if image_final_superseded:
            # 用户在生图期间又说话了:这一轮作废,回外层用新的对话重答。
            continue

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
        for tc in dispatch_calls:
            discovery_call_key = _memory_discovery_call_key(tc)
            if discovery_call_key is not None:
                completed_memory_discovery_tools.add(tc.name)
                completed_memory_discovery_calls.add(discovery_call_key)
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
