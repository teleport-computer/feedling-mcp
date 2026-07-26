# Feedling 发版测试方案（Release Testing Protocol）

> 2026-07-17 · Seven 定框架，Claude 落地。与 `docs/testing/TESTING.md`（开发循环的
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

**待建（§9）：mock relay** —— key 池的"中转站代表"只能测到当天恰好发生的坏；
真实故障族（SSE 中断/假模型名/慢首 token/间歇 5xx）要用 `tools/e2e/` 的 mock
openai_compatible 代理主动注入（见 §4.7 故障注入四连）。

### 1.3 测试账号纪律（test-account-hygiene）

- 每轮 E2E 现场注册、跑完 **当场 `/v1/account/reset`（confirm=delete-all-data）删除**；
- keypair 用 `tools/e2e/` 生成并只存本轮临时目录；
- 绝不复用真实用户账号，绝不在 prod 建号。
- teardown 失败会打 WARNING——**看到必须手动删**（无 admin 删除口）；
  崩溃留下的凭据在 `~/.feedling-e2e-orphans/`，`p0.py --cleanup-orphans` 清扫。

### 1.4 harness 已知坑（拿来即用）

- 注册 `public_key` 是 **base64** 不是 hex（传 hex 409）。
- model slug 会被厂家下线 404——`HostedCell.models` 多给候选按序试。
- genesis 上传返回的 job_id 在 `body["job"]["job_id"]`（嵌套）。
- model_api **首轮冷启动可能丢**（per-user spawn）——FIRST_REPLY_TIMEOUT=300s。
- admin trace 的 stdout excerpt 截断 1000 字节；读 trace 过滤 `ts > 发送时刻`。

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
| 云端蒸馏（上传文件）※ | ✅ | ✅ | ✅ | ❓ |
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
>
> ※ **蒸馏不是一格**——实际是 2 入口（onboarding 首传 / Garden 二次补传）×
> 2 通道（云端 plaintext / 自托管 sealed）× 行为子表，每格独立坏过。
> 触碰蒸馏时按 §4.6 行为子表逐格验。

---

### 2.5 双签范围与 gatekeep 清单（审查是拦截率最高的一道测试）

**必须双签**：用户可见行为变更、共享接缝（§5 表）、并发/存储原语、加密/账号链路、
prompt 注入文本。单签豁免仅限不改变契约/流程/门禁的说明性 docs 与注释——**改变
发版规范、测试义务、API 契约的文档照样双签**。（实证：2026-07-17 一天六批，独立
gatekeep 抓出 10+ 个测试没抓到的真缺陷。）

**gatekeep 最小动作**（审者）：读完整 diff 逐 hunk 问为什么；独立跑测试（别信
提交者数字，PG 依赖用真 PG）；边界三问（空值/保留值？两个同时到？失败路径
fail-open 还是 fail-closed？）；用户可见文字的术语/归因/双语义；push 后远端复验
SHA/文件集/净 diff。

**提交纪律**（提交者）：commit 前 `git diff --cached` 与审定 diff **逐 hunk** 比对
（文件名一致不够）；进行中工作放专属 worktree；mailbox 脚本从主仓根跑，
pytest/build 必须在被审的树里跑。

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
| 2 | 发一条文字消息 | ≤120s 收到 agent 回复，**且用账号私钥解出非空明文**（解不开=硬 fail——usr_f13f 事故：AI 狂发、用户屏幕全乱码，后端毫无感知），无协议碎片泄漏 |
| 3 | 追问一条（上下文连续性） | 回复能接上文（会话/注入正常） |
| 4 | 触发一次记忆写入（明确给一个事实） | `/v1/memory/index` 出现对应卡 |
| 5 | 错误气泡 sanity：故意发超长/停 key 场景跳过，仅检查无 unknown 类气泡出现在本轮 | 聊天里 0 条 system 气泡 |
| 6 | 删号 | reset 200 |

**VPS 侧 P0**（本地起 consumer 连 test 环境，三个 harness 各一遍）：
Claude Code / Codex / Hermes 各：注册 resident 账号 → 本地 consumer 起 →
verify_loop passing → 发消息收回复 → 删号。

