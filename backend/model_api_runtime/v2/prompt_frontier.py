"""Pure total-prompt budget primitives for Hosted Runtime V2.

This module deliberately knows nothing about DB rows, encrypted envelopes,
workers, providers, or the tool loop.  It answers two small questions:

1. Which conservative context-window limit applies to a provider/model route?
2. Does a deterministic set of prompt components fit after reserving output
   and safety headroom?

The estimator uses canonical UTF-8 JSON bytes as conservative *admission
units*.  They are not billing-token estimates. Natural-language prompt content
defaults to one byte per token, intentionally overestimating multilingual text.
ASCII-heavy tool schemas use a separate conservative three-byte ratio instead
of inheriting that text estimate; applying one byte per token to schemas can
inflate their cost by roughly four times and silently erase the whole tool
surface. Non-ASCII schema bytes still count one-for-one, so multilingual tool
descriptions cannot become underestimates through the ASCII calibration. Image
payload bytes are replaced by a separately configurable per-image reserve so
base64 transport encoding does not masquerade as text tokens. Deployments may
calibrate the natural-language byte ratio or provide route context metadata,
but must not weaken the all-required-or-error contract implemented here.

Conversation text never enters :class:`PromptFrontierPlan`; components retain
only a name and a bounded integer estimate.  This makes the result safe to
persist as metrics without accidentally copying prompt content into plaintext
runtime state or logs.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit


DEFAULT_OUTPUT_RESERVE_TOKENS = 4_096
DEFAULT_SAFETY_MARGIN_RATIO = 0.05
MIN_SAFETY_MARGIN_TOKENS = 512
MIN_CONTEXT_WINDOW_TOKENS = 2_048
MAX_CONTEXT_WINDOW_TOKENS = 2_000_000
DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN = 1.0
# Tool/function schemas are overwhelmingly ASCII JSON. Keep this calibration
# inside the frontier so every caller accounts for the same provider-visible
# structure; the deployment knob above remains scoped to natural-language and
# transcript content, where increasing it would undercount CJK text.
TOOL_SCHEMA_UTF8_BYTES_PER_TOKEN = 3.0
DEFAULT_IMAGE_RESERVE_TOKENS = 8_192

# Provider message framing is not present in a content string itself.  The
# canonical JSON estimate already counts roles/keys/delimiters; this small
# fixed allowance covers wire-specific framing that adapters add around it.
MESSAGE_STRUCTURAL_OVERHEAD_TOKENS = 8
TOOL_SCHEMA_STRUCTURAL_OVERHEAD_TOKENS = 64
TOOL_EXCHANGE_STRUCTURAL_OVERHEAD_TOKENS = 12

LimitSource = Literal[
    "deployment_override",
    "provider_metadata",
    "audited_family",
    "unaudited_default",
]
PlanStatus = Literal["fits", "fits_optional_omitted"]

# When the complete catalog does not fit, retain the user-selected MCP surface
# and the smallest useful memory/reply loop before admitting less essential
# platform capabilities. The priority decision belongs here, beside the budget
# that applies it, rather than being copied into each worker/tool-loop caller.
_MCP_TOOL_PREFIX = "mcp__"
_CORE_TOOL_FLOOR_NAMES = frozenset(
    {
        "memory_index",
        "memory_search",
        "memory_fetch",
        "memory_write",
        "memory_organize",
        "reply",
        "send_file",
    }
)


@dataclass(frozen=True)
class ModelPromptLimit:
    """Conservative context-window limit and why it was selected."""

    provider: str
    model: str
    context_window_tokens: int
    source: LimitSource
    family: str | None = None
    override_key: str | None = None


@dataclass(frozen=True)
class PromptBudget:
    """One model context window split into explicit, non-overlapping parts."""

    context_window_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    input_budget_tokens: int


@dataclass(frozen=True)
class PromptComponent:
    """A content-free accounting record for one atomic prompt component.

    ``required`` components are never omitted.  ``priority`` only orders
    optional admission: larger values are considered first, with original
    caller order as the deterministic tie-breaker.
    """

    name: str
    estimated_tokens: int
    required: bool = True
    priority: int = 0

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("prompt component name is required")
        if isinstance(self.estimated_tokens, bool):
            raise ValueError("prompt component estimate must be a non-negative integer")
        try:
            estimate = int(self.estimated_tokens)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "prompt component estimate must be a non-negative integer"
            ) from exc
        if estimate != self.estimated_tokens or estimate < 0:
            raise ValueError("prompt component estimate must be a non-negative integer")
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "estimated_tokens", estimate)
        object.__setattr__(self, "priority", int(self.priority))


@dataclass(frozen=True)
class ComponentDecision:
    name: str
    estimated_tokens: int
    required: bool
    priority: int
    included: bool
    reason: Literal["required", "optional_fit", "optional_over_budget"]


@dataclass(frozen=True)
class PromptFrontierPlan:
    """Successful prompt accounting result; it is impossible to be over budget."""

    model_limit: ModelPromptLimit
    budget: PromptBudget
    decisions: tuple[ComponentDecision, ...]
    estimated_input_tokens: int
    remaining_input_tokens: int
    status: PlanStatus
    offered_tool_names: tuple[str, ...] = ()
    included_tool_names: tuple[str, ...] = ()

    @property
    def included_components(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.decisions if item.included)

    @property
    def omitted_optional_components(self) -> tuple[str, ...]:
        return tuple(
            item.name
            for item in self.decisions
            if not item.required and not item.included
        )

    @property
    def reserved_total_tokens(self) -> int:
        """Input estimate plus output and safety reservations."""

        return (
            self.estimated_input_tokens
            + self.budget.output_reserve_tokens
            + self.budget.safety_margin_tokens
        )


class PromptFrontierExhausted(RuntimeError):
    """Required prompt components exceed the model-aware input budget.

    The exception intentionally contains counts and component names only; it
    never stores or renders component content.
    """

    code = "prompt_frontier_exhausted"

    def __init__(
        self,
        *,
        required_tokens: int,
        input_budget_tokens: int,
        context_window_tokens: int,
        required_components: Sequence[str],
        limit_source: LimitSource,
    ) -> None:
        self.required_tokens = int(required_tokens)
        self.input_budget_tokens = int(input_budget_tokens)
        self.context_window_tokens = int(context_window_tokens)
        self.required_components = tuple(str(name) for name in required_components)
        self.limit_source = limit_source
        super().__init__(
            f"{self.code}: required_tokens={self.required_tokens} "
            f"input_budget_tokens={self.input_budget_tokens} "
            f"context_window_tokens={self.context_window_tokens}"
        )


class PromptContextLimitUnconfigured(RuntimeError):
    """An unaudited route has no trustworthy context-window limit.

    No numeric fallback can be conservative for an unknown provider/model:
    assuming 32K, for example, over-admits prompts for 4K/8K/16K routes.  The
    rendered error is deliberately stable and content-free because it can flow
    through the hosted worker's terminal error path.  Normalized route fields
    remain available to an in-process caller that needs structured diagnostics.
    """

    code = "prompt_context_limit_unconfigured"

    def __init__(self, *, provider: str, model: str) -> None:
        self.provider = str(provider)
        self.model = str(model)
        super().__init__(self.code)


@dataclass(frozen=True)
class _AuditedFamily:
    provider: str
    name: str
    model_prefixes: tuple[str, ...]
    lower_bound_tokens: int


# These are deliberately lower bounds, not advertised maxima.  Restrict the
# direct-provider rules to current large-context families and audited default
# destinations. Unknown/custom relays require provider metadata or deployment
# configuration; inventing a numeric default would not be fail-closed.
_AUDITED_FAMILIES: tuple[_AuditedFamily, ...] = (
    _AuditedFamily(
        "openai",
        "openai_modern",
        ("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4"),
        128_000,
    ),
    _AuditedFamily(
        "anthropic",
        "anthropic_modern",
        (
            "claude-3-",
            "claude-sonnet-4",
            "claude-opus-4",
            "claude-haiku-4",
            "claude-4",
        ),
        128_000,
    ),
    _AuditedFamily(
        "bedrock",
        "bedrock_anthropic_modern",
        (
            "anthropic.claude-3-",
            "anthropic.claude-sonnet-4",
            "anthropic.claude-opus-4",
            "anthropic.claude-haiku-4",
            "us.anthropic.claude-3-",
            "us.anthropic.claude-sonnet-4",
            "us.anthropic.claude-opus-4",
            "us.anthropic.claude-haiku-4",
            "eu.anthropic.claude-3-",
            "eu.anthropic.claude-sonnet-4",
            "eu.anthropic.claude-opus-4",
            "eu.anthropic.claude-haiku-4",
        ),
        128_000,
    ),
    _AuditedFamily(
        "gemini",
        "gemini_modern",
        ("gemini-1.5", "gemini-2", "gemini-3"),
        128_000,
    ),
    _AuditedFamily(
        "deepseek",
        "deepseek_modern",
        ("deepseek-chat", "deepseek-reasoner", "deepseek-v4"),
        64_000,
    ),
    _AuditedFamily(
        "openrouter",
        "openrouter_modern",
        (
            "openai/gpt-4o",
            "openai/gpt-4.1",
            "openai/gpt-5",
            "openai/o1",
            "openai/o3",
            "openai/o4",
            "anthropic/claude-3-",
            "anthropic/claude-sonnet-4",
            "anthropic/claude-opus-4",
            "anthropic/claude-haiku-4",
            "anthropic/claude-4",
            "google/gemini-1.5",
            "google/gemini-2",
            "google/gemini-3",
        ),
        128_000,
    ),
    _AuditedFamily(
        "openrouter",
        "openrouter_deepseek",
        (
            "deepseek/deepseek-chat",
            "deepseek/deepseek-reasoner",
            "deepseek/deepseek-v4",
        ),
        64_000,
    ),
)

_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "bedrock": "https://bedrock-runtime.us-east-1.amazonaws.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com",
}


def normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower().replace("-", "_")
    aliases = {
        "amazon_bedrock": "bedrock",
        "aws_bedrock": "bedrock",
        "bedrock": "bedrock",
        "claude": "anthropic",
        "google": "gemini",
        "google_gemini": "gemini",
        "open_ai": "openai",
        "open_router": "openrouter",
        "deep_seek": "deepseek",
        "compatible": "openai_compatible",
        "custom": "openai_compatible",
        "custom_endpoint": "openai_compatible",
    }
    return aliases.get(value, value)


def _validated_context_window(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer context window")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be an integer context window") from exc
    if (
        parsed != value
        or not MIN_CONTEXT_WINDOW_TOKENS <= parsed <= MAX_CONTEXT_WINDOW_TOKENS
    ):
        raise ValueError(
            f"{label} must be between {MIN_CONTEXT_WINDOW_TOKENS} "
            f"and {MAX_CONTEXT_WINDOW_TOKENS}"
        )
    return parsed


def _normalize_override_key(key: str) -> str:
    raw = str(key or "").strip()
    if ":" not in raw:
        raise ValueError("context-window override keys must be 'provider:model'")
    provider, model = raw.split(":", 1)
    provider = provider.strip()
    model = model.strip().lower()
    if not provider or not model:
        raise ValueError("context-window override keys must be 'provider:model'")
    normalized_provider = "*" if provider == "*" else normalize_provider(provider)
    return f"{normalized_provider}:{model}"


def parse_deployment_overrides(raw: str | None) -> dict[str, int]:
    """Parse a deployment JSON object of ``provider:model -> context window``.

    Invalid deployment configuration raises instead of silently falling back to
    a larger or smaller limit.  Supported wildcard keys are ``provider:*``,
    ``*:model``, and ``*:*``.
    """

    if raw is None or not str(raw).strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("context-window overrides must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("context-window overrides must be a JSON object")
    parsed: dict[str, int] = {}
    for key, limit in value.items():
        normalized_key = _normalize_override_key(str(key))
        if normalized_key in parsed:
            raise ValueError(
                f"duplicate normalized context-window override: {normalized_key}"
            )
        parsed[normalized_key] = _validated_context_window(
            limit, label=f"context-window override {normalized_key}"
        )
    return parsed


def _canonical_base_url(value: str) -> tuple[str, str, int | None, str] | None:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.hostname:
        return None
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    return scheme, host, port, parsed.path.rstrip("/")


def _is_audited_destination(provider: str, base_url: str) -> bool:
    default = _DEFAULT_BASE_URLS.get(provider)
    if default is None:
        return False
    if not str(base_url or "").strip():
        return True
    return _canonical_base_url(base_url) == _canonical_base_url(default)


def _normalized_overrides(overrides: Mapping[str, Any] | None) -> dict[str, int]:
    if not overrides:
        return {}
    normalized: dict[str, int] = {}
    for raw_key, raw_limit in overrides.items():
        key = _normalize_override_key(str(raw_key))
        if key in normalized:
            raise ValueError(f"duplicate normalized context-window override: {key}")
        normalized[key] = _validated_context_window(
            raw_limit, label=f"context-window override {key}"
        )
    return normalized


_UNAUDITED_DEFAULT_ENV = "FEEDLING_V2_UNAUDITED_DEFAULT_CONTEXT_WINDOW_TOKENS"
# A smaller fallback caused custom-relay users with non-trivial persona/world-book
# prompts to hit prompt_frontier_exhausted before any provider call, even with a
# tiny chat history (observed in production). 65536 leaves 58,163 input tokens
# after the default 4,096 output reserve and ceil(5%)=3,277 safety margin.
#
# Raising an unaudited fallback can over-promise a model's real window. The
# current-population check found that all 17 openai-compatible routes among 32
# active configured users name recognizable Claude/Gemini/Minimax/GLM/GPT
# families whose real windows are at least 128K. That evidence supports 64K for
# today's population; re-evaluate this fallback if the route population changes.
# Explicit deployment knowledge still belongs in the env override, and zero
# retains strict fail-closed mode.
_UNAUDITED_DEFAULT_FALLBACK_TOKENS = 65536


def unaudited_default_context_window() -> int:
    """Conservative context window applied to an unaudited route (custom relay /
    unknown model) when nothing more specific is configured.

    A deployment may tune it via ``FEEDLING_V2_UNAUDITED_DEFAULT_CONTEXT_WINDOW_TOKENS``;
    setting it to ``0`` restores the original fail-closed behaviour (reject the
    route until an explicit ``context_window_tokens`` is supplied). This is the
    LOWEST-precedence source in ``resolve_model_limit`` — a deployment override, a
    caller-supplied value, and an audited family all win over it — so enabling it
    never overrides a value someone stated explicitly; it only replaces the hard
    rejection with a safe lower bound so custom relays are usable out of the box."""
    raw = os.environ.get(_UNAUDITED_DEFAULT_ENV)
    if raw is None or not str(raw).strip():
        return _UNAUDITED_DEFAULT_FALLBACK_TOKENS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{_UNAUDITED_DEFAULT_ENV} must be a non-negative integer"
        ) from exc
    if value < 0:
        raise ValueError(f"{_UNAUDITED_DEFAULT_ENV} must be a non-negative integer")
    return value  # 0 disables the default → fail-closed rejection


def resolve_model_limit(
    provider: str,
    model: str,
    *,
    base_url: str = "",
    provider_context_window_tokens: Any | None = None,
    deployment_overrides: Mapping[str, Any] | None = None,
) -> ModelPromptLimit:
    """Resolve deployment override > provider metadata > audited family.

    Override lookup is deterministic and most-specific first:
    ``provider:model``, ``provider:*``, ``*:model``, then ``*:*``.
    Deployment overrides intentionally apply even to custom destinations; a
    custom route without one never inherits a first-party model assumption and
    fails before any provider request. Because override keys do not encode a
    destination, an override must be a safe lower bound for every route it
    matches.
    """

    normalized_provider = normalize_provider(provider)
    normalized_model = str(model or "").strip().lower()
    overrides = _normalized_overrides(deployment_overrides)
    candidates = (
        f"{normalized_provider}:{normalized_model}",
        f"{normalized_provider}:*",
        f"*:{normalized_model}",
        "*:*",
    )
    for key in candidates:
        if key in overrides:
            return ModelPromptLimit(
                provider=normalized_provider,
                model=normalized_model,
                context_window_tokens=overrides[key],
                source="deployment_override",
                override_key=key,
            )

    if provider_context_window_tokens is not None:
        return ModelPromptLimit(
            provider=normalized_provider,
            model=normalized_model,
            context_window_tokens=_validated_context_window(
                provider_context_window_tokens,
                label="provider context-window metadata",
            ),
            source="provider_metadata",
        )

    if _is_audited_destination(normalized_provider, base_url):
        for family in _AUDITED_FAMILIES:
            if family.provider != normalized_provider:
                continue
            if any(
                normalized_model.startswith(prefix) for prefix in family.model_prefixes
            ):
                return ModelPromptLimit(
                    provider=normalized_provider,
                    model=normalized_model,
                    context_window_tokens=family.lower_bound_tokens,
                    source="audited_family",
                    family=family.name,
                )

    # Lowest-precedence fallback: an unaudited route (custom relay / unknown
    # model) with nothing more specific configured gets a conservative default
    # instead of a hard rejection, so custom relays work without every client
    # having to send context_window_tokens. Deployment override, caller-supplied
    # value, and audited family were all tried above and win over this. A
    # deployment can set the default to 0 to restore strict fail-closed.
    default_tokens = unaudited_default_context_window()
    if default_tokens > 0:
        return ModelPromptLimit(
            provider=normalized_provider,
            model=normalized_model,
            context_window_tokens=_validated_context_window(
                default_tokens, label="unaudited default context window"
            ),
            source="unaudited_default",
        )

    raise PromptContextLimitUnconfigured(
        provider=normalized_provider,
        model=normalized_model,
    )


def resolve_model_limit_from_config(
    provider_config: Any,
    *,
    deployment_overrides: Mapping[str, Any] | None = None,
) -> ModelPromptLimit:
    """Resolve a limit from the dependency-neutral ProviderConfig protocol.

    Provider adapters may attach audited ``context_window_tokens`` metadata. The
    current hosted BYOK config does not require it, so operator overrides and
    audited family floors remain fallbacks. An unaudited route with neither is
    rejected before any provider request; there is no safe numeric default.
    """

    return resolve_model_limit(
        str(getattr(provider_config, "provider", "") or ""),
        str(getattr(provider_config, "model", "") or ""),
        base_url=str(getattr(provider_config, "base_url", "") or ""),
        provider_context_window_tokens=getattr(
            provider_config, "context_window_tokens", None
        ),
        deployment_overrides=deployment_overrides,
    )


def _validated_estimator_ratio(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("UTF-8 bytes-per-token estimate must be positive and finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "UTF-8 bytes-per-token estimate must be positive and finite"
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("UTF-8 bytes-per-token estimate must be positive and finite")
    return parsed


def estimate_utf8_tokens(
    text: str,
    *,
    utf8_bytes_per_token: float = DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
) -> int:
    """Estimate text tokens from UTF-8 bytes, rounding up.

    The default one-byte-per-token ratio deliberately overestimates ordinary
    provider tokenizers. Deployments may replace it only after measuring their
    selected route; the prompt frontier still applies a separate safety margin.
    """

    ratio = _validated_estimator_ratio(utf8_bytes_per_token)
    return int(math.ceil(len(str(text or "").encode("utf-8")) / ratio))


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"unsupported prompt structure: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Stable JSON rendering used only for size accounting."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def estimate_structured_tokens(
    value: Any,
    *,
    structural_overhead_tokens: int = 0,
    utf8_bytes_per_token: float = DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
) -> int:
    """Count canonical JSON UTF-8 bytes plus explicit wire framing overhead."""

    if isinstance(structural_overhead_tokens, bool):
        raise ValueError("structural overhead must be a non-negative integer")
    overhead = int(structural_overhead_tokens)
    if overhead < 0 or overhead != structural_overhead_tokens:
        raise ValueError("structural overhead must be a non-negative integer")
    return (
        estimate_utf8_tokens(
            canonical_json(value), utf8_bytes_per_token=utf8_bytes_per_token
        )
        + overhead
    )


def text_component(
    name: str,
    text: str,
    *,
    required: bool = True,
    priority: int = 0,
    utf8_bytes_per_token: float = DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
) -> PromptComponent:
    return PromptComponent(
        name=name,
        estimated_tokens=estimate_utf8_tokens(
            text, utf8_bytes_per_token=utf8_bytes_per_token
        ),
        required=required,
        priority=priority,
    )


def structured_component(
    name: str,
    value: Any,
    *,
    required: bool = True,
    priority: int = 0,
    structural_overhead_tokens: int = 0,
    utf8_bytes_per_token: float = DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
) -> PromptComponent:
    return PromptComponent(
        name=name,
        estimated_tokens=estimate_structured_tokens(
            value,
            structural_overhead_tokens=structural_overhead_tokens,
            utf8_bytes_per_token=utf8_bytes_per_token,
        ),
        required=required,
        priority=priority,
    )


def _image_accounting_value(value: Any) -> tuple[Any, int]:
    """Replace image payload bytes with a fixed, separately budgeted marker."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _image_accounting_value(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        image_count = 0
        sanitized: dict[Any, Any] = {}
        is_image_block = str(value.get("type") or "") == "image_url"
        for key, child in value.items():
            if is_image_block and key == "image_url":
                sanitized_child = {"url": "[image-payload]"}
                if isinstance(child, Mapping) and child.get("detail") is not None:
                    sanitized_child["detail"] = child.get("detail")
                sanitized[key] = sanitized_child
                image_count += 1
                continue
            rendered, nested_count = _image_accounting_value(child)
            sanitized[key] = rendered
            image_count += nested_count
        return sanitized, image_count
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rendered_items: list[Any] = []
        image_count = 0
        for child in value:
            rendered, nested_count = _image_accounting_value(child)
            rendered_items.append(rendered)
            image_count += nested_count
        return rendered_items, image_count
    return value, 0


