"""P0 cell matrix + provider key pool loading.

Key pool file (never committed): ``~/.feedling-e2e-keys.env``, chmod 600 —
see docs/testing/RELEASE_TESTING_PROTOCOL.md §1.2. A cell whose key is absent
is reported as SKIP("no key"), never a failure: the harness must be runnable
before the pool is fully provisioned, and partially when a vendor is down.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

KEYS_FILE = Path(os.environ.get("FEEDLING_E2E_KEYS", "~/.feedling-e2e-keys.env")).expanduser()


def load_keys() -> dict[str, str]:
    env: dict[str, str] = {}
    if not KEYS_FILE.exists():
        return env
    for line in KEYS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


@dataclass
class HostedCell:
    """One hosted (model_api) P0 cell: a provider-key class driving one driver."""
    name: str                       # report label
    provider: str                   # /v1/model_api/setup provider value
    key_env: str                    # variable in the key pool file
    models: list[str]               # candidates tried in order until setup passes
    base_url_env: str = ""          # relay cells carry their own base URL
    extra_env: dict = field(default_factory=dict)

    def key(self, pool: dict[str, str]) -> str:
        return pool.get(self.key_env, "")

    def base_url(self, pool: dict[str, str]) -> str:
        return pool.get(self.base_url_env, "") if self.base_url_env else ""


# The six provider classes of §1.2 — every real hosted access mode has a cell.
# Model candidates are cheap defaults; setup tries them in order.
HOSTED_CELLS: list[HostedCell] = [
    HostedCell("anthropic-official", "anthropic", "E2E_KEY_ANTHROPIC",
               ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]),
    HostedCell("openai-official", "openai", "E2E_KEY_OPENAI",
               ["gpt-5.2", "gpt-5.2-mini"]),
    HostedCell("gemini-official", "gemini", "E2E_KEY_GEMINI",
               ["gemini-3.1-flash", "gemini-3.1-pro"]),
    HostedCell("openrouter", "openrouter", "E2E_KEY_OPENROUTER",
               ["anthropic/claude-sonnet-4.6", "deepseek/deepseek-chat"]),
    HostedCell("relay-openai-compatible", "openai_compatible", "E2E_KEY_RELAY",
               [], base_url_env="E2E_RELAY_BASE"),   # models from E2E_RELAY_MODEL
    HostedCell("deepseek-official", "deepseek", "E2E_KEY_DEEPSEEK",
               ["deepseek-chat"]),
]


@dataclass
class VpsCell:
    """One resident (VPS) P0 cell: which local CLI harness drives the consumer."""
    name: str
    agent_cli_cmd: str
    needs_binary: str               # skip the cell when this binary is absent locally

VPS_CELLS: list[VpsCell] = [
    VpsCell("vps-claude-code", 'claude -p "{message}"', "claude"),
    VpsCell("vps-codex", "codex exec --skip-git-repo-check --json "
                         "--dangerously-bypass-approvals-and-sandbox {message}", "codex"),
    VpsCell("vps-hermes", 'hermes chat -Q --source tool --max-turns 60 -q "{message}"',
            "hermes"),
    # OpenClaw: 暂免（无用户，Seven 2026-07-17 定）——加回时补一格即可。
]
