"""chat 通道 error_class → (blame, user_text) 目录（spec Phase B / B3）。

同源纪律：这 11 类（chat 上游类）、blame、user_text 一字不差搬自
``tools/chat_resident_consumer.py`` 的 ``_ERROR_CLASS_RULES`` +
``classify_agent_error`` 硬编码分支（turn_timeout / reply_parse_failed /
model_not_found[裸404] / unknown）。两处各自维护（consumer 在 tools/ 不能
import backend），一致性由 tests/test_catalog_consumer_parity.py 锁定。

blame 三分类纪律（照 docs/FRONTEND_ERROR_CONTRACT.md §二）：
- user_provider      → 可以引导用户去充值 / 改 key / 改模型名
- provider_transient → 上游临时问题，等它自己恢复
- system             → 我们的问题，绝不能引导用户改配置（会误导用户，dded 案例的教训）
"""
from __future__ import annotations

import re

ERROR_CLASSES = frozenset({
    "quota_insufficient",
    "auth_invalid",
    "model_not_found",
    "provider_incompatible",
    "context_overflow",
    "content_filtered",
    "rate_limited",
    "upstream_unavailable",
    "turn_timeout",
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
    # model_api config-time warning — same rationale as above. Emitted at setup
    # when an openai_compatible relay does not implement /v1/responses: LiteLLM
    # then force-bridges responses→chat-completions, which mangles codex's tool
    # loop so memory/tool calls silently go unreliable (turn still rc=0).
    "responses_unsupported",
})

# error_class -> (blame, user_text)
_CATALOG: dict[str, tuple[str, str]] = {
    "quota_insufficient": (
        "user_provider", "模型服务额度不足，充值后再发消息即可恢复。"),
    "auth_invalid": (
        "user_provider", "API Key 无效或已过期，请到设置里重新保存。"),
    "model_not_found": (
        "user_provider", "模型名不可用，请检查设置里的模型名。"),
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
    "reply_parse_failed": (
        "system", "系统处理回复时出了问题，我们会尽快排查。"),
    "unknown": (
        "system", "连接模型服务时出了问题。"),
    "genesis_failed": (
        "system", "入住蒸馏没能完成，可稍后在记忆花园重试。"),
    "genesis_partial": (
        "system", "入住蒸馏完成了，但有部分记忆没能导入。"),
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
    "responses_unsupported": (
        "user_provider", "你选的中转不支持 Responses 协议，AI 的记忆和工具调用可能不稳定。"
        "建议换一个支持 /v1/responses 的中转，或改用 Claude 类模型。"),
}

_FALLBACK_BLAME = "system"
_FALLBACK_USER_TEXT = "连接模型服务时出了问题。"


def blame_for(error_class: str) -> str:
    """未命中的 error_class 安全兜底到 'system'——绝不 KeyError，也绝不
    误导性地默认成 user_provider（那会让用户白跑一趟改配置）。"""
    entry = _CATALOG.get(error_class)
    return entry[0] if entry is not None else _FALLBACK_BLAME


def user_text_for(error_class: str, **ctx) -> str:
    """``**ctx`` 为未来动态占位（如失败次数）预留——当前 19 类均静态文案，
    ctx 暂被忽略；保留形参使接口稳定，未来加占位不必改调用方签名。"""
    entry = _CATALOG.get(error_class)
    return entry[1] if entry is not None else _FALLBACK_USER_TEXT


# 与 tools/chat_resident_consumer.py 的分类器等价的 backend 侧副本。
# consumer 在 tools/ 不能 import backend（单向依赖），故此处维护一份；
# tests/test_catalog_consumer_parity.py::test_classify_upstream_mirrors_consumer
# 用代表串锁两份不漂移。次序即优先级。
_UPSTREAM_RULES = (
    ("quota_insufficient", re.compile(
        r"余额|额度|insufficient_quota|credit balance|requires more credits"
        r"|payment required|\b402\b|quota", re.I)),
    ("auth_invalid", re.compile(
        r"invalid ?(x-)?api.?key|unauthorized|authentication|\b401\b", re.I)),
    ("model_not_found", re.compile(
        r"invalid model name|model_not_found|no such model", re.I)),
    ("provider_incompatible", re.compile(
        r"unknown variant|not supported|unsupported (parameter|tool)"
        r"|invalid_request_error.*tool", re.I)),
    ("context_overflow", re.compile(
        r"context.{0,20}(length|window)|maximum context|too many tokens"
        r"|prompt is too long", re.I)),
    ("content_filtered", re.compile(
        r"content_filter|content policy|safety|blocked by", re.I)),
    ("rate_limited", re.compile(r"\b429\b|too many requests|rate.?limit", re.I)),
    ("upstream_unavailable", re.compile(
        r"\b5\d{2}\b|overloaded|timed? ?out|connection (refused|reset|error)"
        r"|unreachable|stream disconnected", re.I)),
)


def classify_upstream(text: str) -> str:
    """把上游/运行时错误文本分类到 chat 上游 error_class；未命中返 ""（调用方
    决定兜底，如 genesis 落 genesis_failed）。与 consumer classify_agent_error 的
    规则表等价（不含 turn_timeout/reply_parse_failed 那两个凭异常类型/特定串判定
    的分支——那两类不会出现在 genesis/import 的错误文本里）。"""
    t = text or ""
    lowered = t.lower()
    if re.search(r"\b404\b", t) and "model" in lowered:   # 与 consumer 裸404+model 分支对齐
        return "model_not_found"
    for klass, pat in _UPSTREAM_RULES:
        if pat.search(t):
            return klass
    return ""
