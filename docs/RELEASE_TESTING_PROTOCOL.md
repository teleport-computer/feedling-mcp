# Feedling 发版测试方案（Release Testing Protocol）

> 2026-07-17 · Seven 定框架，Claude 落地。与 `docs/TESTING.md`（开发循环的
> "改什么测什么"决策矩阵）互补：**TESTING.md 管每次改动，本文档管每次发版
> 和每个新功能**。
>
> 背景：近期 MCP 一个功能连出多个 bug（pi 完全无插件集成却已上生产、VPS 用户
> 连不上）、7f3ff266 改 hosted 顺手弄坏 resident 四天无人发现。三类事故对应
> 三层缺失：功能×环境能力矩阵、跨环境 E2E、发版全量回归。本方案补齐这三层。

## 0. 总览：四层 + 两横切

| 层 | 触发 | 执行者 | 产出 |
|---|---|---|---|
| **L0 开发循环** | 每次改动 | 写代码的人/agent | `TESTING.md` §2 对号入座，PR 附证据 |
| **L1 功能验收** | 新功能合入前 | 功能作者 + 双签者 | 能力矩阵申报 + ✅格子逐格 E2E |
| **L2 发版全量回归** | 每次 test→main 前，Seven 喊"跑发版回归" | **Claude**（P0 自动 + P1 半自动），真机项人工 | 结果表，P0 挂 = 不许合 |
| **L3 生产部署验证** | 每次 prod deploy 后 | Claude 照单验收 | 结果表回报 |
| **横切① 能力矩阵** | living doc（§2） | 新功能 PR 必更新（门禁） | 一张表看清每个功能在哪些环境可用 |
| **横切② 事故案例库** | 每次 prod 事故后 | 排查者 | 事故 → 永久回归用例（§7） |

执行约定：**不进 CI**。Seven 手动触发，Claude 跑完出报告。报告格式见 §8。

---

## 1. 测试环境与资源

### 1.1 环境

| 环境 | 用途 | 入口 |
|---|---|---|
| 本地 pytest + 真 PG | L0/L1 单元与集成 | `FEEDLING_TEST_PG=postgresql://…@127.0.0.1:5432/postgres`，`env -u FEEDLING_API_KEY -u FEEDLING_API_URL python3 -m pytest` |
| **test 环境** | L1/L2 全部 E2E | `https://test-api.feedling.app`；enclave `https://173c7f49aeb54acb424676b17b17f78e5e2b2938-5003s.dstack-pha-prod9.phala.network`（in-enclave TLS，verify=False） |
| prod | L3 验收 + 只读排查 | `https://api.feedling.app`（admin 只读；**绝不在 prod 造测试数据**） |

### 1.2 Provider key 池（Seven 已批全套建池）

六类，覆盖真实用户的全部接入方式：

| # | 类别 | driver | 备注 |
|---|---|---|---|
| 1 | Anthropic 官方 | claude | |
| 2 | OpenAI 官方 | codex | |
| 3 | Gemini 官方 | pi | |
| 4 | OpenRouter | pi | 官方聚合器代表 |
| 5 | 中转站代表（openai_compatible） | pi | 流不稳/模型名带标签的典型场景（usr_6f5a 类）；选一家真实用户在用的 |
| 6 | DeepSeek 官方 | claude(base override) | `ANTHROPIC_BASE_URL={base}/anthropic` 路线 |

- 存放：本地 `~/.feedling-e2e-keys.env`（chmod 600，**永不入 git**）；格式
  `E2E_KEY_ANTHROPIC=… / E2E_KEY_OPENAI=… / E2E_KEY_GEMINI=… /
  E2E_KEY_OPENROUTER=… / E2E_KEY_RELAY=…（含 E2E_RELAY_BASE/E2E_RELAY_MODEL）/
  E2E_KEY_DEEPSEEK=…`。
- 建池动作（待 Seven/志豪提供 key）：额度各留最低档即可，P0 单轮消耗很小。

### 1.3 测试账号纪律（test-account-hygiene）

- 每轮 E2E 现场注册、跑完 **当场 `/v1/account/reset`（confirm=delete-all-data）删除**；
- keypair 用 `tools/e2e/` 生成并只存本轮临时目录；
- 绝不复用真实用户账号，绝不在 prod 建号。

---

## 2. 能力矩阵总表（living doc，门禁对象）

**规则（Seven 定：硬门禁 + 产品提示）**：
1. 新功能 PR **必须**在下表加行并逐格申报；漏填 = review 直接拒。
2. 申报 ✅ 的格子必须有对应 E2E 证据（L1）。
3. 申报 ❌ 的格子必须有**产品侧提示**：后端按 active driver 返回
   capability 错误/警告 + iOS 显示"当前模型路线暂不支持"。做不到提示的不许上生产。
