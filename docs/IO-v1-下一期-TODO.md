# IO v1 · 下一期 TODO(memory capture 可靠性 + 可观测 + eval)

> 2026-06-28 · 起因:resident(Codex)onboarding 测试里 chat live 通了,但用户明说"我叫 Z""我的狗叫崽崽"**没落卡**。扒下来发现这不是单点 bug,是一组下一期要补的事。
> 本期只做了应急 guardrail(skill)+ "capture now" 调试按钮 + flow-trace M0;下面是下一期的正经活。

---

## 北极星(reliability boundary)

**当用户明确给出稳定事实时,系统不能静默丢掉 —— 要么写卡,要么进可追踪的 capture queue,要么明确记录 skipped reason。**
不恢复旧 memory floor、不要求 onboarding 批量生成 memory。只补这条可靠性边界。

---

## ⚠️ 工程纪律(碰加密必跑真实 e2e)

**涉及 `envelope id` / `K_enclave` / `AAD` / 加密 envelope 的任何改动 → Definition of Done 必须包含"真实部署 e2e 通过",别只信本地。**
本地 fake-decrypt e2e 只验业务流程,**不验 AEAD AAD / enclave 真解密 / 真 readside**。0628 教训:`92f6849` 事后改 `envelope["id"]` 破坏了 AAD(`owner|v|item_id`),**本地全绿**、真实 test 部署 e2e 才抓到"id 看着稳、实际 enclave 解不开"(readside 空、fetch unavailable);`c9c5a5e` 修对(加密时用原 id 作 `item_id` + 后端校验 id 不等就拒)。详见 memory `io-crypto-changes-need-real-deploy-e2e`。

---

## 现状(下一期的起点,已扒清)

- io_cli **没有"写记忆"的 verb**(只有 memory-index/fetch 读 + identity-write)。记忆写入只有两条路:
  1. **inline**:agent 自然回复里自夹结构化 `memory.*` action → `execute_agent_actions` 执行。**Codex CLI 路短路只取 reply、不解析 action(`chat_resident_consumer.py:~2426`)→ 对 Codex 是死的**;CC 类靠输出格式、不稳。
  2. **capture lane**:consumer 攒够触发 → 专门 prompt 逼 agent 吐 JSON 卡 → `parse_capture_cards` → 写回。结构化 prompt,理论上对任何 agent 更稳。
- 触发是 **breakpoint-gated**:24 轮(`record_chat_append` 服务端每条消息查)/ 安静 20 分(consumer 打 `/v1/capture/tick`)。**短对话到不了阈值 → 根本没触发** = 那次"我叫 Z"没落卡的真因。
- 结论:写入设计其实是**收口到 capture lane**(consumer 驱动 + 结构化 prompt,不靠 agent 自觉);inline 是不稳的旁支。

---

## A. memory capture 可靠性(本期核心,P0)

- [ ] **A1 fast-path:显式稳定事实立即触发 capture,不等 breakpoint。** 用户明说要记 / 明显稳定事实(preferred name、role/background、long-term preferences、boundaries、协作风格)→ 立即 enqueue 一个 capture job。
  - 待定:**怎么判定"显式稳定事实"** —— ① agent 信号(又回到不稳)② 后端轻量启发式(我叫/记住/我的X叫…)③ 先用手动 "capture now" 按钮够测就行。倾向先 ②/③,别依赖 agent 自觉。