> **hosted 深度关**：本节 P0 是"能不能用"的浅冒烟。hosted API-key 路线的**深度认证**
> （五阶段延迟/记忆契约/语义 persona/strict V2）走 §10 的 sxysun `qa/` 引擎（PR #95）——
> 尤其验 `pre` 分支的 Runtime V2 时以 §10 为准。

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
| 14.5 | **重试路径专项** | 每个用户可见"重试"按钮=独立用例：造失败→点重试→断言端点/payload/最终**恰好一份**；同 UUID 双 POST → 1 row；发消息后不读响应直接同 UUID 重发（模拟 lost-202）→ 1 行；known gap：iOS 图片/文件重试尚无幂等键（P1 目标） |
| 14.6 | **故障注入四连**（待 §1.2 mock relay 落地） | 流断首 token 前→单次重试无感/再败气泡怪 relay；流断首 token 后→无"."退化气泡；tool call 400→降级不循环；慢首 token→打字指示不消失不误标失败。**归因必须对**：上游的错说"你的模型服务"，我们的错才说"连接模型服务时出了问题" |
| 15 | 【真机·人工】push / Live Activity / 锁屏 | Seven 或真机持有者按 Health Check 页操作 |
| 16 | 【真机·人工】iOS 端 UI 体感（打字指示、错误展示、进度页） | 同上 |

> 15/16 是仅有的人工项（需要真机）；其余全部 Claude 执行。

### 4.5 体感 Block 回归（全绿也可能是灾难的项，P1 级）

功能测试查"能不能用"，本表查"用起来对不对"。发版回归时在 **claude 官方 +
pi 中转** 两个代表配置上各扫一遍；每项的通过标准是括号里的体感断言，
**"聊天有回复"不算过**。❌ 由 Seven 决定阻断或带票。

| 类 | 症状 | 怎么测 |
|---|---|---|
| 回复语言漂移 | 设英文人设却冒中文 | 人设+记忆写语言 X → 连发 3 条每条必须 X；发语言 Y 一条 → 该轮镜像 Y；proactive 仍回 X；**两路都测** |
| 主动刷屏/自激励 | 短时连发、越发越多 | 单位时间发送量有上限；报错退避不重试；self-wake 被 floor clamp；堆积 job 到期转 expired 不补跑 |
| 消息重复（两族） | 用户看到重复 | 发 1 条 → 恰 1 user row + 1 agent 回复；分诊看"两个 user row"还是"一 row 两回复" |
| 时间/日期正确性 | 说错时间；LTM 日期塌成今天 | 非-UTC 时区问"几点"答对；带 YYYY-MM-DD 的长期记忆上传后 occurred_at 原样保留 |
| onboarding 首屏 | 卡进度页/身份卡空/天数错 | fresh 新号直进 app；身份卡逐字段 fallback（初始名"TA"）；天数第 1 天起算 |

（解密连续性原属此类，已升级为 §3 P0 步骤 2 的硬判据。）

### 4.6 蒸馏行为子表（触碰 genesis/distill 时逐格验）

| 行为 | 通过标准 | 易漏点 |
|---|---|---|
| LTM 日期保留 | occurred_at == 源卡日期，不塌今天 | 仅 material_kind=memory_summary 开；混合上传逐卡判定（_source_family），别整批开 |
| 标签→threads | 源卡 tags 播种进 threads，不丢 | 同上 gating |
| 聊天记录常规蒸馏 | history 素材不触发日期保留 | 全局 schema 的 occurred_at 别泄进 history 卡 |
| 去重 | 语义去重+保守词法兜底，同模板事实不误并 | 阈值太低误并 |
| 混合素材分族 | 同批只有 memory_summary 卡拿日期精修 | merge 收敛丢族信息——靠 per-item marker |
| 空素材话术 | 空文件 → material_empty 具体文案 | iOS 有对应 copy |
| 蒸馏中插聊天 | 聊天先回、续跑不重不丢 | resident 逐窗游标 |
| 首轮冷启动 | runner 首轮可能丢——重试/预热 | 别拿首轮失败下结论 |

