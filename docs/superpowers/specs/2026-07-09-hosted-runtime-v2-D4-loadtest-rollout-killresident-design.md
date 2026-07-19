# Hosted Runtime V2 — D4 Load Test + Gated Rollout + Kill Resident 设计

> **RETIRED / DO NOT DEPLOY.** Historical cutover design; resident rollback and
> supervisor scale-down are complete and cannot be used operationally.

> 子项目 D 的收官，**真正兑现省钱/并发的那一步**。来源：walkthrough §8 gate 6-7 + §9 条件6 + 现状调查（2026-07-09）。**依赖 D0（池在跑+互斥闸+setter+指标）+ D3（proactive 迁走）。** 半代码（压测脚手架）半 ops（灰度/关 resident runbook）。

**Goal:** 用压测证明 V2 对等且不更贵（tokens/turn vs resident 不回归），据证据灰度把用户翻到 db_action_v2，最终停掉常驻 resident 进程——把 RAM/进程数从"随注册数涨"变成"随池大小固定"。

**Architecture:** (1) 压测脚手架（脚本）：造 N 合成用户、驱动 chat+wake 负载、量 RSS/queue-wait-P95/turn-latency/stuck-jobs/tokens-per-turn，并在同一 fixture 上对比 resident；(2) 灰度 rollout runbook（ops）：内测先行→观测→分批→回滚=翻旗；(3) 关 resident：D0.2 互斥闸随翻旗自动 reap，收尾确认零 resident + 全迁后退休 supervisor。

## Global Constraints

- **NO-COMMIT** / **worktree**（同 D0）。
- **压测不烧真钱**：合成用户用 **mock provider**（本地 stub 返回 canned 响应 + 确定 token 计数），不用真 BYOK key。避免压测本身烧余额、且让 tokens/turn 对比确定可复现。
- **tokens/turn 回归 = 回滚条件**（walkthrough gate 6 硬门）。
- **BYOK-only / no-filler** 不因压测放松。
- **灰度证据先行**：任何 default 翻转前，必须有 parity + load 证据（walkthrough gate 7）。
- **测试基线**：同 D0。

## 现状（调查确证）

- **零指标**：无 tokens/turn、latency、queue-depth gauge（见 D0 现状）。→ **压测的被测量对象由 D0.4 提供**（`v2_turn_metrics` + `/v1/admin/v2-metrics`）。本 spec 假设 D0 已落地。
- **翻钥杠杆**：`hosted_runtime_mode` per-user、经 D0.3 的 admin 端点/CLI 翻。回滚 = 翻回 `resident_cli`（default），supervisor 下一 tick 重 spawn resident（`supervisor.py`）。
- **关 resident 杠杆**：D0.2 互斥闸——db_action_v2 用户离开发现 roster → `Supervisor.tick()` ~15s reap+释 lease（`supervisor.py:167`）。**关 resident 是翻旗的自动副作用**，非独立 teardown。
- **前提未满**：proactive 未迁（D3 前 db_action_v2 只重路由 chat，wake 仍 resident）→ **全量关 resident 必须等 D3 部署**。D3 前只能"chat 走 V2、wake 仍 resident"的混合态（此时不能关 resident）。
- **部署/回滚**：runner 镜像 build→bump compose tag→`phala deploy`→发 compose_hash 上链；env/flag 走加密通道无需链上 re-auth；回滚 = 翻 DB mode flag 或 `DEPLOY_*_RUNNER_CVM=false`（`DEPLOYMENTS.md:134,343`）。

## 设计

### D4.1 — mock provider（压测底座）

`scripts/loadtest/mock_provider.py`：一个最小 HTTP server 冒充 anthropic/openai wire，收 chat/messages 请求→返回确定性响应（固定文本 + 固定 `usage.prompt_tokens/completion_tokens`，可配延迟模拟 provider 等待）。合成用户的 model_api config 指向它（`base_url`）。→ 压测零真实 LLM 调用、tokens/turn 确定可复现。

### D4.2 — 压测脚手架

`scripts/loadtest/run_loadtest.py`：
1. `seed_user` 造 N 合成用户（默认 100），各配指向 mock provider 的 model_api config + `hosted_runtime_mode=db_action_v2` + 激活（`first_chat_ok_at`）。
2. 起 serve_worker 池（16/32 worker）+ D3 调度器。
3. 驱动负载：everyone-sends-at-once chat 尖峰（100 job）+ 稳态 wake（heartbeat/scheduled）。
4. 采集：
   - **RSS**：池进程 `psutil` 峰值 RSS（对比：100 resident × 200-400MB）。
   - **queue-wait P95**：`agent_jobs.claimed_at − created_at` 分布。
   - **turn latency**：`finished_at − started_at`（= `v2_turn_metrics.latency_ms`）。
   - **stuck jobs**：reaper `expired` 计数。
   - **tokens/turn**：`v2_turn_metrics` AVG(prompt+completion)。
