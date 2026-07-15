# Batch 4 — 清旧 floor 叙事 + promotion gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 服务端不再对外输出任何旧 floor 硬门槛叙事(memory/verify 旁路、validate/gates 的 floors/counts/missing_tabs、history-import floor_reference);io-onboarding 补 skill promotion gate 清单。

**Architecture:** 保留【新】guidance-only 信号(`memory_floor`/`memory_below_floor`/hint,新曲线);删【旧】per-tab 硬门槛输出。`/v1/memory/verify` 重写为 guidance-only(passing 恒 True,不再输出 "identity_init will 409 until…")。iOS 不解码被删字段(spec 已核),直接删。

**Tech Stack:** Python 3.12,pytest,uv;io-onboarding 仓(markdown)。

## Global Constraints

- **只删旧硬门槛叙事,保留新 guidance 信号**(`memory_floor`/`memory_below_floor`/`hint` 是 Batch 1.5/2 的正牌产物,别误删)。
- **iOS 兼容**:spec 已核 `OnboardingValidationStep` 不解码 floors/counts/missing_tabs(Swift 忽略多余字段→删除安全);bootstrap status 的 `memory_floor`/`memories_count` 保留。
- **iOS 的 stuckPrompt "Pass 1-4" 文案本期不动**(hx 明确;记入遗留)。
- 优先 onboarding 成功率:清完后不允许出现任何"卡数不够→拒绝/409"的语句或行为。
- 工作目录 `/Users/hx/Projects/io/feedling-mcp-batch2`,分支 `feat/onboarding-batch4-floor-cleanup`(基于 batch3 tip);io-onboarding 在 `/Users/hx/Projects/io/io-onboarding-vps-unify`(新分支,基 origin/main)。

---

### Task 1: `/v1/memory/verify` 重写为 guidance-only

**Files:**
- Modify: `backend/memory/memory_core.py::verify`(~L452-520)
- Test: 更新 `tests/test_bootstrap_gates.py` 的 verify 测试(含已 skip 的 per-tab 语义测试可整理)

要点:
- 响应保留 keys:`counts`(现状)、`passing`(**恒 True** —— memory 不是门)、`below_floor` 改为基于**新总量曲线**的单一信号(`{"total": bool}` 加 `memory_floor`/`memory_below_floor` 平铺字段;per-tab 三键保留但恒 False,响应形状兼容)。
- **删除**所有 "identity_init will 409 until…"、"Pass 1-4"、per-tab 配额措辞;suggestions 改为 guidance 语气(低于新曲线时一条:"参考下限 N,材料真实支持的事实尽量都写;绝不编造"),不再暗示任何 gate。
- `passing_full` 同样恒 True(或删除——grep 消费者,无人读则删)。

### Task 2: validate/gates/history-import 旧字段清理

**Files:**
- Modify: `backend/hosted/onboarding_validation.py`(memory_garden step 删 `counts`/`floors`/`missing_tabs`;保留 `memory_count` + `_memory_floor_fields(...)` 产物)
- Modify: `backend/bootstrap/gates.py`(`_bootstrap_state` 删 `floors`/`counts`/`missing_tabs` 输出与 docstring;409 body 里对应字段删除;`memory_floor` 保留=新总量)
- Modify: `backend/hosted/history_import.py`(~L2341 `floor_reference` 删除)
- Test: 跑受影响的 validate/gates/history 测试并更新断言(grep 引用被删字段的测试)

要点:`_bootstrap_state` 的 `memory_floor` 从 `floors["total"]` 改为直接 `memory_service._memory_floor_for_days(...)`;`status_core` 消费处同步。**先 grep 每个被删字段的全部读者**(backend + tests + tools),读者清零才删。

### Task 3: io-onboarding promotion gate + 残余 floor 叙事扫尾(io-onboarding 仓)

**Files:**
- 仓:`/Users/hx/Projects/io/io-onboarding-vps-unify`,新分支 `docs/promotion-gate` 基 origin/main
- Create: `PROMOTION.md`(test→main 的 promote 清单:①test 车道真机验过 ②self-ref URL 与目标车道一致(main 上全指 main)③consumer 分支指向核对 ④diff 审阅人)
- Modify: README 加一行指向 PROMOTION.md
- 扫尾:grep 全仓 `floor|Pass 1|four-pass|four pass` 残余叙事(quickstart/troubleshooting 已重写过,验证即可;有残余则清)

**注**:此仓合 main 的时机同样归 hx;本任务只出分支不推。

### Task 4: 回归(inline)

- 对齐最新 origin/test → 全量 L1 对照预存清单零新增 → pyflakes 零新增。

---

## Self-Review(已跑)

- Spec A5 覆盖:memory/verify(Task 1)、validate/gates/history floor_reference(Task 2)、docs/skill 残余(Task 3)、provider/tool descriptions(Task 2 grep 顺带,当前扫描未见 floor 措辞,执行时复核)。B1 promotion gate(Task 3)。iOS stuckPrompt 明确不动(全局约束)。
- 保留清单明确:新 guidance 信号三件套不许删。
- 响应形状风险:per-tab 键保留恒 False(verify)+ validate/gates 直删(spec 已核 iOS 不解码)。
