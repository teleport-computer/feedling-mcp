"""Unified provider-native tool loop (spec C2). Dependency-clean: no hosted/agent_runtime/db;
all side effects injected. One loop for every model — no is_official branch."""

from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
import inspect
import json
import posixpath
import re
import time
from provider_types import (
    ProviderResponse,
    ToolCall,
    ToolExchange,
    ToolResult,
    ToolSpec,
)
from capabilities import registry as cap_registry
from capabilities import result_budget
from capabilities import tool_schema
from agent_protocol_core import protocol_leak, self_thinking
from chat import language_follow
from model_api_runtime.v2 import prompt_frontier
from model_api_runtime.v2 import provenance
from model_api_runtime.v2 import tool_surface
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
_WORKSPACE_REVISION_RE = re.compile(r"\brevision\s+(\d+)\b", re.IGNORECASE)

# Provider output is untrusted even after its tool names/arguments validate.  These
# defaults bound both fan-out and how much observation text one native exchange can
# add to the next prompt. Production passes env-validated values from worker.py;
# direct callers/tests inherit these safe defaults.
DEFAULT_MAX_TOOL_CALLS_PER_ROUND = 8
DEFAULT_MAX_TOOL_CALLS_PER_TURN = 24
DEFAULT_MAX_CONSECUTIVE_TOOL_ONLY_ROUNDS = 3
DEFAULT_MAX_TERMINAL_TOOL_CALL_RETRIES = 2
DEFAULT_TOOL_RESULT_CHAR_CAP = 2000
DEFAULT_TOOL_BATCH_RESULT_CHAR_CAP = 8000
DEFAULT_MAX_TOOL_ARGS_CHARS = 16000
DEFAULT_MAX_TOOL_BATCH_ARGS_CHARS = 64000
DEFAULT_MAX_NATIVE_ASSISTANT_TURN_CHARS = 65536
DEFAULT_MAX_ASSISTANT_TOOL_TEXT_CHARS = 8192
# ``debug_trace._safe_detail`` retains at most 20 keys and 20 list items.  This
# module stays dependency-clean, so it cannot import debug_trace; a test at the
# real provider-surface capture point pins both ceilings, including the worker's
# later ``lane``/``wake_kind`` keys.  We intentionally share the two compact
# count dictionaries instead of spending the three keys per list required by
# ``debug_trace.bounded_names``.  A bucket count above its list length is the
# explicit truncation signal for the emitted platform-name lists.
_PROVIDER_TOOL_NAME_TRACE_CAP = 20
REJECTED_TOOL_ARGS_SUMMARY_CHAR_CAP = 500
REJECTED_ASSISTANT_TEXT_CHAR_CAP = 500
REJECTED_TOOL_CALL_ID_PREFIX = "feedling_rejected_"
REJECTED_TOOL_NAME_PLACEHOLDER = "feedling_rejected_unknown_tool"
MIN_TOOL_RESULT_ERROR_QUOTA = 64
_RESULT_TRUNCATION_MARKER = "...[truncated]"
_REJECTED_TOOL_ARGS_KEY = "_feedling_rejected_args"
_UNKNOWN_TOOL_REJECTION_REASON = "unknown_tool"
_TOOL_WITHDRAWN_REJECTION_REASON = "tool_withdrawn"
_PROVIDER_CALL_REJECTION_REASON_ASSISTANT_TOOL_TEXT_TOO_LARGE = (
    "assistant_tool_text_too_large"
)
_PROVIDER_CALL_REJECTION_REASON_DUPLICATE_TOOL_CALL_ID = "duplicate_tool_call_id"
_PROVIDER_CALL_REJECTION_REASON_INVALID_IMAGE_REPLY_BATCH = "invalid_image_reply_batch"
_PROVIDER_CALL_REJECTION_REASON_INVALID_OR_OVER_BUDGET_TOOL_EXCHANGE = (
    "invalid_or_over_budget_tool_exchange"
)
_PROVIDER_CALL_REJECTION_REASON_INVALID_STAY_SILENT_BATCH = "invalid_stay_silent_batch"
_PROVIDER_CALL_REJECTION_REASON_INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
_PROVIDER_CALL_REJECTION_REASON_REPEATED_INVALID_TOOL_ARGUMENTS = (
    "repeated_invalid_tool_arguments"
)
_PROVIDER_CALL_REJECTION_REASON_MISSING_TOOL_CALL_ID = "missing_tool_call_id"
_PROVIDER_CALL_REJECTION_REASON_MISSING_TOOL_NAME = "missing_tool_name"
_PROVIDER_CALL_REJECTION_REASON_MIXED_REPLY_AND_MUTATION = "mixed_reply_and_mutation"
_PROVIDER_CALL_REJECTION_REASON_NATIVE_ASSISTANT_TURN_TOO_LARGE = (
    "native_assistant_turn_too_large"
)
_PROVIDER_CALL_REJECTION_REASON_PROVIDER_MEDIA_WITH_TOOL_CALLS = (
    "provider_media_with_tool_calls"
)
_PROVIDER_CALL_REJECTION_REASON_TERMINAL_TOOL_CALL_REJECTED = (
    "terminal_tool_call_rejected"
)
_PROVIDER_CALL_REJECTION_REASON_TOOL_ARGUMENTS_TOO_LARGE = "tool_arguments_too_large"
_PROVIDER_CALL_REJECTION_REASON_TOOL_BATCH_ARGUMENTS_TOO_LARGE = (
    "tool_batch_arguments_too_large"
)
_PROVIDER_CALL_REJECTION_REASON_TOOL_CALL_BUDGET_EXCEEDED = "tool_call_budget_exceeded"
_PROVIDER_CALL_REJECTION_REASON_UNAPPROVED_EXTERNAL_URL = "unapproved_external_url"
_PROVIDER_CALL_REJECTION_REASON_UNCLASSIFIED = "unclassified_rejection"
_REJECTED_TOOL_PLAIN_TEXT_INSTRUCTION = "工具当前不可用,请用纯文本直接回复"
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
# These wires accept the OpenAI-style named-function ``tool_choice`` payload.
# Other providers may expose tools but do not accept this exact forcing shape.
_NAMED_TOOL_CHOICE_PROVIDERS = frozenset(
    {
        "openai",
        "openrouter",
        "openai_compatible",
        "deepseek",
        "anthropic",
        "gemini",
        "bedrock",
    }
)
_WAKE_REPLY_TOOL = "reply"
_WAKE_REPLY_TOOL_SPEC = ToolSpec(
    name=_WAKE_REPLY_TOOL,
    description=(
        "Deliver one non-empty visible reply and end this proactive wake turn. "
        "Use stay_silent instead when there is nothing worth interrupting the user for."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "description": "The complete user-visible reply.",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    },
)
_WAKE_CHOICE_INSTRUCTION = (
    "The previous proactive-wake response ended without visible text or a tool "
    "call. End the wake now by calling exactly one offered tool: call reply with "
    "the complete visible message, or call stay_silent with a non-empty reason."
)
_EMPTY_RESPONSE_CORRECTION = (
    "The previous response completed without visible text or a client tool call. "
    "Complete the user's request now. Return either non-empty visible answer text "
    "or a valid call to one of the offered client tools. Do not return a "
    "thinking-only response."
)
_TERMINAL_TEXT_INSTRUCTION = (
    "Stop calling tools. Using only the information already available in this "
    "conversation and the tool results above, write one complete, self-contained "
    "reply to the user now. Do not emit a tool call, a partial preamble, or "
    "internal reasoning."
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
        "max_output_tokens",
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
# These values are emitted by the provider-surface callback and later summarized
# for admin diagnostics.  Keep the producer vocabulary here, beside the state
# machine that creates it; worker/admin consume these sets instead of copying
# telemetry enums that can silently drift apart.
_PROVIDER_TERMINAL_TEXT_ROUND_REASONS = frozenset(
    {
        "none",
        "force_text_fallback",
        "final_reply_correction",
        "max_calls",
        "other",
    }
)
_PROVIDER_FORCE_TEXT_FALLBACK_REASONS = frozenset(
    {
        "none",
        "tool_schema_rejected",
        "final_reply_correction",
        _PROVIDER_CALL_REJECTION_REASON_INVALID_OR_OVER_BUDGET_TOOL_EXCHANGE,
        _PROVIDER_CALL_REJECTION_REASON_REPEATED_INVALID_TOOL_ARGUMENTS,
        "tool_only_stall",
        "other",
    }
)
# Provider calls that failed, but for which the loop deliberately made a
# degraded retry.  These values are mirrored into the plaintext attempt ledger;
# keep them as producer-owned closed metadata, never exception text.
_PROVIDER_ATTEMPT_FALLBACK_TAGGED_IMAGES = "tagged_images_rejected"
_PROVIDER_ATTEMPT_FALLBACK_TOOL_SCHEMA = "tool_schema_rejected"
_PROVIDER_ATTEMPT_FALLBACK_REASONS = frozenset({
    _PROVIDER_ATTEMPT_FALLBACK_TAGGED_IMAGES,
    _PROVIDER_ATTEMPT_FALLBACK_TOOL_SCHEMA,
})
# Keep the producer inventory separate from the public vocabulary. A regression
# test compares the two so adding a classification branch cannot silently turn a
# rejected round into the same empty list used by a healthy round.
_PROVIDER_CALL_REJECTION_PRODUCER_REASONS = frozenset(
    {
        _PROVIDER_CALL_REJECTION_REASON_ASSISTANT_TOOL_TEXT_TOO_LARGE,
        _PROVIDER_CALL_REJECTION_REASON_DUPLICATE_TOOL_CALL_ID,
        _PROVIDER_CALL_REJECTION_REASON_INVALID_IMAGE_REPLY_BATCH,
        _PROVIDER_CALL_REJECTION_REASON_INVALID_OR_OVER_BUDGET_TOOL_EXCHANGE,
        _PROVIDER_CALL_REJECTION_REASON_INVALID_STAY_SILENT_BATCH,
        _PROVIDER_CALL_REJECTION_REASON_INVALID_TOOL_ARGUMENTS,
        _PROVIDER_CALL_REJECTION_REASON_REPEATED_INVALID_TOOL_ARGUMENTS,
        _PROVIDER_CALL_REJECTION_REASON_MISSING_TOOL_CALL_ID,
        _PROVIDER_CALL_REJECTION_REASON_MISSING_TOOL_NAME,
        _PROVIDER_CALL_REJECTION_REASON_MIXED_REPLY_AND_MUTATION,
        _PROVIDER_CALL_REJECTION_REASON_NATIVE_ASSISTANT_TURN_TOO_LARGE,
        _PROVIDER_CALL_REJECTION_REASON_PROVIDER_MEDIA_WITH_TOOL_CALLS,
        _PROVIDER_CALL_REJECTION_REASON_TERMINAL_TOOL_CALL_REJECTED,
        _PROVIDER_CALL_REJECTION_REASON_TOOL_ARGUMENTS_TOO_LARGE,
        _PROVIDER_CALL_REJECTION_REASON_TOOL_BATCH_ARGUMENTS_TOO_LARGE,
        _PROVIDER_CALL_REJECTION_REASON_TOOL_CALL_BUDGET_EXCEEDED,
        _PROVIDER_CALL_REJECTION_REASON_UNAPPROVED_EXTERNAL_URL,
        _UNKNOWN_TOOL_REJECTION_REASON,
        _TOOL_WITHDRAWN_REJECTION_REASON,
    }
)
_PROVIDER_CALL_REJECTION_REASONS = frozenset(
    _PROVIDER_CALL_REJECTION_PRODUCER_REASONS
    | {_PROVIDER_CALL_REJECTION_REASON_UNCLASSIFIED}
)


def _normalize_provider_call_rejection_reasons(values) -> list[str]:
    """Return unique producer-owned rejection tokens, never arbitrary text."""
    if not isinstance(values, (list, tuple)):
        return []
    return list(
        dict.fromkeys(
            value
            for value in values
            if isinstance(value, str)
            and value in _PROVIDER_CALL_REJECTION_REASONS
        )
    )


def _bounded_provider_tool_names(values) -> tuple[list[str], int]:
    """Return sorted content-free tool names plus the pre-truncation count."""
    names = sorted({str(value) for value in (values or ()) if str(value)})
    return names[:_PROVIDER_TOOL_NAME_TRACE_CAP], len(names)


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
_IDENTITY_WRITE_TOOL_NAMES = frozenset(
    action
    for action in cap_registry.WRITE_ACTIONS
    if action.startswith("identity_")
)


def _identity_write_attempted(transcript) -> bool:
    """Return whether the turn transcript contains an identity-write call."""
    return any(
        call.name in _IDENTITY_WRITE_TOOL_NAMES
        for item in transcript
        if isinstance(item, ToolExchange)
        for call in item.calls
    )


def _identity_write_succeeded(transcript) -> bool:
    """Return whether a matched identity-write result is classified as success."""
    from core import chat_activity as _ca

    for item in transcript:
        if not isinstance(item, ToolExchange):
            continue
        results_by_id = {str(result.call_id): result for result in item.results}
        for call in item.calls:
            if call.name not in _IDENTITY_WRITE_TOOL_NAMES:
                continue
            result = results_by_id.get(str(call.id))
            if result is not None and _ca.result_code(result.content) == "ok":
                return True
    return False


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


def _file_suffix_for_requirement(
    path: str,
    required_suffixes: frozenset[str],
) -> str:
    normalized = str(path or "").strip().casefold()
    matches = [suffix for suffix in required_suffixes if normalized.endswith(suffix)]
    if matches:
        return max(matches, key=len)
    if normalized.endswith(".io.html"):
        return ".io.html"
    suffix = posixpath.splitext(normalized)[1]
    return {
        ".htm": ".html",
        ".markdown": ".md",
        ".yml": ".yaml",
    }.get(suffix, suffix)


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


def _tool_args_char_limit(tc: ToolCall, default_limit: int) -> int:
    """Allow one schema-bounded Canvas body through the generic tool guard."""
    if (
        tc.name == "workspace_write"
        and tc.args_ok
        and str(tc.args.get("path") or "").strip().casefold().endswith(".io.html")
        and tool_schema.validate_tool_args(tc.name, tc.args) is None
    ):
        serialized_size = _serialized_chars(tc.args)
        if serialized_size is not None:
            # The closed tool schema and its capability-level UTF-8 check are
            # authoritative here. Derive the allowance from this exact parsed
            # argument object: JSON escaping grows with quote/backslash density,
            # so a fixed additive allowance would silently narrow the 256KB
            # source contract when a future output budget reaches that boundary.
            return max(default_limit, serialized_size)
    return default_limit


@dataclass(frozen=True)
class _ToolCallRejectionFacts:
    call_ids: list[str]
    image_reply_calls: list[ToolCall]
    stay_silent_calls: list[ToolCall]
    oversized_tool_exchange: bool
    over_tool_call_budget: bool
    malformed: bool
    truncated_tool_arguments: bool
    mixed_reply_write: bool
    invalid_image_batch: bool
    invalid_silence_batch: bool
    rejected: bool
    call_rejection_reasons: list[str]
    reason_tokens: list[str]


def _tool_call_rejection_facts(
    pr: ProviderResponse,
    *,
    tools: list[ToolSpec] | None,
    raw_finish_reason: str,
    terminal_text_round: bool,
    tool_calls_used: int,
    previous_offered_tool_names: frozenset[str],
    completed_memory_discovery_tools: set[str],
    external_content_seen: bool,
    allowed_fetch_urls: set[str],
    mutating_mcp_names: set[str],
    max_tool_args_chars: int,
    max_tool_batch_args_chars: int,
    max_native_assistant_turn_chars: int,
    max_assistant_tool_text_chars: int,
    max_tool_calls_per_round: int,
    max_tool_calls_per_turn: int,
) -> _ToolCallRejectionFacts:
    """Classify one provider tool batch using content-free closed tokens."""
    call_ids = [tc.id for tc in pr.tool_calls]
    argument_sizes = [
        (_serialized_chars(tc.args) if tc.args_ok else len(str(tc.args_raw or "")))
        for tc in pr.tool_calls
    ]
    argument_limits = [
        _tool_args_char_limit(tc, max_tool_args_chars)
        for tc in pr.tool_calls
    ]
    batch_arguments_limit = max_tool_batch_args_chars
    native_assistant_turn_limit = max_native_assistant_turn_chars
    if (
        len(pr.tool_calls) == 1
        and argument_limits
        and argument_limits[0] > max_tool_args_chars
    ):
        batch_arguments_limit = max(batch_arguments_limit, argument_limits[0])
        native_assistant_turn_limit = max(
            native_assistant_turn_limit,
            argument_limits[0] + max_assistant_tool_text_chars + 8192,
        )
    native_turn_size = (
        _serialized_chars(pr.assistant_turn.payload)
        if pr.assistant_turn is not None
        else 0
    )
    oversized_tool_exchange = (
        any(
            size is None or size > limit
            for size, limit in zip(argument_sizes, argument_limits)
        )
        or sum(size or 0 for size in argument_sizes) > batch_arguments_limit
        or native_turn_size is None
        or native_turn_size > native_assistant_turn_limit
        or len(pr.text) > max_assistant_tool_text_chars
    )
    over_tool_call_budget = (
        len(pr.tool_calls) > max_tool_calls_per_round
        or tool_calls_used + len(pr.tool_calls) > max_tool_calls_per_turn
    )
    offered_names = {spec.name for spec in (tools or [])}
    individual_rejection_reasons: list[list[str]] = []
    for tc in pr.tool_calls:
        reasons: list[str] = []
        if not tc.id:
            reasons.append(_PROVIDER_CALL_REJECTION_REASON_MISSING_TOOL_CALL_ID)
        if not tc.name:
            reasons.append(_PROVIDER_CALL_REJECTION_REASON_MISSING_TOOL_NAME)
        if not tc.args_ok:
            reasons.append(_PROVIDER_CALL_REJECTION_REASON_INVALID_TOOL_ARGUMENTS)
        if (
            tc.name not in offered_names
            and tc.name not in completed_memory_discovery_tools
        ):
            reasons.append(
                _TOOL_WITHDRAWN_REJECTION_REASON
                if tc.name in previous_offered_tool_names
                else _UNKNOWN_TOOL_REJECTION_REASON
            )
        elif (
            external_content_seen
            and tc.name == "web_fetch"
            and str(tc.args.get("url") or "").strip() not in allowed_fetch_urls
        ):
            reasons.append(_PROVIDER_CALL_REJECTION_REASON_UNAPPROVED_EXTERNAL_URL)
        individual_rejection_reasons.append(reasons)
    # Provider media is terminal output. Do not silently discard or retain
    # its large inline payload when a broken relay also invents function calls.
    malformed = (
        (terminal_text_round and bool(pr.tool_calls))
        or bool(pr.media)
        or any(individual_rejection_reasons)
    )
    truncated_tool_arguments = provider_client.is_token_limit_stop_reason(
        raw_finish_reason
    ) and any(not tc.args_ok for tc in pr.tool_calls)
    image_reply_calls = [
        tc for tc in pr.tool_calls if tc.name == tool_schema.IMAGE_REPLY_TOOL
    ]
    stay_silent_calls = [
        tc for tc in pr.tool_calls if tc.name == tool_schema.STAY_SILENT_TOOL
    ]
    mixed_reply_write = any(
        tc.name
        in {
            tool_schema.FILE_REPLY_TOOL,
            tool_schema.IMAGE_REPLY_TOOL,
            tool_schema.STAY_SILENT_TOOL,
        }
        for tc in pr.tool_calls
    ) and any(
        tc.name in _PLATFORM_MUTATION_TOOLS or tc.name in mutating_mcp_names
        for tc in pr.tool_calls
    )
    invalid_image_batch = bool(image_reply_calls) and (
        len(image_reply_calls) != 1 or len(pr.tool_calls) != 1
    )
    invalid_silence_batch = bool(stay_silent_calls) and (
        len(stay_silent_calls) != 1 or len(pr.tool_calls) != 1
    )
    duplicate_call_ids = {
        call_id for call_id in call_ids if call_id and call_ids.count(call_id) > 1
    }
    batch_rejection_reasons: list[str] = []
    if terminal_text_round:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_TERMINAL_TOOL_CALL_REJECTED
        )
    if pr.media:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_PROVIDER_MEDIA_WITH_TOOL_CALLS
        )
    if mixed_reply_write:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_MIXED_REPLY_AND_MUTATION
        )
    if invalid_image_batch:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_INVALID_IMAGE_REPLY_BATCH
        )
    if invalid_silence_batch:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_INVALID_STAY_SILENT_BATCH
        )
    if over_tool_call_budget:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_TOOL_CALL_BUDGET_EXCEEDED
        )
    if sum(size or 0 for size in argument_sizes) > batch_arguments_limit:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_TOOL_BATCH_ARGUMENTS_TOO_LARGE
        )
    if native_turn_size is None or native_turn_size > native_assistant_turn_limit:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_NATIVE_ASSISTANT_TURN_TOO_LARGE
        )
    if len(pr.text) > max_assistant_tool_text_chars:
        batch_rejection_reasons.append(
            _PROVIDER_CALL_REJECTION_REASON_ASSISTANT_TOOL_TEXT_TOO_LARGE
        )
    call_rejection_reasons: list[str] = []
    for tc, argument_size, argument_limit, individual_reasons in zip(
        pr.tool_calls,
        argument_sizes,
        argument_limits,
        individual_rejection_reasons,
    ):
        reasons = list(individual_reasons)
        if tc.id in duplicate_call_ids:
            reasons.append(_PROVIDER_CALL_REJECTION_REASON_DUPLICATE_TOOL_CALL_ID)
        if argument_size is None or argument_size > argument_limit:
            reasons.append(_PROVIDER_CALL_REJECTION_REASON_TOOL_ARGUMENTS_TOO_LARGE)
        reasons.extend(batch_rejection_reasons)
        call_rejection_reasons.append(
            ",".join(dict.fromkeys(reasons))
            or _PROVIDER_CALL_REJECTION_REASON_INVALID_OR_OVER_BUDGET_TOOL_EXCHANGE
        )
    rejected = bool(pr.tool_calls) and (
        malformed
        or len(set(call_ids)) != len(call_ids)
        or mixed_reply_write
        or invalid_image_batch
        or invalid_silence_batch
        or over_tool_call_budget
        or oversized_tool_exchange
    )
    reason_tokens = (
        _normalize_provider_call_rejection_reasons(
            [
                token
                for joined_reasons in call_rejection_reasons
                for token in joined_reasons.split(",")
            ]
        )
        if rejected
        else []
    )
    if rejected and not reason_tokens:
        reason_tokens = [_PROVIDER_CALL_REJECTION_REASON_UNCLASSIFIED]
    return _ToolCallRejectionFacts(
        call_ids=call_ids,
        image_reply_calls=image_reply_calls,
        stay_silent_calls=stay_silent_calls,
        oversized_tool_exchange=oversized_tool_exchange,
        over_tool_call_budget=over_tool_call_budget,
        malformed=malformed,
        truncated_tool_arguments=truncated_tool_arguments,
        mixed_reply_write=mixed_reply_write,
        invalid_image_batch=invalid_image_batch,
        invalid_silence_batch=invalid_silence_batch,
        rejected=rejected,
        call_rejection_reasons=call_rejection_reasons,
        reason_tokens=reason_tokens,
    )


