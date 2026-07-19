# Hosted Runtime V2 — Merge-Conditions Backlog

> **RETIRED / DO NOT DEPLOY.** Historical snapshot; use the live Runtime V2
> rollout and parity documents for current operations.

> **STATUS: HISTORICAL SNAPSHOT (2026-07-09).** The live completion and rollout
> gates are tracked in `deploy/HOSTED_RUNTIME_V2_ROLLOUT.md`; this file preserves
> the pre-native-loop audit only.

> **来源**：`~/downloads/feedling-runtime-v2-walkthrough.html`（2026-07-08，sxysun + Claude 的扩展愿景稿）§9「where the implementation stands」列出的 6 条 merge 前置条件 + §6 的 admission ceiling。
> **对账基准**：`feat/hosted-runtime-v2` worktree，A+B+C 已实现未提交（2026-07-09 审计）。
> **本文档只记账，不改代码。** 每条给：现状(带 file:line 证据) / 改法 / 归属(A-C 补丁 vs 子项目 D) / 优先级。
> **铁律**：全程无 commit / 无 git add，由用户自己提交。

## 范围界定（重要）

A+B+C 是按**当初的 A/B/C spec**（`docs/superpowers/specs/2026-07-08-hosted-runtime-v2-abc-design.md`，确定性 `planner→executor→responder`）实现并过审的。
这份 walkthrough 是**更高的愿景稿**——它的 §4/§5 把架构抬到「原生 tool-calling agent loop + 完整对话压缩」，§9 又把这些提成 merge 前置。因此：

- **A/B/C 对自身 spec = 完成。**
- **对 walkthrough §9 六条 gate = 未过**（4 红 2 黄 + 无 admission ceiling）。
- 其中**条件 2、3 是重构级**（改架构），其余是机械可补项。

执行顺序（用户拍板）：**本 backlog(3) → 机械项(1) → 重构立项(2)**。

---

## 汇总表

| # | 条件 | 现状 | 归属 | 优先级 |
|---|------|------|------|--------|
| 1a | send 路径 v2 worker 存活闸 | 🔴 故意跳过、无替代 | A-C 补丁 | P0 |
| 1c | 终态失败 error 事件 + `last_runtime_error` patch | 🔴 只写 `last_error`，iOS 看不到 | A-C 补丁 | P0 |
| 1e | provider 调用异步化 | 🔴 同步 client 桥到默认线程池，卡 ~32 | A-C 补丁 | P1 |
| 1b | deadline_at + reaper | ✅ 已做已接线 | — | 完成 |
| 1d | turn 异常隔离 | ✅ 已做 | — | 完成 |
| 4a | planner 词表 ↔ registry 对齐 | 🔴 planner 吐 executor 静默 skip 的动作 | A-C 补丁 | P1 |
| 4b | `web_search`/`web_fetch` capability | 🔴 缺（仅 legacy 有） | A-C 补丁 | P1 |
| 4c | `memory_search` capability | 🔴 缺 | A-C 补丁 | P2 |
| 5 | 部署目标钉死 + 接线 | ✅ 文档已钉死(runner CVM)；进程启动接线归 D | A-C 补丁(文档) done | 完成(文档) |
| 6 | 子项目 D 需求写进文档 | 🔴 六项一处未记 | 本 backlog + D spec | P1 |
| §6 | send 路径 admission ceiling | 🔴 无条件 persist+enqueue | 子项目 D | D |
| 2 | 完整对话(tail+压缩 summary) | 🔴 只喂 pending 未回消息 | **子项目 D / 重构** | D |
| 3 | 原生 tool-calling agent loop | 🔴 plan→execute→reply 一次成型 | **子项目 D / 重构** | D |

---

## 条件 1 — No silent hangs（🟡 部分）

已做：**1b** `deadline_at` 在 claim(`jobs_store.py:106-108`)/mark_running(`117-119`) 打戳 + `reap_stuck_jobs`(`148-164`) + `_reaper_loop` 已接线(`serve_worker.py:190-210,224-237`)；**1d** `process_job`(`worker.py:177/261`)、`_slot_loop`(`366-369`)、两处 `gather(return_exceptions=True)` 全包异常。

### 1a — v2 worker 存活闸（🔴 P0，A-C 补丁）
- **现状**：`chat_send_core.py:103` `if not _v2_mode and not live:` **对 v2 显式跳过** supervisor 存活检查（注释 86-88 称 v2「不依赖 resident supervisor」）；v2 分支(151-160)**无条件 enqueue**，未对 v2 worker 池补任何存活判据。worker 全死则消息静默 park。
- **改法**：给 v2 补一个独立存活信号（worker 池心跳表 / 最近 claim 活跃度 / advisory-lock 探针任一），send 前检查；死则返回**独立的** 503/busy（不复用 supervisor-dead 的 503 语义）。
- **验收**：kill 所有 serve_worker → send 立即拿到区分性错误，而非无限 loading。

