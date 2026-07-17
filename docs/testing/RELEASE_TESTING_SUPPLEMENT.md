# 发版测试方案 · 补充篇（体感 Block + 蒸馏/加密/onboarding 细分）

> 2026-07-18 · Claude 补。**不替代**、只补 `RELEASE_TESTING_PROTOCOL.md`（框架/能力矩阵/
> P0·P1/事故库）与 `TESTING.md`（改什么测什么）。本篇来自一批**不同的 bug**——长期记忆
> 日期丢失、回复语言漂移、主动消息刷屏、解密连续性、coding-agent 拒演角色、共享接缝
> "改 A 坏 B"——它们的共同点：**能通过所有功能测试（聊天有回复、job done），却照样毁
> 体验或让用户根本读不到内容**。功能测试查"能不能用"，本篇查"用起来对不对、爽不爽"。
>
> 每节末尾标了 **【并入点】**，方便日后折进主文档。

---

## S1. 体感 Block 回归类（Seven 第二优先级：不崩但难用）

主文档 P0/P1 基本是 pass/fail 的功能项。下面这些**全绿也可能是灾难**，每类都栽过真实用户。
发版回归时，除 P0/P1 外，**至少在 claude 官方 + pi 中转 两个代表配置上各扫一遍本表**。

| 类 | 症状（用户视角） | 根因家族 | 怎么测（test 环境） | 出处 |
|---|---|---|---|---|
| **回复语言漂移** | 设了英文/写了英文人设，AI 却时不时冒中文（或反之） | 无硬语言锚；上下文里中文脚手架/记忆/OCR 稀释了人设软指令 | 人设+记忆写语言 X → 连发 3 条，**每条都必须 X**；再发一条语言 Y 的消息 → 该轮镜像成 Y；proactive/背景轮仍回 X。**两路都测**（model_api + resident），并查 resident 时间锚不再是硬编码中文 | reply-language plan；`backend/chat/reply_language.py` |
| **主动消息刷屏/自激励循环** | AI 短时间连发一堆主动消息、越发越多 | self-wake 无 floor、报错不退避、闹钟定太密、job 无 max-age 补跑陈年任务 | 造一个会触发 proactive 的场景，观察 **单位时间发送量有上限**、报错后**退避而非立刻重试**、self-wake 被 floor clamp、堆积 job 到期转 `expired` 而非补跑 | usr_f13f922a（37 条里 20 条 proactive）；prod §5 |
| **消息重复（两族）** | 用户看到重复消息 | ①重发双份（发送侧）②回复侧双条（同一轮出两条） | 发 1 条 → 恰好 **1 个 user row + 1 条 agent 回复**；查 `by_source` 计数、每条回复独立 thinking。两族分诊：看是"两个 user row"还是"一个 user row 两条 agent" | 消息重复事故；`incident-diagnosis-lessons` |
| **时间/日期正确性** | 主动消息说错时间；上传的长期记忆日期全变今天 | hosted agent 拿 UTC 非用户时区；蒸馏丢 occurred_at | 托管账号设非-UTC 时区 → agent 被问"现在几点"答对；长期记忆带 YYYY-MM-DD 上传 → 记忆卡 occurred_at **原样保留**（本次已加 e2e） | be72842（时区）；LTM date fix（bf483fc7） |
| **解密连续性（最隐蔽）** | AI 一直在发消息，但用户屏幕上是乱码/空白，根本读不到 | 用户侧信封解不开，而后端只知道"回复已发出" | **P0 不能只验"回复到达"，必须验用户能真解密**：E2E 用账号私钥 `open_envelope(reply)` 成功且明文非空。解不开=硬 fail | usr_f13f922a（解密失败但 AI 狂发）；`E2EClient.open_envelope` |
| **onboarding/首屏初始态** | 新用户进来卡在进度页、主页身份卡空、天数算错 | fresh 态 gate 误拦、身份卡字段无 fallback | fresh 新号：进度页→直接进 app、主页身份卡空时逐字段 fallback（初始名"TA"）、天数第 1 天起算 | ios-onboarding-initial-state（533e83b） |

> **判据原则**：本表每一项，"聊天能收到回复"都**不是**通过标准。通过标准是括号里那句具体的
> 体感断言。别用"跑通了"糊弄过去。

### 阻断级别（Seven 2026-07-18 定）

- **解密连续性 = 硬 P0**：与聊天回环同级。**解不开就是不许合 main**，修复后重跑。理由：用户
  屏幕上是乱码=产品对该用户完全不可用，比"回复慢/串语言"严重一个数量级；而且它最隐蔽
  （后端一切正常，只有用户端能发现）。**落地动作**：P0 步骤 2 的通过标准从"≤120s 收到 agent 回复"
  升级为"≤120s 收到 **且用账号私钥 `open_envelope` 解出非空明文**"。
