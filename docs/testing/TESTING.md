# Feedling 测试规范（通用）— 改了什么，就测什么

**作者**：Claude（配合 Seven）
**日期**：2026-07-12
**定位**：这是**整体规范**，不是某个功能的排查记录。任何人（Claude / Codex / 人）每完成一类改动，照这张表做对应的测试即可。思维链（CoT）只是矩阵里"E. 网关/driver"那一类的例子。

> **发版和新功能另有一套**：本文档管**每次改动**（开发循环 L0）；每次
> test→main 发版的全量回归、新功能的能力矩阵申报与跨环境（driver × route ×
> provider）E2E，见 **`docs/testing/RELEASE_TESTING_PROTOCOL.md`**（2026-07-17 起）。

---

## 0. 三条总原则

1. **改了什么就测什么** —— 见 §2 决策矩阵，按你**动过的文件类别**对号入座，做齐"必做"项。
2. **要证据，不要感觉** —— "跑通了"不算完成；本地要有 pytest 绿、碰链路要有 E2E `OK`、上了线要有 admin trace 字段。
3. **CI 兜底 ≠ 免你自测** —— CI（§4）会替你跑一部分，但它慢、且只在 push 后。本地先跑，别把 CI 当第一道防线。

---

## 1. 三层测试手段（工具箱）

| 层 | 命令 / 工具 | 证明什么 | 成本 |
|---|---|---|---|
| **L1 本地 pytest + pyflakes** | `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py` + `python -m pyflakes backend/<包>` | 纯逻辑 + ASGI app 正确 | 秒级，每次都跑 |
| **L2 本地 E2E 真链路** | `tests/e2e_model_api_test.py`（起真后端 + enclave 模拟器，走 register→setup→send）；`tools/*_roundtrip_test.py` | 加密/账号/vendor 整条路径通 | 分钟级，碰链路才跑 |
| **L3 部署态 E2E** | test 环境发真实加密信封 → 读 `/v1/admin/data-track/debug?user_id=…`（Bearer = `~/.feedling/data-track-admin-token`） | 部署后真生效、网关/CVM 行为对 | 需部署，碰运行时行为才跑 |

> L1 判据是**「零新增失败」**（有 2 个长期红的 enclave 依赖用例，backlog #12）。

---

## 2. ★决策矩阵★（核心）

**用法**：看你这次动了哪几类文件，把对应行的"必做"全部做齐。动了多类就叠加。