### 1c — 终态失败 error 事件 + `last_runtime_error` patch（🔴 P0，A-C 补丁）
- **现状**：失败路径(`worker.py:261-264`) 只 `mark_failed`→写 `agent_jobs.last_error`(`jobs_store.py:131-137`)。**不发** error 状态事件（`status_stream._KIND_LABEL` 只有 processing/reading_*/writing_reply/done，无 error kind，`status_stream.py:44-55`）；**不 patch** `last_runtime_error`（该字段只有 legacy `hosted/config_store.py:240-242,257-259` 写，v2 从不调用）。iOS 错误 chip 读 `profile.last_runtime_error`(`hosted/setup_core.py:265`)→v2 失败对 iOS 完全不可见。
- **改法**：`status_stream` 加 `error` kind；失败 except 块发 error 状态事件 + 调 config_store patch `last_runtime_error`（含分类后的用户可读文案）。区分 transient(重试) vs terminal(才落 error)。
- **验收**：坏 key/上游 403 → iOS 出错误 chip，不是静默停。

### 1e — provider 调用异步化（🔴 P1，A-C 补丁）
- **现状**：`responder.respond`(`responder.py:103`) 与 `planner.official_plan`(`planner.py:182`) 走**同步** `provider_client.reliable_chat_completion`(sync `httpx.Client`,`provider_client.py:115-125`)，经 `asyncio.to_thread`(`worker.py:204,243`)。`to_thread` 用默认 executor→并发被卡 **~32**，与 §6「slot 是 async task，worker 数是 dial」矛盾。异步版已存在(`chat_completion_async`/`AsyncClient`,`provider_client.py:1158,1178`)但 v2 没用。
- **改法**：v2 responder/planner 改调 `chat_completion_async`；或显式放大线程池并文档化上限。优先前者。
- **验收**：并发 turn 数可超 32（受 ENCLAVE_SEMAPHORE/worker dial 约束，而非线程池隐性封顶）。

---

## 条件 2 — 完整对话（🔴 D / 重构）
- **现状**：模型**只看到 pending 未回 user 消息**——`coalesce_pending`(`coalesce.py:41-72`) 只收 `role∈{user} && ts>since`；`responder._build_messages`(`responder.py:66-84`) 仅用这些 + 可选 action 说明，无 assistant 轮、无 verbatim tail、无 summary；producer `_read_messages`(`serve_worker.py:113-118`) 只读上一条 assistant 回复之后的行。**无任何压缩/summary job**（v2/ 内 grep 只有 `action_digest` 和 last-6 截断的 `recent_chat_digest`,`worker.py:200`）。小克确实「忘了刚才在聊啥」。
- **改法（walkthrough §5）**：persona 摘要 + **加密的 itemized summary（append-and-merge，禁全量重写）** + **verbatim tail** 三层进 prompt；tail 超预算时由 maintenance-lane job（用户 key，冷路径）把最老消息折进 summary 并重加密；配 `cache_control` 缓存前缀。
- **归属**：重构级，**入子项目 D**（也可拆成 A-C 之后的独立 spec）。

## 条件 3 — 真 agent loop（🔴 D / 重构）
- **现状**：`process_job`(`worker.py:192-248`) 控制流 = coalesce→`planner.plan`(一次 JSON)→`executor.execute_plan`→`responder.respond`；`while` 只在新消息触发 REPLAN(≤`replan_budget`,`worker.py:227-235`)时重跑，**非**模型驱动迭代。planner 用 `response_format=json_object`(`planner.py:182-187`)，`reliable_chat_completion` **从没传 `tools=`**；无 mid-turn `reply` 工具（reply 是独立终态 step）。`progress.md:65` 自己已标注 executor 无 mid-plan checkpoint。
- **改法（walkthrough §4）**：`provider_client` 加 `tools=` 原生 tool-calling；一个 loop 服务所有模型（round budget + timeout 约束），每轮可并行 tool batch、可调 mid-turn `reply`；把现有「一次性 JSON plan / 仅凭 prefetch 回答」降级为**每轮 fallback**（由坏 tool-call 触发，而非预分 tier）。
- **归属**：重构级，**入子项目 D**。

---

## 条件 4 — 统一动作词表（🔴 A-C 补丁）