- [ ] **A2 可观测:capture 的 queued / ran / skipped(带 reason)进 flow-trace 面板** —— 显式事实不再静默消失,hx 测时一眼看到"进队了/写了/跳过为什么"。(接 flow-trace M0)
- [ ] **A3 capture 那轮的结构化输出可靠性** —— 验 Codex 在 "只许吐 JSON 卡" 的 prompt 下到底吐不吐得出可解析 JSON。若连 capture 都不稳 → 需给 resident runtime 一个**结构化 action 输出协议(reply + actions)**。【**Codex/zhihao 域**:resident runtime / Codex wrapper】
- [ ] **A4 onboarding validate 加 memory-capture readiness 检查** —— "chat live ≠ capture 可用",validate 不能只证明聊天通。
- [ ] **A5(次要/可选清理)去掉 memory 的 inline**(`execute_agent_actions` 的 memory.* 分支),全收口到 capture;**identity.* 的 inline 必须保留**。注:对 Codex 本来就没在跑,删=零变化,**不紧急**,当顺手清理。
- [ ] **🔴 A6 agent 沙箱禁网 → 读不了记忆(0628 实测 bug,真实用户会踩)。** codex agent 子进程沙箱(`--sandbox read-only` / 默认)禁了 io_cli 的网络 → agentic recall 读不到 memory API → 用户问记忆 agent 假性"查不到"(卡其实在)。影响 codex-driver(host+VPS),claude 不受影响;onboarding 只验聊天回路、不验读记忆 → 静默溜过。修=① **host**:`spawners.py:_default_cli_cmd` codex 默认命令加联网沙箱(`--sandbox danger-full-access` 或 workspace-write+network_access,flag 按 codex 版本验);② **VPS**:`skill-resident-agent.md` 文档化"沙箱必须放网"+ 给对的 codex 命令;③ **onboarding validate 加"真让 agent 跑一次 io_cli 读记忆"检查**(网络断就 fail+报因,不再静默)。待 Codex/zhihao 确认 codex 默认沙箱+flag。详见 memory `io-agent-sandbox-blocks-memory-read`。
- [ ] **A7(P2)capture/dream failed retry backoff + failure visibility。** 0628 修了 capture/dream 调度的"永久 pending / failed 不重试"红线(`450ce49`/`6d50518`,failed 现在能重试、靠 quiet window 节流,不是热循环)。剩下的 backoff **不是上线红线**:它解决"LLM/agent 连续坏掉时反复烧资源、日志刷屏"。加指数 backoff(连续 failed 拉长重试间隔)+ 失败可见性(连续失败次数/原因进 flow-trace,别静默重试)。**不急,单独排。**

M0 已上:原语 + 端点 + 面板 + 埋了 route/genesis/memory 三组(双门控、默认关、零上线影响)。M1:
- [ ] **B1 埋 consumer**:`agent_call` / `actions parsed` / `reply posted` / `capture lane`(Codex 建议优先这组 —— 直接回答"agent 跑没跑、落没落卡")。
- [ ] **B2 blob 换 `db.log_append`** —— 现在环形 blob 是 load-modify-save,并发会丢事件;换流式追加。
- [ ] **B3 turn_id 透传** —— memory 工具请求头带上 turn_id,多轮测试不靠时间猜。
- [ ] **B4 再铺 voice 注入 / proactive / perception 埋点。**

---

## C. eval(P1)

- [ ] **C1 capture 判断 eval**(承接 M3:自然陈述的持久事实漏捕获)—— 用例覆盖"我叫 Z""狗叫蛋子"这类显式稳定事实**必须被捕获**;"顺嘴 ramen"这类**不必**。eval 驱动调 capture prompt 判断阈值。
- [ ] **C2 memory 读写质量 eval** —— 召回精度(agent 选对卡)、capture 精/召回、resolve-before-create 去重(不膨胀桶)、supersede 正确。
- [ ] **C3 语言 eval** —— 验本期刚加的语言约束:中文对话→中文字段(无 "pets"/"travel"),专有名词保留;en 对话→英文(出海不返工)。
- [ ] **C4 reliability-boundary eval** —— 显式稳定事实在短对话里**不被静默丢**(要么写、要么进队、要么 skipped+reason)。

---

## D. guardrail / skill 收尾(P2)

