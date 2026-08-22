"""Single producer-owned registry for public runtime ``error_class`` values.

The registry owns codes, blame, user-safe text and ordered text matchers.  Every
consumer imports derived views from here; none maintains a second list.  The
load export also says when a source could not be measured, so an observation
consumer can distinguish a complete empty value from an unavailable source.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Callable, Iterable, Mapping


REGISTRY_STATUS_VALUES = frozenset({"ok", "partial", "unavailable"})
REGISTRY_SOURCE_NAMES = frozenset({
    "chat",
    "platform",
    "workflow",
    "resident",
    "vision",
    "image_generation",
})
UNREGISTERED_ERROR_CLASS = "error_class_unregistered"
REJECTION_BOUNDARY_DOMAINS: Mapping[str, str] = MappingProxyType({
    "hosted_vision_observer": "vision",
    "resident_image_generation_response": "image_generation",
    "resident_vision_response": "vision",
    "v2_dedicated_vision": "vision",
    "v2_image_generation": "image_generation",
})
REJECTION_FALLBACK_CODES = frozenset({UNREGISTERED_ERROR_CLASS})


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    code: str
    domain: str
    family: str
    blame: str
    safe_text_zh: str
    safe_text_en: str = ""
    matcher_pattern: str = ""
    public: bool = True
    activity_result: bool = False

    def text(self, language: str = "") -> str:
        if str(language or "").strip().lower().startswith("en"):
            return self.safe_text_en or self.safe_text_zh
        return self.safe_text_zh

    def matcher(self) -> re.Pattern[str] | None:
        return (
            re.compile(self.matcher_pattern, re.IGNORECASE)
            if self.matcher_pattern
            else None
        )


@dataclass(frozen=True, slots=True)
class RegistryExport:
    """Importable producer/consumer handoff.

    ``values=None`` is the only all-sources-unavailable shape.  A partial
    result retains the successfully loaded values and names every missing
    source; an empty set therefore never masquerades as a healthy measurement.
    """

    values: frozenset[str] | None
    status: str
    unavailable_sources: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in REGISTRY_STATUS_VALUES:
            raise ValueError(f"invalid registry status: {self.status}")
        missing = tuple(sorted(set(self.unavailable_sources)))
        if missing != self.unavailable_sources:
            raise ValueError("unavailable_sources must be unique and sorted")
        if not set(missing).issubset(REGISTRY_SOURCE_NAMES):
            raise ValueError("unknown registry source")
        if self.status == "ok" and (self.values is None or missing):
            raise ValueError("ok registry requires values and no missing sources")
        if self.status == "partial" and (not self.values or not missing):
            raise ValueError("partial registry requires values and missing sources")
        if self.status == "unavailable" and (self.values is not None or not missing):
            raise ValueError("unavailable registry requires values=None and sources")


def _spec(
    code: str,
    domain: str,
    family: str,
    blame: str,
    zh: str,
    *,
    en: str = "",
    matcher: str = "",
    public: bool = True,
    activity_result: bool = False,
) -> ErrorSpec:
    return ErrorSpec(
        code=code,
        domain=domain,
        family=family,
        blame=blame,
        safe_text_zh=zh,
        safe_text_en=en,
        matcher_pattern=matcher,
        public=public,
        activity_result=activity_result,
    )


def _chat_specs() -> tuple[ErrorSpec, ...]:
    return (
        _spec("model_mismatch", "chat", "provider", "system", "当前运行时没有成功加载所选模型，请重新选择模型或稍后重试。", matcher=r"\bmodel_mismatch\b"),
        _spec("quota_insufficient", "chat", "provider", "user_provider", "模型服务额度不足，充值后再发消息即可恢复。", matcher=r"余额|额度|insufficient_quota|credit balance|requires more credits|payment required|\b402\b|provider_http_402|quota"),
        _spec("auth_invalid", "chat", "provider", "user_provider", "API Key 无效或已过期，请到设置里重新保存。", matcher=r"invalid ?(x-)?api.?key|unauthorized|authentication|\b401\b|provider_http_40[13]"),
        _spec("model_not_found", "chat", "provider", "user_provider", "模型名不可用，请检查设置里的模型名。", matcher=r"invalid model name|model_not_found|no such model|unknown model|supported .{0,40}model names|model .{0,80}does not exist|not a valid model|model[ _]not[ _]found"),
        _spec("cli_config_invalid", "chat", "provider", "user_provider", "Agent 启动命令配置有误（缺少 {message} 占位符），消息传不到模型。请修正 AGENT_CLI_CMD。", matcher=r"missing the \{message\} placeholder"),
        _spec("vision_model_required", "vision", "vision_model", "user_provider", "由于当前模型没有视觉能力，模型无法收到图片信息，建议更改模型或在设置页单独添加视觉模型", en="Your current model can't process images, so it didn't receive this picture. Switch models, or add a dedicated vision model in Settings.", matcher=r"unknown variant `image_url`, expected `text`|no endpoints found that support image input"),
        _spec("provider_incompatible", "chat", "provider", "user_provider", "当前模型不支持这次请求用到的能力，换个模型或到设置里调整。", matcher=r"unknown variant|not supported|unsupported (parameter|tool)|invalid_request_error.*tool"),
        _spec("context_overflow", "chat", "provider", "user_provider", "这次对话太长超出了模型上限，可精简后再试。", matcher=r"context.{0,20}(length|window)|maximum context|too many tokens|prompt is too long"),
        _spec("content_filtered", "chat", "provider", "provider_transient", "这次回复被模型的内容策略拦下了，换个说法再试。", matcher=r"content_filter|content policy|safety|blocked by"),
        _spec("rate_limited", "chat", "provider", "provider_transient", "模型服务限流了，稍等几分钟再试。", matcher=r"\b429\b|provider_http_429|too many requests|rate.?limit"),
        _spec("upstream_unavailable", "chat", "provider", "provider_transient", "你的模型服务暂时不可用，稍后会自动恢复。", matcher=r"\b5\d{2}\b|provider_http_5\d{2}|overloaded|timed? ?out|connection (refused|reset|error)|unreachable|stream disconnected|ended without finish_reason"),
        _spec("turn_timeout", "chat", "provider", "system", "这轮回复超时了，稍后再试。"),
        _spec("provider_timeout", "chat", "provider", "provider_transient", "你配置的模型服务这次没有及时响应。请先检查模型渠道稳定性，不要连续重发。"),
        _spec("provider_empty_reply", "chat", "provider", "provider_transient", "你的模型服务这次返回了空回复，稍后再试；反复出现请检查模型渠道或中转的稳定性。"),
        _spec("reply_parse_failed", "chat", "provider", "system", "系统处理回复时出了问题，我们会尽快排查。"),
        _spec("unknown", "chat", "provider", "system", "连接模型服务时出了问题。"),
        _spec(UNREGISTERED_ERROR_CLASS, "chat", "contract", "system", "系统返回了未注册的错误分类，我们已记录并会尽快排查。", en="The runtime returned an unregistered error classification. We recorded it for investigation."),
    )


def _platform_specs() -> tuple[ErrorSpec, ...]:
    return (
        _spec("platform_queue_timeout", "platform", "platform", "system", "这条消息没有及时开始处理，也没有生成回复。请稍后再试，不要连续发送。"),
        _spec("platform_execution_timeout", "platform", "platform", "system", "这轮回复因系统执行异常没有完成，也不会重复生成回复。请稍后再试，不要连续发送。"),
    )


def _workflow_specs() -> tuple[ErrorSpec, ...]:
    return (
        _spec("genesis_failed", "workflow", "genesis", "system", "入住材料的文件解读没能完成，可稍后在记忆花园重试。"),
        _spec("genesis_partial", "workflow", "genesis", "system", "入住材料的文件解读完成了，但有部分记忆没能导入。"),
        _spec("import_failed", "workflow", "import", "system", "聊天记录导入失败了，请稍后重试。"),
        _spec("import_stale", "workflow", "import", "system", "聊天记录导入卡住已超时，请重新发起。"),
        _spec("memory_backoff", "workflow", "memory", "system", "记忆整理暂时受阻，正在自动重试。"),
        _spec("runner_spawn_failed", "workflow", "runner", "system", "你的 AI 助手进程启动失败，我们正在处理。"),
        _spec("runner_key_decrypt_failed", "workflow", "runner", "system", "你的 AI 助手暂时无法启动（密钥读取失败），我们正在处理。"),
        _spec("runner_degraded", "workflow", "runner", "system", "你的 AI 助手部分能力暂时受限，正在自动恢复。"),
    )


def _resident_specs() -> tuple[ErrorSpec, ...]:
    return (
        _spec("resident_consumer_stale", "resident", "resident", "user_environment", "你的 VPS resident consumer 版本可能太旧或没有正常接走任务，请更新并重启。"),
        _spec("resident_decrypt_source_unavailable", "resident", "resident", "user_environment", "你的 VPS resident 解密源不可用，真实加密消息暂时无法回复。"),
        _spec("resident_decrypt_health_unreported", "resident", "resident", "user_environment", "你的 VPS resident 端没有上报可验证的解密健康状态,通常是 consumer 版本太旧,请更新并重启。"),
        _spec("resident_never_claimed", "resident", "resident", "user_environment", "你的 VPS resident consumer 长时间没有接走入住/记忆蒸馏任务，请更新并重启。"),
    )


def _vision_specs() -> tuple[ErrorSpec, ...]:
    rows = (
        ("vision_model_auth_invalid", "user_provider", "视觉模型的 API Key 无效或已过期，请到设置里重新保存。", "The vision-model API key is invalid or expired. Save it again in Settings."),
        ("vision_model_quota_insufficient", "user_provider", "视觉模型服务额度不足，充值后再试。", "The vision-model service has insufficient quota. Add credit and try again."),
        ("vision_model_not_found", "user_provider", "当前视觉模型不可用，请到设置里更换模型。", "The vision model is unavailable. Choose another model in Settings."),
        ("vision_model_incompatible", "user_provider", "当前视觉模型无法读取这张图片，请到设置里更换模型。", "The vision model cannot read this image. Choose another model in Settings."),
        ("vision_model_rate_limited", "provider_transient", "视觉模型请求太多，请稍等几分钟再试。", "The vision-model service is rate limited. Try again in a few minutes."),
        ("vision_model_unavailable", "provider_transient", "视觉模型暂时无法连接，请稍后重试。", "The vision-model service is temporarily unavailable. Try again later."),
        ("vision_model_empty_response", "provider_transient", "视觉模型没有返回图片内容，请重试或更换模型。", "The vision model returned no image description. Try again or choose another model."),
        ("vision_model_not_ready", "user_provider", "视觉模型尚未准备好，请到设置里重新保存或更换模型。", "The vision model is not ready. Save it again or choose another model in Settings."),
        ("vision_model_failed", "provider_transient", "视觉模型处理失败，请重试；如果仍失败，请更换模型。", "Vision processing failed. Try again or choose another model."),
        ("vision_image_unavailable", "system", "图片已上传，但视觉服务没能读取它，请重新发送。", "The image was uploaded, but the vision service could not read it. Send it again."),
    )
    return tuple(
        _spec(code, "vision", "vision_model", blame, zh, en=en)
        for code, blame, zh, en in rows
    )


def _image_generation_specs() -> tuple[ErrorSpec, ...]:
    rows = (
        ("image_generation_model_required", "user_provider", "当前模型不能生成图片，请到设置里添加生图模型。", "Your current model can't generate images. Add an image generation model in Settings."),
        ("image_generation_model_incompatible", "user_provider", "当前生图模型无法生成图片，请到设置里更换模型。", "This image generation model can't create images. Choose another model in Settings."),
        ("image_generation_auth_invalid", "user_provider", "生图模型的 API Key 无效或已过期，请到设置里重新保存。", "The image generation API key is invalid or expired. Save it again in Settings."),
        ("image_generation_quota_insufficient", "user_provider", "生图模型服务额度不足，充值后再试。", "The image generation service has insufficient quota. Add credit and try again."),
        ("image_generation_model_not_found", "user_provider", "当前生图模型不可用，请到设置里更换模型。", "The image generation model is unavailable. Choose another model in Settings."),
        ("image_generation_model_not_ready", "user_provider", "生图模型尚未准备好，请到设置里重新保存或更换模型。", "The image generation model isn't ready. Save it again or choose another model in Settings."),
        ("image_generation_rate_limited", "provider_transient", "生图模型请求太多，请稍等几分钟再试。", "The image generation service is rate limited. Try again in a few minutes."),
        ("image_generation_unavailable", "provider_transient", "生图模型暂时无法连接，请稍后重试。", "The image generation service is temporarily unavailable. Try again later."),
        ("image_generation_invalid_output", "provider_transient", "生图模型没有返回有效图片，请重试或更换模型。", "The image generation model returned no valid image. Try again or choose another model."),
        ("image_generation_invalid_prompt", "system", "这次生图请求没有正确送达，我们会尽快排查。", "This image request wasn't delivered correctly. We'll investigate."),
        ("image_generation_internal_error", "system", "图片生成后的系统处理出了问题，我们会尽快排查。", "Image generation succeeded, but the result could not be processed. We'll investigate."),
        ("image_generation_failed", "provider_transient", "图片生成失败，请重试；如果仍失败，请更换模型。", "Image generation failed. Try again or choose another model."),
    )
    specs = [
        _spec(
            code,
            "image_generation",
            "image_generation",
            blame,
            zh,
            en=en,
            activity_result=True,
        )
        for code, blame, zh, en in rows
    ]
    specs.extend((
        _spec("image_generation_model_unsupported", "image_generation", "image_generation", "system", "", public=False, activity_result=True),
        _spec("image_generation_model_requires_test", "image_generation", "image_generation", "system", "", public=False, activity_result=True),
    ))
    return tuple(specs)


_DEFAULT_SOURCE_LOADERS: Mapping[str, Callable[[], tuple[ErrorSpec, ...]]] = {
    "chat": _chat_specs,
    "platform": _platform_specs,
    "workflow": _workflow_specs,
    "resident": _resident_specs,
    "vision": _vision_specs,
    "image_generation": _image_generation_specs,
}


def _validate_specs(specs: Iterable[ErrorSpec]) -> tuple[ErrorSpec, ...]:
    materialized = tuple(specs)
    seen: set[str] = set()
    for spec in materialized:
        if not re.fullmatch(r"[a-z0-9_]{1,64}", spec.code):
            raise ValueError(f"invalid error code: {spec.code}")
        if spec.code in seen:
            raise ValueError(f"duplicate error code: {spec.code}")
        if spec.blame not in {
            "user_provider", "provider_transient", "user_environment", "system"
        }:
            raise ValueError(f"invalid blame: {spec.code}")
        if spec.family == "vision_model" and not spec.code.startswith("vision_"):
            raise ValueError(f"vision family prefix mismatch: {spec.code}")
        if spec.family == "image_generation" and not spec.code.startswith(
            "image_generation_"
        ):
            raise ValueError(f"image family prefix mismatch: {spec.code}")
        if spec.public and not spec.safe_text_zh:
            raise ValueError(f"public spec missing safe text: {spec.code}")
        seen.add(spec.code)
    return materialized


def load_specs(
    source_loaders: Mapping[str, Callable[[], Iterable[ErrorSpec]]] | None = None,
) -> tuple[tuple[ErrorSpec, ...], RegistryExport]:
    """Load every source afresh; a previous partial result is never cached."""
    loaders = dict(source_loaders or _DEFAULT_SOURCE_LOADERS)
    if set(loaders) != REGISTRY_SOURCE_NAMES:
        raise ValueError("registry loaders must cover the closed source set")
    specs: list[ErrorSpec] = []
    unavailable: list[str] = []
    for source in sorted(loaders):
        try:
            specs.extend(_validate_specs(loaders[source]()))
        except Exception:  # source identity is exported; raw exception is not
            unavailable.append(source)
    validated = _validate_specs(specs)
    public_values = frozenset(spec.code for spec in validated if spec.public)
    missing = tuple(unavailable)
    if not missing:
        export = RegistryExport(public_values, "ok", ())
    elif public_values:
        export = RegistryExport(public_values, "partial", missing)
    else:
        export = RegistryExport(None, "unavailable", missing)
    return validated, export


def registry_export(
    source_loaders: Mapping[str, Callable[[], Iterable[ErrorSpec]]] | None = None,
) -> RegistryExport:
    return load_specs(source_loaders)[1]


def all_specs() -> tuple[ErrorSpec, ...]:
    specs, export = load_specs()
    if export.status != "ok":
        raise RuntimeError("error registry sources unavailable")
    return specs


def public_specs() -> tuple[ErrorSpec, ...]:
    return tuple(spec for spec in all_specs() if spec.public)


def matcher_specs() -> tuple[ErrorSpec, ...]:
    return tuple(spec for spec in all_specs() if spec.matcher_pattern)


def consumer_specs() -> tuple[ErrorSpec, ...]:
    return tuple(
        spec
        for spec in public_specs()
        if spec.domain in {"chat", "platform", "vision", "image_generation"}
        and spec.code != "image_generation_internal_error"
    )


def activity_image_generation_codes() -> frozenset[str]:
    return frozenset(
        spec.code
        for spec in all_specs()
        if spec.domain == "image_generation" and spec.activity_result
    )


def spec_for(code: str, *, public_only: bool = True) -> ErrorSpec | None:
    candidate = str(code or "").strip()
    for spec in all_specs():
        if spec.code == candidate and (spec.public or not public_only):
            return spec
    return None


def require_spec(code: str, *, public_only: bool = True) -> ErrorSpec:
    spec = spec_for(code, public_only=public_only)
    if spec is None:
        raise ValueError("unregistered error_class")
    return spec


def validate_rejection_dimensions(
    domain: str, boundary: str, fallback: str
) -> None:
    """Reject any rejection-counter dimension outside its closed contract."""
    if REJECTION_BOUNDARY_DOMAINS.get(boundary) != domain:
        raise ValueError("invalid contract rejection boundary")
    if fallback not in REJECTION_FALLBACK_CODES:
        raise ValueError("invalid contract rejection fallback")


def resolve_untrusted(
    code: str,
    *,
    domain: str,
    boundary: str,
    reporter: Callable[[str, str, str], None] | None = None,
) -> ErrorSpec:
    """Resolve a dynamic candidate without ever retaining its raw miss."""
    spec = spec_for(code)
    if spec is not None and (
        spec.domain == domain or spec.code == UNREGISTERED_ERROR_CLASS
    ):
        return spec
    fallback = require_spec(UNREGISTERED_ERROR_CLASS)
    if reporter is not None:
        # Contract mistakes must stay loud.  Only failures inside the
        # best-effort reporting transport are allowed to leave user flow intact.
        validate_rejection_dimensions(domain, boundary, fallback.code)
        try:
            reporter(domain, boundary, fallback.code)
        except Exception:
            pass
    return fallback


def classify_text(text: str) -> ErrorSpec | None:
    candidate = str(text or "")
    for spec in matcher_specs():
        matcher = spec.matcher()
        if matcher is not None and matcher.search(candidate):
            return spec
    return None