def messages_component(
    messages: Sequence[Any],
    *,
    name: str = "messages",
    required: bool = True,
    priority: int = 0,
    utf8_bytes_per_token: float = DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
    image_reserve_tokens: int = DEFAULT_IMAGE_RESERVE_TOKENS,
) -> PromptComponent:
    image_reserve = int(image_reserve_tokens)
    if (
        isinstance(image_reserve_tokens, bool)
        or image_reserve != image_reserve_tokens
        or image_reserve <= 0
    ):
        raise ValueError("image reserve must be a positive integer")
    accounted_messages, image_count = _image_accounting_value(list(messages))
    overhead = len(messages) * MESSAGE_STRUCTURAL_OVERHEAD_TOKENS
    component = structured_component(
        name,
        accounted_messages,
        required=required,
        priority=priority,
        structural_overhead_tokens=overhead,
        utf8_bytes_per_token=utf8_bytes_per_token,
    )
    return PromptComponent(
        name=component.name,
        estimated_tokens=(component.estimated_tokens + image_count * image_reserve),
        required=component.required,
        priority=component.priority,
    )


def tool_schemas_component(
    tools: Sequence[Any],
    *,
    name: str = "tool_schemas",
    required: bool = True,
    priority: int = 0,
) -> PromptComponent:
    """Account for the complete offered tool catalog as one atomic component."""

    overhead = len(tools) * TOOL_SCHEMA_STRUCTURAL_OVERHEAD_TOKENS
    rendered = canonical_json(list(tools)).encode("utf-8")
    ascii_bytes = sum(byte < 0x80 for byte in rendered)
    non_ascii_bytes = len(rendered) - ascii_bytes
    # JSON syntax, field names, and most schemas are ASCII-heavy and use the
    # calibrated ratio. Server-authored descriptions/enums may be CJK or any
    # other UTF-8 text; count every non-ASCII byte as a full admission token so
    # the ASCII calibration can never turn multilingual schemas into an
    # under-estimate (for example, one Chinese character contributes 3 bytes).
    estimated_tokens = int(
        math.ceil(ascii_bytes / TOOL_SCHEMA_UTF8_BYTES_PER_TOKEN)
    ) + non_ascii_bytes + overhead
    return PromptComponent(
        required=required,
        name=name,
        priority=priority,
        estimated_tokens=estimated_tokens,
    )


