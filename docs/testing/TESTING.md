# Feedling 测试规范（通用）— 改了什么，就测什么

**作者**：Claude（配合 Seven）
**日期**：2026-07-12
**定位**：这是**整体规范**，不是某个功能的排查记录。任何人（Claude / Codex / 人）每完成一类改动，照这张表做对应的测试即可。思维链（CoT）只是矩阵里"E. 网关/driver"那一类的例子。

> **发版和新功能另有一套**：本文档管**每次改动**（开发循环 L0）；每次
> test→main 发版的全量回归、新功能的能力矩阵申报与跨环境（driver × route ×
> provider）E2E，见 **`docs/testing/RELEASE_TESTING_PROTOCOL.md`**（2026-07-17 起）。

---

## 0. 五条总原则

1. **改了什么就测什么** —— 见 §2 决策矩阵，按你**动过的文件类别**对号入座，做齐"必做"项。
2. **要证据，不要感觉** —— "跑通了"不算完成；本地要有 pytest 绿、碰链路要有 E2E `OK`、上了线要有 admin trace 字段。
3. **CI 兜底 ≠ 免你自测** —— CI（§4）会替你跑一部分，但它慢、且只在 push 后。本地先跑，别把 CI 当第一道防线。
4. **先定语义边界，再解技术细节** —— 动手前先问一句"这东西的**周期 / 归属 / 触发**由谁决定"，
   答不上就别急着做精度。2026-08-01/02 的 capture 横幅：语义没定死就先做了"按天聚合"，
   并为它的日切精度来回定了一整轮 `perception tz → proactive tz → UTC`；两天后周期被改成
   "由用户关闭驱动"，那套时区链路**整个作废，净删 115 行**。**删掉的复杂度，正是语义没定死
   时长出来的。** 同族问句：这个计数归零由谁触发？这条写入的授权来自谁？这个"今天"以谁的
   时区为准？——先问清楚再动手，比事后返工便宜得多。