- **其余体感项（语言漂移 / 主动刷屏 / 消息重复 / 时间日期 / onboarding 首屏）= P1**：进 P1 半自动
  清单，`❌` 由 Seven 决定阻断或带票发版。放 P1 而非 P0 的原因：部分项（如语言镜像是否恰当）
  判定需人工体感，自动化硬阻断会误杀。

**【并入点】** → 主文档新增"§4.5 体感 Block 回归"承接 P1 五项；**P0 冒烟集（§3）步骤 2 立即
升级**为"收到且能解密"（这一条是硬 P0，先落）。

---

## S2. 蒸馏（genesis/distill）子矩阵——"云端蒸馏"不是一格

能力矩阵里"云端蒸馏（上传文件）"是**一个 ✅**，但蒸馏实际是 **2 入口 × 2 通道 × N 行为**，
每个维度都**独立**坏过。本次 LTM date+tags 修复一口气碰了这里的大半格子。

**两个入口**（都默认走 genesis 蒸馏，`useGenesisOnboarding ?? true`）：
1. **Onboarding** 首次设置上传（云端用户可传 memory_summary；VPS 用户 onboarding **不传文件**，AI 直接写）
2. **Garden 记忆还原** 进 App 后二次补充上传

**两条写入通道**：
- **云端（plaintext）**：API-key/model_api 用户，服务端蒸馏，`backend/genesis/service.py`
- **自托管（sealed）**：VPS 用户，本地蒸馏，`tools/chat_resident_consumer.py`

**要逐格验的行为**（每格 = 入口 × 通道 × 行为）：

| 行为 | 通过标准 | 易漏点 |
|---|---|---|
| 长期记忆**日期保留** | 卡 occurred_at == 源卡 YYYY-MM-DD，不塌成今天 | 只在 `material_kind=memory_summary` 开；onboarding 混合上传要**逐卡**判定（`_source_family` 标记），别整批开 |
| **标签→threads** | 源卡 tags 播种进 threads（AI 可重组，但不丢） | 同上 gating |
| 聊天记录**正常蒸馏** | history 素材**不**触发日期保留，走常规 | 别让全局 schema 的 occurred_at 泄进 history 卡 |
| **去重** | known_memories 语义去重 + 保守词法兜底，不合并"美式/拿铁"同模板事实 | 阈值太低会误并 |
| **混合素材分族**（onboarding） | 同批 memory_summary + history + persona，只有 memory_summary 卡拿日期精修 | merge 收敛成 `source_family=merged` 会丢族信息——靠 per-item marker |
| **空素材话术** | 传空/无效文件 → 具体文案（`material_empty`），不是通用 invalid_request | iOS 侧要有对应 copy 映射 |
| **蒸馏中插聊天** | 蒸馏进行中发消息 → 聊天先回、蒸馏续跑不重不丢 | resident 逐窗游标；5 卡零重复 |
| **首轮冷启动** | runner 首轮可能丢——重试或预热 | test-env recipe 已记 |

**最小 E2E**（云端，已产品化在 `tools/e2e/`，本次用 `/tmp/smoke_ltm_dates.py` 验过）：
provision(model_api) → setup → `POST /v1/genesis/imports/plaintext {memory_summary_content, mode, material_kind}`
→ poll `GET /v1/genesis/imports/{job_id}` 到 done → `POST /v1/memory/index` 读 occurred_at + threads → teardown。

**【并入点】** → 能力矩阵"云端蒸馏"一行拆成上面的行为子表；P1 #1（onboarding 蒸馏）补"混合素材
逐卡日期"与"空素材话术"两个子项。

---

## S3. 共享接缝 → **双路必测**（根治"改 A 坏 B"）

Seven 最担心的"改一个功能联动坏另一个"（7f3ff266 改 hosted 顺手弄坏 resident，4 天无人发现）
**几乎全部**发生在**两条用户路径共用的一段代码**上。规则很简单：

> **动了下列任一"共享接缝"，L2 回归必须 model_api + resident 两路都跑，不许只测一路代表全部。**

