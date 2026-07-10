# D4 Load Test + Gated Rollout + Kill Resident Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（仅 T1-T4 压测脚手架）。T5 灰度/关 resident 是 **ops runbook 非 TDD**，由用户/ops 手动执行。Steps 用 checkbox。

**Goal:** 证明 V2 对等且不更贵（tokens/turn 不回归）→ 据证据灰度翻用户 → 停 resident，兑现省钱/并发。

**Architecture:** 见 spec `…-D4-loadtest-rollout-killresident-design.md`。**依赖 D0（指标/setter/互斥闸）+ D3（proactive 迁走）。**

## Global Constraints

- **NO-COMMIT**；**worktree** 只在 worktree。
- **压测不烧真钱**：合成用户用 mock provider（确定 token 计数）。
- **tokens/turn 回归 = 回滚条件**（硬门）。
- 真压测（100u）**手动运行产报告，不进 CI**；CI 只跑脚手架烟测（≤5 用户）。
- 测试同 D0。

---

### Task 1: mock provider

**Files:** Create `scripts/loadtest/mock_provider.py`；Test `tests/test_loadtest_mock_provider.py`。

**Interfaces:** 起最小 HTTP server 冒充 anthropic/openai wire；收 chat/messages → 返回确定文本 + 固定 `usage.prompt_tokens/completion_tokens`；可配 `--latency-ms` 模拟 provider 等待。

- [ ] TDD 烟测：POST 一条 → 200 + 响应含固定 usage + 文本；latency 配置生效。

---

### Task 2: 采集函数

**Files:** Create `scripts/loadtest/collect.py`；Test `tests/test_loadtest_collect.py`。

**Interfaces:** `p95(samples)->float`、`mean(samples)->float`、`queue_wait_samples(from agent_jobs)`（`claimed_at-created_at`）、`turn_latency_samples`（`v2_turn_metrics.latency_ms`）、`tokens_per_turn(from v2_turn_metrics)`、`peak_rss(pid)`（psutil）。

- [ ] TDD：p95/mean 数学正确；从 agent_jobs/v2_turn_metrics 采样 SQL 正确（塞已知行核）。

---

### Task 3: 压测驱动

**Files:** Create `scripts/loadtest/run_loadtest.py`；Test `tests/test_loadtest_harness_smoke.py`（5 用户端到端烟测）。

**Interfaces:** `--users N --workers W`；seed N 合成用户（指向 mock provider + db_action_v2 + 激活）；起池；驱动 chat 尖峰 + wake 稳态；采集→报告（json/md）。

- [ ] TDD 烟测（5 用户）：端到端跑通、报告字段齐（RSS/queue-wait-P95/latency/stuck/tokens-per-turn）；断言容量模型不矛盾（小规模：队列排空、stuck≈0）。100 用户真跑是手动。

---

### Task 4: tokens/turn vs resident 对比

**Files:** Create `scripts/loadtest/compare_tokens.py`；Test `tests/test_loadtest_compare.py`。

**Interfaces:** 一组 identical fixture 对话分别过 V2 responder 路径 与 resident `call_agent` 路径（都打 mock provider），比 tokens/turn。

- [ ] TDD：fixture 过两路径 → 报告 delta；V2 不显著高于 resident（阈值默认 +10%，可配）；超阈 → 非零退出（= 回滚信号）。差异来源可解释。

---

### Task 5: 灰度 rollout + 关 resident runbook（ops，非 TDD）

**Files:** 本 plan 段即 runbook（也可另落 `deploy/` 下短文档）。**执行者 = 用户/ops，非 subagent。**

前提：D0 全落地（池心跳/互斥闸/setter/指标）；D3 已部署（proactive 迁走）——否则只 chat-only 混合灰度、**不关 resident**。

1. 内测先行：D0.3 CLI 翻 1 内测用户 → 观 `/v1/admin/v2-metrics` + error chip + 对话质量 24-48h。
2. 分批 ramp：5→20→50→全量；每批看 tokens/turn 不回归、queue-wait P95 达标、stuck≈0。
3. 混合态安全：D0.2 互斥闸保每用户单路径；翻了→V2 且 resident 被 reap（~15s）；回滚→翻回 resident_cli，下 tick 重 spawn。
4. 关 resident：每翻一用户其 resident 自动 reap（前提该用户 proactive 已由 D3 覆盖）。全 fleet 迁完 → roster 空 → supervisor 可保留空 roster（留回滚余地）一段时间后退休。**此刻省钱/并发目标兑现。**
5. 验证：`agent_runtime_instances` lease 无 db_action_v2 用户；池 RSS 平稳；无用户双路径。

---

## Self-Review

- spec 覆盖：mock(T1)/采集(T2)/驱动(T3)/对比(T4)/runbook(T5)。
- 一致性：采集字段与 D0.4 `v2_turn_metrics` 一致；关 resident 杠杆与 D0.2 互斥闸一致。
- placeholder：T5 是 ops runbook（刻意非 TDD）；真压测手动——已标注，非遗漏。
- 依赖：显式声明依赖 D0+D3；4 决策（阈值/规模/default 翻转/退休方式）用户 review 定。