5. **mock 只许打在「我们不拥有的边界」上** —— 网络 transport、subprocess、时钟、外部 API
   可以替身；**凡是我们自己写的产生方**（helper、分类器、解析器、清洗器），测试必须调真实的那个。
   替身往里挪一层，测的就是一条生产走不到的路，绿灯只证明替身按你写的方式工作了。
   2026-08-06/07 的空回复归因批**同一形状连犯两次**：①第一版用 `lambda: ""` 冒充 HTTP helper
   的返回，而真实 helper 在**返回之前**就抛异常——新加的分叉对生产链路是死代码，
   核心工单场景（中转 200 + 空 content）根本没修到，测试却全绿；②改完之后只测了 `str` 那条腿，
   `dict` 腿的 BLOCKER 溜过去：判空用的窄提取器读不到 `messages`/`actions`，
   把**我们自己**的协议泄漏压制误判成"provider 给了空"，锅甩反了方向。
   **动手前自查三问**：(a) 我 mock 掉的这个，是我们写的还是外部的？是我们写的就是红灯；
   (b) 把被测的生产代码改坏，这条测试会红吗？不会就是在测替身；
   (c) 生产链路走到被测分支的**前一步**是什么，测试真的经过它了吗？
   **(b) 别停在自问——真去改坏一次（变异验证）**：把刚写的修复逐处改回旧行为，
   跑一遍，看**对应那条**用例是否精确变红。这批最后一轮就是这么确认测试会咬人的
   （两处修复各自点亮各自的用例）。成本一分钟，换掉"我觉得这测试有效"这句感觉。
   **(c) 的"前一步"要一直追到我们代码的入口**，不是追到某个看起来够早的函数：
   这批同一个洞连修三次都没到底——先以为在 `call_agent` 判空够早（helper 返回前
   就抛了），再提到"压制之前"（`_agent_turn_from_raw` 内部还有 sanitizer），
   最后才落到 `_raw_assistant_text(body)`＝**provider 交到我们手里的原始文本**。
   凡是"谁把它弄没的"这类归因判据，锚点只能是**外部交界处的原始值**，
   在那之后我们做的每一步清洗都是自己的行为。
   同族：参数化要覆盖**每一种真实入参形状**（本例 `str` / `dict{messages}` / `dict{actions}` /
   `dict{choices}` 是四条不同的腿），只测最顺手那条 = 只测了自己想得到的那种坏法。

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
| **F. 消费端 consumer / proactive** | `tools/chat_resident_consumer.py` `backend/proactive/*` | ✅（sanitize 等单元断言） | — | ✅ **必跑** | **改完必 `systemctl --user restart feedling-chat-resident`**（否则跑旧内存态）；发消息验不泄漏协议碎片；**并发写自查**（"两个同时到会怎样？"）+ 确定性并发测试（Event gate 模式，禁 sleep 碰运气，样板 `test_debug_trace.py::test_flush_pending_waits_for_worker_in_flight_batch`）；**开关独立性矩阵**（Seven 2026-07-26 定：心跳/照片/到达/解锁/定时/屏幕共享**相互无连带**）——动了 `proactive/controls_v2.py::evaluate_wake_control_v2` 或任一唤醒源，必须逐个关单个开关、断言**只有它那条路被拦、其余全通**（实测 44 活跃用户里 6 个是"心跳关+屏幕共享开"，任何连带都会当场砍掉他们的功能）；consumer 耦合测试集一次跑齐：`grep -l -E 'chat_resident_consumer' tests/test_*.py`（34 个文件，基线 1100 passed / 1 skipped）；**动了定时唤醒（`scheduled_wake_v2` / `schedule_wake` 工具面）必跑 `NO_PROXY='*' python3 -m tools.e2e.repeat_wake_probe`** —— 重复提醒的验收标准是「明天真的响、说停真的停」，那句话跨调度器+存储+上下文注入三层，单测证明不了；探针四格里最要紧的是**用已 fired 的旧 id 能整串取消**（否则用户关不掉一个每天响的提醒） |
| **F2. 记忆写入判据（capture / dream 解析）** | `backend/memory/card_text.py` `*_prompt_v1.py` 的 parse/prompt、`v2/extraction.py`、consumer 的 capture/dream handler | ✅（`test_card_text_gate.py` `test_capture_prompt_v1.py` `test_dream_prompt_v1.py` `test_v2_extraction*.py` `test_dream_gates.py`） | — | ✅ **必跑** | **主风险是误拦不是漏拦**：判严一格 = 用户本该有的卡凭空消失且无声。部署后必跑 `NO_PROXY='*' python3 tools/e2e/card_gate_probe.py`（至少两个模型档：一强一弱），断言真卡落地且**过它自己那把尺子**；改 Unicode/长度判据必须补非拉丁非 CJK 语种（阿拉伯/西里尔/希伯来/重音拉丁）回归——字符区间白名单曾整语种误杀；`strict=False` 的「全脏」分支必须报 `*_after_retry` 让 job 失败，**报成 noop 会推进 capture frontier 把窗口永久丢掉**。**2026-08-05 起 dream 出口只拦「明显不对」**（占位符/协议泄漏/卡id泄漏/墓碑短语/爆炸半径保险丝），**内容质量判断一律不进闸**（15% 增量栅栏与逐提案语义审查员已拆，别再加回来）；改 dream 判据必须跑 `test_dream_gates.py` 的 **V1/V2 跨 lane 一致性锁**——两条 lane 的结构判据是同一套，只改一边 = 行为漂移。**改内容闸必须先列「写入路清单」再定落点**（usr_a40e 2026-08-06）：记忆写入至少有四条路——capture/dream 结构化 parse、**明文 `/v1/memory/actions`（io_cli memory-write/patch，agent 徒手路）**、genesis/history_import 蒸馏、migrate——闸只落 parse 层时徒手路整个绕过（墓碑卡三晚全走明文路，parse 层的新闸一张没拦到）；判据的**单一事实源**放 `card_text.py`/`dream_gates.py`，各路各自接入，绝不复制判据本体。**新闸必须显式决定挂不挂 kill switch，且补 switch-off 回归**：`FEEDLING_MEMORY_CARD_GUARD` 的契约只管协议残片检测，墓碑/占位符闸无条件跑（`test_tombstone_gate_survives_guard_kill_switch`）——挂错伞 = 止血关协议闸时顺手重开事故路 |
| **F3. 错误分类 / 归因（blame）** | `tools/chat_resident_consumer.py::_ERROR_CLASS_RULES`、`backend/notices/catalog.py::_UPSTREAM_RULES` | ✅（`test_catalog_consumer_parity.py` 逐字锁两份规则） | — | ✅（`tools/e2e/turn_failure_smoke.py`） | **规则表是开集，不是穷举**：每接一家 provider/中转就可能冒出新措辞，而**漏判不报错**——只是静默降级成 `FALLBACK_REPLY`（"我这会儿有点慢…你稍后再发一次"），用户永远等不到"你的模型名写错了"。所以：① 新增措辞必须附**真实错误串出处**（admin ledger / 用户截图），不许凭空想 regex；② 两份规则**必须同改**（parity test 会拦，但别等它拦）；③ 改完自问"哪些错误现在还落进 system 兜底？"。案例 usr_a40e（`deepseek-chat` 用户反复收到"没接上"，真因是模型名不可用——失败相关性干净地只命中这一个模型） |
| **F4. 卡里怎么称呼本人（称谓 / 转写标签）** | `backend/identity/user_naming.py`、三条写入路各自的 prompt（capture / dream / `hosted/history_import.py`）、V1 consumer 与 V2 worker 的转写标签 | ✅（`test_card_user_referent.py`：三条路都带规则 + 转写标签绝不写 "User"） | — | ✅ **必跑** | **改规则必须三条路一起改**（蒸馏 / 落卡 / 做梦），只改一条 = 另两条继续泄漏；**根因通常在转写标签不在 prompt**（V2 曾漏传 `user_name`，把本人标成 `user:`，模型照抄进卡）。live 验：`/v1/history_import/upload`（托管蒸馏，**必传 `relationship_started_at` 或 `fresh_start=true`**，否则 job 直接 failed）用**不设名字**的账号跑——有名字时泄漏率本来就 0，测不出东西；素材里要混真产品词（「用户留存」）确认**没被误杀**。**确定性改写器不可上写入路**（`rewrite_user_reference`：锚点是开集，产品散文近 100% 被改坏，2026-07-26 已撤，`test_deterministic_rewriter_is_not_wired_into_the_daily_card_path` 锁死）。⚠️ 已知缺口：V2 的 dream 没有 force 旁路（夜间窗+新卡数+最小间隔三闸），做梦这条路目前**只有单测、无 live 覆盖** |
| **F5. CLI driver 会话 / argv 准备** | `tools/chat_resident_consumer.py` 的 `_prepare_cli_command` / `call_agent_cli`、`--resume`/`--session-id`/`isolated_session` 相关 | ✅（驱动矩阵） | — | ⚠️ 真实二进制 smoke | **测试必须断言「交给驱动的最终 argv」（产物），不能只断言意图 flag**——2026-08-05 前 isolated_session 的两个测试都 mock 掉 call_agent、只断言 `isolated_session=True` 传到了，于是一条根本跑不起来的 claude 命令绿着上线六天（claude 的 `--print --resume` 只认它**自己生成的 UUID** / 已存在 session title，consumer 自造的 bounded 标签首轮必炸；vision probe 与 dream 语义 review 在 claude-driver 家庭因此**全部静默失败**，探针 verdict 被 CLI 错误污染成 `vision_model_failed`）。四个驱动的会话语义各不相同，改会话逻辑必须逐驱动过矩阵：**claude** 最严（session id 只认自产 UUID；隔离 = **不给任何会话旗标**，裸 `claude --print` 即全新会话）、**pi** `--session-id` 是 create-if-missing、收任意串、**hermes** `--resume` 收任意串、**codex** 无 resume 天然每轮全新。验收两件套：①单测钉 argv（样板 `test_prepare_claude_cli_isolated_session_*`，含「隔离轮不得读写共享会话存储」的反断言）；②对**真实 claude 二进制**先跑**负向对照**（手工构造修复前的失败形状、确认报错逐字复现——证明 E2E 踩的是真路径，不是假绿），再跑正向 smoke：隔离首轮成功 + 共享 `--resume` 跨轮召回 + 隔离轮看不到聊天上下文 + 共享 sid 不被搅动（修复参照 1965546c）。**驱动语义有分歧时优先拿 claude 做负向对照**——它反复是最特殊的那个（另见 F 行 claude 工具面泄漏） |
| **F6. 喂给模型的文本是子进程产物(io_cli 目录等)** | `tools/io_cli.py` 任何 `help=`/`epilog=`、`tools/io_cli_catalog.py` | ✅（`test_io_cli_catalog.py`，含 GBK locale 用例） | — | ⚠️ 自建用户环境 | **帮助文本里的一个字符能让整份工具目录静默消失**：`build_catalog` 是逐 verb 跑 `--help` 的子进程，`text=True` 两侧都用**系统 locale** 编码。中文 Windows 是 cp936(GBK)，help 里出现一个 GBK 装不下的字符（2026-08-08 自建用户报的 `identity-redistill` 里的 ⚠️ U+26A0）→ 子进程 UnicodeEncodeError → `build_catalog` 返回 None → consumer 回退到只剩 D3 一行的 fallback，**agent 每一轮都丢掉整个 4KB 工具目录**（失败路径**故意不缓存**，所以每轮重试、每轮再失败），全程无日志、无报错，用户只看到「AI 好像不会用工具了」。三条纪律：①**符号一律用 ASCII**（`[!]` 不要 `⚠️`）——中文散文没问题，GBK 覆盖得了，坏的是 emoji/特殊符号；②**跨进程读文本必须钉死编码**（子进程 `PYTHONIOENCODING=utf-8` + 父进程 `encoding='utf-8', errors='replace'`），别依赖 locale；③**回归用例要模拟目标 locale 真跑一遍构建**，并做负向对照证明它在修复前会红（`test_catalog_survives_a_gbk_locale`）。同族问句：**这段文本最终会被谁、在什么编码下读一遍？** |
| **G. DB schema / migration** | 建表 / 改列 / reset 路径 | ✅（`test_*_migration.py` `test_account_reset_purges_all_tables.py`） | — | ⚠️ | prod 用户极少，clean reinstall 迁移可接受（**须任务明确授权**）；reset 必须 CASCADE 清干净 |
| **H. compose / enclave / 链上不变量** | `deploy/docker-compose*.yaml` `enclave_app.py` compose 段 | ✅ | ✅（envelope roundtrip） | — | **compose 任何字面量变更 → `compose_hash` 变 → 重新上链**（`deploy/DEPLOYMENTS.md`） |
| **I. CVM runner 镜像 / 部署** | `deploy/Dockerfile.agent-runner` bump | — | — | ✅ **必跑** | `phala inspect` 确认 image tag == 目标 hash；`deploy/verify-remote.sh`；litellm 版本没变=桥行为没变 |
| **J. iOS** | `App/FeedlingTest.xcodeproj`（+ Widget target） | Xcode build/test | — | — | **DESIGN.md token 合规**：禁裸 hex / 裸字号 / 裸字体串，用 `Color.feedling…`/`Font.feedling…`/`Spacing.*`/`Radius.*`；改 UI 前先读 DESIGN.md；**动了发送/重试/合并逻辑必查发送状态机走查表**（sending→sent 本地字段不丢；sending→failed 后 text 的 clientMsgID 仍在；重试走对端点复用同 UUID 不出第二气泡；poll 合并跨 source 收敛；用户真心连发两条不被误合并——iOS 无单测 target，走查即测试）；**开关/文案的唯一权威源是 `Localizable.xcstrings` 的 name/description**——admin data-track 的中文标签是另一套词表（后端 `ambient` = admin「陪伴」= App **「心跳」**），照抄它写任务信会写出错误前提 |
| **K. 公开文档** | io-onboarding 的 `skill.md`/`quickstart.md`/`troubleshooting.md` | — | — | — | 在 **io-onboarding repo** 改并 push（不在本 repo）；`skill.md` push 即对所有装机 app 生效、无需 rebuild；改 agent 行为要**双改**（本 repo 代码 + skill.md） |
| **L. 智能合约** | Solidity / `forge` | `forge test -vvv` | — | ⚠️ | `forge build --sizes`；部署测试合约走 `deploy-test-contract.yml`（手动） |
| **M. 多 worker 共享状态** | 引入进程内共享缓存/状态 | ✅（`test_multi_tenant_isolation.py`） | — | — | 必须接 `core/wake_bus.py` 失效广播，否则多 worker 分叉；核对库 `max_connections`（每 worker +~17 连接） |
| **M2. 跨 worker 的「做过没做过」记录** | 任何「读整个 blob → 改字段 → 写回」的状态：`consumer_state`、冷却/节流时间戳、gate 的已完成标记 | ✅（并发覆盖测试，样板 `test_consumer_state_cas.py`） | — | — | **进程内锁挡不住多 worker**：必须走 CAS（本库样板 `db.set_blob_if_unchanged`）**或数据库侧等价的原子条件更新** + 冲突重读**重算**（不是重放旧决策）；CAS 耗尽要 **fail closed**（宁可不做副作用）。测试必须真 PG 双连接强制过期快照，断言两个写者的不相干字段都不被抹。**外加一层幂等**：副作用（发消息/落行）本身按稳定 id 去重，状态丢了也只发一次 |
| **N. 同一口径存在两套实现** | 同一个指标/判据被算两次（新老聚合并存、SQL 与 Python 各算一遍、两处分日/分桶逻辑） | ✅ **交叉断言必写** | — | — | 用**同一批边界数据**同时喂两条实现，断言**所有同口径字段逐字段相等**（不是各自自测通过就算；两侧 schema 可以不同，要对的是同口径那几个字段——如直方图 `total_users`↔DAU `session_dau`、`median_sec`↔`median_user_sec`，按天 `foreground_sec`/`sessions`↔全时段同名字段）。边界必造：本地零点整 / 次日零点整 / 跨日多条 / 脏值 / 空集。口径漂移不会报错，只会让两个页面各说各话 |
| **V. 收紧校验 / 加白名单** | 给任何字段加 allowlist、加必填、把「宽松接受」改成「拒绝」 | ✅ | — | — | **必须先拿 prod 真实数据跑一遍**:把线上已存在的取值全集导出，逐个对新规则判定，确认**我们自己代码写的值一个都不会被拒**(2026-07-30 实测:memory source 白名单初版会拒掉 5 个自家值，`resident_absorb` 292 条正是 agent 写记忆的默认 source，上线即让 resident 用户每次写记忆 400)。白名单与**写入端必须同源**(共享常量或 parity 测试)，别两边各写一份。**取值全集不止在库里**:动作级、传完即弃的字段(如 `capture_mode` 只随 action 走、不落卡)在存量数据里**根本看不到**，导出存量审不出它——必须同时 **grep 全部写入端代码路径**("谁在发这个字段？每个发出的值都进白名单了吗？")。2026-08-02 hosted `genesis_import` 正是这么漏的:白名单按「存量卡里见过的值」定，三天内全 prod 托管导入 100% 失败(修 ca0c0844)。再问一句:**这条新校验会不会把「清理旧脏数据」的路径也一起焊死?**(如 supersede 若继承旧卡的脏字段，就会被自家白名单拒→脏数据永远清不掉) |
| **W. 同一份数据被两套白名单/预算各管一段** | 任何「A 决定收不收、B 决定用不用」的成对常量;任何跨仓库耦合的容量数字(时长上限 ↔ 处理预算、页大小 ↔ 截断阈值) | ✅ | — | ✅ | **测试里不许写死被测常量**——2026-08-10 一轮内因此红了四次(MAX_TOOLS 50→100→128、MAX_SERVERS 10→30、单工具 schema 上限 8192→32768)。两种失败形态,**第二种更坏**:不是"红了要改",而是**测试照绿但闸再也没被触发**——`test_catalog_count_and_schema_budgets_fail_closed` 的「超大」样本按 8192 造,上限提到 32768 后悄悄不再超限,预算闸整个没被验到。修法:①从模块读(`mcp_core.MAX_SERVERS`);②让被测代码交出来(pi harness 的 mapping 模式额外返回 `cap`,测试不再抄魔数);③样本规模按 cap 推导并**断言样本确实超限**(`assert sum(sizes) > cap, "样本没超过上限,裁剪根本不会发生"`),让"没触发"自己红出来;④跨语言常量(Python ↔ JS)必须有一条断言两边相等的测试,否则改一边忘一边不会有任何报错;⑤**改了任何常量,先跑测那个常量的文件**再跑全量——本次会话三次同型都是漏了这步。**成对的白名单必须有测试逼它们一致**,否则不一致会表现为「全绿地什么都没做」。案例:`CAPTURE_LIVE_SOURCES` 决定哪些聊天行**触发** capture,`worker._CAPTURE_PROMPT_SOURCES` 决定哪些行**真进 prompt** —— 语音行只加了前者,于是 **V2 上语音记忆从 2026-08-05 起被静默丢弃整整两天**:capture 正常触发、渲染出空窗口、不落卡、**游标照常推进**(所以下次也不会重来),日志全绿、无告警、无 notice。修法是 `tests/test_capture_source_whitelists_agree.py`,断言「凡触发必进 prompt」。**规律:当一份数据要连过两道独立的关,只加一道等于零 —— 而且失败形态是沉默,不是报错。**同类第二型是**跨仓库的容量耦合**:iOS 把通话时长上限从 5 分钟提到 1 小时,后端 capture 的转写预算若没跟着提,超出部分会被头尾采样**永久丢中段**,而中段恰恰是「确实读了全文」的唯一证据。修法是让测试**去读另一个仓库的常量**反推所需容量(`tests/test_voice_transcript_budget.py` 解析 iOS 的`max_duration_seconds`),谁调大一边而不调另一边就红。**采样/截断/降级这类兜底路径,必须要么不可达、要么留声**——静默生效的兜底就是静默的数据丢失。**第三型是「上限藏在另一个进程里」**(usr_1baf 2026-08-09):用户 MCP 工具经 pi 桥(一个 Node 子进程)注册,桥里写死 `MAX_TOOLS`,超出部分**按服务器名字母序**丢弃,只往桥自己的 stderr 打一行。于是「测试连接通过(探针直连服务器做 initialize+tools/list)、AI 却说搜不到(那台服务器的工具整个没被注册)」——两件事在两个进程里,谁都没说谎。而且 MCP 工具调用**不经 io_cli**,`agent.tool.call` 里永远看不到它们,所以"trace 里没有 MCP 调用"**不能**当成"模型没调 MCP"的证据(我差点用错)。检查法:①凡是在子进程/另一个运行时里做的截断,必须把结果**回传到主进程的 trace**(现已加 `mcp.surface.resolved` / `mcp.surface.missing`);②「测试通过」的按钮验的是什么、和"模型这一轮真的拿得到什么"差几层,差的每一层都要能被看见;③ 埋点本身要按 driver/lane 分档——只有 pi 走桥,claude/codex 各走自己的 MCP 机制,不判 driver 就会给它们每轮刷一条假告警,把 200 条的 trace 环冲掉(我自己写出过)。 |
| **O. 平行运行时 / lane 重写** | `backend/model_api_runtime/v2/*` 等"把老路重写一遍"的实现 | ✅ | — | ✅ | **逐条核对老 lane 上每一条事故硬化守卫是否随迁**，结果登记 `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md` 的 `Incident-hardened guards — ported?` 表。parity 只记"lane 跑起来了"是**不够的**——07-26 一次核对就挖出 5 处：V2 的 dream 不过同意门、屏幕共享开关接了个空实现、唤醒失败退避不落库、无逐字历史/无时间锚点。**规律：重写会带走功能，也会带走当初为事故加的那道坎**（那道坎往往是三行 if，最不像"功能"）。**另一半是切换本身**：把一个存量用户从老 lane 挪到新 lane 时，新 lane 依赖的**「需要显式播种的状态」必须一并建好**——V2 的心跳生产者读 `v2_wake_schedule`，没有行 / `next_heartbeat_at` 为 NULL 的用户**永远不会到期**，于是主动能力静默消失而聊天照常（2026-07-31：4 个 V2 用户里 3 个自切换起再没写过一张记忆卡，回滚 V1 的两个当天都在正常写）。切换前先列清单："新 lane 读哪些表？这个用户在每一张里都有行吗？" **第三型是「两条 lane 的数据视图字段不一致」**(2026-08-08 语音三连):一个纯函数(`voice.message_filter.conversation_rows`)被 V2 与 resident 共用,判据要读`voice_logical_turn_id`——resident 的视图带,**V2 的 `_decrypt_chat_rows` 非 capture 分支不带**。于是同一份代码在 V2 上把通话中的**每一轮**判成"已被取代"整体丢弃(尾巴全空),同一处缺字段还让 `coalesce` 的 correlation 整组丢弃 → 迟到回复抑制**从未武装**。两个 P0 都只在 V2 表现,单测全绿——因为测试 fixture 手工构造时**顺手把字段补齐了**,比生产视图更完整。规律:**共享判据 + 各自视图 = 判据在字段缺失的那条 lane 上静默失效**。检查法:①凡是共享的纯判据,列出它读的每一个字段,逐条确认**每条 lane 的视图都真的产出**;②测试 fixture 必须严格照生产视图的输出形状写,不许"看起来差不多";③加一条端到端断言,从**真实视图函数**的输出喂进判据,而不是从手搓 dict 喂进去。 |
| **P. Agent 工具 schema / 工具调用可靠性** | `tool_schema.py`、`capabilities/*`、各 lane 的 system prompt（V2 `CHAT_SYSTEM_PROMPT`、V1 `agent_tools_prompt.md`） | ✅ | — | ✅ **必跑** | **判据只能是副作用，不能是模型的话**：模型回「好的/已改」而一个工具调用都没发是常态，验收必须查 effects 队列 / admin trace（`0 pending` = 根本没调）。**隔离复现通过 ≠ 真实上下文通过**：同一个 deepseek-v4-flash，短 prompt 单测必调 identity_patch，长聊天里经常直接跳过——必须在**多轮真实上下文**里验，且**弱模型档单独验一遍**（强模型会替你把规则脑补上）。**新增工具参数必须按"模型最自然的形状"验**：`identity_patch` 是 `additionalProperties=false`，`relationship_days` 当初只藏在嵌套 `patch` 里，模型照 rename 的习惯顶层传 → 参数被丢弃 + **谎报成功**（114496d9）；一级参数就该是一级参数，并且顶层/嵌套两种形状都收。**工具描述不等于指令**：光写在 schema 的 description 里弱模型不照做，要在 lane 的 system prompt 里明写"你**能**改、不许假装改"（f76e7f6a 补 V2 与 V1 的这条 parity）——改这类指令会**一次性打掉 provider prompt cache**，属预期。**写能力必须按「用户在不在场」分档，不能只按「是不是写操作」分档**：wake 轮次必须能写记忆（capture/dream 全靠它），但**不该能改身份卡**——usr_a40e（2026-08-01）一次心跳唤醒里模型自主改了签名和相处天数（1388 天写成编造的 220 天），用户全程没说话。加/改任何 agent 可调的写工具时逐个问：**没有用户在场的那一轮，它调这个会怎样？** 现有分档：V2 `provenance.write_gate` 的 `IDENTITY_WRITE_ACTIONS`；V1/resident 走 `FEEDLING_AGENT_LANE` + io_cli 前置（`tests/test_agent_lane_identity_gate.py` 锁死）。**验收必须含「wake 写记忆仍然通过」**，否则很容易顺手把记忆整理一起砍掉 |
| **R. 认证 / 凭据形态变更** | 调用方换认证方式（api-key → 短时 runtime token、加 scope、换 header）、`accounts/auth_core.py`、`asgi/deps.py` | ✅ | — | ✅ **必跑** | **换凭据不是改一处，是改一族**：必须把**所有**消费旧凭据的调用点列全，逐个确认新形态也能过。托管 runtime 从 api-key 换成 runtime token 后，`backend/memory/routes_asgi.py` 里 `index`/`fetch`/`buckets`/`threads`/`legacy_batch` 五个路由都提取并透传了 token，**只有 `actions`（写入路）没传**——参数 `runtime_token` 一路都在、只有那一行没填，于是**所有托管用户改不了已有记忆卡**（409 `memory_decrypt_failed:RuntimeError:api_key_unavailable`），而**读**照常，问题因此长得像"模型不会改"。**测试必须按凭据形态分档**：现有用例全用 api-key 跑，所以这个组合一次都没被覆盖过——新增/修改带鉴权的路由，**读和写各要有一条"以新凭据认证"的用例**。⚠️ 这类失败**不写 trace**（`core/enclave.py:229-233` 的两个 `raise` 在第一次 `_trace_enclave` 之前），trace 里"零错误"证明不了任何事。**同族第二案（2026-08-01 vision observe）**：zero-roster 托管 consumer 全程用 runtime token，`/v1/vision/observe` 只 `extract_api_key`（空）→ 取图能力空凭证打 enclave → 401 → `capability_forbidden`，全体 43 个托管 API 用户的独立视觉**从未可用**。两条新规：①**"验证通过"≠"链路可用"**——App 的验证按钮走 backend 直连凭证，真实链路走 consumer 凭证，任何用户可见的"已验证 ✓"必须有一条**以运行时真实凭证形态跑通全链**的用例；②新增会转发凭证的端点，zero-roster（空 api-key + runtime token）是**必测形态**，且要覆盖链上**每一跳**——取图修好了 provider-key 解密还会在下一跳挂，失败只后移一步不算修 |
| **Q. 契约收紧 / fail-closed 门禁** | 任何"少了字段就 400/拒绝"的新校验（`prompt_frontier`、gate、必填参数） | ✅ | — | ✅ **必跑** | **上线前必须证明客户端真的能传这个字段**——grep iOS 请求构造 + `Localizable.xcstrings` 确认有 UI/有默认值，不是"理论上能传"；**必须回答"存量 NULL/缺省行会怎样"**（老数据不会自己长出新字段）；fail-closed 必须带**可观测**（计数器 / notice / admin 字段），否则堵死是**静默**的。案例 usr_fee1dfed：后端要求 `context_window_tokens`、iOS 没这个 UI → 07-19 起**所有**自定义中转配置必 400，六天无人发现，白名单直连用户完全无感 |
| **S. 分支同步 / 反向合并**（main→test 回灌、平行开发汇流） | 任何把另一条分支整批合进来的操作，尤其对方分支不包含本侧近期提交时 | ✅ | — | — | **合并会静默吃掉对向刚落的修复，且连守卫测试一起吃**（2026-08-01：liko 在 main 的 vision 重构没见过 test 同日的探针修复，同步合并整体取 main 版——修复语义没了、两个验收测试被删，CI 全绿零声响，同款用户 bug 复活）。三条纪律：①合并后 **diff 近 7 天本侧落地的修复关键行**（grep 事故注释/关键常量），逐个确认幸存；②**合并 diff 里出现"测试文件被重写/删除"= 阻断信号**，删测试必须在 commit message 说明理由，静默消失即打回；③易被重构覆盖的语义（回归修复类）在代码正上方写**事故引用注释**（"曾被 merge 覆盖一次"），让下一个改这里的人撞见历史 |
| **T. 能力探针 / 体检类判定** | 任何"测一下模型/服务能不能 X"并把 verdict 持久化的机制（vision 探针、model_api test、catalog 能力字段） | ✅ | — | ✅ | 四条硬规矩（2026-08-01 vision 探针三连案提炼）：①**没答 ≠ 答错**——空回复/无信号只能判 `failed`（可重试、不弹横幅），只有**非空且明确错误的证据**才许判 `unsupported`；判定分支里"空"必须在"错"**之前**拦截（thinking SKU 在 80/256 token 预算下必然空答，曾被判"无视觉"而真实回合看图完全正常——**探针环境 ≠ 真实回合环境**，token 预算/超时都要按最苛刻的真实形态给足）；②**测试矩阵必含 thinking 形状（空可见回复）与无 catalog 中转形状**；③**声明仅作引导，实测为最终裁决**——任何"我们自己维护的能力对照表"都会过时且**错得很自信**（写死 text-only = 好模型永无翻案机会），只认官方 API 显式返回的字段，其余一律落实测兜底；④**verdict 必须有失效路径**——换模型/换 base_url/换 key 时重置为 untested，否则旧判定粘死（用户换了好模型横幅还在）;⑤**探针自己没跑通时,必须交白卷而不是交结论**(2026-08-10 ElevenLabs 静音轮探针)：第一版把「本机 TLS 连不上」直接印成「最小正文不被接受,需要改用 Skip Turn」——一个完全错误的产品结论,而且看起来很权威。**本地传输失败 ≠ 对端拒收**;**前置轮次没通过 ≠ 目标路径被验证**(第二版两轮都发出去、一条回复都没收到,失败其实发生在普通轮,压根没走到要测的静音路径,判定却照样报了「不被接受」)。凡是「A 通了才能测 B」的探针,必须先断言 A 通,否则输出「什么都没验到」并给非零退出码;⑥**绕过真正对端的探针回答不了对端的问题**——直打自己网关的探针验的是我们这一侧,「外部编排器收不收这个响应」只有让它真跑一遍才知道,别把前者当后者。 |
| **X. 我们是别人的被调用方**(把自己挂给外部编排器当 provider) | `backend/voice/routes_asgi.py` 的 Custom LLM 网关;任何"对方按它的协议来调我们、并根据我们的响应决定整个会话生死"的端点 | ✅ | — | ✅ **必跑** | **语法合法 ≠ 协议可接受。**2026-08-08 线上事故:三条「这一轮不说话」的路径(噪音轮 / 通话已结束 / ASR 修订被取代)都返回了一个 HTTP 200 的 SSE 流,**但零个 content 块**。ElevenLabs 判 `1002 custom_llm_error` 并**杀掉整通电话**——失败面从「这一轮没回复」升级成「这个人打不了电话」,而用户看到的文案是「换一个响应更快的主模型」,把排查引向完全错误的方向。三条硬规矩:①**"什么都不返回"要当成一种返回来设计**——列出所有 early-return 分支,逐个问「对方收到这个会怎么解释」;出口是**开集**(以后还会有新的"这轮不说话"),所以保证要放在**共用的响应构造器内部**,不是在每个出口打补丁(漏一个就是又一次事故);②**兜底内容要有证据,不能靠推测**——第一版用一个空格,无法证明对方不会 trim 掉后仍判空;改用**对方自己文档里的**缓冲串,理由是线上每通正常电话都经过它,所以**已知**它不被判空;③**这一环只有真跑才验得到**——直打自己网关的探针绕过了对端,证明不了对方收不收(2026-08-10 起有 `tools/e2e/elevenlabs_silent_turn_probe.py`,⚠️ 尚未跑通,现状与已排除项写在文件头)。**另:同一文件里早有一条注释知道「返回 4xx/5xx 会被拆掉整通电话」,只处理了 4xx/5xx、漏了"200 但空"——修一个失败形态时,把同族的其他形态一起列出来。** |
| **Y. 缓解措施 / 守卫 / 埋点本身**(声称"我做了 X"的机制) | 任何新加的 guard、兜底、诊断字段 | ✅ | — | ⚠️ 视情况 | **这类代码最容易在"它到底做没做到 X"上撒谎,而且谎言看起来很权威。**两个真实案例,都在 2026-08:①**守卫承诺了它兑现不了的事**——通话 cancel 里加的「有内容就留着行等后续 finalize」,而 `voice_call_cancel` 在守卫**之前**就把状态写成 cancelled,`begin_finalize` 见到 cancelled 永远返回 cancelled、路由 409:留下的行**永远等不到那个 finalize**。守卫写完必须端到端问一遍「我承诺的那件事,在它所处的生命周期里真的可能发生吗」;而且那句假承诺**已经写进了公开文档**——撤守卫时文档要一起撤。②**埋点报了错的那个数**——MCP 工具面的 per-server 计数第一版报的是**丢弃前**的发现数,于是一台被裁光时照样显示「有 4 个工具」,恰好把这条埋点唯一要回答的问题答错;更糟的是**我自己的测试还断言了那个错值,把它当成功能正常的证据**。硬规矩:**报数必须是"生效之后"的数**;新埋点的验收要问「如果它要监控的那件坏事现在正在发生,这个字段会显示什么」,而不是「它有没有输出」。③换了算法要**顺手改文案**——把字母序截断换成轮转之后,诊断信息里仍写着「丢的是字母序靠后的那些」,那比没有埋点更坏(看起来权威,会把排查引向错处)。 |
| **U. 入住/记忆「处理」管线**(estimate→commit→蒸馏) | `backend/genesis/*`、`plaintext.py`、checkpoint、staged 生命周期、materials 投影、推荐模型 | ✅ | — | ✅ **必跑 `tools/e2e/processing_probe.py`** | **这条路的失败几乎都逃得过单测**——2026-08-03/04 那批上线前,真跑一次抓出四个契约测试与单测全绿却真实存在的问题,四个各代表一类:①**分支基点缺 test 上的修复** → 蒸馏 100% 挂在 `capture_mode_invalid`,而报错完全不指向真因(缺的是 `ca0c0844` 的白名单一行)——**任何跨越多日的功能分支,合并前必须 rebase 最新 test 并在 rebase 后重跑**,本地手工补丁不能替代;②**开关组合改变代码路径** → `FEEDLING_GENESIS_V2_ENABLED` + `COMBINED_MAP` 本机默认关、三个 compose 全开,不镜像 compose 的开关就是在测另一条路(而且代码注释写着 "prod runs WITHOUT COMBINED_MAP",已过期五个月)——**本地跑管线前先 diff `deploy/docker-compose.phala.test.yaml` 的相关 env**;③**「完成」的判据必须是分母打满**,combined_map 曾让 24 窗只蒸 8 窗就 done、`windows_total` 仍报 24(用户 2/3 历史静默丢弃、进度条永远到不了头)——凡是「采样 + 后台补全」的两段式,都要断言终态 `done==total`;④**进程死亡后的锁必须能自解**,后端重启后 job 卡 processing、per-user 排他锁不放行 → 用户被锁 30 分钟(**每次部署必现**),修法是同机用 `kill(pid,0)` 判权威死亡立即回收、跨机才靠心跳老化。另有两条判据来自客户端:**status 帧的 materials 必须单调非减且首帧非空**(曾 3→0→3,横幅计数闪烁),**中转站要测两家**(`/models` 目录格式差异极大:带日期后缀/带方括号标签/裸名,推荐链路只测一家不够)。**合并前可先用本机全栈真跑**(serve_dev + dev-seed enclave + 本地 PG,client 已放行 127.0.0.1),比上了 test 再查便宜一个量级。 |

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
- **别囤测试账号**：优先复用，用完 `POST /v1/account/reset {"confirm":"delete-all-data"}`（用账号自己的 key）；新建就存 `user_id+api_key+keypair`，否则删不掉（无 admin 删除口）。**探针的 cleanup 路径必须和主路径一样带传输重试**——test 网关 TLS 抖一下，主流程重试活下来了、teardown 却直接死，账号就漏在线上（07-26 漏了两个）；每个新探针都要有 `--cleanup-orphans`。
- **孤儿清单是全局共享的，并行 session 会互删**：`~/.feedling-e2e-orphans` 所有 session 共用，别人跑一次 `p0.py --cleanup-orphans` 就会把**你正在用**的账号当孤儿删掉——表现是长跑探针中途莫名 401 / admin 查 `user_not_found`。撞到就先怀疑这个，别去查鉴权。
- **探针轮询非 200 必须硬失败**（`raise SystemExit`），不许忽略继续循环：否则"账号半路没了"这类事故会被静默藏十几分钟，再以别的形状炸出来。这是 e2e 假 PASS 的同一个形状，2026-07-26 又犯了一次。
- **用户投诉的原话，先全仓 grep 一遍**：usr_a40e 报"AI 一直说没接上"，那句话根本不是模型生成的，是我们自己的 `FALLBACK_REPLY` 硬编码文案（`chat_resident_consumer.py:448`）。**先确认这句话是谁写的，再谈模型有没有问题**——省掉整轮跑偏的 provider 排查。
- **prod 的配置值只能从运行时读，读代码常量必错**：2026-07-27 我拿
  `_UNAUDITED_DEFAULT_FALLBACK_TOKENS = 32768`（代码默认）算 prod 预算，推出"上下文
  装不下"这个根因；实际 `deploy/docker-compose.phala.yaml` 早把
  `FEEDLING_V2_UNAUDITED_DEFAULT_CONTEXT_WINDOW_TOKENS` 覆盖成 **131072**（`27c76414`），
  预算是我算的 4 倍多，假设完全不成立。**凡是"env > 配置 > 代码默认"这种优先级链，
  分诊时必须从最高优先级那层查起**——`deploy/docker-compose*.yaml`、CVM 注入的加密 env，
  代码里那个常量是最后才轮到的。同族：`runtime.test_status=ok` 也只是"轻量 ping 通"，
  不是"真实生成能过"。