def tool_transcript_component(
    exchanges: Sequence[Any],
    *,
    required: bool = True,
    priority: int = 0,
    utf8_bytes_per_token: float = DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
) -> PromptComponent:
    """Account for the entire native transcript as one indivisible component.

    Provider protocols require assistant tool calls and every call-id-matched
    result to be replayed together.  Returning one component rather than one per
    exchange prevents the planner from admitting a prefix and silently dropping
    the rest.
    """

    overhead = len(exchanges) * TOOL_EXCHANGE_STRUCTURAL_OVERHEAD_TOKENS
    return structured_component(
        "tool_transcript",
        list(exchanges),
        required=required,
        priority=priority,
        structural_overhead_tokens=overhead,
        utf8_bytes_per_token=utf8_bytes_per_token,
    )


def build_prompt_budget(
    context_window_tokens: int,
    *,
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    safety_margin_tokens: int | None = None,
) -> PromptBudget:
    context = _validated_context_window(context_window_tokens, label="context window")
    if isinstance(output_reserve_tokens, bool):
        raise ValueError("output reserve must be a positive integer")
    output = int(output_reserve_tokens)
    if output <= 0 or output != output_reserve_tokens:
        raise ValueError("output reserve must be a positive integer")

    if safety_margin_tokens is None:
        safety = max(
            MIN_SAFETY_MARGIN_TOKENS,
            int(math.ceil(context * DEFAULT_SAFETY_MARGIN_RATIO)),
        )
    else:
        if isinstance(safety_margin_tokens, bool):
            raise ValueError("safety margin must be a non-negative integer")
        safety = int(safety_margin_tokens)
        if safety < 0 or safety != safety_margin_tokens:
            raise ValueError("safety margin must be a non-negative integer")

    input_budget = context - output - safety
    if input_budget < 0:
        raise ValueError("output reserve plus safety margin exceeds context window")
    return PromptBudget(
        context_window_tokens=context,
        output_reserve_tokens=output,
        safety_margin_tokens=safety,
        input_budget_tokens=input_budget,
    )


