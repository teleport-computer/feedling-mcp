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

**规则**：以后每个 prod 事故结案时，必须在本表加一行 + 在对应层落一条用例，
否则不算结案。

## 8. 报告格式与阻断规则

- 报告 = 一张结果表（层级/用例/结果/证据链接或日志摘录/耗时），发 Seven。
- **阻断**：P0 任何一格 ❌ → 不许 test→main；P1 的 ❌ → Seven 决定
  （阻断或带票发版）；真机项未跑 → 标注"待真机"不阻断。
- 所有 E2E 产生的测试账号必须在报告里确认已删除。

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