- **别人可能已经查过同一个 bug**：同一天 zhihao 在 origin/test 上已定位并修复了这次事故的
  真因（compaction 自锁 `30793ab4`），而我在旧基线上独立查了半天还查错了方向。
  **动手排查 prod 事故前先 `git fetch && git log origin/test --since=<事故日> --oneline`
  扫一遍**，尤其看提交信息里有没有出现同一个 user_id——一次 grep 省掉整轮重复劳动，
  也避免两个人各修一半在同一个文件里撞车。
- **`runtime.test_status=ok` 骗人**：它只证明轻量 ping 通了，真实生成仍可能全部 timeout（廉价中转限流/欠费/过载）。判"中转是否真活着"要看 `provider_attempt_ledger` 尾部的 `outcome`。
- **openai_compatible 中转验证，`test_status:ok` 之外还有两个独立坑**（2026-07-27 Kimi/Moonshot 验证）：
  ① **key 有区域锁**——同一家中转多个区域 endpoint，key 只在签发区有效：Moonshot 的 key 在 `api.moonshot.cn` 返 200，同 key 打 `api.moonshot.ai` 直接 `401 Invalid Authentication`。用户报 `provider_test_failed` / 401，**先核 `base_url` 区域是否配对 key 的签发区，再谈 key 废没废**（先 `curl {base_url}/models -H "Authorization: Bearer <key>"` 隔离 provider 侧）。
  ② **「能回话」≠「记忆/工具能用」，必须单独验一轮带记忆写入 + 工具调用的回合**——但**没有任何配置字段能替你预测这件事**。曾经的 `responses_unsupported` warning + `supports_responses` 探测（setup 打中转 `/responses`）是错的，2026-07-27 已删除：它的前提「LiteLLM 强制 responses→chat-completions 桥接 mangle codex 工具循环」三条全失效（网关已退役；`openai_compatible` 派生 `pi` 而非 `codex`；V2 全程 `chat_completion_async`，`/responses` 在 `provider_client` 唯一入口是 `provider == "openai"`）。实测：Kimi/Moonshot 在 V1(pi) 与 V2 两条路径上记忆写入、下一轮回读、工具调用全部正常（V2 trajectory 记到 `tool_call_started`/`tool_call_result` 各 3 次）。**验法只有跑真回合**：写一条事实 → 下一轮问回来 → 查 `/v1/memory/index` 有卡；要白盒就查 `v2_trajectory_events.event_kind`（明文列，`user_id` 过滤，删号会 CASCADE 掉，必须在 teardown 前查）。
  旁证（可复用基线）：enclave 能连 `api.moonshot.cn`；Kimi `kimi-k2.5` 经 openai_compatible 端到端可用、原生 thinking 正常。验证走 L2/L3 真链路（`tests/e2e_model_api_test.py` / `tools/e2e/`，register→setup→send→客户端解密）——openai_compatible 只需 setup 传 `provider=openai_compatible` + `base_url` + `context_window_tokens`。