### 4a — planner 词表 ↔ registry 对齐（🔴 P1）
- **现状**：planner 词表(`planner.py:22-25` + prompt `114-118`) 含 `capture_memory/schedule_followup/schedule_wake/cancel_wake/sleep`，**全不在** `registry.CAPABILITIES`(`registry.py:11-25`)；executor 归为 control/unknown 后**静默 skip**(`executor.py:28-31,48-56`，标 `skipped`，从不执行也不报失败)。（注：C5 修复把「失败」改成了「跳过」，消了噪音，但 planner 仍在承诺永不兑现的动作——**未真正 reconcile**。）
- **改法**：二选一或并用——(i) 把 schedule/capture/sleep 做成**真 capability**（或映射到既有 control-action 执行路径）；(ii) 从 planner 词表**删掉**无对应能力的动作。并把每一行落进 parity matrix(`docs/superpowers/specs/runtime-v2-parity-matrix.md`)。
- **验收**：planner 能吐的每个 action_type 都有 registry 或 control-handler 对应；无「承诺了但 drop 在地上」。

### 4b — `web_search`/`web_fetch`（🔴 P1）
- **现状**：`capabilities/` **无** web 能力（仅 legacy `hosted_runtime.py`/`model_api_runtime/tools.py` 有）。walkthrough Phase 1 明确「今天 runtime 有 web，V2 必须也有」。
- **改法**：新增 `capabilities/web.py`（薄 facade over 既有 web 实现），注册进 registry，进 parity matrix。

### 4c — `memory_search`（🔴 P2）
- **现状**：`capabilities/memory.py` 只有 `index/fetch/write`(`memory.py:30-44`)，无关键词/grep 搜索。
- **改法**：新增 `memory_search`（enclave read-side grep over memory cards，TEE-PG 迁移后变一条 SQL）。walkthrough 注明这是**优于 parity**（今天 runtime 根本不能搜 memory）。

---

## 条件 5 — 部署目标钉死（✅ 文档已完成；进程启动归 D）
- **已做（Step1-T7）**：(i) `serve_worker.py` 模块 docstring 顶部加「部署目标（已钉死）」段——同 backend 镜像的兄弟入口、跑在 runner CVM（agent-runner supervisor）内、与常驻 consumer+genesis worker 并肩，**非**独立 HTTP 服务/**非**贴主 app FastAPI；`main()` 里含糊的「may run in a separate process/CVM/pod」改成「its own entrypoint in the runner CVM」（保留 schema-init 的防御理由）。(ii) `DEPLOYMENTS.md` runner-CVM 段加一行 "Hosted Runtime V2 worker pool (planned)"，钉死 where=runner CVM、注明 manifest 未接、进程启动归 D。B/C 矛盾已消、含糊已清。
- **留给 D**：deploy manifest/compose 里加 `serve_worker` 容器/命令（rollout flag gated）+ `hosted_runtime_mode` 逐用户切——本条只钉「跑在哪」，D 接「真的跑」。

---

## 条件 6 — 子项目 D 需求写进文档（🔴 P1，本 backlog 起头 + D spec 落实）
六项一处未记（grep `docs/superpowers/**` + `.superpowers/sdd/progress.md` 全 0 命中）。在此登记，待 D spec 展开：
1. **prompt caching**：`provider_client` 加 `cache_control`，稳定前缀正好是可缓存形状；弱中转优雅降级为无缓存。
2. **tokens/turn vs resident**：同 fixture 上对比 resident 运行时；**输了 = rollback 条件**（walkthrough Phase 6）。
3. **admission ceiling**：见下条独立项。
4. **dream-lane 失败轨迹复盘**：每回合写 durable trajectory，失败回合→回归 fixture。
5. **typing-signal 预热**：iOS「正在输入」信号触发投机 prefetch（解密对话/暖 memory index/暖 prompt cache），send 落地时 turn 已热，省 1–2s。
6. **关掉 resident 进程**：wake 迁到 job 行后停掉 per-user 常驻进程——**并发/内存收益在此兑现**，不在 A-C。

## §6 — send 路径 admission ceiling（🔴 D）
- **现状**：`db_action_v2` 分支(`chat_send_core.py:151-160`)**无条件** persist+enqueue，无 est-wait/SLA/队深闸；唯一背压是 `enqueue_job` 的单飞 coalesce(`jobs_store.py:56-71`)。
- **改法（walkthrough §6）**：send 前算 est-wait = 前面 job 数 × 滚动均服务时长；≤SLA 则 admit（persist+enqueue，回「thinking」）；>SLA 则**在任何持久化之前**回独立「busy」响应（区别于 supervisor-dead 503）。
- **归属**：子项目 D（属容量/背压层，与 wake/压测同批）。

---

## 下一步（本 backlog 之后）

- **第 1 步（机械项，A-C 补丁）**：1a / 1c / 1e / 4a / 4b / 4c / 5(文档) / 6(本文档已起头)。走 subagent-driven-development，每 task 实现+审查+修，NO-COMMIT。
- **第 2 步（重构，立新项）**：条件 2 + 3（+ §6 admission ceiling + 其余 D 项）→ 新一轮 brainstorm→spec→plan，即**子项目 D**。
