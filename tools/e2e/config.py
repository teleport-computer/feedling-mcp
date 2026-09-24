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
               ["gemini-3.6-flash", "gemini-3.1-pro-preview"]),
    HostedCell("openrouter", "openrouter", "E2E_KEY_OPENROUTER",
               ["anthropic/claude-sonnet-4.6", "deepseek/deepseek-chat"]),
    # 2026-09-25 T726(Seven「没用的中转站可以从回归中去掉 如果中转站还有钱就是模型下架就换一个模型」):
    # 原 E2E_RELAY_MODEL=gpt-5.5 仍列在该中转 /models 里,但对这把 key 的分组 404「not available
    # for this group」;同 key 其它模型可调 ⇒ 有钱、该型号对本 key 分组不可用(不是目录下架),
    # 换成实测会发 tool_calls 的裸名模型。型号从本机 key 文件
    # 挪进这里入版本库(key 仍只在 key 文件)。当日直打:kimi-k3 4.2s、qwen3.8-max 3.4s 均 200 +
    # finish_reason=tool_calls。
    HostedCell("relay-openai-compatible", "openai_compatible", "E2E_KEY_RELAY",
               ["kimi-k3", "qwen3.8-max"], base_url_env="E2E_RELAY_BASE"),
    HostedCell("deepseek-official", "deepseek", "E2E_KEY_DEEPSEEK",
               ["deepseek-chat"]),
    # 2026-09-25 T726 移除 jiushi-relay 与 zhailian-relay(Seven 同上原话):jiushi 403
    # 「用户剩余额度 ¥0.007708」没钱;zhailian 的 key 在 Free 分组下 /v1/models 返回 0 个、
    # 两个候选均 503「No available channel」,无模型可换。⚠️覆盖面损失:hosted P0 少了第二、第三家
    # 中转;剩下的 relay-openai-compatible 选的是裸名型号,所以「选中方括号标签名 / 斜杠名型号
    # 并走完 setup→回合」这段往返不再实跑。目录解析本身仍覆盖:该中转 /models 里仍有方括号与
    # 斜杠名,型号预检照常读取整份目录。原 09-14 的两家目录形状记录见 git 历史。
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
    # Hermes: excluded from release P0 by Seven (2026-09-06); this does not
    # remove the harness or its feature-specific regression coverage.
    # OpenClaw: 暂免（无用户，Seven 2026-07-17 定）——加回时补一格即可。
]