| 你改动的类别 | 典型文件 | L1 pytest | L2 本地E2E | L3 部署态 | 额外必做 |
|---|---|:--:|:--:|:--:|---|
| **A. 纯后端逻辑** | `service/` `core/` `actions/` | ✅ | — | — | pyflakes；对应 `test_<域>_*.py` 补/更新 |
| **B. 新增/改路由** | `*/routes_asgi.py` | ✅ | — | ⚠️ 视情况 | PR 描述**列出路由变更**（url_map 是回归基线）；补 `test_asgi_<域>.py` |
| **C. 错误返回 / slug** | 任何返回 `{"error":...}` 的地方 | ✅ | — | — | **同 PR 登记 `docs/API_ERRORS.md`**（有守卫测试）；slug 冻结、语义变更走新 slug |
| **D. 加密 / 信封 / 账号链路** | `content_encryption.py` `model_api` setup·send、`enclave_app.py`、`/v1/envelope/*` | ✅ | ✅ **必跑** | ⚠️ 建议 | `tools/e2e_encryption_test.py` / `v1_envelope_roundtrip_test.py`；确认"服务端永不见明文" |
| **E. Provider / driver（含思维链）** | `agent_runtime/spawners.py` `provider_client.py` | ✅（`test_hosted_agent_runtime_driver.py` 等） | ✅ 各 provider | ✅ **必跑** | 部署 CVM 后读 trace：`thinking_present` / `reasoning_output_tokens` / `AGENT_CLI_CMD`；**按模型家族分层验**（Anthropic/OpenAI/Gemini/中转 wire 各不同） |
| **F. 消费端 consumer / proactive** | `tools/chat_resident_consumer.py` `backend/proactive/*` | ✅（sanitize 等单元断言） | — | ✅ **必跑** | **改完必 `systemctl --user restart feedling-chat-resident`**（否则跑旧内存态）；发消息验不泄漏协议碎片；**并发写自查**（"两个同时到会怎样？"）+ 确定性并发测试（Event gate 模式，禁 sleep 碰运气，样板 `test_debug_trace.py::test_flush_pending_waits_for_worker_in_flight_batch`） |
| **F2. 记忆写入判据（capture / dream 解析）** | `backend/memory/card_text.py` `*_prompt_v1.py` 的 parse/prompt、`v2/extraction.py`、consumer 的 capture/dream handler | ✅（`test_card_text_gate.py` `test_capture_prompt_v1.py` `test_dream_prompt_v1.py` `test_v2_extraction*.py`） | — | ✅ **必跑** | **主风险是误拦不是漏拦**：判严一格 = 用户本该有的卡凭空消失且无声。部署后必跑 `NO_PROXY='*' python3 tools/e2e/card_gate_probe.py`（至少两个模型档：一强一弱），断言真卡落地且**过它自己那把尺子**；改 Unicode/长度判据必须补非拉丁非 CJK 语种（阿拉伯/西里尔/希伯来/重音拉丁）回归——字符区间白名单曾整语种误杀；`strict=False` 的「全脏」分支必须报 `*_after_retry` 让 job 失败，**报成 noop 会推进 capture frontier 把窗口永久丢掉** |
| **F4. 卡里怎么称呼本人（称谓 / 转写标签）** | `backend/identity/user_naming.py`、三条写入路各自的 prompt（capture / dream / `hosted/history_import.py`）、V1 consumer 与 V2 worker 的转写标签 | ✅（`test_card_user_referent.py`：三条路都带规则 + 转写标签绝不写 "User"） | — | ✅ **必跑** | **改规则必须三条路一起改**（蒸馏 / 落卡 / 做梦），只改一条 = 另两条继续泄漏；**根因通常在转写标签不在 prompt**（V2 曾漏传 `user_name`，把本人标成 `user:`，模型照抄进卡）。live 验：`/v1/history_import/upload`（托管蒸馏，**必传 `relationship_started_at` 或 `fresh_start=true`**，否则 job 直接 failed）用**不设名字**的账号跑——有名字时泄漏率本来就 0，测不出东西；素材里要混真产品词（「用户留存」）确认**没被误杀**。**确定性改写器不可上写入路**（`rewrite_user_reference`：锚点是开集，产品散文近 100% 被改坏，2026-07-26 已撤，`test_deterministic_rewriter_is_not_wired_into_the_daily_card_path` 锁死）。⚠️ 已知缺口：V2 的 dream 没有 force 旁路（夜间窗+新卡数+最小间隔三闸），做梦这条路目前**只有单测、无 live 覆盖** |
| **G. DB schema / migration** | 建表 / 改列 / reset 路径 | ✅（`test_*_migration.py` `test_account_reset_purges_all_tables.py`） | — | ⚠️ | prod 用户极少，clean reinstall 迁移可接受（**须任务明确授权**）；reset 必须 CASCADE 清干净 |
| **H. compose / enclave / 链上不变量** | `deploy/docker-compose*.yaml` `enclave_app.py` compose 段 | ✅ | ✅（envelope roundtrip） | — | **compose 任何字面量变更 → `compose_hash` 变 → 重新上链**（`deploy/DEPLOYMENTS.md`） |
| **I. CVM runner 镜像 / 部署** | `deploy/Dockerfile.agent-runner` bump | — | — | ✅ **必跑** | `phala inspect` 确认 image tag == 目标 hash；`deploy/verify-remote.sh`；litellm 版本没变=桥行为没变 |
| **J. iOS** | `App/FeedlingTest.xcodeproj`（+ Widget target） | Xcode build/test | — | — | **DESIGN.md token 合规**：禁裸 hex / 裸字号 / 裸字体串，用 `Color.feedling…`/`Font.feedling…`/`Spacing.*`/`Radius.*`；改 UI 前先读 DESIGN.md；**动了发送/重试/合并逻辑必查发送状态机走查表**（sending→sent 本地字段不丢；sending→failed 后 text 的 clientMsgID 仍在；重试走对端点复用同 UUID 不出第二气泡；poll 合并跨 source 收敛；用户真心连发两条不被误合并——iOS 无单测 target，走查即测试） |
| **K. 公开文档** | io-onboarding 的 `skill.md`/`quickstart.md`/`troubleshooting.md` | — | — | — | 在 **io-onboarding repo** 改并 push（不在本 repo）；`skill.md` push 即对所有装机 app 生效、无需 rebuild；改 agent 行为要**双改**（本 repo 代码 + skill.md） |
| **L. 智能合约** | Solidity / `forge` | `forge test -vvv` | — | ⚠️ | `forge build --sizes`；部署测试合约走 `deploy-test-contract.yml`（手动） |
| **M. 多 worker 共享状态** | 引入进程内共享缓存/状态 | ✅（`test_multi_tenant_isolation.py`） | — | — | 必须接 `core/wake_bus.py` 失效广播，否则多 worker 分叉；核对库 `max_connections`（每 worker +~17 连接） |
| **M2. 跨 worker 的「做过没做过」记录** | 任何「读整个 blob → 改字段 → 写回」的状态：`consumer_state`、冷却/节流时间戳、gate 的已完成标记 | ✅（并发覆盖测试，样板 `test_consumer_state_cas.py`） | — | — | **进程内锁挡不住多 worker**：必须走 CAS（本库样板 `db.set_blob_if_unchanged`）**或数据库侧等价的原子条件更新** + 冲突重读**重算**（不是重放旧决策）；CAS 耗尽要 **fail closed**（宁可不做副作用）。测试必须真 PG 双连接强制过期快照，断言两个写者的不相干字段都不被抹。**外加一层幂等**：副作用（发消息/落行）本身按稳定 id 去重，状态丢了也只发一次 |
| **N. 同一口径存在两套实现** | 同一个指标/判据被算两次（新老聚合并存、SQL 与 Python 各算一遍、两处分日/分桶逻辑） | ✅ **交叉断言必写** | — | — | 用**同一批边界数据**同时喂两条实现，断言**所有同口径字段逐字段相等**（不是各自自测通过就算；两侧 schema 可以不同，要对的是同口径那几个字段——如直方图 `total_users`↔DAU `session_dau`、`median_sec`↔`median_user_sec`，按天 `foreground_sec`/`sessions`↔全时段同名字段）。边界必造：本地零点整 / 次日零点整 / 跨日多条 / 脏值 / 空集。口径漂移不会报错，只会让两个页面各说各话 |
| **P. Agent 工具 schema / 工具调用可靠性** | `tool_schema.py`、`capabilities/*`、各 lane 的 system prompt（V2 `CHAT_SYSTEM_PROMPT`、V1 `agent_tools_prompt.md`） | ✅ | — | ✅ **必跑** | **判据只能是副作用，不能是模型的话**：模型回「好的/已改」而一个工具调用都没发是常态，验收必须查 effects 队列 / admin trace（`0 pending` = 根本没调）。**隔离复现通过 ≠ 真实上下文通过**：同一个 deepseek-v4-flash，短 prompt 单测必调 identity_patch，长聊天里经常直接跳过——必须在**多轮真实上下文**里验，且**弱模型档单独验一遍**（强模型会替你把规则脑补上）。**新增工具参数必须按"模型最自然的形状"验**：`identity_patch` 是 `additionalProperties=false`，`relationship_days` 当初只藏在嵌套 `patch` 里，模型照 rename 的习惯顶层传 → 参数被丢弃 + **谎报成功**（114496d9）；一级参数就该是一级参数，并且顶层/嵌套两种形状都收。**工具描述不等于指令**：光写在 schema 的 description 里弱模型不照做，要在 lane 的 system prompt 里明写"你**能**改、不许假装改"（f76e7f6a 补 V2 与 V1 的这条 parity）——改这类指令会**一次性打掉 provider prompt cache**，属预期 |