5. 输出报告（markdown/json）。

walkthrough 容量目标校验：100 用户，single-flight+coalesce 封顶 chat 深度 100；16 worker × ~20s/turn ≈ 50 turns/min → 尖峰 ~2min 排空。断言 queue-wait P95 与该模型不矛盾。

### D4.3 — tokens/turn vs resident 对比（回滚门）

`scripts/loadtest/compare_tokens.py`：一组 identical fixture 对话，分别过 (a) V2 responder 路径、(b) resident `call_agent` 路径，都打 mock provider，比 tokens/turn。**V2 不得显著高于 resident**（阈值如 +10%）——超阈 = 回归 = 回滚条件。差异来源须能解释（prompt 组装差异、caching 缺失等）。

### D4.4 — 灰度 rollout runbook（ops，非 TDD）

1. **前提**：D0 全落地（池心跳、互斥闸、setter、指标）；D3 已部署（proactive 迁走）——否则只做 chat-only 混合灰度、**不关 resident**。
2. **内测先行**：D0.3 CLI 翻 1 个内测用户 → 观测 `/v1/admin/v2-metrics` + error chip + 用户主观对话质量，24-48h。
3. **分批 ramp**：5 → 20 → 50 → 全量；每批看 tokens/turn 不回归、queue-wait P95 达标、stuck jobs≈0。
4. **混合态安全**：D0.2 互斥闸保证每用户只一条路径（翻了走 V2 且 resident 被 reap；没翻走 resident）。回滚 = 翻回 resident_cli，下一 tick 重 spawn。
5. **default 翻转**：证据齐后，才改默认（`get_hosted_runtime_mode` 的 fallback 或发现口径）——最后一步。

### D4.5 — 关 resident（收尾）

- **自动**：每翻一个 db_action_v2 用户，D0.2 互斥闸使其 resident 下一 tick 被 reap。→ 关 resident 是灰度的自动副作用。
- **前提**：该用户 proactive 也已由 D3 覆盖（否则 wake 丢）。
- **全量退休**：全 fleet 迁完后，roster 空 → supervisor 可 scale 到零 / `DEPLOY_*_RUNNER_CVM` 只留 V2 池。这一刻并发/省钱目标真正兑现（RAM 随池大小固定，不随注册数）。
- **验证**：`agent_runtime_instances` lease 表无 db_action_v2 用户；池 RSS 平稳；无用户既在 resident 又在 V2。

## 已定（2026-07-09 用户拍板）

- **tokens/turn 回归阈值** = +10%。✅
- **压测规模/机器** = **本地跑**（100u/16-32w harness 照写；但本地 mac 的 RSS/绝对延迟是**指示性**数字，非 CVM 权威——正式放行数字若需要，临近 cutover 再在贴近 4c/8GB 的机器复跑）。✅
- **default 翻转本轮不做**：D4 只到"全量灰度 + 关 resident"，default fallback 翻转留后续单独一小步（改一行、易回滚）。✅
- **resident 退休方式** = 保留 supervisor 空 roster 一段（留回滚余地）后再退休。✅

## 落地文件（汇总）

- `scripts/loadtest/mock_provider.py`（新）：确定性 mock provider。
- `scripts/loadtest/run_loadtest.py`（新）：压测驱动 + 采集 + 报告。
- `scripts/loadtest/compare_tokens.py`（新）：V2 vs resident tokens/turn 对比。
- `docs/superpowers/plans/…-D4-…`：runbook 段（灰度/关 resident，ops）。
- 测试：`test_loadtest_harness.py`（脚手架烟测：mock provider 返 usage、采集函数算 P95/AVG 正确、小规模端到端 5 用户跑通）。**注意**：这是脚手架的单元烟测，真压测（100u）是手动运行、产报告，不进 CI。

## 自查

- placeholder：无 TBD（4 决策显式待拍板）。
- 一致性：被测指标（tokens/turn/queue-wait/latency/stuck）与 D0.4 `v2_turn_metrics` 字段一致；关 resident 杠杆与 D0.2 互斥闸一致；回滚路径与 DEPLOYMENTS 一致。
- scope：压测脚手架（代码）+ 灰度/关 resident（ops runbook）——刻意分离，harness 可 TDD 烟测、cutover 是手动序列。
- 依赖：显式声明依赖 D0+D3；D3 前只能 chat-only 混合灰度不关 resident——已写明，非遗漏。