| 共享接缝 | 文件 | 一改就双路受影响的原因 |
|---|---|---|
| 蒸馏事实写入 | `backend/genesis/worker.py::_fact_write` | 云端和自托管**都过它**——本次 LTM 一个 prompt 改动同时影响两路 |
| 上下文/prompt 组装 | `backend/hosted/context.py`、`backend/genesis/prompts.py` | 语言锚、记忆注入两路共享 |
| 回复语言策略 | `backend/chat/reply_language.py` | 明确设计成两路共用同一 helper |
| 信封加解密 | `content_encryption.py`、`enclave_app.py` | 两路同一套 box_seal；改了 = 所有用户解密受影响 |
| 记忆动作 | `backend/memory/actions.py` | capture/dream/genesis 共用写卡 |
| proactive 核 | `backend/proactive/*` | 频控/floor 两路共用 |

**自查话术**：改动前问自己——"这段代码**只有** model_api 用户会跑到吗？"答不上"是"，就双路测。

**【并入点】** → 主文档 §5（VPS harness × 功能矩阵）前面加这张"共享接缝"表作为**触发器**：
命中即强制双路。

---

## S4. "代码合了 ≠ 跑着的进程/CVM 生效"——给出**硬验证命令**

`TESTING.md §6` 说了"git pull ≠ 生效"，但没给"怎么确认**部署态真的是我的代码**"。本次 LTM 上线
就是靠这个确认的：

- **后端 CVM 版本**：`curl -sk https://test-api.feedling.app/healthz` → `release.git_commit` **必须 == 目标 SHA**。
  （本次：`bf483fc7…` 对上才开跑冒烟；uptime_s 也能看部署时点。）
- **runner CVM 镜像**：CI 自动 bump `docker-compose.phala.*.yaml` 的 image tag 到 `:<sha7>` 并 `[skip ci]`
  推 test/main；`phala inspect` 确认 image tag == 目标 hash。
- **resident consumer 版本**：admin user detail 里 consumer header 的 commit，随 chat-poll 的
  `expected_consumer_commit` 自更新；改了 consumer 必 `systemctl --user restart feedling-chat-resident`。

> 铁律：**跑 E2E 前先 `/healthz` 对 git_commit**。对不上就是还没部署完，此刻的任何"失败"都是假阴性。

**【并入点】** → 主文档 §6（L3 生产验证）固定项 + `TESTING.md §5` 部署态标准动作第 0 步："先对 git_commit"。

---

## S5. E2E harness 实操坑（拿来即用，少踩一遍）

做 E2E 时反复栽、文档里查不到的：

- **公钥是 base64 不是 hex**：`/v1/users/register` 的 `public_key` 走 `base64.b64encode(bytes(sk.public_key))`；
  传 hex 会 409/invalid pubkey。
- **model slug 会 404**：厂家会下线旧名（`anthropic/claude-3.5-haiku` → "No endpoints found"）；
  setup 前用 provider 的 `/v1/models` 核一下，或 setup 里多给几个候选按序试（`HostedCell.models`）。
- **job_id 在 `body["job"]["job_id"]`**：genesis 上传返回是**嵌套**的，别按 top-level 取。
- **runner 首轮冷启动会丢**：model_api 第一条可能因 per-user spawn 慢/丢——`FIRST_REPLY_TIMEOUT=300s` 且允许重试；别拿首轮失败下结论。
- **admin trace stdout excerpt 截断**：1000 字节（`TESTING.md` 记的），读 trace **过滤 `ts > 你发消息的 ts`**，别读到上一轮 proactive 旧事件。
- **账号纪律**：`E2EClient` 用 context manager，`__exit__` 保证 `POST /v1/account/reset {"confirm":"delete-all-data"}`；
  teardown 失败会打 WARNING——**看到 WARNING 必须手动删**，否则留孤儿账号（无 admin 删除口）。
- **`~/.feedling-e2e-keys.env`**：chmod 600、永不入 git；缺某家 key 的格子报 SKIP 不报 FAIL。

**【并入点】** → 主文档 §1.3 测试账号纪律 + 新增"§1.4 harness 已知坑"。

---

## S6. 一页纸：发版前我会额外问的 6 个问题

主文档回答了"跑哪些用例"。基于我踩过的坑，发版前我还会强制自己回答：

1. **体感**：回复语言对吗？会不会刷屏？用户**真能解密**读到吗？（S1）
2. **蒸馏**：日期/标签保住了吗？两个入口、两条通道都验了吗？（S2）
3. **联动**：碰的是共享接缝吗？两路都跑了吗？（S3）
4. **生效**：`/healthz` 的 git_commit 对上目标 SHA 了吗？（S4）
5. **分层**：模型家族/harness 分层验了吗，还是拿一个代表了全部？（主文档 §5 + `TESTING.md §6`）
6. **收尾**：测试账号删干净了吗？事故有没有落一条永久回归用例？（主文档 §7 + §1.3）