---

## 3. 三个"何时才需要下沉一层"的判断

- **只动纯逻辑/文案** → L1 够了。
- **碰了加密、账号、信封、vendor 调用** → 必须 L2（本地真链路），因为 L1 mock 不掉 enclave 包/解。
- **碰了运行时行为**（driver 选择、网关 wire、consumer 提取、proactive 清洗、CVM 镜像）→ 必须 L3（部署态 + admin trace），因为**代码合了不等于跑着的进程/CVM 生效**。

---

## 4. CI 会自动替你跑什么（`.github/workflows/`）

推上去后 CI 跑（**别依赖它当第一道防线**）：

- **`ci.yml`**：
  - forge build/test/coverage（合约）
  - 起后端 → `tests/test_api.py --multi-tenant` → 隔离回归（`test_db.py` `test_multi_tenant_isolation.py`）→ Round 3 V2 回归
  - `docker compose build --no-cache`（`--require-hashes`）+ healthcheck
  - syntax + static（pyflakes）
- **`continuity-canary.yml`**（每日 06:17 UTC cron）：prod day-0 信封解密连续性（`tools/continuity_canary.py`）——防"某天起解不开老信封"。
- **`deploy-test-contract.yml`**（手动）：部署 FeedlingAppAuth 到 Sepolia。
- **`docker-publish.yml`**：镜像发布。

---

## 5. 部署态 E2E 标准动作（L3 展开）

0. **先对版本（铁律）**：`curl -sk <api>/healthz` 的 `release.git_commit`
   必须 == 目标 SHA 才开跑——对不上 = 还没部署完，此刻任何"失败"都是假阴性。
1. **复用**（优先）或新建 test model_api 账号。
2. 拿账号 X25519 keypair；`whoami` 拿 `public_key`。
3. `backend/content_encryption.py::build_envelope(...)` 构造加密信封。
4. 发一条真实加密消息（`/v1/.../chat/send`）。
5. `GET /v1/admin/data-track/debug?user_id=<uid>`（Bearer admin token）。
6. 读 `agent.model.call.done.detail` 的字段验收。
   - ⚠️ stdout excerpt **1000 字节截断** → 用短 prompt 或多拉 done 事件。
   - ⚠️ 读 trace 要**过滤 `ts > 你发消息的 ts`**，别读到上一轮 proactive 的旧事件。

