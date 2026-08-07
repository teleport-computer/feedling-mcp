"""chat 通道 error_class → (blame, user_text) 目录（spec Phase B / B3）。

同源纪律：chat 上游类的 blame、user_text 一字不差搬自
``tools/chat_resident_consumer.py`` 的 ``_ERROR_CLASS_RULES`` +
``classify_agent_error`` 硬编码分支（turn_timeout / reply_parse_failed /
model_not_found[裸404] / unknown）。两处各自维护（consumer 在 tools/ 不能
import backend），一致性由 tests/test_catalog_consumer_parity.py 锁定。

blame 分类纪律（照 docs/FRONTEND_ERROR_CONTRACT.md §二）：
- user_provider      → 可以引导用户去充值 / 改 key / 改模型名
- provider_transient → 上游临时问题，等它自己恢复
- system             → 我们的问题，绝不能引导用户改配置（会误导用户，dded 案例的教训）
- user_environment   → 用户自托管运行环境问题，例如 VPS resident consumer 过旧/离线
"""
from __future__ import annotations

import re

ERROR_CLASSES = frozenset({
    "quota_insufficient",
    "auth_invalid",
    "model_not_found",
    "cli_config_invalid",
    "vision_model_required",
    "provider_incompatible",
    "context_overflow",
    "content_filtered",
    "rate_limited",
    "upstream_unavailable",
    "turn_timeout",
    "provider_empty_reply",
    "reply_parse_failed",
    "unknown",
    # genesis-only classes (Phase C / C1) — not part of the 11-class chat parity
    # set above, so they're intentionally excluded from
    # test_catalog_consumer_parity.py's consumer-side comparisons.
    "genesis_failed",
    "genesis_partial",
    # history_import-only classes (Phase C / C2) — same rationale as above.
    "import_failed",
    "import_stale",
    # memory-maintenance lane backoff (Phase C / C3) — same rationale as above.
    "memory_backoff",
    # runner/supervisor-only classes (Phase C / C4) — same rationale as above.
    "runner_spawn_failed",
    "runner_key_decrypt_failed",
    "runner_degraded",
    # Self-hosted resident maintenance classes. They are not upstream model
    # errors; blame points at the user's own VPS/runtime environment.
    "resident_consumer_stale",
    "resident_decrypt_source_unavailable",
    "resident_decrypt_health_unreported",
    "resident_never_claimed",
    "vision_model_auth_invalid",
    "vision_model_quota_insufficient",
    "vision_model_not_found",
    "vision_model_incompatible",
    "vision_model_rate_limited",
    "vision_model_unavailable",
    "vision_model_empty_response",
    "vision_model_not_ready",
    "vision_model_failed",
    "image_generation_model_required",
    "image_generation_model_incompatible",
    "image_generation_auth_invalid",
    "image_generation_quota_insufficient",
    "image_generation_model_not_found",
    "image_generation_model_not_ready",
    "image_generation_rate_limited",
    "image_generation_unavailable",
    "image_generation_invalid_output",
    "image_generation_invalid_prompt",
    "image_generation_failed",
})