- **改用户可见文案前，先从屏幕反向追到抛点**：确认这条 error code 在**目标运行时**真会走到用户面前。V2 抛的是 `prompt_frontier_exhausted`（裸协议码），不是 provider 的 `context_overflow`——改后者的话术对 V2 用户一个字都不会生效（07-26 险些上线一条死分支，撤回）。
- **判"某个缺陷修没修好"，先确认你的用例真能让它复现**：称谓泄漏只在**账号没名字**时发生，拿有名字的账号怎么测都是 0——不是修好了，是根本没触发。概率性缺陷的验收用例必须先证明"改之前它会挂"。
- **上下文注入了新成分，就要证明模型真读到了**：问一个**答案只存在于新注入段**的问题，判分（`tools/e2e/temporal_probe.py` 的做法：问"距上一条多久"，答"刚刚"判 FAIL）。"prompt 里有这段字符串"≠"模型用上了"。
- **index 对齐的旁路数组必须喂毒样本**：任何"按消息序号对齐"的附加结构（时间戳块、引用表），它的 skip 分支必须和渲染循环**逐字一致**；用空 content、NaN 时间戳跑一遍——错开一位就是给每条消息标错时间，而且全程不报错。
- **枚举"合法的东西"在开集上必然失败**：判据靠白名单/锚点列举时，先问这是闭集还是开集。称谓改写器四轮补白名单全被新反例推翻，最后连"产品复合词不接限定词"这个前提本身都被证伪——开集上唯一正确的动作是**不做**，换成 prompt 约束 + 遥测度量。
- **gatekeep/自测"只跑改动到的文件"会漏——加表 / 改共享函数签名会触发 changed-file 集之外的守卫**（2026-07-29 vision/voice/activity 大整合，合进 test 后被 CI 连挂两轮）：① 新增 DB 表 → `test_tee_table_registry.py::test_every_rds_table_is_registered`（每张 RDS 表必须在 `tee_shadow/table_registry.py` 声明 lane）——迁移文件在 diff 里、但这个守卫测试不在；② 改了共享函数签名（如 voice 给 `call_agent_cli` 加 `stream_update`）→ 波及**所有** mock 它的测试，其中很多不 import 被改文件、不在 changed set 里。**对策：改动含"加表 / 改共享签名 / 平行运行时"时，gatekeep 必须按 CI 原命令跑这几套**——`test_tee_table_registry` + `test_tee_registry_guard_enforced`、`grep -l chat_resident_consumer tests/test_*.py | pytest`（consumer 耦合集）、Round-3 V2 套、Hosted V2 safety 套（命令见 `.github/workflows/ci.yml`）——**别只跑 changed files，那正是 CI 反复抓你的地方**。另：本机 Python 3.10、CI 是 3.12，签名/行为差异也可能只在 CI 冒出。
- **回退不许悄悄降级成"更差但形状相同"的结果**：`io_cli.cmd_identity_read` 先打 enclave 的 decrypt-and-serve，失败就回退到后端同名端点——而后端那个**按设计返回密文信封**，于是 agent 拿到一份 `ok: True` 的密文，如实报告"被加密长字段占满"。docstring 写的回退意图是"没配 enclave 时"，但那个 `if` 同时命中"配了但这次调用失败"。**真正的代价不是这次读不到，是把真因的证据一并销毁了**——enclave 的状态码/响应体整个被吞，至今不知道它为什么失败。写回退时问两句：降级后的结果**形状相同但语义更差**吗？失败原因**还留得下来**吗？
- **`resident` 一词两义，读数据前先确认是哪个**：接入路线 `route=resident` 指**用户自己的服务器**；而托管运行时 `state=resident`（`resident_cli`）指**我们 CVM 上的 V1 consumer**（`hosted/config_store.py:559`）。`/v1/admin/runtime-allowlist` 报的是**后者**——我按前者读，把两个 API-key 托管用户误判成自有服务器用户，整条分诊方向都偏了。同族前案：后端 `ambient` = admin「陪伴」= App「心跳」。
- **"trace 里零错误"证明不了没错**：失败若在第一次写 trace **之前**抛出（`core/enclave.py` 的 `enclave_unavailable` / `api_key_unavailable`），trace 里一个事件都不会留。另外 ring 只有 200 条且被 `perception:*` 高频解密刷满，活跃用户只回看得到约 1 小时——**事故要趁热拉**，凉了就只能靠代码推。
- **判"环境挂了"前先验域名存在性**（`dig @1.1.1.1 <域名>`）：本机 VPN 的 fake-IP（198.18.x）会把 **NXDOMAIN 域名也"接住"**——TCP 能连、TLS 握手被掐，形状和"ingress 半死"一模一样。2026-08-01 排查 test"宕机"烧了半小时才发现 `api.feedling.dev` 压根不存在（正典是 `test-api.feedling.app`）。同时**恢复验证必须打正典公网域名**——拿网关内侧/旧域名验"已恢复"会误报（codex3 同日踩过）。
- **连续 push test = 连环整 CVM 重部署窗**：每次 push 触发全量 phala 重部署，公网断数分钟（TCP 通 / TLS 断 / `phala ps` 空 = **部署切换态的正常表现**，不是崩溃）；多人接连 push 会把窗叠成"持续宕机"假象。定性前先对 CI deploy run 时间线；**部署窗内绝不手动重启容器**（会和部署控制器打架，把可恢复状态搞成真事故）。跑 e2e 前确认没人在连环 push。
- **e2e 断言别写死单一状态码**（register 是 201 不是 200，写 `st == 200` 白炸一轮还留孤儿号）；孤儿号凭证**先落盘再断言**，任何一步炸了都能凭 creds 文件善后。test 环境 e2e 全配方(域名/信封大写 `K_user`/vision 路由须先 warmup 让 runner 注册能力头)见 `tools/e2e` 与 Router entry msa53tbe。

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
[ ] 新增"没有就拒"的必填/门禁：客户端能传（有证据）+ 存量行有出路 + 拒绝分支可观测
[ ] 在平行运行时里重写了老 lane：老 lane 的事故守卫逐条核对，parity 矩阵已登记
[ ] 动了工具 schema / 工具调用指令：弱模型档 + 多轮真实上下文验过，判据是 effects/trace 不是模型的话
[ ] asgi_app.py diff 仅装配/注入（理想零 diff）；无向上 import；无 app.py facade 引用
```

---

- **双签范围内的改动**（用户可见行为/共享接缝/并发存储原语/加密账号链路/prompt
  注入文本）：有独立 gatekeep 记录（清单见 RELEASE_TESTING_PROTOCOL §2.5）。

## 8. 一句话

**对号入座（§2 矩阵）→ 逐层拿证据（§1 工具箱）→ 满足 DoD（§7）才叫完成。** 规范的重点不是"多测"，而是"**改了哪类、就精确补哪几项、每项有硬证据**"。