- [x] onboarding guardrail("not a gate" ≠ "skip all stable facts")已加 `skill.md` line 133 + `skill-api.md`(io-onboarding test `5d564d0`)。
- [ ] **D1** 若测下来 agent 还过度克制:平衡 skill 里其它几处单边"not a gate"措辞(189 onboarding prerequisite、644 summary)。
- [ ] **D2** skill 落卡 baseline 段画清 **inline(仅显式/identity/纠正)vs capture(ambient/后见之明)** 的分工,别让前台 agent 把 ambient 也 inline 落。【prompt 域 = Seven】
- [ ] **D3** 本期语言约束(5 个 prompt)是 Seven 的 prompt 域 —— 知会她、措辞她可调。
- [ ] **D4 onboarding 文档收口:VPS 初始化无"蒸馏 memory"这一步(v1 定论,hx 0628 拍)。**【Seven · 用户面叙事】
  - **定论**:VPS resident 路初始化**不做历史回扫/蒸馏** —— identity 先行 + garden 靠对话自然生长 + 0 卡合法,**不在 onboarding 从已有信息/对话史扫一遍生成卡**。是有意的 v1 取舍(`IO-memory-v1极简重做-给codex.md:23/:69`"画像/自动蒸馏 v1 不做"),不是切 agent_runtime 弄丢的。
  - VPS skill 本身**已干净**:`io-onboarding/skill-resident-agent.md:22`("no four-pass / no floors / garden grows naturally")。
  - ⚠️ **HOST genesis 蒸馏(上传历史→服务端 worker 蒸出 voice/identity/memory)是单独例外、有意保留 —— 别误伤。** "不回扫"只针对 VPS resident 路。
  - **要删/重写的旧残留(覆盖多路 + genesis 路可能仍需保留 → route 拆分重写,不能一删了之,故归 Seven 不由 CC 单方 gut)**:
    - `io-onboarding/quickstart.md`:整篇前提即旧模型(L3、L15"bootstrap 核心=从对话史抢救记忆/空账号没意义"、L33、L121"four passes/几百张卡"、L130 floors 5/15/30)。
    - `io-onboarding/troubleshooting.md`:floor 排障(#4 重做四遍扫、`feedling_memory_verify` floor,中英各一份)。
    - `io-onboarding/skill-api.md:23/:45-46`:残留"memory may optionally be seeded from imported history"(model-API 路可选门)。
  - 备份记忆:CC memory `io-vps-no-memory-distillation`。

---

## E. carry-over(已做完代码、待验/待并)

- [ ] **E1 老卡迁移(§3)** 代码 + 3 轮 Codex review 完,在 `feat/memory-card-migration`;待 hx 真机测(测试清单已于 2026-06-28 通过盖章后归档删除,见 E4)。
- [ ] **E2** flow-trace M0 在 test;hx 真机验主流程(`docs/V1-功能效果核对清单-给hx.md` 带面板签名)。
- [ ] **E3** test→main 别人负责,不归 hx。
- [x] **E4 legacy→v1 migration 真实 test 部署 e2e —— 已通过(0628,`c9c5a5e` 之后)。** seed 4 张老卡 → `legacy_batch` 命中 → `capture/tick` enqueue → 第一批 `migrated=3/4`(1 张 CAS stale 留下,符合预期)→ 第二批 `no legacy`、`migration_state=done`、`legacy_remaining=0`。迁移后:**4 个原 id 都在、能解密读出、bucket/threads 非占位、内容保留、并发改动("蛋子是比熊")没被覆盖、再 tick 不重复**。真环境盖章完成。(过程中真实 e2e 抓到了本地漏掉的 AAD 解密 bug → 见上方"⚠️ 工程纪律"。)

---

## 分工速记

- **CC(我)写代码 + 出计划**:A1/A2/A4/A5、B*、C 的 harness、文档。
- **Codex review + 测** + **A3(resident runtime 结构化协议)主理**(consumer/runtime 是 zhihao/Codex 域)。
- **Seven**:D2/D3/D4(prompt + onboarding 用户面叙事域)、capture/落卡 prompt 的措辞。
- **hx**:真机测(E1/E2)、拍 A1 的判定策略 + 上线节奏。