4. ❓ 只允许存在于存量行，出现即建任务限期核实。

**托管（model_api）**：

| 功能 | claude(官方) | codex(官方) | pi(Gemini/OR/中转) | deepseek(claude+base) |
|---|---|---|---|---|
| 聊天回环（文字） | ✅ | ✅ | ✅ | ✅ |
| 图片消息 | ✅ | ✅(--image) | ✅(@path) | ❓ |
| 思维链展示 | ✅ | ✅ | ✅(thinking) | ❓ |
| 记忆花园（capture/dream/index） | ✅ | ✅ | ✅ | ❓ |
| 身份卡（init/replace/patch） | ✅ | ✅ | ✅ | ❓ |
| 主动唤醒（心跳/定时/照片/屏幕） | ✅ | ✅ | ✅ | ❓ |
| 感知信号（io_cli 18 signals） | ✅ | ✅ | ✅(-t bash) | ❓ |
| **用户 MCP** | ✅ | ✅ | **❌ 无 pi 桥（志豪修，docs/PI_USER_MCP_GAP）** | ❓ |
| 云端蒸馏（上传文件） | ✅ | ✅ | ✅ | ❓ |
| 错误气泡分类 | ✅ | ✅ | ✅ | ❓ |

**VPS 自托管（resident）**（OpenClaw 暂免——无用户，Seven 定）：

| 功能 | Claude Code | Codex | Hermes |
|---|---|---|---|
| 聊天回环 | ✅ | ✅ | ✅ |
| 会话续接 | ✅(--resume，2026-07-17 修复) | ✅(transcript 注入) | ✅(--resume) |
| resident 蒸馏（本地自蒸馏） | ✅ | ✅ | ❓ |
| 用户 MCP | ✅(--mcp-config) | ✅(config.toml) | ❓ |
| 主动唤醒 | ✅ | ✅ | ✅ |
| 图片消息 | ✅ | ✅ | ❓ |

> ❓ 存量待核（建任务）：deepseek 全列、Hermes 的蒸馏/MCP/图片。核实后改
> ✅/❌，❌ 的补产品提示。

---

## 3. P0 冒烟集（全自动，Claude 执行，~30-45 分钟）

**目标**：每个 driver 一个临时账号，把"用户能不能正常用"的最小闭环跑通。
**工具**：`tools/e2e/`（由 2026-07-17 蒸馏 E2E 的脚本产品化而来：provision /
upload_material / send_chat / run_consumer / teardown）。
**触发**：Seven 说"跑发版回归"→ Claude 依次执行下表；任何一格挂 = P0 fail =
**不许合 main**（修复后重跑）。

每个 key（§1.2 的 6 类）各跑一遍：

| 步 | 动作 | 通过标准 |
|---|---|---|
| 1 | 注册临时账号（对应 route/driver），配 provider key，`/v1/model_api/setup` test | test_status=ok |
| 2 | 发一条文字消息 | ≤120s 收到 agent 回复，无协议碎片泄漏 |
| 3 | 追问一条（上下文连续性） | 回复能接上文（会话/注入正常） |
| 4 | 触发一次记忆写入（明确给一个事实） | `/v1/memory/index` 出现对应卡 |
| 5 | 错误气泡 sanity：故意发超长/停 key 场景跳过，仅检查无 unknown 类气泡出现在本轮 | 聊天里 0 条 system 气泡 |
| 6 | 删号 | reset 200 |

**VPS 侧 P0**（本地起 consumer 连 test 环境，三个 harness 各一遍）：
Claude Code / Codex / Hermes 各：注册 resident 账号 → 本地 consumer 起 →
verify_loop passing → 发消息收回复 → 删号。

## 4. P1 全功能清单（Claude 半自动执行，1-2 小时）

在 **claude 官方 + pi 中转站** 两个代表性配置上（一个最稳、一个最刁）过全表；
其余 driver 若 P0 全绿则免。逐项 ✅/❌/跳过+原因。