### 4.7 注入文本审计（喂给模型的每个词都会出现在用户屏幕上）

usr_fee1 教训：模型是复读机——转写标签/prompt 术语/硬编码兜底文案会照搬进用户
可见产物（"记忆卡叫她'用户'"根因=转写行首写死 `user:`）。规则：

- 新增/修改任何喂给模型的注入段 → grep 禁词表：`user:`、`agent:`、裸"用户"、
  裸"TA"(指人)——出现即需论证；
- 用户可见的模型产物（记忆卡/主动消息）E2E 断言不含系统称谓；
- "名字"类字段写读双端防呆：写入端拒占位词、读取端 sanitize（单一保留词事实源），
  占位名不得遮蔽后备真名；
- 保留词双语义（产品面 TA=AI vs prompt 内部 TA=用户）维护小表，prompt 借用必须
  显式声明"仅指令内标记"；
- 改称谓/术语时全仓 grep（backend/enclave/consumer/iOS xcstrings）——usr_fee1
  第一轮就漏了 enclave readside 的 legacy 路径。

## 5. VPS harness × 功能矩阵 E2E（新功能触碰 consumer 时加跑）

**共享接缝触发器（根治"改 A 坏 B"）**：动了下表任一文件，L2 回归必须
model_api + resident **两路都跑**，不许一路代表全部。自查话术："这段代码
只有 model_api 用户会跑到吗？"答不上"是"就双路测。