# error_class -> (blame, user_text)
_CATALOG: dict[str, tuple[str, str]] = {
    "quota_insufficient": (
        "user_provider", "模型服务额度不足，充值后再发消息即可恢复。"),
    "auth_invalid": (
        "user_provider", "API Key 无效或已过期，请到设置里重新保存。"),
    "model_not_found": (
        "user_provider", "模型名不可用，请检查设置里的模型名。"),
    "cli_config_invalid": (
        "user_provider", "Agent 启动命令配置有误（缺少 {message} 占位符），消息传不到模型。请修正 AGENT_CLI_CMD。"),
    "vision_model_required": (
        "user_provider",
        "由于当前模型没有视觉能力，模型无法收到图片信息，建议更改模型或在设置页单独添加视觉模型"),
    "provider_incompatible": (
        "user_provider", "当前模型不支持这次请求用到的能力，换个模型或到设置里调整。"),
    "context_overflow": (
        "user_provider", "这次对话太长超出了模型上限，可精简后再试。"),
    "content_filtered": (
        "provider_transient", "这次回复被模型的内容策略拦下了，换个说法再试。"),
    "rate_limited": (
        "provider_transient", "模型服务限流了，稍等几分钟再试。"),
    "upstream_unavailable": (
        "provider_transient", "你的模型服务暂时不可用，稍后会自动恢复。"),
    "turn_timeout": (
        "system", "这轮回复超时了，稍后再试。"),
    "provider_empty_reply": (
        "provider_transient",
        "你的模型服务这次返回了空回复，稍后再试；反复出现请检查模型渠道或中转的稳定性。"),
    "reply_parse_failed": (
        "system", "系统处理回复时出了问题，我们会尽快排查。"),
    "unknown": (
        "system", "连接模型服务时出了问题。"),
    "genesis_failed": (
        "system", "入住材料的文件解读没能完成，可稍后在记忆花园重试。"),
    "genesis_partial": (
        "system", "入住材料的文件解读完成了，但有部分记忆没能导入。"),
    "import_failed": (
        "system", "聊天记录导入失败了，请稍后重试。"),
    "import_stale": (
        "system", "聊天记录导入卡住已超时，请重新发起。"),
    "memory_backoff": (
        "system", "记忆整理暂时受阻，正在自动重试。"),
    # runner/supervisor-only classes (Phase C / C4) — same rationale as above.
    "runner_spawn_failed": (
        "system", "你的 AI 助手进程启动失败，我们正在处理。"),
    "runner_key_decrypt_failed": (
        "system", "你的 AI 助手暂时无法启动（密钥读取失败），我们正在处理。"),
    "runner_degraded": (
        "system", "你的 AI 助手部分能力暂时受限，正在自动恢复。"),
    "resident_consumer_stale": (
        "user_environment", "你的 VPS resident consumer 版本可能太旧或没有正常接走任务，请更新并重启。"),
    "resident_decrypt_source_unavailable": (
        "user_environment", "你的 VPS resident 解密源不可用，真实加密消息暂时无法回复。"),
    "resident_decrypt_health_unreported": (
        "user_environment", "你的 VPS resident 端没有上报可验证的解密健康状态,通常是 consumer 版本太旧,请更新并重启。"),
    "resident_never_claimed": (
        "user_environment", "你的 VPS resident consumer 长时间没有接走入住/记忆蒸馏任务，请更新并重启。"),
    "vision_model_auth_invalid": (
        "user_provider", "视觉模型的 API Key 无效或已过期，请到设置里重新保存。"),
    "vision_model_quota_insufficient": (
        "user_provider", "视觉模型服务额度不足，充值后再试。"),
    "vision_model_not_found": (
        "user_provider", "当前视觉模型不可用，请到设置里更换模型。"),
    "vision_model_incompatible": (
        "user_provider", "当前视觉模型无法读取这张图片，请到设置里更换模型。"),
    "vision_model_rate_limited": (
        "provider_transient", "视觉模型请求太多，请稍等几分钟再试。"),
    "vision_model_unavailable": (
        "provider_transient", "视觉模型暂时无法连接，请稍后重试。"),
    "vision_model_empty_response": (
        "provider_transient", "视觉模型没有返回图片内容，请重试或更换模型。"),
    "vision_model_not_ready": (
        "user_provider", "视觉模型尚未准备好，请到设置里重新保存或更换模型。"),
    "vision_model_failed": (
        "provider_transient", "视觉模型处理失败，请重试；如果仍失败，请更换模型。"),
    "image_generation_model_required": (
        "user_provider", "当前模型不能生成图片，请到设置里添加生图模型。"),
    "image_generation_model_incompatible": (
        "user_provider", "当前生图模型无法生成图片，请到设置里更换模型。"),
    "image_generation_auth_invalid": (
        "user_provider", "生图模型的 API Key 无效或已过期，请到设置里重新保存。"),
    "image_generation_quota_insufficient": (
        "user_provider", "生图模型服务额度不足，充值后再试。"),
    "image_generation_model_not_found": (
        "user_provider", "当前生图模型不可用，请到设置里更换模型。"),
    "image_generation_model_not_ready": (
        "user_provider", "生图模型尚未准备好，请到设置里重新保存或更换模型。"),
    "image_generation_rate_limited": (
        "provider_transient", "生图模型请求太多，请稍等几分钟再试。"),
    "image_generation_unavailable": (
        "provider_transient", "生图模型暂时无法连接，请稍后重试。"),
    "image_generation_invalid_output": (
        "provider_transient", "生图模型没有返回有效图片，请重试或更换模型。"),
    "image_generation_invalid_prompt": (
        "system", "这次生图请求没有正确送达，我们会尽快排查。"),
    "image_generation_failed": (
        "provider_transient", "图片生成失败，请重试；如果仍失败，请更换模型。"),
}