def plan_prompt(
    *,
    model_limit: ModelPromptLimit,
    components: Sequence[PromptComponent],
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    safety_margin_tokens: int | None = None,
) -> PromptFrontierPlan:
    """Build a deterministic all-required-or-error prompt budget plan.

    Required components are admitted as a single set.  If that set does not
    fit, :class:`PromptFrontierExhausted` is raised before any provider call.
    Optional components are atomic and considered by descending priority; an
    omitted component is recorded explicitly in the returned decisions.
    """

    budget = build_prompt_budget(
        model_limit.context_window_tokens,
        output_reserve_tokens=output_reserve_tokens,
        safety_margin_tokens=safety_margin_tokens,
    )
    ordered = tuple(components)
    names = [component.name for component in ordered]
    if len(set(names)) != len(names):
        raise ValueError("prompt component names must be unique")

    required = tuple(component for component in ordered if component.required)
    required_tokens = sum(component.estimated_tokens for component in required)
    if required_tokens > budget.input_budget_tokens:
        raise PromptFrontierExhausted(
            required_tokens=required_tokens,
            input_budget_tokens=budget.input_budget_tokens,
            context_window_tokens=budget.context_window_tokens,
            required_components=[component.name for component in required],
            limit_source=model_limit.source,
        )

    optional_by_admission_order = sorted(
        (
            (index, component)
            for index, component in enumerate(ordered)
            if not component.required
        ),
        key=lambda pair: (-pair[1].priority, pair[0]),
    )
    included_optional_indexes: set[int] = set()
    used = required_tokens
    for index, component in optional_by_admission_order:
        if used + component.estimated_tokens <= budget.input_budget_tokens:
            included_optional_indexes.add(index)
            used += component.estimated_tokens

    decisions: list[ComponentDecision] = []
    for index, component in enumerate(ordered):
        if component.required:
            included = True
            reason: Literal["required", "optional_fit", "optional_over_budget"] = (
                "required"
            )
        elif index in included_optional_indexes:
            included = True
            reason = "optional_fit"
        else:
            included = False
            reason = "optional_over_budget"
        decisions.append(
            ComponentDecision(
                name=component.name,
                estimated_tokens=component.estimated_tokens,
                required=component.required,
                priority=component.priority,
                included=included,
                reason=reason,
            )
        )

    remaining = budget.input_budget_tokens - used
    # Defence in depth: no successful object may describe an overflow, even if
    # this function is refactored later.
    if remaining < 0:  # pragma: no cover - construction above proves this
        raise AssertionError("prompt frontier produced an over-budget plan")
    omitted = any(not item.included for item in decisions)
    return PromptFrontierPlan(
        model_limit=model_limit,
        budget=budget,
        decisions=tuple(decisions),
        estimated_input_tokens=used,
        remaining_input_tokens=remaining,
        status="fits_optional_omitted" if omitted else "fits",
    )