| 共享接缝 | 文件 |
|---|---|
| 蒸馏事实写入 | backend/genesis/worker.py::_fact_write |
| 上下文/prompt 组装 | backend/hosted/context.py、backend/genesis/prompts.py |
| 回复语言策略 | backend/chat/reply_language.py |
| 信封加解密 | content_encryption.py、enclave_app.py |
| 记忆动作 | backend/memory/actions.py |
| proactive 核 | backend/proactive/* |

改了 `tools/chat_resident_consumer.py` 或 agent 侧协议 → L2 之外加跑：
三 harness（Claude Code/Codex/Hermes）各把 §3 VPS P0 + 受影响功能过一遍。
历史教训：思维链问题当年就是 CloudCode/Codex/Hermes/OpenClaw 逐个测才定位
——**模型家族/harness 失败要分层测，别拿一个 harness 的结果代表全部**。

## 6. L3 生产部署验证

每次 prod deploy 后，从当次发版内容生成验证清单照单跑（模板见
`docs/testing/PROD_DEPLOY_VERIFICATION_2026-07.md` 的结构：逐项 ✅/❌/跳过+原因、
写明"什么不算失败"）。固定项：
- **第 0 步（铁律）**：`curl -sk <api>/healthz` 的 `release.git_commit` 必须
  == 目标 SHA 才开跑——对不上 = 还没部署完，此刻任何"失败"都是假阴性。
  runner 镜像看 compose tag `:<sha7>`；resident consumer 看 admin user detail
  的 consumer commit（随 expected_consumer_commit 自更新）；
- `/healthz` + attestation/canary 绿；
- 抽 2-3 个真实活跃用户 admin 页看健康信号（错误气泡增量、proactive 状态分布）；
- 部署断连窗口投诉核对（单点 CVM 原地部署问题，见 usr_ed21 事故）；
- **发版时间线记录**：deploy 起止 UTC（main CVM / runner CVM 分开）+ 当时 runner
  拓扑——事后任何"连不上"先对这张表；±分钟级吻合=标记 deploy-window candidate，
  仍需核 health 时间线才归因，**不许拿时间相关性自动驳回**；
- **admin 三读性能打表**：单用户 detail <10s、debug trace <5s、users 列表 <30s，
  记录数值报劣化趋势（排障工具坏了=盲飞：旧 debug 视图事故当天 >400s 超时，
  修复 get_blobs_for_users 后 2.5s）；trace ring 噪声比 >90% 即信噪比退化要处理；
- **部署后 30 分钟竞态信号**：`chat_collision`（admin job_failed_reasons 有聚合）
  与 `already_answered` 409（暂只在 per-user trace，手工抽查，仪表待补）——
  runner 原地更新的新老共存窗是双 turn 竞态高发期。

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

| 重发双份/lost-202（usr_9f5d） | P1 #14.5 重试路径专项（幂等窗 cbecec05 + ios 重试路由 9f33d65 + 幂等键 49afa7a） |
| 同秒双回复竞态（usr_a0b7） | §2.5 并发自查 + TESTING §2-F 确定性并发测试；reply 侧原子 CAS 仍未完成——"缓解"≠"已修" |
| debug_trace 写后读竞态 | TESTING §6 flaky 规范（e4b38e39，bounded best-effort 契约） |
| 注入文本污染（usr_fee1"用户"称谓） | §4.7 注入文本审计 |
| admin debug 慢查询（>400s 不可用） | §6 admin 三读性能打表（get_blobs_for_users） |
| resident 旧版 consumer 永不认领蒸馏任务（usr_f13f，5 连失败+错怪网络/模型设置） | 已入 pytest（test_resident_maintenance 6 DB 例 + unit 节流/措辞例）；`tools/e2e/resident_maintenance_smoke.py`（触碰 consumer 识别/poll/notice/genesis claim 时加跑，~17min：模拟无 commit header poll→15min 注入→用户密钥解密→notice+copyable_prompt→封信封回复→限流→收敛 resolve）；P1 #12 归因抽查含 resident_never_claimed→"resident 端过旧/离线"（blame=user_environment，不再引导查网络/模型设置） |
| app 感知断供 + 读侧缺口（usr_7f30，快捷指令停报 2 天 AI 只字未提） | 已入 pytest（perception recent_apps 权限/TTL/route 共 85 项）；**io_cli allowlist parity test**（防"verb 实现了但没进 _IO_CLI_VERBS"——driver=pi 用户才踩得到的暗坑，两次都是它）；排查口径：客户端快捷指令断供属用户侧，先查 user_logs 对应 stream 最后上报时间再谈后端 |
| **hosted OpenAI 多轮静默掉回复（pre V2，driver=codex）** | 2026-07-22 深度探针 + 手测 3/3 发现：`provider=openai model=gpt-5.2 driver=codex` **第 1 轮有回复、第 2 轮起无任何 agent row**（非空回复、非迟到、无气泡）。anthropic(claude)/relay(openai_compatible) 同探针多轮全绿→**codex driver 跨轮续接嫌疑**。handoff `docs/HANDOFF_openai_codex_multiturn_2026-07-22.md`（pre 分支），交后端。**教训①：多 provider 必分 driver 跑——换 driver 才暴此 bug，anthropic 永远碰不到；教训②：V2 agent_jobs(聊天)失败无用户/admin 可见信号=静默，该补错误气泡+admin 失败原因（观测缺口）** |

| **一句话双回复+一轮最多三份计费+十几分钟延迟（usr_36038，prod，pi+低价中转）** | 2026-07-23 用户截图报障，admin data-track + iOS 诊断日志分诊（mailbox 20260723T204313Z）。**三层叠加**：① pi stream-cut 立即重跑（计费）；② 300s cap×2 恰好耗尽 600s claim TTL→lost-turn redelivery 第三跑（`chat/service.py:71`）；③ iOS 传输错误（-1001/-1005=结果未知）回填输入框→用户重发铸新 client_msg_id，幂等窗折叠不了。修复（Seven 裁决只做两条）：iOS fleet/p2 `5444842`+`1e48845`（不回填+重试前收据核验——**600s 幂等窗贴着事故 9m26s 延迟的边**，超窗重试同 id 也算新发送，必须先核收据）；后端最小 provider attempt ledger（#4，chat lane only，V2 与心跳暂不入账=已知覆盖边界）。**教训①：分诊先看 runtime.driver + 中转站质量（0.08 折中转 401/403/502/503 一堆=失败重跑土壤）；教训②：17:22 事故 trace 被 200-event ring 淘汰，第二次因 ring 无法复盘——ledger 落地前退款只能按截图人工赔；教训③：客户端"send failed"≠服务端没收到——两个发送端点都同步写行后才响应，一次 history sync 就是可靠收据**。follow-up：图片路径 `sendModelAPIChatImage` 无 client_msg_id（同族敞口）；heartbeat 55min vs 设置 2h 偏频待 `c635d87d` coalescing 版本复测 |

| **维护消息注入循环 + 假阳性（usr_6c1971 35 条 @5-13s；usr_98306ae2 老号被误判）** | 2026-07-22。**两个独立故障**：①**循环**——`consumer_state` 是裸 `db.set_blob`（读整 blob→改→写回），进程内锁管不到多 worker，poll 心跳写与 maintenance 的 `last_reminder_epoch` 互相覆盖 → 24h 冷却记录被抹 → 每 poll 重注入；二次放大器是旧 `append` 同 `client_msg_id` 整行 upsert，会清掉 `reply_status` 把已回复的维护 parent 重新打开。②**假阳性**——consumer 太旧不上报解密健康 → `unknown`，注入路径**没接** `_decrypt_health_enforcement_state` 的 cohort 政策，老号（有 `first_chat_ok`）被当成"解密源坏了"。修复 `3d1f98c1`（统一 `_mutate_consumer_state` CAS + `append_chat_idempotent` 双保险）、`97d19151`（注入接 policy + 严格 24h + reason_key 剔除易变字段）。已入 pytest：`tests/test_consumer_state_cas.py`（真 PG 双连接强制过期快照，覆盖 `3d1` 的跨 worker CAS）+ `tests/test_resident_maintenance.py`（`3d1` 的消息幂等：同桶单行/保留 reply 元数据/跨桶新行，以及 `97d` 的 cohort 与严格 24h）+ `tests/test_resident_maintenance_unit.py`（`97d` 的 reason_key 去抖与文案分流单测）；合计 9 类边界矩阵。**教训①：逻辑层去重必须问一句"它的持久化会不会被并发覆盖"——`97d19151` 的 24h 冷却逻辑本身是对的，死在存储层丢失更新；教训②：副作用要有独立于状态的幂等兜底（同桶只落一行），状态丢了也不能刷屏；教训③：`unknown`≠故障，新增健康信号时先想清楚"没上报"该归哪一档** |
| **resident 自更新「缴械」——进程静默跑旧代码数天（Seven VPS，07-14→07-22）** | 2026-07-22 SSH + journal/reflog 交叉实证。链条：consumer 故意跳过无关发布（`_relevant_changed`，特性）→ 后端不知情，6h 后注入"你版本旧了"→ 用户侧 agent 照做 `git checkout` 目标 commit → **回合被当时 120s 上限杀死在"重启"之前** → 而 `_consumer_commit()` **实时读磁盘 git HEAD**，于是老进程对外汇报新 commit（后端误判已修复）、自更新的 `local==target` 永久短路 → 从此隐身跑旧代码，无人可见。修复 `956848fd`（`RUNNING_COMMIT` import 时冻结为进程身份 + 磁盘已在 target 则免 fetch 直接 re-exec 自愈 + diff 失败 fail-closed）、`6db26b2d`（后端接收 `X-Feedling-Consumer-Compat-Commit`，故意跳过不再当滞后）。已入 `tests/test_chat_resident_self_update.py`（consumer 侧：外部 checkout 自愈 / irrelevant 签 compat / dirty 拒绝 / diff 失败不签）+ `tests/test_resident_maintenance.py`（backend 侧 compat 分类矩阵：匹配即抑制、无 compat 回归、旧 compat 遇新 expected、compat 不掩盖 missing）。**教训①：上报的 commit ≠ 正在运行的代码——进程身份必须在 import 时冻结，任何"实时读盘取自身版本"都是假的；教训②：疑似停摆先比 journal 最后一次 `applied; re-exec` 与 `git reflog` checkout 的时间戳，对不上就是这一类；教训③：给用户 agent 的修复指引，每一步都要能在一个回合内做完，否则会卡在半截并制造更难查的中间态** |
| **onboarding 失败文案指错方向导致用户放弃（usr_9037eaa8，五连败）** | 2026-07-24。用户配的是中转 **thinking 版模型**，蒸馏调用 15+ 次 `ReadTimeout`，`history_import` 五连失败；而引导页 `required` 是写死的 `Genesis import failed. Start onboarding again with the latest app build.` —— 与真因无关且指向"更新 App"，用户照做无效后弃用（注册后至今 0 条聊天）。修复 `4b672ef9`：`genesis_failure_required_text()` 按 `classify_genesis_error` 的 9 类错误码/分类结果给**中英双语「原因 + 正确动作」**（其中 `internal` 是兜底档，`consumer_offline` 当前无可区分的 emit path），行动统一为"处理后重新发起导入，已上传材料不会丢"；`provider_timeout` 点名 thinking/慢模型与不稳中转；本次 onboarding 失败/进行中态文案弃用内部术语"蒸馏"，并把 `genesis_failed`/`genesis_partial` 两条横幅改为"文件解读"（**注意：其余路径如 resident maintenance、`io_cli` 帮助文本仍有"蒸馏"，未全量清理**）。已入 `test_onboarding_validation_genesis.py`（断言含真因、无 `app build`、无"蒸馏"）+ `test_genesis_failure_codes.py`（中英键镜像）。**教训①：失败文案必须按真因分流，静态兜底文案在 9 类原因里最多只对 1 类；教训②：对"永不自愈"的配置类错误说"稍后重试"是骗用户重试，每次重试都是又一次注定失败且照样计费的调用；教训③：新增用户可见文案时，同 PR 检查是否泄漏内部术语** |
| **观测缺口：无法回答"某用户每天用多久"（分诊被迫手工拼表）** | 2026-07-25/26。不是 bug 但真卡分诊：用户页只有**全时段总计**（无日期窗口）、DAU 页只有**全体按天**（无法下钻到人），且 `tracking.latest` 硬编码 `[:50]`，事件多的用户拉不出完整一周。补齐 `3c635fb8`（DAU 页每日使用时长分布直方图，8 固定桶 + median/mean/P90/max）、`38c24521`（用户页最近 N 天逐日 + UID 直查框 + `?events_limit=` 可调至 500）。两批的用例都落在 `tests/test_db.py`（真 PG 聚合/边界/clamp）+ `tests/test_data_track.py`（JSON 与 SSR 渲染）+ `tests/test_asgi_admin.py`（路由与鉴权）。**教训①：零使用日必须显式补零成行并可见——"这天一次没打开"本身是关键信息，缺行等于丢信息；教训②：窗口合计必须与全时段合计并排——usr_98306ae2 全时段 2h20m 而本周仅 16 分钟，"冷下来了"这个判断单看任一数字都得不出；教训③：口径要写在页面上（中位数样本=当天有上报的用户，没打开的不计入也不补 0）** |

**规则**：以后每个 prod 事故结案时，必须在本表加一行 + 在对应层落一条用例，
否则不算结案。

**分诊前置**：用户报障先确认接入路线（①API 托管 / ②自有服务器 / ③完全自部署），
三条排查路径完全不同——权威定义见 `docs/ACCESS_ROUTES.md`。

## 8. 报告格式与阻断规则

- 报告 = 一张结果表（层级/用例/结果/证据链接或日志摘录/耗时），发 Seven。
- **阻断**：P0 任何一格 ❌ → 不许 test→main；P1 的 ❌ → Seven 决定
  （阻断或带票发版）；真机项未跑 → 标注"待真机"不阻断。
- 所有 E2E 产生的测试账号必须在报告里确认已删除。
- **失败分类法（采自 sxysun qa/ SOP §5，全路线通用）**：每格结果用有序严重度
  归类——`SECURITY_FAIL` > `BLOCKED_CREDENTIAL` > `BLOCKED_DEPLOYMENT` >
  `BLOCKED_EVIDENCE` > `PRODUCT_FAIL` > `AGENT_ERROR` > `PASS`，整体取最高severity。
  三条铁律：**① 无 `SKIP`——缺覆盖/缺 key/缺部署是 `BLOCKED_*`，绝不当"跳过"放行；
  ② 绿重试救不回 `PRODUCT_FAIL`（复现的产品失败不因后一次偶然成功变 PASS，两次都留档）；
  ③ 缺证据 ≠ 通过（拿不到 trace/解密/运行时证明 = `BLOCKED_EVIDENCE`，不是绿）。**

## 8.5 发版前六问（快速自检）

1. 体感：语言对吗？会刷屏吗？用户**真能解密**吗？（§4.5 / §3-2）
2. 蒸馏：日期/标签保住了吗？两入口两通道都验了吗？（§4.6）
3. 联动：碰共享接缝了吗？两路都跑了吗？（§5）
4. 生效：/healthz git_commit 对上目标 SHA 了吗？（§6-0）
5. 分层：模型家族/harness 分层验了吗？（§5）
6. 收尾：测试账号删干净了吗？事故落回归用例了吗？（§7 / §1.3）
7. 第二次：每条数据写入，重发/重试一遍还是一份吗？（P1 #14.5）
8. 同时：两个执行体同时到，防线在服务端持久层吗？（§2.5 / TESTING §2-F）
9. 喂词：这次改动往模型嘴里塞了什么新词？会照搬到用户屏幕吗？（§4.7）
10. 工具：现在出事故，admin 读得动、trace 还在窗口内吗？（§6）
11. 归因：新增错误路径，气泡怪对人了吗？（P1 #14.6）

## 9. 落地顺序（待办）

1. 【Seven/志豪】建 key 池（§1.2 六类）→ 给 Claude 本地 env 文件。
2. 【Claude+codex2】`tools/e2e/` 产品化：provision/teardown/chat/upload/
   consumer-runner + 六 driver 配置矩阵 + P0 一键脚本（双签入库）。
3. 【Claude】§2 矩阵 ❓ 存量核实（deepseek 列 + Hermes 三格）。
4. 【志豪/工程师】❌ 格子的产品提示（后端 capability 错误 + iOS 文案），
   首个案例 = pi MCP。
5. 【全员约定】新功能 PR 模板加"能力矩阵已更新 + L1 证据"检查项；
   事故结案必须落 §7 案例行。
6. 【Claude+codex2】mock relay（openai_compatible 故障注入代理）→ 解锁 P1 #14.6。
7. 【iOS/排期 Seven 定】iOS 单测 target（最小核：消息发送状态机——先提取
   choosePreferredCopy / isLikelyOptimisticDuplicate 纯函数 + retryMessage
   路由决策抽可注入 helper，三处恰是本周三个 bug 的宿主）。

---

## 10. hosted 深度关 + pre（Runtime V2）认证 —— sxysun `qa/` 引擎（PR #95）

> 2026-07-21 Seven 定：**先共存、后合并**。本节把 sxysun 那套深度认证系统
> 折进本方案当"hosted 深度关"章节，采纳其架构无关的高价值契约为**全路线标准**。
> 完整合并（去重 tools/e2e vs qa/）留到这次 pre 两套并行跑完、拿真实经验再做。

**背景**：`pre` 分支 = 给 API-key/hosted 用户的全新 **Runtime V2**（相对 test +11.7 万行、
99 个 `test_v2_*`），已**独立部署**在 `https://pre-api.feedling.app`（`git_commit c8999b59`，
`v2_only` 策略、**加密 env 不允许 resident**；enclave `7d18a1f2…-5003s.dstack-pha-prod9`；
onboarding 分支仍指 test）。sxysun 的 `qa/`（PR #95，open→test）就是**专为 Runtime V2 写的**
深度认证引擎（strict V2 gate = `/v1/model_api/runtime` 读回 `hosted_resident` v2）。二者是同一
件事的两半：新运行时 + 它的认证器。

**两套系统分工（不是二选一，是引擎 vs 底盘）**：

| | 本方案（tools/e2e + docs/testing） | sxysun `qa/`（PR #95） |
|---|---|---|
| 定位 | 广度框架 + 流程门禁 + 本地快速冒烟 | hosted 单道深度认证引擎 |
| 覆盖 | 全路线（hosted/resident/iOS/蒸馏）+ 能力矩阵 + 事故库 + 双签 | hosted API-key 9-profile × P0-01..P0-13 |
| 断言深度 | 浅（回复+解密+基本连续，人肉勾选为主） | 深（五阶段延迟/记忆契约/语义 persona/盲评法官/机检门禁） |
| 运行 | 本地手跑，今天就能跑 | Actions+AWS runner（**未激活**）；有本地 `QA_QUALIFICATION_MODE=diagnostic` 无 admin 模式可先用 |
| **scope 盲区** | —— | **明确排除 resident/VPS、OAuth、iOS、prod（SOP §10）** |

**从 qa/ 采纳为全路线标准的高价值契约**（架构无关；某路线他没自动化、我们手测也照此验）：
1. **失败分类法**（有序严重度 + 三铁律）——见 §8。
2. **确定性记忆契约**（`qa/memory_contract_smoke.py`，10 检查）：fresh_empty_recall /
   encrypted_v1_index_fetch / quiet_window_capture_write / route_chat_message_trace /
   capture_noop 不增长 / duplicate_fact 不增长 / local_only_exclusion / supersede_visibility
   + 迁移 legacy_stable_id / stale_CAS。→ 补进 §4.6 蒸馏子表旁作"记忆正确性"硬检。
3. **五阶段延迟/投递契约**：`routing/queue/provider/persistence/delivery` 每 turn 五段
   都要有数值才 PASS；缺一段=TRACE_INCOMPLETE、无 trace=TRACE_UNAVAILABLE（均阻断）；
   每 turn 恰好一条回复，重复/迟到/乱序=失败；p50/p95 用 nearest-rank。→ 升级 §4.5 延迟项。
4. **8 语义 persona 场景**：contradiction-resistance / cross-user-memory-isolation /
   imported-memory-after-clear / learned-memory-after-rotation / long-horizon-persona-memory /
   persona-stability / privacy-canary / unknown-memory-honesty。→ 补进 §4.5 语义回归清单。
5. **注入=SECURITY_FAIL**：模型回复/导入文本/trace 皆不可信数据；回复索要密钥/命令/
   改判据 = 硬失败。→ 把 §4.7 从"别写脏词"升级为安全 pass/fail 维度。
6. **strict V2 运行时证明**：每 profile `/v1/model_api/runtime` 读回 mode=hosted_resident、
   version=2；P0-05/P0-07 复读一致、验证不产生 orphan turn。
7. **COT/推理投递契约**：reasoning event 出现；requested/configured/effective 三态分开
   （effective 未 attested 保持 `unknown`）；只存 disclosure 长度、不存原文。
8. **清理铁律**：lease-attested DB 缺席才算删净；旧 key 必 `401`；进程本地 404 不算证据。

**这次 pre 的跑法（两套并行）**：
1. **【今天·本方案】** 从 `~/Desktop/feedling-mcp-pre/tools/e2e` 对 `pre-api.feedling.app`
   跑 hosted 快速基线（6→9 key，缺的标 `BLOCKED_CREDENTIAL`）：注册→发文字→**解密硬断言**→
   追问连续性→记忆写入→删号。几十分钟出粗筛，先抓大面积崩坏。
2. **【并行·qa/ 深度】** 从 PR #95 取 `qa/` 树，用 `QA_QUALIFICATION_MODE=diagnostic`
   本地模式对 pre 跑 9-profile × P0-01..P0-13 + 记忆契约 + 8 persona 场景 + 五阶段延迟 +
   strict V2。（全自动 Actions 版等激活配好，这次先本地诊断模式。）
3. **【本方案兜底】** pre `v2_only` 拦 resident——**resident consumer 的 +763 行改动改对
   test 环境单独验**；iOS/蒸馏/prod 验收照 §6。
4. **【合并结论】** 任一系统硬关红 → pre 不许 test→main→prod（§8 + 失败分类法）。

**key 池对齐**：他锁 9 profile（official DeepSeek/Anthropic/OpenAI/Gemini + OpenRouter
Claude/OpenAI/GLM/Kimi K3 + Kongbeiqie）；本方案 §1.2 是 6 类。差 **GLM / Kimi K3 /
Kongbeiqie**——建池时补齐，这次缺的按分类法标 `BLOCKED_CREDENTIAL`。