| # | 功能 | 验法（test 环境） |
|---|---|---|
| 1 | 上传路 onboarding（蒸馏→身份→首条问候） | sealed 上传 40k 文档 → job done、卡片内容正确无重复、身份卡生成 |
| 2 | fresh 路 onboarding（零导入直接聊） | 新号不上传直接发消息 → 正常回复，无 gate 误拦 |
| 3 | 聊天：文字/图片/文件 | 三种消息各一条，回复正确引用内容 |
| 4 | **蒸馏中插聊天**（2026-07-17 案例） | 蒸馏进行中发消息 → 聊天先回、蒸馏续跑不重不丢 |
| 5 | 记忆花园全链路 | capture 落卡 → index 可查 → Garden 引用（quoted memory）进对话 |
| 6 | 做梦（dream） | 触发 dream job → 卡片 thicken/supersede 正确 |
| 7 | 身份卡操作 | 改名/signature patch → 生效且不触发误重建 |
| 8 | 主动唤醒 4 lane | 心跳/定时/照片/屏幕各触发一次 → 消息符合人设、频率受控 |
| 9 | **用户轮优先**（fix ① 案例） | 主动批处理中发消息 → 最多等一个模型轮 |
| 10 | 感知信号 | agent 被问"我现在在哪/天气" → io_cli 调用成功 |
| 11 | 用户 MCP（矩阵 ✅ 格子） | 连一个测试 MCP server → agent 能列出并调用工具 |
| 12 | 错误话术抽查 | 停用 key 发消息 → 气泡是 auth 类话术（不是 unknown/怪我们） |
| 13 | 账号生命周期 | key regenerate（数据保留）/ reset（硬删）/ recover（keypair 找回） |
| 14 | admin data-track | 用户页各区块渲染正常、时间北京、无 5xx |
| 15 | 【真机·人工】push / Live Activity / 锁屏 | Seven 或真机持有者按 Health Check 页操作 |
| 16 | 【真机·人工】iOS 端 UI 体感（打字指示、错误展示、进度页） | 同上 |

> 15/16 是仅有的人工项（需要真机）；其余全部 Claude 执行。

## 5. VPS harness × 功能矩阵 E2E（新功能触碰 consumer 时加跑）

改了 `tools/chat_resident_consumer.py` 或 agent 侧协议 → L2 之外加跑：
三 harness（Claude Code/Codex/Hermes）各把 §3 VPS P0 + 受影响功能过一遍。
历史教训：思维链问题当年就是 CloudCode/Codex/Hermes/OpenClaw 逐个测才定位
——**模型家族/harness 失败要分层测，别拿一个 harness 的结果代表全部**。

## 6. L3 生产部署验证

每次 prod deploy 后，从当次发版内容生成验证清单照单跑（模板见
`docs/PROD_DEPLOY_VERIFICATION_2026-07.md` 的结构：逐项 ✅/❌/跳过+原因、
写明"什么不算失败"）。固定项：
- `/healthz` + attestation/canary 绿；
- 抽 2-3 个真实活跃用户 admin 页看健康信号（错误气泡增量、proactive 状态分布）；
- 部署断连窗口投诉核对（单点 CVM 原地部署问题，见 usr_ed21 事故）。

## 7. 事故回归案例库（每案一条永久用例）

| 案例 | 回归用例落点 |
|---|---|
| 思维链跨 harness 不一致 | §5 分层测原则 |
| 用户轮优先级反转（usr_a7b0aba） | P1 #9 |
| resident 会话回归"来了"循环（usr_c190） | P0 步骤 3（连续性）+ 消费者测试 344 项 |
| pi 流断/退化回复（usr_6f5a） | P1 #12 + pi 中转站作为 P1 代表配置 |
| pi 无 MCP 桥（usr_6f5a） | §2 矩阵门禁本身 |
| resident 假掉线 | P1 #14 |
| DAU 历史缩水 | 快照冻结测试（已入 pytest） |
| 单点 CVM 部署断连（usr_ed21） | §6 固定项 |

**规则**：以后每个 prod 事故结案时，必须在本表加一行 + 在对应层落一条用例，
否则不算结案。

## 8. 报告格式与阻断规则

- 报告 = 一张结果表（层级/用例/结果/证据链接或日志摘录/耗时），发 Seven。
- **阻断**：P0 任何一格 ❌ → 不许 test→main；P1 的 ❌ → Seven 决定
  （阻断或带票发版）；真机项未跑 → 标注"待真机"不阻断。
- 所有 E2E 产生的测试账号必须在报告里确认已删除。

## 9. 落地顺序（待办）

1. 【Seven/志豪】建 key 池（§1.2 六类）→ 给 Claude 本地 env 文件。
2. 【Claude+codex2】`tools/e2e/` 产品化：provision/teardown/chat/upload/
   consumer-runner + 六 driver 配置矩阵 + P0 一键脚本（双签入库）。
3. 【Claude】§2 矩阵 ❓ 存量核实（deepseek 列 + Hermes 三格）。
4. 【志豪/工程师】❌ 格子的产品提示（后端 capability 错误 + iOS 文案），
   首个案例 = pi MCP。
5. 【全员约定】新功能 PR 模板加"能力矩阵已更新 + L1 证据"检查项；
   事故结案必须落 §7 案例行。