_FALLBACK_BLAME = "system"
_FALLBACK_USER_TEXT = "连接模型服务时出了问题。"

# Classes removed from the catalog that may still sit in users' notice streams.
# Read-side filtering (notices.core.list_notices) is how a retired class stops
# being shown: user_logs rows are mirrored to the TEE shadow DB, so a SQL
# backfill would only touch the primary and leave the two diverged.
#
# responses_unsupported (retired 2026-07-27): warned openai_compatible users
# that a relay without /v1/responses made memory/tool calls unreliable. Its
# premise — LiteLLM force-bridging responses→chat-completions and mangling
# codex's tool loop — no longer holds anywhere: the gateway is retired,
# openai_compatible derives `pi` not `codex`, and V2 calls every provider over
# chat/completions. Verified on test: Kimi/Moonshot wrote memory, recalled it
# next turn, and ran 3/3 tool calls on both the V1 and V2 paths.
RETIRED_ERROR_CLASSES = frozenset({"responses_unsupported"})


def blame_for(error_class: str) -> str:
    """未命中的 error_class 安全兜底到 'system'——绝不 KeyError，也绝不
    误导性地默认成 user_provider（那会让用户白跑一趟改配置）。"""
    entry = _CATALOG.get(error_class)
    return entry[0] if entry is not None else _FALLBACK_BLAME


def user_text_for(error_class: str, **ctx) -> str:
    """Return stable fallback text, localized where the caller has a locale."""
    if str(ctx.get("language") or "").strip().lower().startswith("en"):
        if error_class == "vision_model_required":
            return (
                "Your current model can't process images, so it didn't receive this "
                "picture. Switch models, or add a dedicated vision model in Settings."
            )
        image_generation_text = {
            "image_generation_model_required": (
                "Your current model can't generate images. Add an image generation "
                "model in Settings."
            ),
            "image_generation_model_incompatible": (
                "This image generation model can't create images. Choose another "
                "model in Settings."
            ),
            "image_generation_auth_invalid": (
                "The image generation API key is invalid or expired. Save it again "
                "in Settings."
            ),
            "image_generation_quota_insufficient": (
                "The image generation service has insufficient quota. Add credit and "
                "try again."
            ),
            "image_generation_model_not_found": (
                "The image generation model is unavailable. Choose another model in "
                "Settings."
            ),
            "image_generation_model_not_ready": (
                "The image generation model isn't ready. Save it again or choose "
                "another model in Settings."
            ),
            "image_generation_rate_limited": (
                "The image generation service is rate limited. Try again in a few "
                "minutes."
            ),
            "image_generation_unavailable": (
                "The image generation service is temporarily unavailable. Try again "
                "later."
            ),
            "image_generation_invalid_output": (
                "The image generation model returned no valid image. Try again or "
                "choose another model."
            ),
            "image_generation_invalid_prompt": (
                "This image request wasn't delivered correctly. We'll investigate."
            ),
            "image_generation_failed": (
                "Image generation failed. Try again or choose another model."
            ),
        }.get(error_class)
        if image_generation_text is not None:
            return image_generation_text
    entry = _CATALOG.get(error_class)
    return entry[1] if entry is not None else _FALLBACK_USER_TEXT