---

## 6. 通用坑（文档里查不到、只有做过才知道）

- **git pull ≠ 生效**：拉代码只更新文件，跑着的 Python 进程仍是旧内存态 → 改 consumer 必 `systemctl --user restart feedling-chat-resident`。
- **复用账号 config 可能是旧的**：改了网关行为要 `phala inspect` 确认 CVM 真部署了新镜像；litellm 版本没变 = 桥行为没变。
- **别一个诊断套所有模型**：必做「模型家族失败分层」（Anthropic 家族 / OpenAI·o 系 / Gemini / 中转，wire 形状与行为各异）——web_search 400 和思维链上都栽过这个。
- **driver 决定命运**：claude driver（Anthropic 家族 + DeepSeek）走原生 thinking；codex driver 有固有天花板；CLI 从 session 文件读。查前先看 `AGENT_CLI_CMD` 定 driver。
- **绝不自产假思维链**：源头不给就不展示（产品铁律）。
- **别囤测试账号**：优先复用，用完 `POST /v1/account/reset {"confirm":"delete-all-data"}`（用账号自己的 key）；新建就存 `user_id+api_key+keypair`，否则删不掉（无 admin 删除口）。
- **孤儿清单是全局共享的，并行 session 会互删**：`~/.feedling-e2e-orphans` 所有 session 共用，别人跑一次 `p0.py --cleanup-orphans` 就会把**你正在用**的账号当孤儿删掉——表现是长跑探针中途莫名 401 / admin 查 `user_not_found`。撞到就先怀疑这个，别去查鉴权。
- **探针轮询非 200 必须硬失败**（`raise SystemExit`），不许忽略继续循环：否则"账号半路没了"这类事故会被静默藏十几分钟，再以别的形状炸出来。这是 e2e 假 PASS 的同一个形状，2026-07-26 又犯了一次。
- **判"某个缺陷修没修好"，先确认你的用例真能让它复现**：称谓泄漏只在**账号没名字**时发生，拿有名字的账号怎么测都是 0——不是修好了，是根本没触发。概率性缺陷的验收用例必须先证明"改之前它会挂"。
- **枚举"合法的东西"在开集上必然失败**：判据靠白名单/锚点列举时，先问这是闭集还是开集。称谓改写器四轮补白名单全被新反例推翻，最后连"产品复合词不接限定词"这个前提本身都被证伪——开集上唯一正确的动作是**不做**，换成 prompt 约束 + 遥测度量。

---

- **Flaky test 先当真 bug**：出现即立案排查，拿到"确属测试自身问题"的证据（干净
  HEAD 复现 + 根因分析）才允许改测试；**禁止** flaky 标记/retry/skip 静默掩盖。
  教训：`test_memory_capture_trace` 被当 fixture 问题挂了几天，实为 trace 异步化
  引入的真实读写竞态（生产 admin 同样读旧数据，修复 e4b38e39）。排查起手式：
  单跑 vs 全量差异、`git archive` 导出的干净树（别 stash 共享区）、进程内全局状态清单。

## 7. "完成"的定义（Definition of Done）

一个改动可以宣布完成，当且仅当：

```
[ ] 按 §2 矩阵，本类改动的"必做"测试全部做齐并通过
[ ] L1：全量 pytest 零新增失败；pyflakes 干净
[ ] 碰链路的：L2 本地 E2E 相关 provider/roundtrip = OK
[ ] 碰运行时行为的：L3 部署态 admin trace 拿到预期字段（有证据）
[ ] 动了 compose/路由集/加密路径/slug 的：PR 描述写明 + 对应登记（API_ERRORS.md / 上链）
[ ] 消费端改动：已 restart 服务并复验
[ ] 动了工具 schema / 工具调用指令：弱模型档 + 多轮真实上下文验过，判据是 effects/trace 不是模型的话
[ ] asgi_app.py diff 仅装配/注入（理想零 diff）；无向上 import；无 app.py facade 引用
```

---

- **双签范围内的改动**（用户可见行为/共享接缝/并发存储原语/加密账号链路/prompt
  注入文本）：有独立 gatekeep 记录（清单见 RELEASE_TESTING_PROTOCOL §2.5）。

## 8. 一句话

**对号入座（§2 矩阵）→ 逐层拿证据（§1 工具箱）→ 满足 DoD（§7）才叫完成。** 规范的重点不是"多测"，而是"**改了哪类、就精确补哪几项、每项有硬证据**"。