def _truncate_result_content(content: str, cap: int, *, marker: str = _RESULT_TRUNCATION_MARKER) -> str:
    """Deterministically cap one result, including the truncation marker."""
    text = str(content or "")
    if len(text) <= cap:
        return text
    if cap <= len(marker):
        return marker[:cap]
    return text[: cap - len(marker)] + marker


def _rejected_tool_args(tool_call: ToolCall) -> dict:
    """Return one bounded, provider-encodable summary of rejected arguments."""
    if tool_call.args_ok:
        try:
            raw = json.dumps(
                tool_call.args,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError):
            raw = "<unserializable arguments>"
    else:
        raw = str(tool_call.args_raw or "<invalid arguments>")
    best = ""
    low = 0
    high = min(len(raw), REJECTED_TOOL_ARGS_SUMMARY_CHAR_CAP)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = _truncate_result_content(raw, midpoint)
        encoded = {_REJECTED_TOOL_ARGS_KEY: candidate}
        size = _serialized_chars(encoded)
        if size is not None and size <= REJECTED_TOOL_ARGS_SUMMARY_CHAR_CAP:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return {_REJECTED_TOOL_ARGS_KEY: best}


def _rejected_tool_exchange(
    tool_calls: list[ToolCall],
    *,
    assistant_text: str,
    rejection_reasons: list[str],
    attempt: int,
) -> ToolExchange:
    """Synthesize a bounded, non-executed exchange for one rejected batch.

    Provider-native ids and assistant payloads can themselves be malformed, so
    neither is retained. The reserved id prefix plus the monotonically
    increasing provider-attempt number makes every synthetic id distinct within
    this loop while keeping calls and results exactly paired on every wire.
    """
    if len(tool_calls) != len(rejection_reasons):
        raise ValueError("rejected tool calls and reasons must match")
    calls: list[ToolCall] = []
    results: list[ToolResult] = []
    for index, (tool_call, reason) in enumerate(
        zip(tool_calls, rejection_reasons)
    ):
        call_id = f"{REJECTED_TOOL_CALL_ID_PREFIX}{attempt}_{index}"
        normalized_reason = str(reason or "tool_call_rejected").strip()
        calls.append(
            ToolCall(
                id=call_id,
                name=(
                    str(tool_call.name or "").strip()
                    or REJECTED_TOOL_NAME_PLACEHOLDER
                ),
                args=_rejected_tool_args(tool_call),
            )
        )
        results.append(
            ToolResult(
                call_id=call_id,
                content=(
                    f"Tool call rejected: {normalized_reason}. No tool was "
                    f"executed. {_REJECTED_TOOL_PLAIN_TEXT_INSTRUCTION}"
                ),
                metadata={"rejected": normalized_reason},
            )
        )
    return ToolExchange(
        calls=tuple(calls),
        results=tuple(results),
        assistant_text=_truncate_result_content(
            assistant_text,
            REJECTED_ASSISTANT_TEXT_CHAR_CAP,
        ),
        assistant_turn=None,
    )


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
    the loop to fold the newly arrived input and ask the provider again.
    """


class ProviderEmptyReply(RuntimeError):
    """A structurally valid provider success had no foreground-usable output."""


class ProviderOutputTruncated(RuntimeError):
    """A provider stopped while serializing a tool call at its output limit."""

    reason = "output_truncated"

    def __init__(self):
        super().__init__(self.reason)


class FileDeliveryIncomplete(RuntimeError):
    """A requested attachment was saved but still failed bounded delivery."""


class ValidatedFinalReply(str):
    """A sanitized structured reply whose language check budget is settled.

    The worker's ordinary terminal-text path may ask the provider to rewrite a
    reply that omits its private thinking envelope.  A file completion has
    already passed through language validation and its bounded correction policy,
    then is published atomically with the staged attachment. Treating it as
    ordinary text can replace that settled delivery bubble with a second,
    wrong-language provider answer. A ``str`` subtype keeps the callback API and
    test equality stable while preserving that provenance at the worker boundary.
    """


class CanvasDeliveryIncomplete(FileDeliveryIncomplete):
    """Canvas source was saved but its card still failed bounded delivery."""


def _delivery_incomplete(path: str, reason: str) -> FileDeliveryIncomplete:
    if str(path or "").strip().casefold().endswith(".io.html"):
        return CanvasDeliveryIncomplete(reason)
    return FileDeliveryIncomplete(reason)


@dataclass(frozen=True)
class FinalReplyCorrectionRequest:
    """Ask the loop for one text-only rewrite before publishing a final reply.

    ``original_text`` is the caller's pre-publication provider text.  It stays
    private to the loop and is published unchanged if the bounded rewrite is
    unusable.  Only foreground Chat returns this marker; other lanes keep the
    historical ``on_reply -> None`` contract.
    """

    instruction: str
    original_text: str
    original_reasoning: str = ""
    on_cancel: Callable[[], None] | None = None


@dataclass(frozen=True)
class FinalReplyCorrectionRejected:
    """The one rewrite was usable text but failed the caller's acceptance gate."""


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
    on_provider_call_event=None,
    on_empty_provider_response=None,
    on_provider_success=None,
    on_provider_failure=None,
    fold_before_first: bool = False,
    on_progress=None,
    on_trajectory_event=None,
    extra_tool_specs=None,
    refresh_extra_tool_specs=None,
    refresh_pressure_collapsed_extra_tool_specs=None,
    refresh_protected_extra_tool_names=None,
    extra_tool_recovery_name: str = "",
    extra_tool_recovery_active=None,
    on_extra_tool_surface_plan=None,
    extra_mutating_tool_names=None,
    disabled_tool_names=None,
    # Wake turns retain memory add/update but must never be offered delete.
    # Execution authorization is independently enforced by the caller's
    # dispatch_tools closure; this parameter controls only the provider surface.
    memory_delete_allowed: bool = False,
    on_stay_silent=None,
    include_reasoning: bool = False,
    # Self-authored thinking: when True, NEVER request provider-native reasoning —
    # not via include_reasoning, and NOT via reasoning_effort either. The model then
    # has no separate native-CoT channel and emits its thinking in the reply's
    # <think> block instead (which the seal surfaces). This is what aligns V2 with
    # the V1 resident: without it a reasoning-capable model (e.g. sonnet) puts its
    # thought in the native channel — shown raw, often in the wrong language — and
    # skips the <think>. Default False → other lanes unchanged.
    suppress_native_reasoning: bool = False,
    # Whether a text-free provider reply is an immediate ERROR. Defaults to
    # True for foreground chat. Wake passes False so this loop can inspect an
    # empty 200 and force the bounded reply/stay_silent choice itself; the
    # provider parser must not fail before lane policy runs.
    require_reply: bool = True,
    # One lane-specific correction may be appended after a semantically empty
    # provider success. Callers that carry a stronger delivery contract (for
    # example, a due scheduled reminder) can restate that contract here without
    # inventing a synthetic user turn.
    empty_response_correction: str = _EMPTY_RESPONSE_CORRECTION,
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
    initial_screen_pixels_blocked: bool = False,
    # ⚠️ 2026-08-21 Seven 裁定(T107):「只有 OCR、用户没有发消息的时候就禁掉;
    # 只有用户发消息的时候,回复那个轮次才带上 Tool」。
    # 判据是**这一轮有没有用户消息**,不是"有没有屏幕内容":
    #   带屏幕内容 + 无用户消息(screen_watch 唤醒轮)→ 平台写工具全禁
    #   有用户消息的前台轮 → 不动(用户在场,出错他能当场阻止)
    # 为什么必须是第三个语义位而不是复用上面那个:`screen_pixels_blocked` 管的是
    # **外泄**(不许把看到的东西发出去),这一条管的是**注入**(不许让看到的东西
    # 指挥你干活),两者拦的集合不同,合成一个 bool 就是 T166 之前那个病。
    initial_untrusted_screen_only: bool = False,
    tagged_image_message_key: str = "",
    on_tagged_images_rejected=None,
    max_consecutive_tool_only_rounds: int = (
        DEFAULT_MAX_CONSECUTIVE_TOOL_ONLY_ROUNDS
    ),
    max_terminal_tool_call_retries: int = (
        DEFAULT_MAX_TERMINAL_TOOL_CALL_RETRIES
    ),
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
    file_output_max_tokens: int = provider_client.CHAT_OUTPUT_MAX_TOKENS,
    prompt_safety_margin_tokens: int | None = None,
    prompt_estimator_utf8_bytes_per_token: float = prompt_frontier.DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
    prompt_image_reserve_tokens: int = prompt_frontier.DEFAULT_IMAGE_RESERVE_TOKENS,
    # Chat passes its configured policy explicitly. Other lanes retain the
    # pressure-only behavior until they own a schema-search recovery state.
    tool_schema_collapse_policy: str = (
        tool_surface.COLLAPSE_POLICY_UNDER_PRESSURE
    ),
    on_tail_window=None,
    on_prompt_frontier_exhaustion=None,
    on_prompt_frontier_exhausted_detail=None,
    absolute_deadline: float | None = None,
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
    Foreground Chat may instead return :class:`FinalReplyCorrectionRequest` before
    publishing, then either publish the accepted rewrite or return
    :class:`FinalReplyCorrectionRejected`.  The loop gives that request exactly one
    text-only provider call and otherwise republishes the original fail-open.

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
    max_consecutive_tool_only_rounds = _positive_limit(
        max_consecutive_tool_only_rounds,
        name="max_consecutive_tool_only_rounds",
    )
    max_terminal_tool_call_retries = _positive_limit(
        max_terminal_tool_call_retries,
        name="max_terminal_tool_call_retries",
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
    file_output_max_tokens = _positive_limit(
        file_output_max_tokens,
        name="file_output_max_tokens",
    )
    normalized_empty_response_correction = str(
        empty_response_correction or _EMPTY_RESPONSE_CORRECTION
    ).strip()
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
    last_offered_tool_names: frozenset[str] = frozenset()
    tool_calls_used = 0
    consecutive_tool_only_rounds = 0
    terminal_tool_call_retries = 0
    reasoning_fragments: list[str] = []
    seen_reasoning_fragments: set[str] = set()
    force_text_fallback = False
    force_text_fallback_reason = ""
    generic_validation_retry_used = False
    empty_response_recovery_used = False
    empty_response_retry_instruction = ""
    wake_choice_recovery_used = False
    wake_choice_required = False
    final_reply_correction_request: FinalReplyCorrectionRequest | None = None
    final_reply_correction_instruction = ""
    external_content_seen = False
    mutation_outcome_unknown = False
    # Keep the two provenance sources separate. Screen pixels are an
    # untrusted, system-chosen input and therefore fence mutating user MCP;
    # private reads retain the 2026-08-12 user-selected-MCP relaxation.
    screen_pixels_blocked = bool(initial_screen_pixels_blocked)
    # 注入面(T107,Seven 2026-08-21 裁定):这一轮的**唯一指令来源**就是屏幕上
    # 那段字 —— 没有用户消息,所以它不能驱动平台写。
    #
    # 代码可达性(读代码即可复核,不依赖任何模型实验):`worker.py` 的
    # screen_watch 分支只下架了 `_IDENTITY_WRITE_ACTIONS` 与 `memory_organize`,
    # 因此 `memory_write` / `schedule_wake` / `cancel_wake` /
    # `workspace_write` / `workspace_delete` 在**有 frame 的无人值守轮**里仍被 offer。
    #
    # 提示词里的 `UNTRUSTED … never instructions` 抬头是**软标注**:它约束不了
    # 模型的选择,只能表达意图。模型侧的选择率有一次探针测量,数字与其口径边界
    # 记在台账 T107(⚠️ 那次探针的工具面**比生产宽**、且只记录工具名不验参数,
    # 因此它是风险信号不是落地证明 —— 别把它当成本处的判据焊在这里)。
    untrusted_screen_only = bool(initial_untrusted_screen_only)
    private_read_seen = False
    tagged_image_fallback_active = False
    file_requirement_message_state = [
        {
            **dict(message),
            "role": str(message.get("role") or "user"),
        }
        for message in file_requirement_messages
        if isinstance(message, dict)
    ]

    def _latest_user_delivery_request() -> str:
        """Keep the model's final Canvas metadata grounded in the live request."""

        for message in reversed(file_requirement_message_state):
            if str(message.get("role") or "").strip().lower() != "user":
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = "\n".join(
                    str(part.get("text") or "").strip()
                    for part in content
                    if isinstance(part, dict)
                    and str(part.get("text") or "").strip()
                )
            if isinstance(content, str) and content.strip():
                return _truncate_result_content(
                    content.strip(),
                    DEFAULT_TOOL_RESULT_CHAR_CAP,
                )
        return ""

    def _delivery_control_uses_han(request: str) -> bool:
        """Keep compact delivery prompts aligned with the visible request."""

        return _delivery_request_writing_system(request) == "han"

    def _delivery_request_writing_system(request: str) -> str:
        """Classify the conversational shell, not Latin-heavy file terms.

        A Chinese request can contain a long English filename or subject, which
        makes the shared character-count classifier report ``latin``.  A small
        Han share can equally be an English request quoting a filename or term,
        so the compact-delivery override requires at least two Han characters
        and a 20% share whenever Latin is also present.  Kana keeps Japanese
        requests out of this Chinese/Latin override.
        """

        han_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", request))
        latin_count = len(re.findall(r"[A-Za-z]", request))
        letter_count = han_count + latin_count
        strong_han_shell = bool(
            han_count
            and re.search(r"[\u3040-\u30ff]", request) is None
            and (
                latin_count == 0
                or (han_count >= 2 and han_count / letter_count >= 0.20)
            )
        )
        if strong_han_shell:
            return "han"
        writing_system = language_follow.classify_writing_system(request)
        if writing_system == "indeterminate":
            if latin_count:
                return "latin"
            if han_count:
                return "han"
        return writing_system

    def _delivery_completion_matches_request(
        request: str, completion_message: str
    ) -> bool:
        """Validate the model-authored delivery bubble against the live request."""

        expected = _delivery_request_writing_system(request)
        actual = language_follow.classify_writing_system(completion_message)
        if expected == "mixed":
            return True
        if expected == "indeterminate":
            if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", request) is not None:
                expected = "han"
            elif re.search(r"[A-Za-z]", request) is not None:
                expected = "latin"
            else:
                return True
        if actual == expected or actual == "mixed":
            return True
        if actual != "indeterminate":
            return False
        if expected == "han":
            return re.search(
                r"[\u3400-\u4dbf\u4e00-\u9fff]", completion_message
            ) is not None
        if expected == "latin":
            return re.search(r"[A-Za-z]", completion_message) is not None
        return False

    def _compact_delivery_system_prompt(
        instruction: str, *, require_self_thinking: bool = True
    ) -> str:
        """Preserve the normal final-reply contract in compact delivery rounds."""

        if not suppress_native_reasoning or not require_self_thinking:
            return instruction
        return instruction.rstrip() + "\n\n" + self_thinking.INSTRUCTION.strip()

    def _normalize_file_requirement(value) -> tuple[bool, frozenset[str]]:
        suffixes = frozenset(
            str(suffix).strip().casefold() for suffix in (value or ())
        )
        if any(
            re.fullmatch(r"\.[a-z0-9]{1,16}(?:\.[a-z0-9]{1,16})?", suffix)
            is None
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
    identity_write_failed_bounces = 0
    image_claim_retry_instruction = ""
    identity_write_failed_instruction = ""
    file_delivery_fallback_reasoning = ""
    file_delivery_recovery_needed = False
    workspace_write_applied = False
    workspace_delivery_target: tuple[str, int] | None = None
    workspace_delivery_candidate: tuple[str, int] | None = None
    existing_file_delivery_choice_required = False
    compact_delivery_validation_exchange: ToolExchange | None = None
    compact_delivery_mismatch_retry_used = False
    compact_delivery_args_retry_used = False
    file_delivery_callback_retry_used = False
    compact_delivery_confirmation_needed = False
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
    if on_stay_silent is None:
        disabled_names.add(tool_schema.STAY_SILENT_TOOL)
    else:
        disabled_names.discard(tool_schema.STAY_SILENT_TOOL)
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
        catalog = [
            spec
            for spec in (_catalog() + current_extra_specs)
            if spec.name not in disabled_names
        ]
        if not memory_delete_allowed:
            catalog = [tool_schema.without_memory_delete(spec) for spec in catalog]
        return catalog
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

    async def _provider_call_event(event_kind: str, detail: dict) -> None:
        # Cheap, content-free provider telemetry is best-effort. The assembly
        # owns persistence and bounding; this dependency-clean loop supplies
        # only closed metadata from the exact call boundary.
        if on_provider_call_event is None:
            return
        try:
            emitted = on_provider_call_event(event_kind, detail)
            if inspect.isawaitable(emitted):
                await emitted
        except Exception:  # noqa: BLE001 - diagnostics cannot alter a turn
            pass

    async def _emit_provider_tool_surface(
        detail: dict | None,
        rejection_reasons=(),
    ) -> None:
        """Emit the request surface once its same-round response is classified."""
        if on_provider_tool_surface is None or detail is None:
            return
        try:
            await on_provider_tool_surface(
                {
                    **detail,
                    "call_rejection_reasons": (
                        _normalize_provider_call_rejection_reasons(
                            list(rejection_reasons)
                        )
                    ),
                }
            )
        except Exception:  # noqa: BLE001 - diagnostics cannot alter a turn
            pass

    def _provider_error_facts(exc: BaseException) -> dict[str, object]:
        """Derive closed failure metadata without trusting exception text."""
        status_code = getattr(exc, "status_code", None)
        try:
            timed_out = provider_client.is_timeout_error(exc)
        except Exception:  # noqa: BLE001 - diagnostics cannot alter a turn
            timed_out = False
        try:
            error_family = provider_client.classify_provider_error(exc)
        except Exception:  # noqa: BLE001 - diagnostics cannot alter a turn
            error_family = "unknown"
        return {
            "finish_reason": (
                "timeout"
                if timed_out
                else (
                    "http_error"
                    if isinstance(status_code, int)
                    and not isinstance(status_code, bool)
                    else "provider_error"
                )
            ),
            "status_code": status_code,
            "error_class": type(exc).__name__,
            "provider_error_class": error_family,
        }

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

    async def _publish_final_correction_fallback(outcome: str) -> None:
        """Publish the saved usable answer when its one rewrite cannot be used."""

        request = final_reply_correction_request
        if request is None:
            raise RuntimeError("final reply correction fallback without request")
        decision = await on_reply(
            request.original_text,
            final=True,
            reasoning=request.original_reasoning,
            correction_outcome=outcome,
        )
        if decision is not None:
            raise RuntimeError("final reply correction fallback was not published")

    def _cancel_final_reply_correction() -> None:
        """Drop an old-target correction when newly folded input supersedes it."""

        nonlocal final_reply_correction_request
        nonlocal final_reply_correction_instruction
        nonlocal force_text_fallback, force_text_fallback_reason
        request = final_reply_correction_request
        if request is not None and request.on_cancel is not None:
            try:
                request.on_cancel()
            except Exception:
                pass
        final_reply_correction_request = None
        final_reply_correction_instruction = ""
        if force_text_fallback_reason == "final_reply_correction":
            force_text_fallback = False
            force_text_fallback_reason = ""

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
                consecutive_tool_only_rounds = 0
                if final_reply_correction_request is not None:
                    _cancel_final_reply_correction()
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
                file_requirement_message_state.extend(
                    {
                        **dict(message),
                        "role": str(message.get("role") or "user"),
                    }
                    for message in folded
                    if isinstance(message, dict)
                )
                if resolve_required_file_suffixes is not None:
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
                        workspace_write_applied = False
                        workspace_delivery_target = None
                        workspace_delivery_candidate = None
                        existing_file_delivery_choice_required = False
                        compact_delivery_validation_exchange = None
                        compact_delivery_mismatch_retry_used = False
                        compact_delivery_args_retry_used = False
                        file_delivery_callback_retry_used = False
                        compact_delivery_confirmation_needed = False
                        if on_file_requirement_changed is not None:
                            await on_file_requirement_changed()

        messages = build_messages(list(transcript))
        turn_catalog = _turn_catalog()
        wake_choice_tool_available = any(
            spec.name == tool_schema.STAY_SILENT_TOOL for spec in turn_catalog
        )
        # Reserve the configured final provider attempt for a terminal reply.
        # ``max_calls`` is the deployment-configurable stop threshold; the loop
        # must not grow an unbounded second budget after reaching it.
        terminal_text_round = (
            force_text_fallback
            or final_reply_correction_request is not None
            or compact_delivery_confirmation_needed
            or (attempts == max_calls - 1 and not wake_choice_required)
        )
        terminal_text_round_reason = "none"
        if terminal_text_round:
            terminal_text_round_reason = (
                "force_text_fallback"
                if force_text_fallback
                else (
                    "final_reply_correction"
                    if final_reply_correction_request is not None
                    else (
                        "compact_delivery_confirmation"
                        if compact_delivery_confirmation_needed
                        else "max_calls"
                    )
                )
            )
        # Bounded corrections share the same transient system suffix:
        # missing file delivery, an unbacked image claim, a semantically empty
        # response, foreground language correction, and the tools-disabled
        # terminal answer. None is persisted in the transcript.
        terminal_text_instruction = (
            _TERMINAL_TEXT_INSTRUCTION
            if terminal_text_round
            and final_reply_correction_request is None
            and not compact_delivery_confirmation_needed
            else ""
        )
        retry_instructions = "\n\n".join(
            instruction
            for instruction in (
                delivery_retry_instruction,
                image_claim_retry_instruction,
                identity_write_failed_instruction,
                empty_response_retry_instruction,
                _WAKE_CHOICE_INSTRUCTION if wake_choice_required else "",
                final_reply_correction_instruction,
                terminal_text_instruction,
            )
            if instruction
        )
        if retry_instructions:
            messages = _with_system_suffix(messages, retry_instructions)
        # Providers with a real tool_choice=none keep schemas referenced by their
        # native history; other wires omit tools as before. A 400/422 schema
        # rejection or repeated malformed call also forces this bounded fallback.
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
            and not compact_delivery_confirmation_needed
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
            external_content_seen
            or mutation_outcome_unknown
            or screen_pixels_blocked
            # T107:这一位必须**单独**能触发整个策略块。生产里它总是与
            # `screen_pixels_blocked` 同时为真(两者都由 screen_frame_message 决定),
            # 少写这一行照样"能跑" —— 但那是靠另一个条件恰好也成立,
            # 而不是靠它自己。我的用例只翻这一位,当场红了。
            or untrusted_screen_only
            or private_read_seen
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
            # Screen pixels are not a user-selected destination or trusted
            # instruction source. They may influence read-only user MCP, but
            # cannot authorize a mutation or choose a fresh outbound target.
            if screen_pixels_blocked:
                blocked_tools.update({"web_search", "web_fetch"})
                blocked_tools.add(tool_schema.TASK_TOOL)
                blocked_tools.update(mutating_mcp_names)
            # T107(Seven 2026-08-21 原话):「只有 OCR、用户没有发消息的时候就禁掉;
            # 只有用户发消息的时候,回复那个轮次才带上 Tool」。
            # 判据是**这一轮有没有用户消息**,不是"屏幕上写了什么" ——
            # 前台轮不走这条,因为用户在场、出错他能当场阻止。
            #
            # ⚠️ 范围只到「带屏幕内容的无人轮」,**不能扩到所有 wake lane**:
            # 心跳/capture/dream 那些轮次**必须能写记忆**(docs/testing/TESTING.md §P:
            # 「wake 轮次必须能写记忆(capture/dream 全靠它),但不该能改身份卡」)。
            # 一刀切禁写会把记忆维护整条打掉 —— 这正是 T190 学到的
            # 「硬闸会逼出一串特例」,所以这里只收窄到注入面。
            #
            # 已知代价(明写,别当 bug 修回去):屏幕轮里模型**合法**想写点什么
            # (看到用户在改简历、想记一张卡)同样会被挡。Seven 接受这个代价。
            if untrusted_screen_only:
                blocked_tools.update(_PLATFORM_MUTATION_TOOLS)
            # A private read may expose persona, workspace/artifact, or memory
            # content. That observation cannot influence a later platform-owned
            # query/URL/task call. User-selected MCP endpoints remain available,
            # including mutations, under the 2026-08-12 trust-boundary decision.
            # Local durable edits remain available: read-then-edit is a core
            # workspace/working-memory workflow and still carries the original
            # user/wake seed plus generation fence.
            if private_read_seen:
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
        file_delivery_choice_required = False
        if (
            not terminal_text_round
            and tools is not None
            and workspace_delivery_target is not None
        ):
            forced_delivery_tool = tool_schema.FILE_REPLY_TOOL
            target_path, _ = workspace_delivery_target
            exact_canvas_delivery = target_path.casefold().endswith(".io.html")
            tools = [
                spec for spec in tools
                if spec.name == forced_delivery_tool
                or (
                    not exact_canvas_delivery
                    and spec.name == extra_tool_recovery_name
                )
            ]
            surface_reason = "file_delivery_forced"
        elif (
            not terminal_text_round
            and tools is not None
            and file_delivery_required
            and not requirement_already_met
        ):
            # Keep the recovery path narrow enough for weaker tool-using models.
            tools = [
                spec for spec in tools
                if spec.name in _FILE_DELIVERY_TOOLS
                or spec.name == extra_tool_recovery_name
            ]
            surface_reason = "file_delivery_forced"
            if workspace_write_applied:
                forced_delivery_tool = tool_schema.FILE_REPLY_TOOL
            elif (
                existing_file_delivery_choice_required
                and workspace_delivery_candidate is not None
            ):
                choice_names = {
                    spec.name
                    for spec in tools
                    if spec.name in {"workspace_write", tool_schema.FILE_REPLY_TOOL}
                }
                tools = [spec for spec in tools if spec.name in choice_names]
                if choice_names == {tool_schema.FILE_REPLY_TOOL}:
                    forced_delivery_tool = tool_schema.FILE_REPLY_TOOL
                elif choice_names:
                    file_delivery_choice_required = True
            elif file_delivery_recovery_needed:
                available_names = {spec.name for spec in tools}
                if "workspace_write" in available_names:
                    forced_delivery_tool = "workspace_write"
            if forced_delivery_tool:
                tools = [
                    spec for spec in tools
                    if spec.name in {
                        forced_delivery_tool,
                        extra_tool_recovery_name,
                    }
                ]
        if wake_choice_required:
            stay_silent_spec = next(
                (
                    spec
                    for spec in turn_catalog
                    if spec.name == tool_schema.STAY_SILENT_TOOL
                ),
                None,
            )
            if stay_silent_spec is None:
                await _trajectory(
                    "wake_choice_unavailable",
                    {
                        "round": attempts + 1,
                        "action": "fail_wake_choice_tool_unavailable",
                    },
                )
                exc = ProviderEmptyReply("empty_reply")
                if on_provider_failure is not None:
                    try:
                        await on_provider_failure(exc)
                    except Exception:
                        pass
                raise exc
            tools = [_WAKE_REPLY_TOOL_SPEC, stay_silent_spec]
            surface_candidate_tools = list(tools)
            surface_reason = "wake_choice_required"
            forced_delivery_tool = ""
        compact_delivery_phase = ""
        if compact_delivery_confirmation_needed:
            compact_delivery_phase = "confirm"
            current_user_request = _latest_user_delivery_request()
            if _delivery_control_uses_han(current_user_request):
                messages = [
                    {
                        "role": "system",
                        "content": _compact_delivery_system_prompt(
                            "对方要求的内容已经成功保存并发送。请用你自己的口吻结束本轮，"
                            "使用与下方当前请求相同的语言，把这件事实自然地告诉对方；"
                            "文件现在已经可以打开或下载。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": current_user_request or "请完成对我的回复。",
                    },
                ]
            else:
                messages = [
                    {
                        "role": "system",
                        "content": _compact_delivery_system_prompt(
                            "The work the user asked for was saved and delivered "
                            "successfully. Finish this turn in your own voice, using the "
                            "same language as the user's current request below, with "
                            "that fact available; the user can open the work now."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            current_user_request or "Finish your response to me."
                        ),
                    },
                ]
            # The compact confirmation replaces the normal transcript-shaped
            # messages above. Re-attach bounded correction instructions here;
            # otherwise a language rewrite is marked as attempted but the
            # provider never sees the instruction that requested it.
            if retry_instructions:
                messages = _with_system_suffix(messages, retry_instructions)
        elif (
            forced_delivery_tool == tool_schema.FILE_REPLY_TOOL
            and workspace_delivery_target is not None
        ):
            compact_delivery_phase = "send_file"
            target_path, target_revision = workspace_delivery_target
            current_user_request = _latest_user_delivery_request()
            use_han_control = _delivery_control_uses_han(current_user_request)
            metadata_instruction = (
                (
                    " send_file 必须同时提供 completion_message，作为发送附件后的"
                    "完整可见聊天气泡；必须使用中文并保持你自己的口吻。"
                )
                if use_han_control
                else (
                    " send_file must also provide completion_message as the complete "
                    "visible chat bubble after delivery; write it in English and in "
                    "your own voice."
                )
            )
            if target_path.casefold().endswith(".io.html"):
                metadata_instruction += (
                    (
                        " 这是 Canvas 文件，所以还必须提供简洁的 title 和 subtitle；"
                        "按照当前请求保留或修改这些元数据。"
                    )
                    if use_han_control
                    else (
                        " This is a Canvas file, so send_file also requires a concise "
                        "title and subtitle. Preserve or change that metadata according "
                        "to the current user request."
                    )
                )
            request_instruction = ""
            if current_user_request:
                request_instruction = (
                    (" 对方当前请求：" if use_han_control else " Current user request: ")
                    + json.dumps(current_user_request, ensure_ascii=False)
                    + "。"
                )
            delivery_instruction = (
                (
                    "作品源文件已经保存。现在调用 send_file 一次完成发送，"
                    "目标必须完全一致："
                )
                if use_han_control
                else (
                    "The work's source is already saved. Complete the delivery "
                    "now by calling send_file once with this exact target: "
                )
            )
            messages = [
                {
                    "role": "system",
                    "content": _compact_delivery_system_prompt(
                        delivery_instruction
                        + json.dumps(
                            {"path": target_path, "revision": target_revision},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + metadata_instruction
                        + request_instruction,
                        require_self_thinking=False,
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "现在完成待发送的文件。"
                        if use_han_control
                        else "Complete the pending delivery now."
                    ),
                },
            ]
            if compact_delivery_validation_exchange is not None:
                messages.append(compact_delivery_validation_exchange)
        tail_window = None
        required_schema_names = (
            historical_tool_names
            if terminal_schema_guard
            else completed_memory_discovery_tools
        )
        if forced_delivery_tool:
            required_schema_names = set(required_schema_names) | {
                forced_delivery_tool
            }
        if file_delivery_choice_required:
            required_schema_names = set(required_schema_names) | {
                "workspace_write",
                tool_schema.FILE_REPLY_TOOL,
            }
        if wake_choice_required:
            required_schema_names = {
                _WAKE_REPLY_TOOL,
                tool_schema.STAY_SILENT_TOOL,
            }
        pressure_collapsed_specs = {}
        if refresh_pressure_collapsed_extra_tool_specs is not None:
            try:
                pressure_collapsed_specs = {
                    str(name): spec
                    for name, spec in dict(
                        refresh_pressure_collapsed_extra_tool_specs() or {}
                    ).items()
                    if str(name)
                }
            except Exception:
                pressure_collapsed_specs = {}
        protected_extra_names: set[str] = set()
        if refresh_protected_extra_tool_names is not None:
            try:
                protected_extra_names = {
                    str(name)
                    for name in (
                        refresh_protected_extra_tool_names() or ()
                    )
                    if str(name)
                }
            except Exception:
                protected_extra_names = set()
        recovery_active = False
        if extra_tool_recovery_active is not None:
            try:
                recovery_active = bool(extra_tool_recovery_active())
            except Exception:
                recovery_active = False
        adaptive_planner = getattr(build_messages, "plan_provider_round", None)
        try:
            if compact_delivery_phase:
                frontier_plan = prompt_frontier.plan_provider_round(
                    model_limit=model_prompt_limit,
                    messages=messages,
                    tools=tools,
                    output_reserve_tokens=prompt_output_reserve_tokens,
                    safety_margin_tokens=prompt_safety_margin_tokens,
                    utf8_bytes_per_token=prompt_estimator_utf8_bytes_per_token,
                    image_reserve_tokens=prompt_image_reserve_tokens,
                )
            elif callable(adaptive_planner):
                messages, frontier_plan, tail_window = adaptive_planner(
                    transcript=list(transcript),
                    tools=tools,
                    required_tool_names=required_schema_names,
                    protected_tool_names=protected_extra_names,
                    collapsed_tool_specs=pressure_collapsed_specs,
                    recovery_tool_name=extra_tool_recovery_name,
                    recovery_tool_active=recovery_active,
                    tool_schema_collapse_policy=tool_schema_collapse_policy,
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
                    protected_tool_names=protected_extra_names,
                    collapsed_tool_specs=pressure_collapsed_specs,
                    recovery_tool_name=extra_tool_recovery_name,
                    recovery_tool_active=recovery_active,
                    tool_schema_collapse_policy=tool_schema_collapse_policy,
                    output_reserve_tokens=prompt_output_reserve_tokens,
                    safety_margin_tokens=prompt_safety_margin_tokens,
                    utf8_bytes_per_token=prompt_estimator_utf8_bytes_per_token,
                    image_reserve_tokens=prompt_image_reserve_tokens,
                )
        except prompt_frontier.PromptFrontierExhausted as exc:
            if on_prompt_frontier_exhausted_detail is not None:
                try:
                    emitted = on_prompt_frontier_exhausted_detail(exc)
                    if inspect.isawaitable(emitted):
                        await emitted
                except Exception:
                    pass
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
        if (
            extra_tool_recovery_name
            and not getattr(
                frontier_plan,
                "schema_recovery_needed",
                getattr(frontier_plan, "mcp_recovery_needed", False),
            )
        ):
            surface_candidate_tools = [
                spec for spec in surface_candidate_tools
                if spec.name != extra_tool_recovery_name
            ]
        if planned_offered_tool_names:
            included_tool_names = set(planned_tool_names)
            pressure_collapsed_names = set(
                getattr(
                    frontier_plan,
                    "pressure_collapsed_tool_names",
                    (),
                )
                or ()
            )
            tools = [
                pressure_collapsed_specs.get(
                    spec.name,
                    tool_surface.collapsed_tool_spec(spec),
                )
                if spec.name in pressure_collapsed_names
                else spec
                for spec in (tools or [])
                if spec.name in included_tool_names
            ] or None
            if pressure_collapsed_names and not surface_reason:
                surface_reason = "frontier_collapsed"
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
        if on_extra_tool_surface_plan is not None:
            planned = on_extra_tool_surface_plan(
                {
                    "pressure_collapsed_names": tuple(
                        getattr(
                            frontier_plan,
                            "pressure_collapsed_tool_names",
                            (),
                        )
                        or ()
                    ),
                }
            )
            if inspect.isawaitable(planned):
                await planned
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
                "file_delivery_choice_required": file_delivery_choice_required,
                "wake_choice_required": wake_choice_required,
                "compact_delivery_phase": compact_delivery_phase,
                "prompt_frontier": frontier_plan,
                "tail_window": tail_window,
            },
        )
        previous_offered_tool_names = last_offered_tool_names
        current_offered_tool_names = frozenset(
            str(spec.name) for spec in (tools or [])
        )
        attempts += 1
        _progress("provider_start")
        await _provider_call_event(
            "start",
            {
                "round": attempts,
                "provider": provider_name,
                "model": str(getattr(provider_config, "model", "") or ""),
            },
        )
        provider_call_started_at = time.monotonic()
        provider_error: BaseException | None = None
        provider_surface_detail: dict | None = None
        try:
            # V2 owns the lane-specific empty-response policy. Let the provider
            # parser return any structurally valid success so an abnormal HTTP
            # 200 is not retried as though it were a transient network failure.
            provider_kwargs = {"tools": tools, "require_reply": False}
            if terminal_schema_guard and tools is not None:
                provider_kwargs["tool_choice"] = "none"
            if wake_choice_required:
                provider_kwargs["tool_choice"] = "required"
            if file_delivery_choice_required:
                provider_kwargs["tool_choice"] = "required"
            if allow_image_output and not terminal_text_round:
                provider_kwargs["allow_image_output"] = True
            if (
                forced_delivery_tool
                and not terminal_text_round
                and tools is not None
                and provider_name in _NAMED_TOOL_CHOICE_PROVIDERS
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
                # JSON. File generation owns a separate output budget: the
                # prompt frontier's reserve is input accounting, and increasing
                # it would silently evict otherwise usable history. Wake/child/
                # screen lanes omit on_file_reply and keep their existing limits.
                provider_kwargs["max_tokens"] = (
                    min(file_output_max_tokens, 512)
                    if compact_delivery_phase
                    else file_output_max_tokens
                )
            if on_provider_tool_surface is not None:
                candidate_names = {
                    str(spec.name) for spec in surface_candidate_tools
                }
                sent_names = {str(spec.name) for spec in (tools or [])}
                mcp_candidate_names = candidate_names & mcp_names
                mcp_sent_names = sent_names & mcp_names
                provider_surface_detail = {
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
                    "terminal_text_round": terminal_text_round,
                    "terminal_text_round_reason": terminal_text_round_reason,
                    "force_text_fallback_reason": (
                        force_text_fallback_reason or "none"
                    ),
                    "empty_response_recovery": bool(
                        empty_response_retry_instruction
                    ),
                    "wake_choice_required": wake_choice_required,
                }
                withdrawn_names = (
                    previous_offered_tool_names - current_offered_tool_names
                )
                platform_tool_names = {spec.name for spec in _catalog()}
                withdrawn_platform_names, withdrawn_platform_count = (
                    _bounded_provider_tool_names(
                        withdrawn_names & platform_tool_names
                    )
                )
                provider_surface_detail.update(
                    {
                        "withdrawn_platform_tool_names": (
                            withdrawn_platform_names
                        ),
                        "withdrawn_tool_counts": {
                            "platform": withdrawn_platform_count,
                            "mcp": len(withdrawn_names & mcp_names),
                            "other": len(
                                withdrawn_names
                                - platform_tool_names
                                - mcp_names
                            ),
                        },
                    }
                )
            # Update the history at the exact outbound boundary. Classification
            # of this response keeps the saved previous set, while a provider
            # error followed by another loop round still remembers what was sent.
            last_offered_tool_names = current_offered_tool_names
            result = await provider_client.reliable_chat_completion_async(
                provider_config,
                messages,
                max_attempts=(
                    1 if forced_delivery_tool or wake_choice_required else 2
                ),
                base_delay_sec=0.2,
                max_delay_sec=1.0,
                absolute_deadline=absolute_deadline,
                **provider_kwargs,
            )
        except Exception as exc:
            tagged_image_rejected = (
                not tagged_image_fallback_active
                and _has_tagged_image_message(messages, tagged_image_message_key)
                and getattr(exc, "status_code", None) in {400, 404, 415, 422}
            )
            if tagged_image_rejected:
                tagged_error_facts = _provider_error_facts(exc)
                await _trajectory(
                    "provider_error",
                    {
                        "round": attempts,
                        "error_class": type(exc).__name__,
                        "tools_enabled": tools is not None,
                        "tagged_images_rejected": True,
                        "status_code": tagged_error_facts["status_code"],
                        "provider_error_class": tagged_error_facts[
                            "provider_error_class"
                        ],
                        "dur_ms": (
                            time.monotonic() - provider_call_started_at
                        ) * 1000,
                        "fallback_reason": (
                            _PROVIDER_ATTEMPT_FALLBACK_TAGGED_IMAGES
                        ),
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
                        absolute_deadline=absolute_deadline,
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
            tool_schema_rejected = (
                tools is not None
                and isinstance(exc, provider_client.ProviderError)
                and exc.status_code in {400, 422}
                and attempts < max_calls
                and _is_probably_tool_schema_rejection(exc)
            )
            await _emit_provider_tool_surface(provider_surface_detail)
            provider_error_facts = _provider_error_facts(exc)
            provider_call_dur_ms = (
                time.monotonic() - provider_call_started_at
            ) * 1000
            await _provider_call_event(
                "error",
                {
                    "round": attempts,
                    "provider": provider_name,
                    "model": str(
                        getattr(provider_config, "model", "") or ""
                    ),
                    **provider_error_facts,
                    "dur_ms": provider_call_dur_ms,
                },
            )
            attempt_trace = provider_client.runtime_provider_attempt_trace(exc)
            provider_error_detail = {
                "round": attempts,
                "error_class": type(exc).__name__,
                "tools_enabled": tools is not None,
                "provider_attempt_trace": attempt_trace,
                "status_code": provider_error_facts["status_code"],
                "provider_error_class": provider_error_facts[
                    "provider_error_class"
                ],
                "dur_ms": provider_call_dur_ms,
            }
            if tool_schema_rejected:
                provider_error_detail["fallback_reason"] = (
                    _PROVIDER_ATTEMPT_FALLBACK_TOOL_SCHEMA
                )
            await _trajectory("provider_error", provider_error_detail)
            # TurnMetrics' docstring promises failed provider calls ARE counted
            # (model_calls bumped, just with no token usage) — add_usage(None)
            # before either falling back or propagating.
            add_usage(None)
            if on_provider_failure is not None:
                try:
                    await on_provider_failure(exc)
                except Exception:
                    pass
            if final_reply_correction_request is not None:
                # Language correction is a best-effort polish over an already
                # usable answer. A failed rewrite must never turn that answer
                # into a failed Chat turn.
                try:
                    await _publish_final_correction_fallback("retry_error")
                except FinalReplySuperseded:
                    _cancel_final_reply_correction()
                    reasoning_fragments.clear()
                    seen_reasoning_fragments.clear()
                    _progress("final_reply_superseded")
                    await _trajectory(
                        "final_reply_superseded",
                        {"round": attempts},
                    )
                    if attempts < max_calls:
                        continue
                    return LoopOutcome(
                        "", attempts, "input_advanced", replied_intermediate
                    )
                return LoopOutcome(
                    final_reply_correction_request.original_text,
                    attempts,
                    "final_text",
                    replied_intermediate,
                )
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
            if tool_schema_rejected:
                force_text_fallback = True
                force_text_fallback_reason = "tool_schema_rejected"
                await _trajectory(
                    "protocol_fallback",
                    {"round": attempts, "reason": "tool_schema_rejected"},
                )
                _progress("provider_retry_boundary")
                continue
            raise provider_error
        raw_finish_reason = str(result.get("stop_reason") or "").strip().lower()
        await _provider_call_event(
            "done",
            {
                "round": attempts,
                "provider": provider_name,
                "model": str(getattr(provider_config, "model", "") or ""),
                "finish_reason": (
                    raw_finish_reason
                    if raw_finish_reason in _CONTENT_FREE_STOP_REASONS
                    else ("other" if raw_finish_reason else "unspecified")
                ),
                "dur_ms": (
                    time.monotonic() - provider_call_started_at
                ) * 1000,
            },
        )
        _progress("provider_complete")
        add_usage(result.get("usage"))
        upstream_response_envelope = protocol_leak.is_upstream_response_envelope(
            result.get("reply")
        )
        raw_has_usable_output = bool(
            (
                str(result.get("reply") or "").strip()
                and not upstream_response_envelope
            )
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
        rejection_facts = (
            _tool_call_rejection_facts(
                pr,
                tools=tools,
                raw_finish_reason=raw_finish_reason,
                terminal_text_round=terminal_text_round,
                tool_calls_used=tool_calls_used,
                previous_offered_tool_names=previous_offered_tool_names,
                completed_memory_discovery_tools=(
                    completed_memory_discovery_tools
                ),
                external_content_seen=external_content_seen,
                allowed_fetch_urls=allowed_fetch_urls,
                mutating_mcp_names=mutating_mcp_names,
                max_tool_args_chars=max_tool_args_chars,
                max_tool_batch_args_chars=max_tool_batch_args_chars,
                max_native_assistant_turn_chars=(
                    max_native_assistant_turn_chars
                ),
                max_assistant_tool_text_chars=max_assistant_tool_text_chars,
                max_tool_calls_per_round=max_tool_calls_per_round,
                max_tool_calls_per_turn=max_tool_calls_per_turn,
            )
            if pr.tool_calls
            else None
        )
        # Parsed calls with invalid domain arguments are separate from broken
        # provider protocol (for example ``args_ok=False`` above), but both are
        # rejected exchanges and must be visible on the same content-free
        # provider surface. Compute this once before emitting the surface, then
        # reuse the exact per-call reasons in the rejection transcript below.
        validation_errors: dict[str, str] = {}
        if rejection_facts is not None and not rejection_facts.rejected:
            validation_errors = {
                tc.id: validation_error
                for tc in pr.tool_calls
                if tc.name not in mcp_names
                and (
                    validation_error := tool_schema.validate_tool_args(
                        tc.name,
                        tc.args,
                        live_model_call=(compact_delivery_phase == "send_file"),
                    )
                )
                is not None
            }
            if (
                compact_delivery_phase == "send_file"
                and _latest_user_delivery_request()
            ):
                for tc in pr.tool_calls:
                    if (
                        tc.name == tool_schema.FILE_REPLY_TOOL
                        and tc.id not in validation_errors
                        and not str(tc.args.get("completion_message") or "").strip()
                    ):
                        validation_errors[tc.id] = (
                            "send_file requires completion_message for the "
                            "visible delivery bubble"
                        )
            if compact_delivery_phase == "send_file" and len(pr.tool_calls) != 1:
                validation_errors.update(
                    {
                        tc.id: (
                            "pending Canvas delivery requires exactly one "
                            "send_file call"
                        )
                        for tc in pr.tool_calls
                    }
                )
        repeated_generic_validation = bool(
            validation_errors
            and compact_delivery_phase != "send_file"
            and generic_validation_retry_used
        )
        schema_rejection_reasons = (
            [
                (
                    _PROVIDER_CALL_REJECTION_REASON_REPEATED_INVALID_TOOL_ARGUMENTS
                    if repeated_generic_validation
                    else _PROVIDER_CALL_REJECTION_REASON_INVALID_TOOL_ARGUMENTS
                )
                for _tc in pr.tool_calls
            ]
            if validation_errors
            else []
        )
        surface_rejection_reasons = _normalize_provider_call_rejection_reasons(
            [
                *(rejection_facts.reason_tokens if rejection_facts is not None else []),
                *schema_rejection_reasons,
            ]
        )
        surface_exchange_rejected = bool(
            (rejection_facts is not None and rejection_facts.rejected)
            or validation_errors
        )
        if surface_exchange_rejected and not surface_rejection_reasons:
            surface_rejection_reasons = [_PROVIDER_CALL_REJECTION_REASON_UNCLASSIFIED]
        if wake_choice_required or tools is None:
            # These branches have their own terminal/empty response contracts;
            # they never enter the generic rejected-tool-exchange path below.
            surface_rejection_reasons = []
        if (
            terminal_text_round
            and pr.tool_calls
            and (tools is not None or pr.text.strip())
        ):
            surface_rejection_reasons = _normalize_provider_call_rejection_reasons(
                [
                    *surface_rejection_reasons,
                    _PROVIDER_CALL_REJECTION_REASON_TERMINAL_TOOL_CALL_REJECTED,
                ]
            )
        unavailable_platform_call_labels: list[str] = []
        unavailable_call_counts = {"platform": 0, "mcp": 0, "other": 0}
        if rejection_facts is not None:
            platform_tool_names = {spec.name for spec in _catalog()}
            for tc, joined_reasons in zip(
                pr.tool_calls,
                rejection_facts.call_rejection_reasons,
            ):
                reason_tokens = set(joined_reasons.split(","))
                for reason in (
                    _TOOL_WITHDRAWN_REJECTION_REASON,
                    _UNKNOWN_TOOL_REJECTION_REASON,
                ):
                    if reason in reason_tokens:
                        if tc.name in platform_tool_names:
                            unavailable_call_counts["platform"] += 1
                            unavailable_platform_call_labels.append(
                                f"{reason}:{tc.name}"
                            )
                        elif tc.name in mcp_names:
                            unavailable_call_counts["mcp"] += 1
                        else:
                            unavailable_call_counts["other"] += 1
        unavailable_platform_call_labels = sorted(
            unavailable_platform_call_labels
        )[:_PROVIDER_TOOL_NAME_TRACE_CAP]
        if provider_surface_detail is not None:
            provider_surface_detail.update(
                {
                    "unavailable_platform_tool_call_labels": (
                        unavailable_platform_call_labels
                    ),
                    "unavailable_tool_call_counts": unavailable_call_counts,
                }
            )
        await _emit_provider_tool_surface(
            provider_surface_detail,
            surface_rejection_reasons,
        )

        if wake_choice_required:
            wake_reply_calls = [
                tc for tc in pr.tool_calls if tc.name == _WAKE_REPLY_TOOL
            ]
            stay_silent_calls = [
                tc
                for tc in pr.tool_calls
                if tc.name == tool_schema.STAY_SILENT_TOOL
            ]
            selected_call = pr.tool_calls[0] if len(pr.tool_calls) == 1 else None
            selected_reply_text = (
                selected_call.args.get("text")
                if selected_call is not None
                and selected_call.name == _WAKE_REPLY_TOOL
                and selected_call.args_ok
                and set(selected_call.args) <= {"text"}
                else None
            )
            reply_text = (
                selected_reply_text.strip()
                if isinstance(selected_reply_text, str)
                else ""
            )
            silent_reason = (
                str(selected_call.args.get("reason") or "").strip()
                if selected_call is not None
                and selected_call.name == tool_schema.STAY_SILENT_TOOL
                and selected_call.args_ok
                else ""
            )
            valid_reply_choice = bool(
                selected_call is not None
                and selected_call.id
                and len(wake_reply_calls) == 1
                and reply_text
                and not pr.media
                and len(reply_text) <= max_assistant_tool_text_chars
            )
            valid_silent_choice = bool(
                selected_call is not None
                and selected_call.id
                and len(stay_silent_calls) == 1
                and silent_reason
                and not pr.media
                and tool_schema.validate_tool_args(
                    tool_schema.STAY_SILENT_TOOL,
                    selected_call.args,
                )
                is None
            )
            await _trajectory(
                "wake_choice_response",
                {
                    "round": attempts,
                    "choice": (
                        _WAKE_REPLY_TOOL
                        if valid_reply_choice
                        else (
                            tool_schema.STAY_SILENT_TOOL
                            if valid_silent_choice
                            else "invalid"
                        )
                    ),
                    "tool_call_count": len(pr.tool_calls),
                    "provider_text_present": bool(pr.text.strip()),
                },
            )
            if valid_reply_choice:
                tool_calls_used += 1
                # Feed the selected text into the ordinary terminal-text path.
                # Any provider text beside the call is only a preamble, exactly
                # like other tool rounds, and is never published separately.
                pr = ProviderResponse(
                    text=reply_text,
                    tool_calls=[],
                    usage=pr.usage,
                    raw=pr.raw,
                    assistant_turn=None,
                    media=(),
                )
                wake_choice_required = False
            elif valid_silent_choice:
                wake_choice_required = False
            else:
                exc = ProviderEmptyReply("empty_reply")
                if on_provider_failure is not None:
                    try:
                        await on_provider_failure(exc)
                    except Exception:
                        pass
                raise exc

        if (
            not require_reply
            and on_stay_silent is not None
            and not pr.text.strip()
            and not pr.tool_calls
            and not pr.media
        ):
            wake_choice_supported = (
                on_stay_silent is not None
                and provider_name in _NAMED_TOOL_CHOICE_PROVIDERS
            )
            can_force_wake_choice = (
                on_stay_silent is not None
                and not wake_choice_recovery_used
                and wake_choice_supported
                and wake_choice_tool_available
                and attempts < max_calls
                and tool_calls_used < max_tool_calls_per_turn
            )
            if can_force_wake_choice:
                action = "force_wake_choice"
            elif wake_choice_required:
                action = "fail_forced_wake_choice_empty"
            elif not wake_choice_supported:
                action = "fail_wake_choice_unsupported"
            elif not wake_choice_tool_available:
                action = "fail_wake_choice_tool_unavailable"
            else:
                action = "fail_wake_choice_budget_exhausted"
            await _trajectory(
                "empty_provider_response",
                {
                    "round": attempts,
                    "reason": "empty_provider_success",
                    "response_shape": _empty_response_shape(pr),
                    "action": action,
                },
            )
            if on_empty_provider_response is not None:
                try:
                    await on_empty_provider_response(_empty_response_shape(pr))
                except Exception:
                    # Plaintext diagnostics are best-effort and cannot alter
                    # the recovery/failure decision below.
                    pass
            if can_force_wake_choice:
                wake_choice_recovery_used = True
                wake_choice_required = True
                reasoning_fragments.clear()
                seen_reasoning_fragments.clear()
                _progress("wake_choice_retry_boundary")
                continue
            exc = ProviderEmptyReply("empty_reply")
            if on_provider_failure is not None:
                try:
                    await on_provider_failure(exc)
                except Exception:
                    pass
            raise exc

        if (
            not require_reply
            and on_stay_silent is None
            and not pr.text.strip()
            and not pr.tool_calls
            and not pr.media
        ):
            # Non-wake internal callers retain the historical weak empty-success
            # behavior. The forced two-choice contract is lane-gated by the
            # presence of the wake-only stay_silent callback.
            await _trajectory(
                "empty_provider_response",
                {
                    "round": attempts,
                    "reason": "empty_provider_success",
                    "response_shape": _empty_response_shape(pr),
                    "action": "accept_silent_empty",
                },
            )
            if on_empty_provider_response is not None:
                try:
                    await on_empty_provider_response(_empty_response_shape(pr))
                except Exception:
                    pass

        if (
            final_reply_correction_request is not None
            and (not pr.text.strip() or upstream_response_envelope)
            and not pr.tool_calls
            and not pr.media
        ):
            # Do not hand an empty language rewrite to the ordinary empty-response
            # recovery, which would create a second correction loop. The original
            # answer is already usable, so publish it immediately instead.
            await _trajectory(
                "empty_provider_response",
                {
                    "round": attempts,
                    "reason": (
                        "upstream_response_envelope"
                        if upstream_response_envelope
                        else "empty_provider_success"
                    ),
                    "response_shape": _empty_response_shape(pr),
                    "action": "language_correction_fallback",
                },
            )
            if on_empty_provider_response is not None:
                try:
                    await on_empty_provider_response(_empty_response_shape(pr))
                except Exception:
                    pass
            try:
                await _publish_final_correction_fallback("retry_empty")
            except FinalReplySuperseded:
                if final_reply_correction_request is not None:
                    _cancel_final_reply_correction()
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
                final_reply_correction_request.original_text,
                attempts,
                "final_text",
                replied_intermediate,
            )

        if (
            require_reply
            and (not pr.text.strip() or upstream_response_envelope)
            and not pr.tool_calls
            and not pr.media
        ):
            semantic_empty = bool(
                upstream_response_envelope
                or str(pr.raw.get("reasoning") or "").strip()
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
                    "reason": (
                        "upstream_response_envelope"
                        if upstream_response_envelope
                        else "empty_provider_success"
                    ),
                    "response_shape": _empty_response_shape(pr),
                    "action": (
                        "semantic_correction"
                        if can_correct
                        else "fail_provider_empty_reply"
                    ),
                },
            )
            if on_empty_provider_response is not None:
                try:
                    await on_empty_provider_response(_empty_response_shape(pr))
                except Exception:
                    # Plaintext diagnostics are best-effort and must never
                    # alter the retry/failure decision below.
                    pass
            if can_correct:
                empty_response_recovery_used = True
                empty_response_retry_instruction = (
                    normalized_empty_response_correction
                )
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

        # This request was already an explicit request for a complete answer using
        # existing information. If a broken relay or model still invents a tool
        # call, do not publish its accompanying ``pr.text``: text beside a tool call
        # is only a preamble and may be partial or claim an operation that was never
        # executed. Give transient provider failures a small configurable number of
        # fresh chances, then terminate without returning to tool dispatch or the
        # malformed-exchange fallback.
        if (
            terminal_text_round
            and pr.tool_calls
            and (tools is not None or pr.text.strip())
        ):
            retrying = (
                terminal_tool_call_retries < max_terminal_tool_call_retries
                and attempts < max_calls
            )
            transcript.append(
                _rejected_tool_exchange(
                    pr.tool_calls,
                    assistant_text=pr.text,
                    rejection_reasons=[
                        _PROVIDER_CALL_REJECTION_REASON_TERMINAL_TOOL_CALL_REJECTED
                        for _tool_call in pr.tool_calls
                    ],
                    attempt=attempts,
                )
            )
            await _trajectory(
                "protocol_fallback",
                {
                    "round": attempts,
                    "reason": _PROVIDER_CALL_REJECTION_REASON_TERMINAL_TOOL_CALL_REJECTED,
                    "action": "retry" if retrying else "terminate",
                    "retry": terminal_tool_call_retries,
                    "transcript_appended": True,
                },
            )
            if retrying:
                terminal_tool_call_retries += 1
                _progress("terminal_tool_call_retry_boundary")
                continue
            break

        # A tools-disabled request is terminal. The guard above has already
        # rejected any undeclared call carrying text. A wholly text-free broken
        # response retains the existing empty-reply failure classification.
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

            # D scheme: a structured identity-write attempt that did not produce
            # a successful result gets one extra provider round. Do not inspect
            # or classify the model's prose.
            if (
                pr.text
                and identity_write_failed_bounces < 1
                and _identity_write_attempted(transcript)
                and not _identity_write_succeeded(transcript)
            ):
                identity_write_failed_bounces += 1
                await _trajectory(
                    "identity_write_failed_bounced",
                    {"round": attempts, "text_chars": len(pr.text)},
                )
                identity_write_failed_instruction = (
                    "上一轮你调用了身份写工具,但那次调用**没有成功**"
                    "(被拒绝、出错或仍在排队),身份没有真的改动。"
                    "请据实处理:要么重试一次,要么照实告诉他没改成 —— "
                    "不要把这次未生效的改动说成已经完成。"
                )
                if attempts < max_calls:
                    _progress("identity_write_failed_retry_boundary")
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
                    if workspace_delivery_candidate is not None:
                        candidate_path, candidate_revision = (
                            workspace_delivery_candidate
                        )
                        delivery_retry_instruction = (
                            "REQUIRED FILE DELIVERY: The user explicitly requested "
                            f"downloadable output in {target}. An existing exact "
                            "workspace revision is available at "
                            + json.dumps(
                                {
                                    "path": candidate_path,
                                    "revision": candidate_revision,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + ". Do not finish with plain text or an internal link. "
                            "Call workspace_write if the source bytes still need "
                            "the user's requested change; otherwise call send_file "
                            "for that exact existing revision."
                        )
                        existing_file_delivery_choice_required = True
                    else:
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
                    # Reset stale stall history so required write/send recovery
                    # is not preempted immediately after workspace_write.
                    consecutive_tool_only_rounds = 0
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
                reply_decision = await on_reply(pr.text, **reply_kwargs)
                if isinstance(reply_decision, FinalReplyCorrectionRequest):
                    if (
                        final_reply_correction_request is None
                        and attempts < max_calls
                    ):
                        final_reply_correction_request = reply_decision
                        final_reply_correction_instruction = str(
                            reply_decision.instruction or ""
                        ).strip()
                        if not final_reply_correction_instruction:
                            await on_reply(
                                reply_decision.original_text,
                                final=True,
                                reasoning=reply_decision.original_reasoning,
                                correction_outcome="skipped",
                            )
                            return LoopOutcome(
                                reply_decision.original_text,
                                attempts,
                                "final_text",
                                replied_intermediate,
                            )
                        # A rewrite is not authorized to repeat tools or side
                        # effects. Keep historical schemas only where the wire
                        # requires them, paired with tool_choice=none.
                        force_text_fallback = True
                        force_text_fallback_reason = "final_reply_correction"
                        reasoning_fragments.clear()
                        seen_reasoning_fragments.clear()
                        _progress("final_reply_correction_boundary")
                        continue
                    # No call budget (or a malformed second request): publish
                    # the already usable original instead of failing the turn.
                    fallback = (
                        final_reply_correction_request or reply_decision
                    )
                    final_reply_correction_request = fallback
                    await _publish_final_correction_fallback("skipped")
                    return LoopOutcome(
                        fallback.original_text,
                        attempts,
                        "final_text",
                        replied_intermediate,
                    )
                if isinstance(reply_decision, FinalReplyCorrectionRejected):
                    if final_reply_correction_request is None:
                        raise RuntimeError(
                            "final reply correction rejected without request"
                        )
                    await _publish_final_correction_fallback(
                        "kept_original_still_mismatch"
                    )
                    return LoopOutcome(
                        final_reply_correction_request.original_text,
                        attempts,
                        "final_text",
                        replied_intermediate,
                    )
                if reply_decision is not None:
                    raise RuntimeError("unsupported final reply decision")
            except FinalReplySuperseded:
                if final_reply_correction_request is not None:
                    _cancel_final_reply_correction()
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

        assert rejection_facts is not None
        image_reply_calls = rejection_facts.image_reply_calls
        stay_silent_calls = rejection_facts.stay_silent_calls
        malformed = rejection_facts.malformed
        truncated_tool_arguments = rejection_facts.truncated_tool_arguments
        mixed_reply_write = rejection_facts.mixed_reply_write
        over_tool_call_budget = rejection_facts.over_tool_call_budget
        oversized_tool_exchange = rejection_facts.oversized_tool_exchange
        call_rejection_reasons = rejection_facts.call_rejection_reasons
        if rejection_facts.rejected:
            # Invalid, over-budget, and duplicate-id batches are all-or-nothing:
            # executing a valid subset and then asking for a correction can
            # duplicate durable writes on the corrected round. Missing/duplicate
            # ids also cannot form a provider-native result exchange. Record one
            # bounded synthetic rejection and make exactly one text-only fallback.
            # Wires that require schemas for historical calls may retain the
            # matching definitions, but tool_choice remains none.
            # An outbound delivery plus either a platform or MCP mutation is
            # rejected for the same reason: the bubble cannot truthfully claim
            # success before the later sink commits. The model may mutate in one
            # round and reply only after observing its result in the next.
            # A provider that explicitly reports its output-token limit while
            # returning unparseable tool arguments stopped in the middle of the
            # JSON payload. Retrying the same artifact with the same budget
            # cannot repair it and rewrites the real cause as a tool-usage error.
            # Fail once with content-free evidence; no partial call is executed.
            if truncated_tool_arguments:
                await _trajectory(
                    "provider_output_truncated",
                    {
                        "round": attempts,
                        "reason": "output_truncated",
                        "finish_reason": provider_client.normalize_stop_reason(
                            raw_finish_reason
                        ),
                        "malformed_tool_arguments": True,
                        "retry": False,
                    },
                )
                raise ProviderOutputTruncated()
            if attempts >= max_calls:
                break
            transcript.append(
                _rejected_tool_exchange(
                    pr.tool_calls,
                    assistant_text=pr.text,
                    rejection_reasons=call_rejection_reasons,
                    attempt=attempts,
                )
            )
            await _trajectory(
                "protocol_fallback",
                {
                    "round": attempts,
                    "reason": _PROVIDER_CALL_REJECTION_REASON_INVALID_OR_OVER_BUDGET_TOOL_EXCHANGE,
                    "malformed": malformed,
                    "mixed_reply_write": mixed_reply_write,
                    "over_tool_call_budget": over_tool_call_budget,
                    "oversized_tool_exchange": oversized_tool_exchange,
                    "transcript_appended": True,
                },
            )
            force_text_fallback = True
            force_text_fallback_reason = (
                _PROVIDER_CALL_REJECTION_REASON_INVALID_OR_OVER_BUDGET_TOOL_EXCHANGE
            )
            continue

        # Parsed calls with invalid domain arguments are not a broken provider
        # protocol. Return one native result per call and let the model correct
        # the all-or-nothing batch once; no call in the invalid batch is
        # dispatched. Keep the generic retry separate from compact Canvas
        # delivery retries so ordinary argument repair cannot consume a pending
        # file's exact-target or metadata correction.
        if validation_errors:
            if repeated_generic_validation:
                if attempts >= max_calls:
                    break
                transcript.append(
                    _rejected_tool_exchange(
                        pr.tool_calls,
                        assistant_text=pr.text,
                        rejection_reasons=schema_rejection_reasons,
                        attempt=attempts,
                    )
                )
                await _trajectory(
                    "protocol_fallback",
                    {
                        "round": attempts,
                        "reason": (
                            _PROVIDER_CALL_REJECTION_REASON_REPEATED_INVALID_TOOL_ARGUMENTS
                        ),
                        "malformed": False,
                        "mixed_reply_write": False,
                        "over_tool_call_budget": False,
                        "oversized_tool_exchange": False,
                        "transcript_appended": True,
                    },
                )
                force_text_fallback = True
                force_text_fallback_reason = (
                    _PROVIDER_CALL_REJECTION_REASON_REPEATED_INVALID_TOOL_ARGUMENTS
                )
                continue
            if (
                compact_delivery_phase == "send_file"
                and compact_delivery_args_retry_used
            ):
                delivery_path = (
                    workspace_delivery_target[0]
                    if workspace_delivery_target is not None
                    else (
                        workspace_delivery_candidate[0]
                        if workspace_delivery_candidate is not None
                        else (
                            str(pr.tool_calls[0].args.get("path") or "")
                            if pr.tool_calls
                            else ""
                        )
                    )
                )
                canvas_delivery = delivery_path.casefold().endswith(".io.html")
                await _trajectory(
                    "protocol_fallback",
                    {
                        "round": attempts,
                        "reason": (
                            "repeated_invalid_canvas_delivery_args"
                            if canvas_delivery
                            else "repeated_invalid_file_delivery_args"
                        ),
                        "invalid_tool_names": sorted(
                            {
                                tc.name
                                for tc in pr.tool_calls
                                if tc.id in validation_errors
                            }
                        ),
                    },
                )
                raise _delivery_incomplete(
                    delivery_path,
                    (
                        "invalid_canvas_delivery_args"
                        if canvas_delivery
                        else "invalid_file_delivery_args"
                    ),
                )

            if compact_delivery_phase == "send_file":
                compact_delivery_args_retry_used = True
            else:
                generic_validation_retry_used = True
            tool_calls_used += len(pr.tool_calls)
            validation_results: list[ToolResult] = []
            for tc in pr.tool_calls:
                await _tool_event(tc, "tool_call_started", {})
                validation_error = validation_errors.get(tc.id)
                content = (
                    f"error: invalid args for {tc.name}: {validation_error}. "
                    "Nothing in this tool batch was executed. Correct the "
                    "arguments and call the tool again."
                    if validation_error is not None
                    else (
                        "error: tool batch not executed because another call had "
                        "invalid arguments. Resubmit after correcting that call."
                    )
                )
                result = ToolResult(call_id=tc.id, content=content)
                validation_results.append(result)
                await _tool_event(tc, "tool_call_result", {"result": result})
            validation_results = _normalize_tool_results(
                validation_results,
                per_result_cap=tool_result_char_cap,
                batch_cap=tool_batch_result_char_cap,
            )
            await _trajectory(
                "tool_batch_validation_failed",
                {
                    "round": attempts,
                    "calls": pr.tool_calls,
                    "results": validation_results,
                },
            )
            validation_exchange = ToolExchange(
                calls=tuple(pr.tool_calls),
                results=tuple(validation_results),
                assistant_text=pr.text,
                assistant_turn=pr.assistant_turn,
            )
            transcript.append(validation_exchange)
            if compact_delivery_phase == "send_file":
                compact_delivery_validation_exchange = validation_exchange
            continue

        tool_calls_used += len(pr.tool_calls)

        # text accompanying tool_calls = preamble/thinking, NOT a bubble.
        file_reply_calls = [
            tc for tc in pr.tool_calls if tc.name == tool_schema.FILE_REPLY_TOOL
        ]
        loop_reply_tools = {
            tool_schema.FILE_REPLY_TOOL,
            tool_schema.IMAGE_REPLY_TOOL,
            tool_schema.STAY_SILENT_TOOL,
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
                metadata={"memory_discovery_reused": True},
            )
            reply_results[tc.id] = repeated_result
            await _tool_event(
                tc, "tool_call_result", {"result": repeated_result}
            )
        for tc in stay_silent_calls:
            reason = str(tc.args.get("reason") or "").strip()
            await _tool_event(tc, "tool_call_started", {})
            await _trajectory(
                "stay_silent_planned",
                {"round": attempts, "call_id": tc.id, "reason": reason},
            )
            if on_stay_silent is None:
                raise RuntimeError("stay_silent callback is unavailable")
            await on_stay_silent(reason)
            silent_result = ToolResult(call_id=tc.id, content="ok: staying silent")
            await _tool_event(tc, "tool_call_result", {"result": silent_result})
            return LoopOutcome("", attempts, "stay_silent", replied_intermediate)

        file_completion_message = ""
        file_completion_validated = False
        for tc in file_reply_calls:
            workspace_path = str(tc.args.get("path") or "").strip()
            workspace_revision = int(tc.args["revision"])
            is_canvas_delivery = workspace_path.casefold().endswith(".io.html")
            await _tool_event(tc, "tool_call_started", {})
            await _trajectory(
                "file_reply_planned",
                {"round": attempts, "call_id": tc.id},
            )
            # Presence is guaranteed by the offer gate above. Keep the explicit
            # check as a defense for direct/internal callers.
            if on_file_reply is None:
                raise RuntimeError("send_file callback is unavailable")
            file_suffix = _file_suffix_for_requirement(
                workspace_path,
                normalized_required_suffixes,
            )
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
            if (
                workspace_delivery_target is not None
                and (workspace_path, workspace_revision)
                != workspace_delivery_target
            ):
                target_path, target_revision = workspace_delivery_target
                file_result = ToolResult(
                    call_id=tc.id,
                    content=(
                        "error: pending_delivery_target_mismatch; no file was "
                        "delivered. Call send_file with exactly "
                        + json.dumps(
                            {"path": target_path, "revision": target_revision},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                )
                reply_results[tc.id] = file_result
                compact_delivery_validation_exchange = ToolExchange(
                    calls=(tc,),
                    results=(file_result,),
                    assistant_text=pr.text,
                    assistant_turn=pr.assistant_turn,
                )
                await _tool_event(
                    tc, "tool_call_result", {"result": file_result}
                )
                if compact_delivery_mismatch_retry_used:
                    raise _delivery_incomplete(
                        target_path, "pending_delivery_target_mismatch"
                    )
                else:
                    compact_delivery_mismatch_retry_used = True
                continue
            if (
                workspace_delivery_target is None
                and existing_file_delivery_choice_required
                and workspace_delivery_candidate is not None
                and (workspace_path, workspace_revision)
                != workspace_delivery_candidate
            ):
                target_path, target_revision = workspace_delivery_candidate
                file_result = ToolResult(
                    call_id=tc.id,
                    content=(
                        "error: existing_delivery_candidate_mismatch; no file was "
                        "delivered. Call send_file with exactly "
                        + json.dumps(
                            {"path": target_path, "revision": target_revision},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                )
                reply_results[tc.id] = file_result
                await _tool_event(
                    tc, "tool_call_result", {"result": file_result}
                )
                if compact_delivery_mismatch_retry_used:
                    raise _delivery_incomplete(
                        target_path, "existing_delivery_candidate_mismatch"
                    )
                compact_delivery_mismatch_retry_used = True
                continue
            completion_message = str(
                tc.args.get("completion_message") or ""
            ).strip()
            thinking_status, _thinking, visible_completion = (
                self_thinking.strip_all_thinking(completion_message)
            )
            if thinking_status == self_thinking.COMPLETE:
                completion_message = visible_completion
            elif thinking_status in {self_thinking.SILENT, self_thinking.FAILED}:
                completion_message = ""
            current_user_request = _latest_user_delivery_request()
            if current_user_request and not _delivery_completion_matches_request(
                current_user_request, completion_message
            ):
                if compact_delivery_args_retry_used:
                    await _trajectory(
                        "file_completion_language_follow",
                        {
                            "round": attempts,
                            "expected": _delivery_request_writing_system(
                                current_user_request
                            ),
                            "actual": language_follow.classify_writing_system(
                                completion_message
                            ),
                            "outcome": "kept_after_bounded_correction",
                        },
                    )
                else:
                    required_script = _delivery_request_writing_system(
                        current_user_request
                    )
                    file_result = ToolResult(
                        call_id=tc.id,
                        content=(
                            "error: completion_message language mismatch; no file was "
                            f"delivered. Rewrite completion_message using {required_script} "
                            "and call send_file again with the same path and revision. "
                            "If the current request explicitly requires another language, "
                            "resubmit the same completion_message unchanged to confirm "
                            "that choice."
                        ),
                    )
                    reply_results[tc.id] = file_result
                    compact_delivery_validation_exchange = ToolExchange(
                        calls=(tc,),
                        results=(file_result,),
                        assistant_text=pr.text,
                        assistant_turn=pr.assistant_turn,
                    )
                    await _tool_event(
                        tc, "tool_call_result", {"result": file_result}
                    )
                    compact_delivery_args_retry_used = True
                    continue
            try:
                if is_canvas_delivery:
                    await on_file_reply(
                        workspace_path,
                        workspace_revision,
                        title=str(tc.args.get("title") or ""),
                        subtitle=str(tc.args.get("subtitle") or ""),
                    )
                else:
                    await on_file_reply(workspace_path, workspace_revision)
            except Exception as exc:
                await _tool_event(
                    tc, "tool_call_error", {"error": type(exc).__name__}
                )
                await _trajectory(
                    "file_reply_failed",
                    {
                        "round": attempts,
                        "call_id": tc.id,
                        "canvas": is_canvas_delivery,
                        "action": (
                            "fail"
                            if file_delivery_callback_retry_used
                            else "retry"
                        ),
                    },
                )
                file_result = ToolResult(
                    call_id=tc.id,
                    content=(
                        "error: file delivery did not complete. Nothing was "
                        "attached. Create or refresh the requested source with "
                        "workspace_write, then call send_file again using the "
                        "returned path and revision."
                    ),
                )
                reply_results[tc.id] = file_result
                await _tool_event(
                    tc, "tool_call_result", {"result": file_result}
                )
                if file_delivery_callback_retry_used:
                    # ``send_file`` is the publication boundary. The workspace
                    # source may already exist at the selected revision, but
                    # loading, validating, or staging the attachment can still
                    # fail. Export the stable path-aware class after one bounded
                    # correction so the worker/terminal outbox cannot collapse
                    # this into ``unknown``.
                    raise _delivery_incomplete(
                        workspace_path,
                        "file_delivery_callback_failed",
                    ) from exc
                file_delivery_callback_retry_used = True
                file_delivery_recovery_needed = True
                if workspace_delivery_target is not None:
                    # Compact delivery rounds replace the normal transcript;
                    # carry the native result explicitly so the exact-target
                    # retry still sees why its previous send_file failed.
                    compact_delivery_validation_exchange = ToolExchange(
                        calls=(tc,),
                        results=(file_result,),
                        assistant_text=pr.text,
                        assistant_turn=pr.assistant_turn,
                    )
                continue
            delivered_file_suffixes.add(file_suffix)
            workspace_write_applied = False
            workspace_delivery_target = None
            workspace_delivery_candidate = None
            existing_file_delivery_choice_required = False
            compact_delivery_validation_exchange = None
            compact_delivery_mismatch_retry_used = False
            compact_delivery_args_retry_used = False
            file_delivery_callback_retry_used = False
            file_completion_message = (
                completion_message
                if is_canvas_delivery or current_user_request
                else ""
            )
            file_completion_validated = bool(
                file_completion_message and current_user_request
            )
            requirement_now_met = (
                bool(delivered_file_suffixes)
                if not normalized_required_suffixes
                else normalized_required_suffixes.issubset(delivered_file_suffixes)
            )
            if requirement_now_met and not file_completion_message:
                compact_delivery_confirmation_needed = True
            replied_intermediate = True
            file_result = ToolResult(
                call_id=tc.id,
                content="ok: file delivered",
            )
            reply_results[tc.id] = file_result
            await _tool_event(
                tc, "tool_call_result", {"result": file_result}
            )

        if file_completion_message:
            # Attachment metadata and the visible completion bubble are one model
            # expression. Publishing the tool-authored bubble here keeps the
            # staged attachment and its text in the same final effect, and avoids
            # a second provider round seeded by runtime-authored English copy.
            compact_delivery_confirmation_needed = False
            await _trajectory(
                "reply_planned",
                {
                    "round": attempts,
                    "final": True,
                    "text": file_completion_message,
                    "reason": "file_tool_completion",
                },
            )
            try:
                reply_text = (
                    ValidatedFinalReply(file_completion_message)
                    if file_completion_validated
                    else file_completion_message
                )
                reply_decision = await on_reply(
                    reply_text,
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
                return LoopOutcome(
                    "", attempts, "input_advanced", replied_intermediate
                )
            if isinstance(reply_decision, FinalReplyCorrectionRequest):
                if (
                    final_reply_correction_request is None
                    and attempts < max_calls
                    and str(reply_decision.instruction or "").strip()
                ):
                    final_reply_correction_request = reply_decision
                    final_reply_correction_instruction = str(
                        reply_decision.instruction
                    ).strip()
                    force_text_fallback = True
                    force_text_fallback_reason = "final_reply_correction"
                    reasoning_fragments.clear()
                    seen_reasoning_fragments.clear()
                    _progress("final_reply_correction_boundary")
                else:
                    final_reply_correction_request = reply_decision
                    await _publish_final_correction_fallback("skipped")
                    return LoopOutcome(
                        reply_decision.original_text,
                        attempts,
                        "final_text",
                        replied_intermediate,
                    )
            elif isinstance(reply_decision, FinalReplyCorrectionRejected):
                raise RuntimeError(
                    "final reply correction rejected without request"
                )
            elif reply_decision is not None:
                raise RuntimeError("unsupported final reply decision")
            else:
                return LoopOutcome(
                    file_completion_message,
                    attempts,
                    "final_text",
                    replied_intermediate,
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

        # This is the provider-neutral trust boundary: platform, MCP, and any
        # future injected dispatcher all receive identical prompt budgets.
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
                request_url = str(tc.args.get("url") or "").strip()
                allowed_fetch_urls.discard(request_url)
                metadata = result.metadata or {}
                next_offset = metadata.get("web_fetch_next_offset")
                continuation_urls = metadata.get("web_fetch_continuation_urls")
                if type(next_offset) is int and isinstance(
                    continuation_urls, (tuple, list)
                ):
                    allowed_fetch_urls.update(
                        str(url).strip()
                        for url in continuation_urls
                        if str(url).strip()
                    )
        for tc in dispatch_calls:
            discovery_call_key = _memory_discovery_call_key(tc)
            if discovery_call_key is not None:
                completed_memory_discovery_tools.add(tc.name)
                completed_memory_discovery_calls.add(discovery_call_key)
        for tc in dispatch_calls:
            if tc.name != "workspace_read":
                continue
            result = ordered_results_by_id[tc.id]
            if str(result.content).strip().lower().startswith("error"):
                continue
            metadata = result.metadata or {}
            path = metadata.get("workspace_read_path")
            revision = metadata.get("workspace_revision")
            if not isinstance(path, str) or type(revision) is not int:
                continue
            suffix = _file_suffix_for_requirement(
                path, normalized_required_suffixes
            )
            matches_required_delivery = file_delivery_required and (
                not normalized_required_suffixes
                or suffix in normalized_required_suffixes
            )
            if matches_required_delivery and revision > 0:
                workspace_delivery_candidate = (path, revision)
        for tc in dispatch_calls:
            if tc.name != "workspace_write":
                continue
            result = ordered_results_by_id[tc.id]
            if not str(result.content).strip().lower().startswith("ok"):
                continue
            path = str(tc.args.get("path") or "").strip()
            suffix = _file_suffix_for_requirement(path, normalized_required_suffixes)
            model_chose_shared_work = path.casefold().endswith(".io.html")
            matches_required_delivery = file_delivery_required and (
                not normalized_required_suffixes
                or suffix in normalized_required_suffixes
            )
            if not model_chose_shared_work and not matches_required_delivery:
                continue
            workspace_write_applied = True
            metadata = result.metadata or {}
            revision = metadata.get("workspace_revision")
            if type(revision) is not int or revision <= 0:
                match = _WORKSPACE_REVISION_RE.search(str(result.content))
                revision = int(match.group(1)) if match else None
            if type(revision) is int and revision > 0:
                workspace_delivery_target = (path, revision)
                workspace_delivery_candidate = None
                existing_file_delivery_choice_required = False
                compact_delivery_args_retry_used = False
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
            private_read_seen = True

        transcript.append(
            ToolExchange(
                calls=tuple(pr.tool_calls),
                results=tuple(ordered_results),
                assistant_text=pr.text,
                assistant_turn=pr.assistant_turn,
            )
        )
        if pr.tool_calls and not pr.text.strip() and not pr.media:
            consecutive_tool_only_rounds += 1
        else:
            consecutive_tool_only_rounds = 0
        if (
            consecutive_tool_only_rounds
            >= max_consecutive_tool_only_rounds
            and attempts < max_calls
        ):
            # This is intentionally independent of ``max_calls``. The latter is
            # the absolute provider-call ceiling; this configurable threshold
            # detects a model that is making only tool calls without producing
            # any visible text, and reserves one fresh round for a complete
            # answer from the observations already collected.
            force_text_fallback = True
            force_text_fallback_reason = "tool_only_stall"
    # Only reachable for max_calls == 0, or when a malformed response consumed the
    # last reserved attempt before a fallback could be made.
    await _trajectory(
        "loop_exhausted",
        {"rounds": attempts, "tool_calls_used": tool_calls_used},
    )
    return LoopOutcome("", attempts, "budget_exhausted", replied_intermediate)