# 与 tools/chat_resident_consumer.py 的分类器等价的 backend 侧副本。
# consumer 在 tools/ 不能 import backend（单向依赖），故此处维护一份；
# tests/test_catalog_consumer_parity.py::test_classify_upstream_mirrors_consumer
# 用代表串锁两份不漂移。次序即优先级。
_UPSTREAM_RULES = (
    # genesis/import worker 的 provider 错误把状态码编在 ``provider_http_<code>``
    # slug 里（下划线是 \w，\b<code>\b 形态永远打不中），按类补 slug 匹配。
    ("quota_insufficient", re.compile(
        r"余额|额度|insufficient_quota|credit balance|requires more credits"
        r"|payment required|\b402\b|provider_http_402|quota", re.I)),
    ("auth_invalid", re.compile(
        r"invalid ?(x-)?api.?key|unauthorized|authentication|\b401\b"
        r"|provider_http_40[13]", re.I)),
    # 与 consumer _ERROR_CLASS_RULES 的同名规则逐字对齐（见那边的长注释：上游下线
    # 一个模型时措辞五花八门，窄正则会把可自助修复的错误误判成 blame=system）。
    ("model_not_found", re.compile(
        r"invalid model name|model_not_found|no such model|unknown model"
        r"|supported .{0,40}model names"      # DeepSeek: "The supported API model names are …"
        r"|model .{0,80}does not exist"       # OpenAI: "The model `x` does not exist…"
        r"|not a valid model"
        r"|model[ _]not[ _]found", re.I)),
    ("cli_config_invalid", re.compile(
        r"missing the \{message\} placeholder", re.I)),
    # DeepSeek chat/completions and OpenRouter, observed 2026-07-30. Keep this
    # before provider_incompatible and the broad 404+model fallback below.
    ("vision_model_required", re.compile(
        r"unknown variant `image_url`, expected `text`"
        r"|no endpoints found that support image input", re.I)),
    ("provider_incompatible", re.compile(
        r"unknown variant|not supported|unsupported (parameter|tool)"
        r"|invalid_request_error.*tool", re.I)),
    ("context_overflow", re.compile(
        r"context.{0,20}(length|window)|maximum context|too many tokens"
        r"|prompt is too long", re.I)),
    ("content_filtered", re.compile(
        r"content_filter|content policy|safety|blocked by", re.I)),
    ("rate_limited", re.compile(
        r"\b429\b|provider_http_429|too many requests|rate.?limit", re.I)),
    ("upstream_unavailable", re.compile(
        r"\b5\d{2}\b|provider_http_5\d{2}|overloaded|timed? ?out"
        r"|connection (refused|reset|error)"
        r"|unreachable|stream disconnected"
        r"|ended without finish_reason", re.I)),
)


def classify_upstream(text: str) -> str:
    """把上游/运行时错误文本分类到 chat 上游 error_class；未命中返 ""（调用方
    决定兜底，如 genesis 落 genesis_failed）。与 consumer classify_agent_error 的
    规则表等价（不含 turn_timeout/reply_parse_failed 那两个凭异常类型/特定串判定
    的分支——那两类不会出现在 genesis/import 的错误文本里）。
    ⚠️ 刻意不含 ``provider_empty_reply``:那个类由 consumer 侧的
    ``EMPTY_PROVIDER_REPLY_MARK`` 在 helper 抛出点铸造,backend 从不铸它,
    所以这里没有对应规则(同 turn_timeout / reply_parse_failed 的既有排除)。
    若将来 backend 也开始产这个标记,记得两边一起加,否则
    test_classify_upstream_mirrors_consumer_on_samples 会红。
    """
    t = text or ""
    lowered = t.lower()
    if "resident_never_claimed" in lowered:
        return "resident_never_claimed"
    for klass, pat in _UPSTREAM_RULES:
        if pat.search(t):
            return klass
    if re.search(r"\b404\b", t) and "model" in lowered:   # 与 consumer 裸404+model 分支对齐
        return "model_not_found"
    return ""
