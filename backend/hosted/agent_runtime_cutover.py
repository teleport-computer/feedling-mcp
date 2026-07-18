"""Small hosted-response compatibility helpers for Runtime V2.

The public response still carries the historical provider-driver label. It is a
wire-compatibility label only: Runtime V2 calls providers natively and there is
no hosted resident CLI process or supervisor behind it.
"""

from __future__ import annotations

import os

import provider_client

# The public API historically exposed a provider-derived ``driver`` field.
# Runtime V2 keeps those labels for wire compatibility only; every provider now
# executes in the same native in-process tool-calling loop.
_CLAUDE_PROVIDERS = {"anthropic", "deepseek"}
_CODEX_PROVIDERS = {"openai"}
_PI_PROVIDERS = {"openai_compatible", "gemini", "openrouter"}


def driver_for_provider(provider: str) -> str:
    """Return the historical response label for a provider, not an executor.

    The values preserve the pre-V2 API mapping: anthropic/deepseek →
    ``claude``; OpenAI-compatible relays → ``pi``; OpenAI → ``codex``. No
    configured fit → ``legacy``. None of these labels selects a separate V2
    execution path."""
    p = provider_client.normalize_provider(provider)
    if p in _CLAUDE_PROVIDERS:
        return "claude"
    if p in _PI_PROVIDERS:
        return "pi"
    if p in _CODEX_PROVIDERS:
        return "codex"
    return "legacy"


def codex_transport(provider: str) -> str:
    """Historical telemetry field: OpenAI is native, other labels are empty."""
    p = provider_client.normalize_provider(provider)
    if driver_for_provider(p) != "codex":
        return ""
    return "native"


class UnsupportedProviderError(Exception):
    """provider 未配置或 Runtime V2 没有 native provider mapping。"""


def assert_hosting_ready() -> None:
    """Fail fast unless the pooled V2 worker token contract can be honored."""
    from hosted import config_store

    config_store.hosted_runtime_policy()
    missing = []
    if not os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip():
        missing.append("FEEDLING_RUNTIME_TOKEN_SECRET")
    if missing:
        raise RuntimeError(
            "托管前置缺失：" + ", ".join(missing) +
            "。缺少运行时前置会让当前托管策略无法安全处理消息；"
            "请在 backend 与对应 runtime worker 两侧设置后再启动。"
        )


def resolve_driver(config: dict | None) -> str:
    """返回 wire-compatible provider label：``claude`` / ``codex`` / ``pi``。

    Runtime V2 不按 label 分层或选择 loop；所有支持 provider 均进入同一 loop。
    无法托管时 raise ``UnsupportedProviderError``。"""
    provider = str((config or {}).get("provider") or "")
    driver = driver_for_provider(provider)
    if driver not in ("claude", "codex", "pi"):
        raise UnsupportedProviderError(provider or "unconfigured")
    return driver


def _runtime_block(driver: str) -> dict:
    return {"engine": "feedling_agent_runtime", "mode": "hosted_agent", "driver": driver, "version": 1}


def build_processing_response(user_row: dict, *, driver: str) -> tuple[dict, int]:
    """The client reads the eventual V2 result via Chat history/poll. Always
    202; never a `reply` field
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
