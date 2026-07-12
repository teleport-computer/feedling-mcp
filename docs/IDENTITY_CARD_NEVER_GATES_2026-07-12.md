# Spec — 身份卡内容永不作门槛（two-path onboarding）

Status: DRAFT for claude↔codex alignment (2026-07-12)
Owners: claude = agent_runtime; codex = 纯后端(gates / genesis / onboarding_validation)
双签后方可进工程。

---

## 0. 一句话原则

**身份卡写没写、写了什么（空/非空），永远不是"能不能开口"的门槛。**
唯一的完成信号是 **genesis job `status=done`（= 前台就绪，后台补记忆之前）**；从零用户走 fresh_start，同一条信号。

## 1. 产品模型（两条入口，同一逻辑）

| | 上传材料（A） | 从零开始（B / fresh_start） |
|---|---|---|
| 交互回复 | 随时可回 | 第 0 轮即可聊 |
| 首条主动 | 前台就绪即发（不等后台记忆） | 同上（fresh_start 也过前台） |
| 身份/性格来源 | 蒸馏产出（满/半/**空**卡都算数） | 边聊边**增量长出** |
| "完成"判据 | **genesis done**（空卡也算完成） | 同上 |

## 2. 现状病灶：一个信号被三套判据各说各话

- `_bootstrap_state` 门：`_load_identity is not None` → 无卡=needs_identity(拦主动)，空卡=main_loop(放行)。
- `_identity_card_complete`：卡**非空** → 空卡/无卡都判"未完成"，面板楔死 + "Wait for non-empty"。
- `hosted_chat_ok = genesis done`：**已经**在前台就绪放行（进 App / greeting 已发）。

实测 usr_ff94557e(空卡)/usr_8ba45d12(无卡)：人都在 App 里、交互能聊，但 8ba 主动被静音、两人面板永久未完成。**首条消息触发点(genesis done)本身是对的，不用动**；要改的是前两套判据 + agent_runtime 侧的介绍触发。

---

## 3. 改动清单（按 owner）

### 【CODEX · 纯后端】

**C1. `backend/bootstrap/gates.py::_bootstrap_state`** — 拆掉身份硬门
- 删除 `identity_written → stage=needs_identity` 的分支；stage 默认 `main_loop`。
- `identity_written` 保留为信息字段（面板用），不再驱动 stage。
- `_gate_bootstrap_for_chat` 的 needs_identity 分支随之退役；A''(`_user_has_spoken`) 特判因门已拆而变为死代码，一并清理。
- **保留不动**：main_loop 分支里 resident 路的 `needs_resident_consumer` / `needs_live_connection` 活性检查（那是"有没有活 consumer 收消息"，与身份无关；model_api 本就绕过）。
- 效果：8ba 主动消息立即恢复；B 类用户从第 0 轮不再被门。

**C2. `backend/hosted/onboarding_validation.py::_identity_card_complete`** — 完成判据改为"蒸馏完成"
- 判据从"卡非空(agent_name/dimensions)" 改为 **genesis 已 done / 蒸馏已触达身份**；空卡返回 True。
- 干掉 `identity_step.required` 的 "Wait for Genesis to write a non-empty Identity Card." 及等价文案（含 :412 history-import 版本）。
- 四处调用点保持一致：约 :198 / :315 / :458 / :513。
- 保留 `relationship_anchor` / `hosted_chat` 步语义不变。
- 效果：ff/8ba 面板不再楔死，**均无需补卡**。

**C3. `backend/genesis/plaintext.py:786-790`** — 区分 provider 抽风 vs 合法空卡
- 现状：上传真实内容但没 derive 出身份 → 一律 `mark_failed(onboarding_no_identity:provider_unstable)`。
- 改为二分：
  - **(a) 可判定为 provider/LLM 抽风**（`id_warnings` 里带 provider 失败信号，即现有 `_provider_identity_failure` 命中）→ 维持 `mark_failed` 让用户重试。
  - **(b) 非抽风、材料本就无身份信号**（只有记忆）→ **照常完成**：走空卡 nameless-done + 保留 greeting，等同 fresh_start 的 nameless 允许路径，不 fail。
- 注意与 :778-785 fresh_start nameless-done 合流，避免两套语义。

### 【CLAUDE · agent_runtime】

**A1. `backend/agent_runtime/supervisor.py` 介绍触发去卡依赖**
- `_fetch_identity_plain_for_intro` 对无卡用户返回 `identity_not_found` → `_needs_introduction_identity(None)=False` → 当前无卡用户**永不**发 agent 自我介绍。
- 改为：首条主动/介绍 job 只依赖**统一信号**：`proactive_activation_ready()`(= `first_chat_ok_at`，进 App) + "尚未介绍过"。
- "尚未介绍过"的判定与身份卡内容解耦：卡存在则沿用 `self_introduction/signature` 空判；卡**缺失/空**时视为"需要介绍"（而非现在的 skip）。
- 去重：`_has_active_introduction_job` + A2 的持久标记，防止重复 enqueue。

**A2. 持久"已介绍"标记（`backend/core/store.py`，服务于 agent_runtime）**
- 新增独立于身份卡的标记（如 `proactive_settings.introduced_at`，与 `first_chat_ok_at` 同机制），介绍 job 落地成功后置位。
- 防止无卡用户因"没有 self_introduction 可写"而每轮重复自我介绍。

---

## 4. 测试（各自补，交叉审）

- **codex**：`_bootstrap_state` 不再产出 needs_identity；空卡/无卡用户 chat+proactive 不被 409；`_identity_card_complete(空卡)=True` 且四校验器一致；genesis 抽风→fail、无身份材料→done 的分层用例。
- **claude**：无卡/空卡用户 `first_chat_ok` 后能 enqueue 一次介绍 job；`introduced_at` 置位后不重复；有卡且已自我介绍者不回归。
- 全量：`FEEDLING_TEST_PG` + 系统 `python3 -m pytest`（`.venv-audit` 缺 uvicorn）。

## 5. 存量用户（拆门后自愈，均不补卡）

- usr_8ba45d12(无卡)：C1 后主动恢复；A1 后可发一次介绍。
- usr_ff94557e(空卡)：本就没被门拦；C2 后面板不再未完成。
- 部署后按 debug trace 复核：gated 归零、主动投递恢复。

## 6. 明确保留 / 不做

- VPS 活性门（needs_resident_consumer / needs_live_connection）——保留。
- genesis "绝不编造"策略——保留（空就是空，不填假名）。
- 首条主动消息触发点(genesis done / 前台就绪)——不动，已正确。

## 7. 待确认（对齐点）

- [ ] C1：拆门后是否还要留一个"记录性"needs_identity 值供面板显示，还是彻底移除 stage 概念？（建议：stage 只留 main_loop，identity 缺失用独立字段表达。）
- [ ] A1：B 类全新用户（注册即 fresh_start、零对话）是否要 agent **主动**自我介绍开口？默认按 Seven 意图=可以，靠 A2 标记防刷。若想更保守可设开关。