def plan_provider_round(
    *,
    model_limit: ModelPromptLimit,
    messages: Sequence[Any],
    tools: Sequence[Any] | None,
    required_tool_names: Collection[str] = (),
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    safety_margin_tokens: int | None = None,
    utf8_bytes_per_token: float = DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
    image_reserve_tokens: int = DEFAULT_IMAGE_RESERVE_TOKENS,
) -> PromptFrontierPlan:
    """Budget the exact messages and exact offered schemas for one provider call.

    Native exchanges are separated only for accounting/diagnostics; the caller
    still sends its original chronological ``messages`` list unchanged. Tool
    schemas are normally optional because the unified loop can safely degrade to
    one text-only terminal request. Schemas named in ``required_tool_names`` stay
    required when their native call/result history is retained; the remaining
    catalog may still be omitted atomically. Conversation messages and native
    call/result pairs are required and are never silently truncated.
    """

    ordinary_messages = [
        message for message in messages if not _is_tool_exchange(message)
    ]
    exchanges = [message for message in messages if _is_tool_exchange(message)]
    components = [
        messages_component(
            ordinary_messages,
            name="message_context",
            utf8_bytes_per_token=utf8_bytes_per_token,
            image_reserve_tokens=image_reserve_tokens,
        )
    ]
    if exchanges:
        components.append(
            tool_transcript_component(
                exchanges,
                utf8_bytes_per_token=utf8_bytes_per_token,
            )
        )
    offered_tools = list(tools or [])
    required_tools: list[Any] = []
    optional_tools: list[Any] = []
    if tools is not None:
        required_names = {str(name) for name in required_tool_names if str(name)}
        required_tools = [
            tool
            for tool in tools
            if str(getattr(tool, "name", "")) in required_names
        ]
        optional_tools = [
            tool
            for tool in tools
            if str(getattr(tool, "name", "")) not in required_names
        ]
        if required_tools:
            components.append(
                tool_schemas_component(
                    required_tools,
                    name="required_tool_schemas",
                    required=True,
                )
            )
        if optional_tools:
            components.append(
                tool_schemas_component(
                    optional_tools,
                    required=False,
                    priority=1,
                )
            )
    plan = plan_prompt(
        model_limit=model_limit,
        components=components,
        output_reserve_tokens=output_reserve_tokens,
        safety_margin_tokens=safety_margin_tokens,
    )
    offered_names = tuple(str(getattr(tool, "name", "")) for tool in offered_tools)
    required_name_set = {
        str(getattr(tool, "name", "")) for tool in required_tools
    }
    if not optional_tools or "tool_schemas" not in plan.omitted_optional_components:
        return dataclasses.replace(
            plan,
            offered_tool_names=offered_names,
            included_tool_names=offered_names,
        )

    # The complete optional catalog is too large. Re-plan the same exact prompt
    # with one atomic component per remaining schema so pressure trims a
    # deterministic tail instead of turning every tool off. User MCP and the
    # core memory/reply loop are admitted first; schemas referenced by retained
    # native history remain required exactly as above.
    granular_components = [
        component for component in components if component.name != "tool_schemas"
    ]
    optional_component_names: list[tuple[str, str]] = []
    for index, tool in enumerate(optional_tools):
        tool_name = str(getattr(tool, "name", ""))
        component_name = f"tool_schema_{index}"
        priority = (
            2
            if tool_name.startswith(_MCP_TOOL_PREFIX)
            or tool_name in _CORE_TOOL_FLOOR_NAMES
            else 1
        )
        granular_components.append(
            tool_schemas_component(
                [tool],
                name=component_name,
                required=False,
                priority=priority,
            )
        )
        optional_component_names.append((component_name, tool_name))
    plan = plan_prompt(
        model_limit=model_limit,
        components=granular_components,
        output_reserve_tokens=output_reserve_tokens,
        safety_margin_tokens=safety_margin_tokens,
    )
    included_components = set(plan.included_components)
    included_optional_names = {
        tool_name
        for component_name, tool_name in optional_component_names
        if component_name in included_components
    }
    included_names = tuple(
        name
        for name in offered_names
        if name in required_name_set or name in included_optional_names
    )
    return dataclasses.replace(
        plan,
        offered_tool_names=offered_names,
        included_tool_names=included_names,
    )


def _is_tool_exchange(value: Any) -> bool:
    """Structural check avoids importing provider_types into this pure module."""

    return (
        dataclasses.is_dataclass(value)
        and value.__class__.__name__ == "ToolExchange"
        and hasattr(value, "calls")
        and hasattr(value, "results")
    )
