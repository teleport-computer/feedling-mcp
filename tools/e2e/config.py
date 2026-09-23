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
    HostedCell("relay-openai-compatible", "openai_compatible", "E2E_KEY_RELAY",
               [], base_url_env="E2E_RELAY_BASE"),   # models from E2E_RELAY_MODEL
    HostedCell("deepseek-official", "deepseek", "E2E_KEY_DEEPSEEK",
               ["deepseek-chat"]),
    # 第二、第三个中转站:不同中转站的 /models 目录格式差异很大,推荐链路(§2-10)
    # 必须都能匹配 —— 只测一个不够。2026-09-14 hojimi 退役(Seven 定,连续两轮把
    # 兜底话术当正文复读),换成玖时 + 宅恋。当日实测 /models 形状(去内容化):
    #   (四类计数按特征各自统计,会重叠,不是互斥分布)
    #   relay-openai-compatible  171 个:方括号标签 143(其中 1 个同时带 8 位日期后缀
    #                            [MAX-CC]claude-opus-4-5-20251101)/ 无标签 28 = 裸名 27
    #                            + 斜杠 1(BAAI/bge-m3)
    #   jiushi-relay             140 个:方括号标签 122 / 裸名 18(gpt-*/gemini-*)
    #   zhailian-relay             6 个:全是「[标签]厂商/型号」带斜杠的形状
    #   空悲切(.env KONGBEIQIE_*)的 key+base 与 E2E_RELAY_* 逐字节相同 ⇒ 它就是
    #   relay-openai-compatible 这一格,不另开格(同一家两个名字会测两遍)。
    # ⚠️ hojimi 原来覆盖的「裸名 + 8 位日期后缀」(claude-haiku-4-5-20251001)形状
    # 两家都没有;换来的是 zhailian 的斜杠形状。两家目录里都没有 haiku,候选取
    # 当日目录里的 claude 系 + 一个非 claude 兜底(未比价)。宅恋 09-14 22:5x 观测:
    # 本机直打 opus-5/deepseek/GLM 各 1 次 60s ReadTimeout、kimi-k3 1 次 45s 通过后
    # 下一次 429;test 后端 setup 对 opus-5、deepseek 各 1 次 ReadTimeout。kimi-k3
    # 作为 setup 候选尚未在 p0 里跑过。该格 setup 红时先看中转连通性再看产品。
    # 2026-09-15(T596,Seven 拍板 A:gpt-5.5 → gemini → [AG4]claude):[AG4]claude-sonnet-4-6
    # 通道后端按 Gemini 格式转发,对我们工具 schema 里漏出的本地标记 enforceItemBounds
    # 返 400 → 运行时裁掉全部工具、记忆链不可用(T589 定界;根修在 T595 剥离标记)。
    # 同一中转直打(reports/T589-run-20260915/T596-jiushi-schema-probe.txt):
    # gemini-3-flash-preview / gpt-5.5 带全部 36 个工具 200,[AG4]claude 剥掉标记后 200。
    # p0 实跑(同目录 p0_jiushi_*.log + T596-jiushi-gemini-trace-timeline.txt):
    # gemini-3-flash-preview 2/2 round1 tool_calls 正常、round2 finish_reason=timeout
    # → 兜底;gpt-5.5 1/1 harness 六步 ✅ 但 memory 步 WARN(300s 内 index 0 卡,
    # 库 trace 无 capture 事件)。故候选顺序 gpt > gemini > [AG4]claude。
    HostedCell("jiushi-relay", "openai_compatible", "E2E_KEY_JIUSHI",
               ["gpt-5.5", "gemini-3-flash-preview", "[AG4]claude-sonnet-4-6"],
               base_url_env="E2E_JIUSHI_BASE"),
    HostedCell("zhailian-relay", "openai_compatible", "E2E_KEY_ZHAILIAN",
               ["[0.01]限时/claude-opus-5", "[0.01]限时/kimi-k3"],
               base_url_env="E2E_ZHAILIAN_BASE"),
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
