# Feedling MCP — Changelog

> Landmark diffs over time. Two months from now, this is how we remember
> when a decision was made and why.
>
> Source-of-truth for "where we are now" is this changelog plus `git log`.
> `PROJECT_BRIEF.md` and `ROADMAP.md` were retired 2026-04-20 — historical
> references to them below are preserved verbatim.

---

## 给 Claude Code 的说明

**每次开新对话时**，请按顺序读：
1. `CHANGELOG.md`（最近的变化——尤其是最上面 3-5 条）
2. `CLAUDE.md`（当前 repo-level guardrails；`HANDOFF.md` 已删除）

**每次完成一个 task 或做出决策时**，在文档顶部追加一条记录。格式见下面。

---

## 记录格式

每条记录格式统一：

```
## YYYY-MM-DD

### [TAG] 一句话标题
- 改了什么 / 发生了什么
- 为什么改（如果是决策类）
- 影响哪些文档 / 任务
```

**Tag 用哪些**：

| Tag | 用在 |
|-----|------|
| `[DECISION]` | Open Decision 被拍板（记录拍了什么、为什么） |
| `[DONE]` | Task 完成标记 |
| `[BLOCKER]` | 遇到卡住的问题（不是普通 bug，是影响方向的） |
| `[PIVOT]` | 产品方向的重要调整 |
| `[UI]` | UI 设计稿更新或 UI_SPEC 变化 |
| `[FEEDBACK]` | 内测反馈驱动的改动 |

---

## 记录正文（最新的在上面）

## 2026-07-04

<<<<<<< Updated upstream
### [DONE] enclave Flask→FastAPI/asyncio 迁移，全仓 flask 依赖清零

`backend/enclave_app.py`（2333 行 Flask 单文件，全仓最后一个 flask 使用方）
迁移为模块化 `backend/enclave/` 包 + FastAPI/asyncio，16 个 task 完成
（主 backend 的同类迁移已在 PR #44 合入，见 992d908；enclave 是收尾）。

- **模块划分**：`config/keys/attestation/state/envelope/auth/backend_client/
  readside/visual` + `routes/{health,envelope,memory,worldbook,chat,identity,
  frames}` + `asgi_worker.py`（enclave 专用 UvicornWorker）+ `serving.py`（内嵌
  gunicorn 组装、TLS PEM 材料化）；`enclave_app.py` 收缩成 ~30 行薄入口，
  compose 命令 `python -u backend/enclave_app.py` 不变（compose_hash 不变）。
- **混合并发模型**：11 条路由全 `async def`；enclave→backend 回环换成进程级
  `httpx.AsyncClient`；解密批处理按请求整批下放 `anyio.to_thread.run_sync`
  （libsodium/cryptography 释放 GIL，真并行，事件循环不被 CPU 工作阻塞）；
  whoami 缓存的 singleflight 从 `threading.Lock` 改 per-key `asyncio.Future`；
  `/v1/envelope/decrypt` 保持"每次实时解析、绝不走缓存"不变。
- **手工 Range/ETag**：`/image` 路由手写替代 Flask `send_file(conditional=True)`
  ——单区间 206 + `Content-Range`、非法区间 416、`ETag`/`If-None-Match` → 304，
  是 dstack-gateway 每连接 ~1Mbps 限速下并行分块拉图的关键能力，专项测试覆盖。
  flask-compress gzip 换成 Starlette `GZipMiddleware`（阈值对齐 500 字节）。
- **`FEEDLING_ENCLAVE_THREADS` 语义变更**：从 gthread worker 线程数变为 anyio
  解密线程池容量（CapacityLimiter），默认值仍是 32，环境变量名不变，compose
  不用动。`FEEDLING_ENCLAVE_WORKERS`（prod=2）语义不变。
- **两处有意行为偏差**（spec 明确标注，不是回归）：
  1. `OPTIONS` 请求：Flask 自动 200 → 新栈 FastAPI 路由表行为，未注册的
     OPTIONS 回 405 + `Allow`（`test_enclave_routes_health.py`）；
  2. `/v1/envelope/decrypt` 非对象 body：旧代码曾 500 → 新代码归一为 400
     （`envelope.py` 内联注释 "有意偏差 #2"）。
  错误码/错误字符串（含两套并存拼法 `missing_api_key`/`missing api_key` 等）
  逐字保留，未借机统一命名。
- **依赖清零**：`backend/requirements.txt` 删除 `flask>=3.0.0` /
  `flask-compress>=1.14` 两行及其头部说明段；`uv pip compile` 重新生成
  `requirements.lock`，diff 只消失 flask 系传递闭包八个包（flask/
  flask-compress/itsdangerous/blinker/jinja2/werkzeug/brotli/backports-zstd，
  后两个是 flask-compress 的压缩算法依赖），无关依赖零变化。新增
  `tests/test_no_flask_anywhere.py` 守卫（grep 全 backend 无 `import flask`
  + 逐个 import 11 个 `enclave.*` 模块断言 `"flask" not in sys.modules`），
  锁死"全仓无 flask"不变量。
- **验证**：全量测试 2125 passed（较主迁移后基线 2037 净增 88，全部来自本次
  新增的 enclave 单测），4 skipped，9 xfailed；已知非本次引入的 2 个失败
  （`tests/test_data_track.py` 两个 fast-validation 断言，未改动过、与本次
  迁移无关）与 `tests/test_api.py` 收集错误（需 `:5001` 活服务，非本次回归）
  按既有基线原样保留，无新增失败。`docker-compose.memory-sandbox.yaml` 冒烟
  通过：`up -d --build` 后 backend + enclave 均 healthy，`curl /healthz` →
  `{"ok":true,"ready":true}`，`/attestation` 正常返回 quote/measurements，
  enclave 日志确认跑在 `enclave.asgi_worker.EnclaveUvicornWorker` 上。
- **test CVM 待验收**（不属于本次范围，spec §5/§8，由用户驱动上线后跑）：
  TLS 钉扎三条硬验收——① `openssl s_client` 取实际 served leaf cert，
  sha256(DER) 与 attestation 指纹比对必须相等；② iOS 审计卡实测通过；
  ③ 最低 TLS 版本 ≥1.2（握手对 TLS1.1 必须失败）。理论上钉扎不破（同一份
  PEM 材料化路径），但 uvicorn/gunicorn 构建 SSLContext 与现有自定义
  `ssl_context` hook 的协商细节可能有出入，需真实 TDX 环境验证。另需在
  test CVM 上过一遍 chat/memory/identity/frames e2e、Range 并行分块拉图、
  runtime-token 与 api-key 双路径。
- **影响文档**：`docs/superpowers/specs/2026-07-04-enclave-asgi-migration-design.md`
  是本次设计的 spec 源文档。本条目工作树未 commit（worktree
  `enclave-asgi-migration`，基于 test 分支），由用户在验收后提交。

### [DONE] 自定义陪伴频率 wake_interval_sec + 心跳激活门（三层，已 ship）
=======
### [DONE] 丢回合重投递兜底：consumer respawn 窗口不再永久丢用户消息（未 commit / 未部署）

- **背景**：prod ASGI 切换当日全量巡检发现 3 条 model_api 用户消息永久无回复。根因不在
  HTTP 层：supervisor 一检测到 config changed（model_api/setup、driver、preferences 写入都算）
  就 signal 15 respawn consumer，而 consumer 重启后游标从 checkpoint/最新历史 ts 播种，
  `since` 严格大于 ⇒ 重启前落库、未回复的消息被永久跳过——claim CAS 机制再也看不到它。
- **修**（server 为主）：`chat/service._pending_chat_messages_for_poll` 加重投递兜底——
  `ts <= since` 的 user 消息在同时满足「未回复 + claim 空/过期 + 在
  `FEEDLING_CHAT_REDELIVERY_WINDOW_SEC`（默认 3600s，0=关）窗口内 + 在未回复尾巴上
  （其后没有已回复的可见 user 消息，见 `_redelivery_floor`；verify_ping 不算对话也不重投）」
  时仍可投递。**backstop claim 走 DB CAS 严格模式** `db.chat_try_claim_reply(redelivery=True)`——
  两轮 Codex review 抓出的多 worker 缓存问题都封在这一条 SQL 里：① 拒绝**任何**未过期
  claim（含自己的；不同于正常路径的幂等自刷新）——防止把进行中的重投回合再发给它的
  claimer 跑重复回合；② `NOT EXISTS` 在 claim 时刻权威判定 supersede 尾巴——父消息
  reply_status 元数据更新不跨 worker 广播，缓存侧 `_redelivery_floor` 可能漏掉「更新的
  消息已被回复」，没有这条乱序迟到回复仍可能发生。缓存侧检查降级为快速预过滤
  （prod 跑 4 worker，缓存不可信）。重试节奏 = claim TTL：`CHAT_POLL_CLAIM_TTL_SEC`
  默认 120→600——重投递生效后 claim 过期意味着重复回合（双烧 provider，409 只挡双落库），
  TTL 必须盖过最长正常回合。
- **批量恢复走滚动**（第三轮 Codex review）：per-poll 重投预算
  `FEEDLING_CHAT_REDELIVERY_BATCH_MAX`（默认 5）——consumer 逐条跑回合（30-90s/条），
  不设预算的话大批 claim 的尾部会在处理到之前过期（重复回合）+ 超出解密拉取量；
  超预算的消息**不 claim**、下次 poll 立即接上（滚动恢复，不是等 TTL）。预算只管
  backstop，`ts > since` 的活对话永不受限。
- **配套**（consumer 两处）：`tools/chat_resident_consumer.py` 解密窗口改
  `_poll_decrypt_since(last_ts, poll_messages)`——重投的消息 ts 在游标之前，原
  `since=last_ts` 取不到明文会走 wedge-skip 白烧 claim；现在窗口拉回到批次最旧消息之前，
  且拉取量 `_poll_decrypt_limit` 按批次放大（2×批次+20，下限 50）保证整批可解密。
- 测试：`tests/test_chat_poll_redelivery.py`（16 项，含 supersede/窗口/TTL/排序/陈旧缓存
  ×2/预算滚动×2 语义）+ `tests/test_consumer_decrypt_since.py`（6 项）；全量回归通过。
- 部署注意：backend 镜像 + runner 镜像都要更新（consumer 走自身 self-update 亦可）；
  乱序回复不可能出现——重投只发生在「其后无任何已回复消息」的尾巴上。
>>>>>>> Stashed changes

两件事一起落地（Seven 主导）。**① 自定义陪伴频率**:用户在设置页选「陪伴频率」7 档
(15min–12h,**默认 2h**),per-user `wake_interval_sec`(clamp `[900,43200]`),consumer 即时
生效、无需重启。为什么:原写死全局 30min 太勤/费 token——Seven 拍默认改 2h、最小档 15min(去 10min);
硬地板同步提到 900,防客户端绕 UI。**② 心跳激活门**:**首次成功聊天前,所有自发主动唤醒
(heartbeat/photo_added/arrived_at_anchor/unlock_after_absence/screen_watch/introduction)在
enqueue 前拦掉、零 token**;首聊成功后自然开启。为什么:用户刚填 API key、卡 onboarding、根本没和
AI 说过话时就开心跳纯烧 token、不符预期(Seven 要求加门)。

- **后端(Codex)**:`core/store.py` 加 `wake_interval_sec`(常量+`normalize_*`+F1 keep-old-on-save)
  与 `first_chat_ok_at` flag(幂等、不进 save 白名单防伪造激活);`proactive/gate.py`
  `_build_proactive_v2_wake_decision` decision 带 `wake_interval_sec`,且**未激活 && 非 manual →
  `activation_pending` 不 enqueue**(优先于原 wake_control);`perception/service.py` 4 条 direct-wake
  旁路(`_maybe_wake`/`_fire_wake`/`_submit_wake_event_v2_compat`/`_fire_wake_event_v2`)同拦;
  `agent_runtime/supervisor.py` post-respawn introduction 也拦;`chat/routes.py` **flip 点**=
  `/v1/chat/response` 成功回复 `role=user & source=model_api` 消息时 `mark_first_chat_ok()`。
- **consumer(CC)**:`_proactive_tick_interval_for_broadcast_state` 加 per-user 入参,读
  `decision.wake_interval_sec` + clamp `[900,43200]`,env fallback 对齐 7200;调度点 L6271 接线。
- **iOS(CC)**:设置页 `proactiveCard` 7 档 Menu 选择器(默认高亮 2h、ambient 关置灰)+
  `ProactiveStateResponse.wake_interval_sec` + `updateProactiveSwitch(wakeIntervalSec:)` + 双语文案。
- **验证**:全量 PG e2e **1462 passed**(10 个 pre-existing 非本次,已用清洁 origin/test worktree 隔离);
  consumer 189 passed;iOS `xcodebuild`(iphonesimulator) **BUILD SUCCEEDED**。双向 review(CC 审后端 /
  Codex 审 consumer)。e2e 中揪出并修:F1 introduction 旁路未拦、11 个 perception 测试漏更新激活。
- **commit**:test `0bc9e7d`、iOS main `758c823`。默认 30min→2h、最小 10min→15min。
- **影响文档**:`docs/WAKE_INTERVAL_CUSTOM_PLAN_2026-06-29.md` 整合成 SHIPPED 最终版(含激活门)。

### [BLOCKER→FIXED] claude 有 Read 授权仍"没权限读图"——非交互缺 permission-mode → 幻觉

单 runner + wedge 修好后,claude(sonnet-4-5)发图**仍"我需要权限才能读取这张图片"**。
本地用部署完全同款命令 + claude-code 2.1.195 复现并二分定位:**即便 `Read({home}/images/**)`
在 `--allowed-tools` 和 settings.json 里都有**,`claude -p`(尤其 `--output-format stream-json`
的 thinking 路径)在**非交互**下仍**拒绝自己的 Read**——allow 规则被当"提示",默认 permission
模式无交互审批者时对文件读 auto-deny,vision 模型于是**瞎编**(实测回过"棒棒糖小熊",探针图实为
蓝底黄框 MANGO-7391)。二分结论:元凶是缺 permission-mode,不是授权缺失;`--allowed-tools` 单独
(无 settings.json)反而能读,加了 settings.json 的 `permissions.allow` 但没 `defaultMode` 才触发拒绝。

修:两个 claude 命令 builder 都加 **`--permission-mode acceptEdits`**(`spawners.py`,
`_CLAUDE_PERMISSION_FLAG`),外加 settings.json 的 `permissions.defaultMode=acceptEdits`。这是能让
预授权 allowlist 在非交互下被认可的**最小权限**做法,不用 `--dangerously-skip-permissions`
那种 codex 式全 bypass;Bash 仍限定 io_cli。实测:同款命令现在正确读出图(暗号+配色),不再幻觉。
1512 passed。教训:claude headless 读文件要显式给 permission-mode,光有 allow 规则/settings 不够。

### [BLOCKER→FIXED] 测试用户切 claude 后"没响应" — checkpoint wedge + 测试 runner 改单节点

修完 thinking-claude Read 后,测试用户切 claude 发图**仍没响应**(连报错都没有)。SSH 进测试
runner CVM 看日志坐实:consumer 的聊天 checkpoint 卡在 12:11(`checkpoint.json last_ts`),日志
反复刷 `poll returned claimed messages but decrypt history did not include those ids; keeping
checkpoint for retry` —— poll 认领了消息 id,但 `get_decrypted_history(since=cursor)` 返回里不含这些
id(解不出 / 或该消息 ts 正好等于 exclusive `since` 边界取不到)→ `_filter_messages_to_poll_ids`
得空 → **无限重试、cursor 永不前进** → 16:15/16:18 等新消息全被堵。**跟看图修复无关**(该用户
AGENT_CLI_CMD 已带 Read,已验证)。

结构性诱因:测试 runner CVM `feedling-io-agents-test` 跑**两个** agent-runner 容器(0/1),**各自
独立数据卷**(`feedling_agent_runtime_r0/r1`);per-user 聊天 checkpoint 存在卷里、**不在 lease 行**,
所以 lease 从一个 runner 漂到另一个时,会从另一卷的**陈旧 checkpoint** 重放并 wedge。线上 runner
(`docker-compose.phala.prod.runner.yaml`)本就是**单 runner 单卷**,不犯此病。

两处修复:
1. **测试 runner 改单节点**(`deploy/docker-compose.phala.runner.yaml`):2 runner→1 `agent-runner`、
   全新卷 `feedling_agent_runtime_runner`(对齐线上;新卷顺带清掉旧陈旧 checkpoint)。test/prod compose
   分开,不影响线上。CI 的镜像 pin 用全局 sed 匹配,单服务不受影响。
2. **wedge 根因**(`tools/chat_resident_consumer.py`):认领消息持续取不到时不再无限重试——同一 cursor
   连续 `CHAT_POLL_WEDGE_SKIP_AFTER`(默认 5)次 miss 后,`_advance_past_unfetchable` 把 cursor 推过这批
   认领消息的最大 ts(边界情况 nudge `+1e-3`),跳过解不出的消息、放行后续。有界重试保留了瞬时解密抖动
   的自愈窗口。纯函数 + 两个单测。1512 passed。

### [BLOCKER→FIXED] 图片修复不完整:thinking-claude 命令漏了 Read 授权（claude 仍看不到图）

### [BLOCKER→FIXED] 图片修复不完整:thinking-claude 命令漏了 Read 授权（claude 仍看不到图）

PR #40 合并部署后,用户切 anthropic/claude-sonnet-4-5 实测**仍"没获得看图片的权限"**。
SSH 进测试 runner CVM 读到实况:该 claude consumer 的 `--allowed-tools`(AGENT_CLI_CMD)里
**没有 Read**,而 settings.json 里**有**——`claude -p` 下 `--allowed-tools` 覆盖 settings.json,
故 Read 被拒。真因:除 `_default_cli_cmd` 外还有一条 **`_default_thinking_claude_cmd`**
(stream-json/effort,`_claude_cli_should_stream_thinking` 判定 deepseek + anthropic 的
sonnet-4/opus-4/3-7 走它),它仍用 `_io_cli_allow_rules`(无 Read)——#40 只补了非 thinking 分支
与 settings.json,漏了这条。修:thinking 分支也改用 `_claude_allow_rules(io_cli, home)`
(`spawners.py`,一行 + 两个 thinking 测试加 Read 断言)。三处 claude 授权点(非thinking/thinking/
settings)现已统一带图片 Read。教训:改 claude 授权要覆盖**所有** claude 命令 builder。1487 passed。

## 2026-07-03

### [DONE] 聊天 AI「看不到图片/截图/屏幕共享」— cli 路径像素从没喂进模型（API+VPS 双中招）

**真因**:托管(API)+ VPS 用户跑同一份 `chat_resident_consumer.py`,在 `AGENT_MODE=cli`
下,图片虽已解密落盘(`IMAGE_TEMP_DIR={home}/images`)、路径也下传,但**像素从没作为
多模态输入到达模型**。`_prepare_cli_command` 只在模板含 `{image_path}` token 时附图,否则
退化到 `_message_for_agent`——把**文件路径当纯文本**塞进 prompt。而两个 driver 的默认命令
都不含该 token:codex(`codex exec … {message}`)从不带 `-i`;claude(`claude -p`)的
`--allowed-tools` 只有 io_cli 动词、**无 Read**,`-p` 非交互下打不开那个图片文件。结果模型
只看到一句路径字符串,如实回答"看不到图"。`usr_6d8c6387242778cb` 报的「API 屏幕共享看不到
内容」即此。

**改了什么**(分支 `fix/chat-images-cli-vision`,PR #40 → test):
- **codex**(`tools/chat_resident_consumer.py`):`_prepare_cli_command` 检测 codex 命令且有
  图时,注入 `_inject_codex_images`——按图加 **`--image=<path>`**(codex 原生 vision 输入),插在
  `exec` 子命令后。用 `=` 绑定单值而非裸 `-i <path>`,否则 clap 的变参 `--image <FILE>...` 会把
  紧跟的 positional prompt 当成第二个图片值吞掉(精简模板 `codex exec {message}` 会丢消息——Codex
  review P2 抓到)。同时**跳过**误导性的路径文本(codex 直接拿到像素)。已含 `-i`/`--image`/
  `{image_path}` 的自配模板不双注入。修复托管 + VPS codex,VPS 零配置。
- **claude**(`backend/agent_runtime/spawners.py`):默认 `--allowed-tools` 与 `settings.json`
  的 allowlist 加 `Read({home}/images/**)`(`_claude_allow_rules` = io_cli 动词 + 图片 Read),
  紧扣 IMAGE_TEMP_DIR、最小授权,让 `claude -p` 能打开被注入路径的图。自配 `cli_cmd` 的 VPS
  用户需自己加(README 已说明)。
- **文档**:`tools/README.md` 补 CLI 模式"怎么让模型真看到图"一节 + 屏幕帧 `on_mention` 触发词/
  `SCREEN_CONTEXT_MODE=always` 说明。
- 测试:`tests/test_chat_resident_consumer.py`(codex `--image=` 注入 + prompt 不被吞 + 自配
  `-i` 不双注入)、`tests/test_agent_runtime_spawners.py`(默认 claude cmd + settings.json 含图片
  Read)。rebase 到 test 后全量 `tests/` 通过。

**没改**(刻意):屏幕 `SCREEN_CONTEXT_MODE=on_mention` 默认与触发正则——正则已很宽(屏幕/共享/
画面/看到/这个/screen/share/look at…),`always` 有隐私/token 成本,真凶是上面的像素投递,已修。
仅文档化。

## 2026-07-02

### [DONE] 非官方托管模型如实报自身模型（identity honesty，未部署未提交）

deepseek/gemini/中转站等跑在 claude/codex/pi 壳子里的模型，被问「你是什么模型」
会继承壳子自带 base prompt 的「我是 Claude Code / Codex」身份。在三 driver 共用的
追加系统提示（`spawners.agent_home_files` 拼的 `system_append`）顶部按 provider 注入
一段身份改写块：非官方模型如实自称配置的 model id，官方原生 anthropic/openai 不加
任何 prompt。判定与内容均为纯函数（`_is_official_identity` / `_identity_override_block`，
白名单＝provider∈{anthropic,openai} 且 base_url 空；**provider 缺省也按官方处理**——
legacy/native/default 条目（claude→原生 anthropic、codex→原生 openai，同 `_codex_transport`
「missing→native」约定）不误伤，真实第三方一定带显式 provider；自称源＝model id，空则回退
provider 名）。身份块置于 persona 之上、与人设解耦（只压「什么模型」不动「你是谁」）。`base_url`
纳入 `_spawn_identity` 使 endpoint 变更触发 reseed。纯 prompt 软约束，个别模型可能偶发
漏说。**未提交、未部署。**

两轮 Codex review 修复：(1) 缺省 provider 按官方处理，legacy/native/default 条目不误伤
（`_codex_transport`「missing→native」同款约定）；(2) 官方 provider 的 base_url 只有为
**非默认**值才翻非官方——`validate_config` 会给官方 provider 也持久化默认 base_url，单纯
非空不算冒充（用 `provider_client.default_base_url` 比对，惰性导入避免破坏 consumer 导入
契约）；(3) gateway codex 用户 `model` 已被改写成内部 `gw-<uid>` 别名，新增 `identity_model`
贯穿 `_wire_gateway_models`→`materialize_home`，身份块自称真实上游模型而非别名，并纳入
`_spawn_identity`。
spec/plan：`docs/superpowers/{specs,plans}/2026-07-02-agent-model-identity-honesty*.md`。

## 2026-06-29

### [DONE] photo_added 唤醒加 new-photo 提示（拉取式）

相册来新图时,`photo_added` 唤醒(原触发不变)现在多一条 `new_photo` 提示:大概是什么
(scene_hint)/是否截图/时段/photo_id,并告诉 agent「想看就 `photo_read(id, include_image=true)`
拉真实像素」+「看完想说就说,像注意到朋友的照片、不是交差报告」。**拉取式,不自动附图,agent
自判看不看**。consumer 侧:`_new_photo_hint()`(best-effort,取最近 1 张 metadata,失败则空、不崩
唤醒),只在 photo_added job 注入。真机前 e2e:提示渲染正确 + `photo_read --include-image` 解出
~203KB JPEG。提交 `5a13264` + `d9a45ca`。

### [BLOCKER→FIXED] io_cli `PHASE2_VERBS` 冲突 → 所有 io_cli 命令全崩

`schedule-wake` 在 `3f98b39` 被加成真子命令,但仍留在 `PHASE2_VERBS`(「未实现」占位 loop),
`sub.add_parser('schedule-wake')` 启动即 `conflicting subparser` → **io_cli 一跑就崩 → OpenClaw 所有
走 io_cli 的原生工具(photo_read/memory_*/screen_*/schedule_wake…)自 3f98b39 起全废**。修:从
`PHASE2_VERBS` 删掉;加防回归测试 `tests/test_io_cli_parser.py`(PHASE2 永不与真子命令重名 + 子进程
冒烟)。提交 `517f7e1`。教训:加真子命令时必须同步从占位列表移除。

### [DONE] 今日 wake prompt / action 协议改动部署后双向 e2e

`3f98b39`(schedule/cancel wake 工具化、删死 ai_state、request_broadcast 折叠进 message、message=
纯回复)+ `a873eeb`(删 battery 字段、平衡 speak/quiet、数字不照报)部署后验证:
- consumer 侧(CC):prompt 构建无 battery / 有时间锚(距上次 Nh,有间隔才加) / 平衡措辞 /
  无 ai_state / new_photo 只在照片唤醒;schedule+cancel wake 工具 ok;action 分类(sleep /
  request_broadcast→可见消息)ok。
- 后端侧(Codex):`pytest tests/test_proactive_* test_perception_* test_io_cli_*` = **185 passed**;
  `/v1/proactive/scheduled/actions` 真触发链 schedule→pending→fire→fired、cancel→canceled 验证;
  gate 路径 / photo_added differ 链完好。
- 残留(待下次部署一起清,不单独 redeploy):consumer `_split_proactive_actions` 的 `set_ai_state`
  死分类(本次已本地删、未提交);后端 `ai_state` legacy/dashboard 字段(Codex 评估)。

### [BLOCKER] test CVM 重部署脆弱性 + 链上 compose_hash 依赖 gas（运维教训）

排查"consumer 全 401 崩溃循环"时发现根因**不是账号/key/代码**,而是 **test CVM 后端被当天连环
部署(4 次 deploy-test-cvm)搞到不健康**:`test-api`/`test-mcp`(走 dstack-ingress)整层 TLS 挂、
但 enclave :5003 直连口仍活;Andrew 在 Phala 层 restart 后恢复。两条运维教训记下:
- **这台 test CVM 每次重部署会整体 blip ~2min**(enclave 先回、test-api 随后),通常**自愈**,
  不要看到瞬时 000 就当宕机;短时间连推多次才会雪崩。
- **每次 deploy 都要发 compose_hash 上 Sepolia test 合约**(`0x9AC0…F2D5`,owner
  `0xa0eBcd26…`)。`517f7e1` 这次 publish **因部署钱包 gas 不足而失败** → 新 compose_hash 没上链 →
  iOS attestation 会拒新 CVM、挡 onboarding。充 Sepolia ETH + re-run `deploy CVM (test)` job 后
  publish success、白名单补齐。**部署钱包余额是真机 onboarding 的隐性前置依赖。**

## 2026-06-27

### [DONE] 照片解密读取修复 + 快捷指令 app 上报打通（真机 e2e）

感知输入真机排查发现两个问题,均已解决:

- **照片像素解密(真 bug,Codex `9b25544` 修)**:enclave 三处解密(`/decrypt` `/caption`
  `/image`)假设明文是屏幕帧的 UTF-8/JSON,但**照片明文是裸 JPEG 字节**(0xFF D8 FF)→
  `plaintext_parse` 502,agent 永远拿不到照片像素。修法:`_parse_visual_plaintext` 按 magic-byte
  (JPEG/PNG/WebP/HEIC/AVIF)识别裸图并 base64 包装;坏字节/非 dict JSON 仍 fail-closed;屏幕帧
  JSON 路径零改动(无回归)。CC 审计通过。真机验:`photo-read --include-image` → `decrypt_status=ok`,
  **image_b64_len=208696**;`screen-read` 回归 ok。
- **caption(enclave VLM)残留 gap**:**test enclave 未配
  `FEEDLING_SCREEN_VLM_API_KEY`** → `/caption` 在鉴权/解密前返回 503
  `screen_caption_unconfigured`;raw-photo caption parser 目前由单测覆盖,真实出图描述待部署 key 后补验。
- **快捷指令 app 上报**:URL/key/端点全对,问题是 iOS 自动化设成了「确认后运行」→ 改「立即运行」
  后,真机打开淘宝实时落 `recent_apps` + app 信号 ✅。
- **照片分类**:正常(真照片 `is_screenshot=false`)。
- **照片仍是"拉取式"**:photo_added wake 只通知,不自动附图;Seven 倾向改"推送式"(wake 自动附
  解密照片,复用屏幕帧 `images=` 机制)——follow-up 待做。
- 体检表更新:`docs/PERCEPTION_COVERAGE_HEALTHCHECK_2026-06-27.md` §B。

### [DONE] 主动 wake digest：从「健康独大 top-N」改成「均衡跨域桌面 + Agent 自判」

`/v1/agent/perception/digest` 之前第二半 `change` = `notable_changes()` 取 top-8,**只比较量化
数值信号**(vitals/metabolic/weather/activity/sleep/body/cycle)→「什么值得主动提」结构上只能是
健康数值,Agent 退化成身体监测机。现改:后端**均衡铺开跨域近况**,健康折叠成 1 行,与音乐/位置/
app/照片/提醒/天气/心情/日历/屏幕**平级**;**后端不产 flag**,Agent 读桌面自判最多 2-3 条拟人化
值得一提(可跨域组合,优先生活情境而非健康播报)。spec/示例:`docs/DIGEST_CROSSDOMAIN_REDESIGN_PLAN_2026-06-27.md`。

- **后端**:`backend/perception/history.py` 新增纯函数 `cross_domain_recent()`(10 域;health 复用
  `notable_changes` 折叠;轻量 novelty `new_artist`/`long_dwell` 作事实上下文,非排名);
  `backend/agent/routes.py` digest 端点返回 `{days, changes, domains}`(`changes` 保留向后兼容)。
- **消费端**:`tools/chat_resident_consumer.py` `_proactive_perception_digest` 改 3 元
  `(presence, change, domains)`;wake prompt 渲染 `cross_domain_board_json` + 自判指令;旧后端无
  `domains` 时回退 legacy change。
- **测试**:cross_domain_recent 单测(均衡/折叠/novelty/诚实报空/降级)+ 路由 + 消费端渲染/兼容,
  本地 276 passed(4 个 `dstack_sdk` 缺失 error 与本改动无关);DB 测试由 CI 跑。VPS resident e2e 待部署后验。
- **路径**:resident/VPS,不碰 hosted。**全感知接口/字段清单**(对比用):同 spec 文档 §1。

### [DONE] Agent 声音/身份/Genesis：全链路建成 + 部署 test CVM（e2e 待跑）

host(API key)用户的空白 runtime,在 **CVM/enclave 内**从上传历史蒸馏出**声音(persona 文件)+
事实(Garden)+ 展示身份卡**。spec:`docs/AGENT_VOICE_IDENTITY_SPEC_2026-06-27.md`。CC×Codex 分工
(CC=agent_runtime/prompt/审计;Codex=backend/infra),每个 Codex 交付经 CC 审 diff + 重跑测试再 push。

- **流水线**(origin/test):chunked 加密上传 ledger → CVM worker(claim/解密/map-reduce/写) →
  按 `source_kind` 路由(history→声音+事实;ai_persona→adopt 主干;memory_summary→事实;
  user_profile→事实+防火墙) → §7.B persona+voice 跨 job 加密合并 → CC 的 supervisor 激活 hook
  (default-off daemon,`FEEDLING_GENESIS_WORKER_ENABLED`)。事实走 `/v1/memory/actions` 同 capture
  lane schema(source=`genesis_import`)。
- **隐私姿态**(`PRIVACY_MODE=backend_storage_no_plaintext_user_provider_authorized`):raw 上传只存密文;
  解密+LLM 只在 CVM 内、用**用户自己的 key**;persona/voice/identity blob 全加密;outputs 只存 hash/count;
  chunk owner 绑定+完整性校验。**不 overclaim**(明文会发给用户自己配的 provider)。
- **审计抓到并修复**(CC 作为审核员):persona/outputs 明文落库→加密;fresh-start 死锁→gate 放行;
  AI persona 没 adopt→source_kind 路由;声音丢失→§7.B 合并;spec 隐私 overclaim→改精确口径。
- **CC 自做**:host session cap 24、persona seed+解密 reader、genesis gate、`io_cli identity-write`(7.D)、
  激活 hook、`tools/genesis_e2e.py` e2e harness(自助注册测试用户→封装上传→验证)。
- **已部署**:`a3475c6` 把 `:34ce885` 部署到 test CVM,worker 上线(dormant 待 job)。
- **待办**:真 e2e 跑一遍(需一把测试 provider key 喂 harness;`TEST_FEEDLING_RUNTIME_TOKEN_SECRET`
  已确认存在);§7.B order-edge(history 须先于 ai_persona)留 fast-follow;iOS 上传客户端;7.D 触发编排;
  VPS skill.md 的 grounding 子句。

## 2026-06-26

### [DONE] A-full：落卡 capture lane（Phase-1）+ 退役 proactive 模拟工具路（Phase-2）

承接 A-lite（perception 统一到 CLI tools）。memory v1 后端落地后启动，目标：proactive 全走原生
CLI 工具，消灭"把 agent 当裸模型返 JSON"的双路。

**Phase-1 — 落卡 capture lane（对齐《IO 记忆·落卡+Dream 完整方案》第一部分）**
- 独立 capture lane（复用 job 原语，不复用 proactive reach-out 语义）：PR A `2136073` 基座
  (typed `job_kind=memory_capture` + `capture_key` 幂等 + poll 跳 wake gate + 分发)；PR B `457ba01`
  触发 coordinator（append_chat 钩子 + `/v1/device/events` 边界 + `/v1/capture/tick` 静默兜底 +
  `capture_state` 去重）；PR C.1 `e148da2` 落卡 prompt+parser；PR C.2 `bf2cf66` 原生 handler
  （window→原生 call_agent→parse→封 v1 信封→`/v1/memory/actions`，不写 chat/不投递）。
- 触发 = 会话断点（静默 1200s / 退后台 / 轮数 24 兜底），**不是 agent 每轮主动调**。
  **不变量（测试钉死）：关「AI 主动找我」≠ 停记忆。**
- VPS e2e：静默触发→handler 调真 agent 回看 55 轮→写 2 张高质量卡（memory 38→40，桶复用"我们的关系"）。
- 验证交接文档：`docs/CAPTURE_LANE_VERIFICATION_2026-06-26.md`（给工程师独立验证）。

**Phase-2 — 退役模拟工具路**
- 审计发现：VPS proactive reach-out 当时走的就是模拟路（`RUNTIME_V2_DEFAULT_ON=true`）且功能完整，
  native/legacy 是退化旧桩 → 不能直接删。先 P2-1 `a3d2d9b` 补原生 reach-out 同等能力
  （native send_message action-only / schedule_wake·cancel_wake 解 gate / perception digest 改直连
  `/v1/agent/perception` / 唤醒 agent 可调原生 perception·memory·screen / cost 标签 D2），再
  P2-2a `b008909` 翻 test 默认到 native 验证（prod 不动），native VPS e2e 通过后 P2-2b `aa31380`
  删除：`run_tool_loop_v2`(tool_loop_v2.py)、`/v1/proactive/tool/execute` 路由、`_resident_run_agent_v2`、
  `_resident_call_tool_v2` + 对应测试/ci。proactive wake 现在**始终走原生**。
- **保留** `tool_executor_v2`/`tool_catalog_v2`（hosted 前台 chat / dashboard / runtime_v2 仍用）。
- 删后 VPS e2e：手动+自动唤醒都走 native（digest 直连、agent 跑、sleep 有理有据、**零 `/v1/proactive/tool/execute`**、无报错）。
**Tail（同日收尾）**
- Tail-1 修：`FEEDLING_RUNTIME_V2_DEFAULT_ON` 是**共享 baseline**（perception ingress / chat / screen caption /
  hosted chat 都跟它），P2-2a 为验 wake 把 test 翻 false 顺带关了 test 感知 v2；wake 已 native-only 后翻回 true
  恢复（`4d72392`），prod 一直 true 未动。
- Tail-3 `io_cli`：补 `photo-recent` 原生读工具（`b7d52a6`）；`send/wait-for-wake/schedule-wake` 是 native 输出
  动作不是 pull 工具，改成澄清 stub。
- **Tail-2 Dream（方案 Part 2，已 ship）**：`job_kind=memory_dream` 复用 capture 基座（PR D.1 `30378a9`
  prompt+parser、PR D.2 `6a0f687` lane）。夜间/攒量触发→原生 call_agent 整理（merge/thicken/supersede）→
  **只 `memory.supersede` 软退绝不硬删**、不写 chat、不走 reach-out gate、questions 落 status 不发用户。
  VPS e2e：force tick→agent 整理 40 卡→8 consolidations（merged 5 / superseded 31），active 40→17，
  旧卡 status=superseded 仍可 fetch、superseded_by 链保留、零 chat。
- 尾巴（后排）：prod 切 native 是一次 prod 部署（待点头，code 已 native-only）；Dream eval 留后；
  io_cli send/wait/schedule 仍 stub（设计上就是输出动作）。

### [DONE] resident consumer 自动更新（路径感知锁步）

自托管 consumer/io_cli 走 git clone 分发，onboarding 后除非手动 `git pull`+重启否则永远跑旧代码。现在让 consumer 自动跟上后端部署的 commit：

- **后端下发期望版本**：`backend/chat/consumer.py` 新增 `expected_consumer_commit()`（`FEEDLING_EXPECTED_CONSUMER_COMMIT` 显式 pin → 回退 `FEEDLING_GIT_COMMIT`）；`/v1/chat/poll` 三个 return 加 `client_release.expected_consumer_commit`。后端不需要 `.git`。
- **consumer 路径感知自更新**（`tools/chat_resident_consumer.py`）：idle（timed_out）poll 时比对本地 `HEAD` 与下发 commit；**仅当** `git diff HEAD..target` 命中本进程实际加载的 repo 文件（从 `sys.modules` 自动推导 + 显式补 `io_cli.py`/requirements）才 `git fetch`+`checkout --detach`+`os.execv` 原地重启。**无关后端发版不触发**（解决"每次发版都 pull"）。
- **安全边界**：默认开（`FEEDLING_AUTO_UPDATE=0` 关）；脏工作区跳过并告警，不丢本地改动；requirements 变了先 `pip install` 兜底；hosted（`FEEDLING_RUNTIME_TOKEN_FILE` 存在的 in-CVM）禁用（镜像不可变 + attestation）。io_cli 同仓库免费搭车。
- 测试：`tests/test_chat_resident_self_update.py`（纯函数真值表 + 编排 mock）、`tests/test_expected_consumer_commit.py`、`tests/test_chat_poll_client_release.py`。文档：`tools/README.md` § Auto-update、`deploy/chat_resident.env.example`。

## 2026-06-25

### [DONE] 主动陪伴系统打通（心跳/三开关/定时器）+ 感知后端 catch-up（全新信号暴露给 agent）

**主动陪伴（Bug A/B 端到端修复 + VPS 真测）** — resident 路从"从未真正工作"到全通：
- **Bug B（心跳从不触发）**：resident consumer 空 broadcast 派生成 `heartbeat_unknown` 被 gate 拦死 → 改成 `heartbeat_broadcast_off`（对齐 hosted）。`008079f`。
- **Bug A（开关不 gate）**：`/v1/proactive/tick` 改用 `evaluate_wake_control_v2`（移除 legacy dnd/user_state 拦 wake，符合 D6/D16：dnd 只 gate Delivery）；resident `/jobs/poll` 按开关 gate pending job；`/chat/response` 应用 delivery gate（提醒关→写 chat 不 buzz）。`008079f`。
- **resident 定时器服务端 fire**：新增 `POST /v1/proactive/scheduled/fire` + consumer 60s loop（`fire_due_timers`，复用 scheduled gate + 透明回灌）。`f772691`。
- **VPS 三个隐藏阻塞**（修复后才跑通）：env `PROACTIVE_POLL/TICK_ENABLED=false`（总开关关着）→ 开；consumer venv 缺 `psycopg`+`psycopg_pool` → 装（已进 `tools/chat_resident_requirements.txt`）；插件 SIGNALS 扩到 17。
- **真测通过**：心跳→主动消息端到端；C1 关陪伴 gate、F2 不连坐（关陪伴定时仍 fire）、C3 关定时 gate；E2 定时器到点 fire→消息。仅 resident，**API/hosted 一行未动（待砍）**。

**感知后端 catch-up** — iOS 采集远超后端暴露,补齐 `_SIGNAL_FIELDS` 让 agent 真能拉：`7a8be95`。
- 新增 6 信号:`reminders` + `health_activity/body/metabolic/cycle/mood`;扩 `weather`（体感/湿度/降水/UV/预警）、`health_sleep`（core/deep/rem）、`health_vitals`（current_hr/hrv/呼吸/血氧/vo2max）。catalog + resolve + ios_contract（加密 allowlist）+ agent routes + io_cli + 插件 + 测试/fixture 全同步。
- **真机验通**:新包上 focus=true（iOS `2504c3e` entitlement 修复）、睡眠分期 511/298/76/137、心率 68/呼吸 17、身高 156/体重 52、活动能量 — 真值端到端;无数据项诚实 null。

**iOS 修复（拉入）**：`2504c3e` focus Communication Notifications entitlement（focus 修好）、`ed9c54f` 发图 picker 改 fullScreenCover（修跳 home）。

**文档整合**：新增 `PROACTIVE_COMPANION_FUNCTION_AND_TEST_SPEC_2026-06-25.md`（功能定义 + 详尽测试)、`PERCEPTION_FIELDS_REALDEVICE_CHECKLIST_2026-06-25.md`（逐字段真机核对)。删除被取代的时点文档:`ROUND3_REALDEVICE_TEST_PLAN.md`（→ 上述两份）、`PERCEPTION_FIELD_RECONCILIATION_2026-06-23.md`（→ 06-25 字段核对表）。

## 2026-06-25

### [DONE] 即时感知收口：focus/audio_route 可 pull + place_label/气温口径校正 + spec 校准
- **focus + audio_route 暴露给 agent pull**：发现这俩被 iOS 采集、后端也接收存储，但 `/v1/agent/perception` 的 `_SIGNAL_FIELDS` 漏了它们 → agent 实际拉不到（spec 却把它们列为 pull-only "agent 自己拉"）。补：后端 `_SIGNAL_FIELDS` + `_SIGNAL_PERMISSION_KEYS` 加 focus/audio_route（不入默认快档）、`tools/io_cli.py` 加 `EXTRA_SIGNALS`、OpenClaw `feedling-io-tools` 插件 SIGNALS 9→11 + 重启 gateway。**12 个文档信号现在全可 pull**（io_cli `b7a7e3d` / 后端 `e49b6da` 已部署 test；插件改动仅在 VPS）。
- **`place_label` 回退 `outdoor` → `unknown_place`**：`outdoor` 误导（用户没配 geofence 时在家也报 outdoor，像"在户外"）。它真实含义是"有定位但不在任何已命名地点"。iOS resolver + 后端 `resolve.py` + spec 同步;`unknown`（无 fix）保留。**未推**（与下条一起，待用户 push）。
- **气温 `temperature_bucket`(5℃ 桶) → `temperature`(精确摄氏度)**：产品决定——5℃ 桶无价值，天气敏感度低、weather 是 pull-only 不参与 changed，精确值无额外代价。iOS WeatherValues + 后端 catalog/resolve/routes/tool_executor + 5 个测试文件 + spec 同步。**未推**。
- **删除过时 spec**：`Specs/perception-report-fields.md`（2026-06-08 预 V2 版，声称原样上报精确坐标/BSSID/地址，与部署的 V2 加密粗化口径冲突）已删，统一以 `perception-data-and-reporting.md` 为准。
- **本轮 spec 校准**（`perception-data-and-reporting.md`）：修"12 个 key"→14（13 信号 + unsupported，对齐 `EXPECTED_REPORT_KEYS_V2`）；§3.8 日历事件时间标注为**设备本地时区**（ISO8601 带偏移）；§3.4/§3.5/§6/§1.1 同步 unknown_place + 精确气温口径。其余逐字段核过与代码一致。
- 日历加回参会人/组织者、天气加降水预报/体感、HealthKit 加心情(State of Mind)/活动三环/HRV 等**新增**字段 → 用户决定**交给 iOS 工程师**实现，不在本轮 scope。
- 待推 bundle（用户统一 push）：iOS `cf8ece5`(outdoor)/`96540fa`(temp)/本轮 spec 校准；后端 `7bc2218`(outdoor)/`1d4b717`(temp)。

## 2026-06-23

### [DONE] resident agent 原生感知端到端跑通（io_cli + OpenClaw 插件）+ config 去硬编码
- 验证 resident（OpenClaw）经 io_cli 原生工具调 `/v1/agent/perception` **端到端通**：agent 在真实聊天里报出真实电量 / 位置 / 睡眠（睡眠 390min=6.5h、位置 outdoor/home、电量 70%）。
- **根因修复**：OpenClaw `feedling-io-tools` 插件的 config 没经 gateway 交付（`register(api, config)` 收到空 `{}`）→ `path.resolve(undefined)` 抛错 → 发消息时工具崩。改成 **`config → 环境变量 → 报错`（零代码硬编码）**，host 路径放 `openclaw-gateway.service` 的 systemd Environment；插件在 `definePluginEntry` 里声明 `configSchema`；改完必须 `systemctl --user restart openclaw-gateway` 重载（gateway 常驻、缓存插件）。工具失败改返 `{ok:false,error}` 不再崩。
- 清掉 OpenClaw 两处 stale 配置（`skills.feedling` 指 localhost:5001、`lossless-claw`）。
- **注**：插件代码只在 VPS（`~/.openclaw/workspace/plugins/feedling-io-tools/`），**未进仓库**——后续应落仓或转 MCP server，见 `AGENT_CLI_INTEGRATION_SURVEY.md`。

### [DONE] 感知增强：上报城市 locality + ±14 天日历列表 + 日历本地时区（iOS + 后端）
- **#1 city**：iOS location 上报新增 `locality`（反地理编码城市名，如"深圳市"）；后端 catalog/resolver/`/v1/agent/perception` 落地暴露。**有意放开城市级定位口径**（街道 / 坐标仍不出设备）——因 `place_label` 对没配 geofence 的用户恒为 `outdoor`，agent 没有"在哪座城"的感知。
- **#3 calendar**：从"24h 单个 next_event"扩成 `calendar_events` **前后 14 天列表**（含全天事件、按 start 排序、封顶 40 条 + `calendar_events_truncated`），保留 `calendar_next_event` 给唤醒/快照（changed 判定只看它，避免窗口滑动误唤醒）。
- **时区**：日历事件时间改用**设备本地时区**输出（ISO8601 带 `+08:00` 偏移），agent 直接读本地钟点（如 15:00），不靠它自觉用 `now.timezone` 换算——少一处出错点。
- 提交：iOS `4edf2bd`（city + ±14天列表）、`a97a73a`（本地时区）；后端 `31ae6c9`（已部署 test CVM）。后端 71/84 测试过，用**真实信封 body 形状**覆盖。

### [FEEDBACK] 感知字段语义对齐审计（以 iOS 上报端为准）
- 系统性对齐 iOS 采集/上报 ↔ 后端接收/存储/暴露：**契约逐字 1:1，无后端凭空字段**（`timezone`/`temperature_bucket` 等 iOS 端确实存在）。完整对照 + 修正清单入 `PERCEPTION_FIELD_RECONCILIATION_2026-06-23.md`。
- 真机查清各信号语义：`location` 恒 `outdoor`=没配 home/work geofence（resolver 回退）；`sleep`=近 24h 滚动入睡时长（凌晨读=昨晚）；`steps` 凌晨 null=今天还没走（正确）；`weather` null=`WeatherService` 抛错（entitlement 在，疑 Apple Portal WeatherKit capability 未生效，**留工程师**，Xcode Console 搜 `weather fetch failed`）；`calendar` 只读 iOS 已同步的日历账户（飞书/Google 工作日历若没同步进 iOS Calendar 就读不到）。
- `focus` / `audio_route` 后端 `/v1/agent/perception` 未暴露（iOS 在采集）——待补。

### [DONE] iOS 聊天 typing 指示器多条待回修复 + 一个自递归崩溃
- 修"连发两条、reply1 一到就灭点点点"：改用 `pendingReplies` 计数，全部待回落地才隐藏指示器；`isWaitingForReply=false` 时自动归零防卡死。
- 修一个**自己引入的崩溃**：上面改动用 `replace_all` 抽取 5 分钟超时块时，误把新加的 `beginAwaitingReply()` helper **自身函数体**也替换成调用自己 → 无限递归 → **发送即崩溃**（栈溢出）。恢复 helper 体。提交 `a1ecd3e`。**教训**：无限递归是运行时错、`xcodebuild` 能过不代表不崩，改完必须真机/模拟器跑一次冒烟。

### [DONE] docs 清理 + agent-CLI 调研文档
- 删 12 个已 ship 的 Round 3 PR 执行脚手架文档（`PROACTIVE_PERCEPTION_PR1…PR10`；聚合的 `ROUND3_EXECUTION_PLAN` / `RUNTIME_V2_MIGRATION` 保留作 PR 总览 + 迁移契约）。
- 二次清理：删 `PROACTIVE_GATE_V1.md`（V2 后自标 archived、非活跃路径）、`ROUND3_HANDOFF.md`（merge-前交接清单，branch 早已 merge 进 test）、`ROUND3_VALIDATION_STATUS.md`（06-20 审计快照；当前真机状态以 `ROUND3_REALDEVICE_TEST_PLAN.md` + 本 changelog 为准）。`MODEL_API_PATH_P0.md` **保留**——它是托管 Model-API 这条 live 路径的唯一设计文档、且被 `PROJECT_OVERVIEW` 文档索引引用。docs 41 → 26。
- 新增 `AGENT_CLI_INTEGRATION_SURVEY.md`：各 agent（OpenClaw/Hermes/Claude Code/Codex）接 CLI 的机制调研。结论——**io_cli + skill/exec 是有 shell 能力 agent 的通用最小公分母**，不必每个 agent 写专属 adapter；native 插件/MCP 是更强的"升级位"；≥2 个非 OpenClaw runtime 要 production-grade typed 工具就做 **Feedling MCP server**，而不是继续扩散 per-agent adapter。

## 2026-06-22

### [DECISION] V2 baseline 扩展到全部 4 个 rollout flag + prod 也默认 ON
- **背景**：上一条只把 `perception_ingress` / `resident_wake` / `resident_chat` 接入 env baseline。但 hosted(API) 用户线还有 `hosted_wake_runtime_v2_enabled`、`hosted_chat_full_tool_loop_v2_enabled` 仍 OFF → hosted 用户感知数据进来了（ingress 已 ON），但 wake 走 legacy executor、前台聊天不 pull 感知工具（半截）。`screen_caption_enabled` 也 OFF。
- **改动**：
  - 三个 reader（`hosted/wake_consumer.py`、`hosted/chat_routes.py`、`proactive/screen_flag_v2.py`）改为未设值时回落 `core/util.runtime_v2_default_on()` baseline；显式 per-user 值仍优先。
  - `hosted/config_store.py` 停止播种这三个 + perception 共 **4 个** flag；scrub 从单 flag 的 bool marker 泛化为 **set marker** `v2_autoseed_scrubbed_flags`（`AUTOSEED_SCRUB_FLAGS` 列表），兼容旧 `perception_v2_autoseed_scrubbed` bool（迁移进 set 并删除旧 key）。每个 flag 一次性清理历史播种 False，之后运维显式写的 False（per-user opt-out）存活。
  - **prod 也默认 ON**：上一轮已给 `docker-compose.phala.yaml` 加了 `FEEDLING_RUNTIME_V2_DEFAULT_ON: "true"`，所以 4 个 flag 在 test+prod 两个 compose 下都默认 ON。**两个 compose 不用再改**——新接入的 flag 共用同一个 env baseline 自动跟着 ON。
- **screen_caption 隐私决定**：它把屏幕截图外发第三方 VLM(OpenRouter)，原为 fail-closed opt-in。用户**明确选择默认 ON**（含 prod）。reader 仍保留 error→OFF 的 fail-closed。
- **测试**：`test_runtime_v2_default_flag.py` 扩展（4-flag scrub + set marker + 旧 bool marker 迁移 + hosted_wake/hosted_chat/screen_caption baseline）；本地非 DB 回归 200 passed。需 PG 的（`test_hosted_wake_v2_cutover` / `test_model_api_wake` / `test_proactive_tool_execute_route`）交给 CI。
### [DECISION] Perception/Resident V2 rollout flags 改为 env-gated baseline（test 默认 ON / prod OFF）
- **背景**:三个 V2 灰度 flag(`perception_ingress_runtime_v2_enabled`、
  `resident_wake_runtime_v2_enabled`、`resident_chat_runtime_v2_enabled`)默认全 OFF,
  又**没有任何 setter**(只能 `db.set_blob` 直写 per-user blob),test 上每个账号都得手翻,很烦。
- **改动**:
  - 新增 `core/util.runtime_v2_default_on()` 读环境变量 `FEEDLING_RUNTIME_V2_DEFAULT_ON`
    作为三个 flag 的**基线默认**;显式的 per-user blob 值仍然优先(operator opt-in/opt-out 不变)。
  - 三个 reader(`perception/service.py`、`proactive/resident_runtime_v2.py`)未设值时回落到基线。
  - **修坑**:`hosted/config_store._ensure_model_api_runtime_profile` 之前会把
    `perception_ingress_runtime_v2_enabled` 自动播种成 `False`,把每个 hosted profile 钉死、
    让 env 基线失效。现在①不再播种该 key ②对已存在的"自动播种 False"做**一次性** scrub——
    用 `perception_v2_autoseed_scrubbed` marker 门控,只清理一次历史 artifact;**marker 落下后,
    运维日后显式写的 `False`(per-user 回滚/opt-out)会被保留**,不再每读必删(Codex review P2)。
    显式 `True` 任何时候都保留。
  - `deploy/docker-compose.phala.test.yaml` 的 **backend** 服务加 `FEEDLING_RUNTIME_V2_DEFAULT_ON: "true"`;
    **prod compose 不加** → prod 仍 OFF、保留 legacy 回滚口子。
- **为什么不硬 `True`**:这些 flag 的设计就是"翻一个 flag 即回滚,不用回滚代码";env-gated 既解了
  test 的手翻痛点,又不动 prod 的回滚安全性。
- **测试**:`tests/test_runtime_v2_default_flag.py`(6 例:env 基线、显式 override、scrub、保留 True)全过;
  perception/ingress/runtime_v2 回归 89 passed。resident 聊天 consumer 在 VPS 上无需 env——它从
  `/v1/proactive/jobs/poll` 的 `runtime_v2` 拿服务端已算好的基线值。
- **影响文档**:`PROACTIVE_PERCEPTION_PR7_INGRESS_CUTOVER.md` / `PR9_RESIDENT_CUTOVER.md` 里"default
  false"现在应理解为"prod 基线 false / test 基线 true,per-user 仍可覆盖"。

### [DONE] 修 resident reply loop:OpenClaw 输出解析 + verify_loop 真调 agent + skill 路由硬规则
- **背景**:一次 VPS onboarding 实测——onboarding 各步显示"成功 + 发了问候",但用户回消息后
  iOS 一直 loading、永远收不到回复。SSH 进 VPS 看 consumer 日志定位到三个叠加问题。
- **诊断(VPS 日志 + 复现 OpenClaw 命令)**:
  - consumer 收到了消息、解密成功;调 OpenClaw → OpenClaw **回得好好的**
    (`result.payloads[0].text="能看到..."`,status=ok);**但 consumer 解析不出来** →
    `_reply_from_json_obj`/`_agent_turn_from_obj` 不认 `result.payloads[].text`(只认到 `result` 就停)
    → 判 "no usable reply" + `SEND_FALLBACK_ON_AGENT_ERROR=false` → 什么都不发 → iOS 永转。
  - **verify_loop=true 是假阳性**:consumer 见 verify ping 走"罐头 liveness 回复"短路、**根本没调
    真 agent**,所以掩盖了上面的解析失败,让 onboarding 误判通过。
  - agent 还把 consumer 接到了 **OpenClaw**(用户其实在跟 Hermes 对话),并改了 OpenClaw 的
    IDENTITY.md/BOOTSTRAP.md——把"agent_name 别叫 Hermes"误套到"换 runtime 当传输"。
- **改动**:
  - **②(consumer,feedling-mcp test)**`tools/chat_resident_consumer.py`:加 `_openclaw_payload_texts`
    显式 extractor,接进 `_reply_from_json_obj` / `_multi_reply_json_from_obj` / `_agent_turn_from_obj`
    三处,支持 OpenClaw `result.payloads[].text`(含多气泡);加 3 个回归测试。
  - **③(consumer)**verify ping 不再罐头短路,改成**有界真 agent 探活**:慢(>20s,可配
    `VERIFY_PROBE_TIMEOUT_SEC`)→ 回退罐头 ack(不冤枉健康慢 agent);完成但无可用回复 →
    **不 ack,让 verify 失败**(把解析/传输坏掉的链路在 onboarding 阶段就暴露)。
  - **①(skill,io-onboarding main)**`skill-resident-agent.md` 加硬规则:**consumer 的 agent 入口
    必须是收到 onboarding 指令的那个 runtime 本身**,多 runtime 同机时不许改接"更顺手"的兄弟;
    runtime 自报名字是 agent_name 的事,别为此换 runtime 或改 IDENTITY.md/BOOTSTRAP.md。
  - **④(consumer)**自测中发现:test 版 consumer 顶层 import `proactive.adapters_v2`/
    `runtime_v2` → `observability_v2` → `db` → **psycopg**,而 resident(纯 HTTP 客户端、无 DB)
    的 venv 没有 psycopg → 切 test 分支后 import 直接崩。这俩符号只在 proactive-job 路用,
    已改**惰性导入**,聊天回复路 import 即 psycopg-free。
- **端到端自测(真实 VPS + 真实 OpenClaw)**:把 VPS consumer 切到 test 分支、重启(decrypt
  source OK enclave、无 crash),调 `/v1/chat/verify_loop` → **`passing=true`,response 15.1s**;
  consumer 日志 `verify ping — exercising real agent path` → `real agent reply OK`。即
  poll→真调 OpenClaw→解析 payloads→回写 整条链已通,原"回消息没回复"复现并修复。
- **遗留**:① 是 skill 约束,不能 100% 强制 agent 守规;OpenClaw 仍非文档化入口(但现在能解析了);
  proactive-job 路仍需 psycopg(把 `merge_wakes_v2` 从 db-bound 模块拆出是单独的后端清理)。

## 2026-06-21

### [DONE] 修 VPS resident 入驻接不上(MCP→enclave 迁移残留,跨仓库)
- **现象**:test 环境 VPS 用户复制连接信息给自己的 agent,agent 卡在 Live
  connection——consumer 去探 `test-mcp.feedling.app/mcp`、`/sse` 全 404,
  decrypt source 不可达,verify_loop 永远 false。memory/identity 都没问题。
- **根因(三个叠加,均非 agent 的错)**:
  1. iOS `FeedlingAPI.residentConsumerConfig` 仍发死的 `FEEDLING_MCP_URL/KEY`,
     且**完全不发** `FEEDLING_ENCLAVE_URL`(MCP 下线后 consumer 唯一的解密源)。
  2. `skill-resident-agent.md` 让 agent 拉 `origin/main` 且以 HEAD==main 为 gate;
     但 main 停在 MCP 下线**前**(aef4809),那版 consumer 仍走 MCP,与 test 后端对不上。
     enclave 直连 consumer 在 `test` 分支。
  3. 结果根本没人给 `FEEDLING_ENCLAVE_URL`。
- **验证前提**:curl test enclave `-5003s.../v1/chat/history` → 无 key 401、带
  Bearer key 200、attestation 200 → 解密源可用,方案成立。
- **改动(决定:consumer ref 用 `test` 分支;解密走 enclave 直连)**:
  - **iOS**(feedling-mcp-ios):`CVMEndpoints` 加派生量 `enclaveURL`
    (`https://<appId>-5003s.<gateway>`,按环境自动出 test/prod);
    `residentConsumerConfig` 去掉 `FEEDLING_MCP_URL/KEY`、改发 `FEEDLING_ENCLAVE_URL`;
    `connectionDetailsBlock` 删掉死的 "Chat-client MCP command" 行。
  - **io-onboarding** `skill-resident-agent.md`(EN+ZH):连接信息加 `FEEDLING_ENCLAVE_URL`;
    consumer 来源 `origin/main`→`origin/test`、删 HEAD==main gate;新增"agent_name(卡里名字)
    ≠ 选哪个 runtime 当传输,别为改名去换 runtime 或改 IDENTITY.md/BOOTSTRAP.md"。
- **顺带解释用户的"想让 Hermes 连却选了 OpenClaw"**:agent 把"名字别叫 Hermes"误套到
  "用哪个 runtime 传输",还改了 IDENTITY.md/BOOTSTRAP.md(违反"别包新人格")——已在 skill 拆清。
- **未做(留作单独清理)**:chat-client(路由A)的 `mcpConnectionString`/empty-state MCP 命令
  仍是死的;`main` 落后 `test` 两个月的 release 卫生;prod 是否同病(取决于 prod-mcp 是否还活)。
- 验证 enclave 可达通过;iOS/skill 改动仅配置/文档,未跑构建(需 Xcode)。

### [BLOCKER] 感知工具循环只在 wake 路,前台聊天未收敛(违反 D1)→ 已派 Codex
- **审出的缺口**(外部 Claude 排查 + 我代码核实):`run_tool_loop_v2` + `ToolExecutorV2`
  (全 catalog perception+memory)只接进**主动 wake 路**;**前台聊天两路都没接**——
  hosted `chat_routes._run_model_api_memory_tool_loop` 只认 memory 工具(`MEMORY_INDEX/FETCH`,
  无 perception.*),resident `_process_messages`(consumer:3154)走老单发回复、不进 tool loop。
  结果:**聊天时 agent 无法按上下文 pull 感知**(perception 只是被动 push 的快照)。
- **定性**:不是 spec 遗漏,是实现缺口 + **违反 D1**(chat 与 proactive 应是同一引擎)。
  spec 明确要求聊天 agentic 调工具:D1 / §2.1("聊运动 pull 步数")/ §6+B2(前台 agentic =
  路由器本身,D9 硬前置)。
- **派 Codex**(mailbox 20260621T145308Z):hosted+resident 聊天两路收敛到 `run_tool_loop_v2` +
  `combined_runtime_adapters_v2`(全 catalog),flag 默认 OFF;**关键约束**:前台延迟敏感,
  必须守**快档 cost_class 预算 + 软交棒**(D17/D9/§6)——slow 工具走 `needs_background` 后台
  回灌,不能像 wake 那样内联跑 slow 工具把用户卡在"思考中"。审计待 Codex 实现后做。
- 影响文档:`PROACTIVE_PERCEPTION_SPEC_V2.md` §9 B2 状态改为"部分完成"。

### [DONE] Proactive tool-loop execution (D11: bounded multi-turn for both hosted + resident)
- **Unified loop shipped**: Both hosted and resident proactive wakes now run `run_tool_loop_v2()` — a bounded multi-turn agent loop that calls the model, parses `tool_calls` JSON, executes tools, and feeds results back (max 4 iterations, capped at `MAX_TOOL_ITERS_V2`). One shared `ToolExecutorV2` instance per run provides budget continuity and unified tool implementations.
- **Hosted wiring (in-process)**: Proactive runtime injects call-model and call-tool closures into the loop; tools execute immediately in-process.
- **Resident wiring (HTTP)**: `chat_resident_consumer` wraps the external agent call (`call_agent`, Hermes CLI/HTTP) in `run_tool_loop_v2` for V2 jobs only (legacy single-shot path untouched). Tool calls go to the new endpoint `POST /v1/proactive/tool/execute`, which runs the shared `ToolExecutorV2` server-side (perception/memory/screen tools; only `screen.read` reaches the enclave) and returns `ToolResultV2.as_dict()` (ok, outcome, result, error_code, needs_background, trace).
- **Budget handoff stops the turn**: when a tool in a turn returns `needs_background`, the loop stops executing that turn's remaining `tool_calls` and defers immediately — avoids wasted inline work (and an HTTP round-trip per call on resident) after the decision to background.
- **Resident tool-only replies survive normalization**: `tool_calls` are now preserved through the resident agent-output normalizer on every transport — the early-return guards in the string/CLI path (`_agent_turn_from_obj`, `call_agent_cli`) and the OpenAI-HTTP path (`_call_agent_http_openai`) previously only treated messages/actions/thinking as "usable", so a tool-only model reply wrapped in any log/header text was flattened to a plain message and the tool never ran. `tool_calls` are also de-duped (one emission could arrive via multiple nested JSON paths, e.g. an OpenAI `choice.message`), so the loop never double-executes a single call.
- **Impact**: `screen.read` and all V2 tools now reachable end-to-end for both user types (hosted + resident). Cross-HTTP-call budget accumulation for resident deferred (iteration cap bounds cost per turn).
- **Changelog + CI**: Added D11 test suite to `.github/workflows/ci.yml` (4 new test files); `tests/test_tool_loop_v2.py` added to pure-unit conftest. No further changes needed.
- **未做**: Native function-calling; cross-HTTP-call budget accumulation for resident; proactive caption-on-change (tool loop is the foundation, not the feature).

## 2026-06-21

### [DONE] Screen frame VLM captioning via in-enclave OpenRouter (Tasks 1-6 complete)
- **Tasks 1-5 shipped**: New enclave route `GET /v1/screen/frames/<id>/caption` decrypts frame IN-ENCLAVE and calls OpenRouter `qwen/qwen3-vl-8b-instruct` via `provider_client`, returning caption text only (never pixels). Backend never holds plaintext pixels. New backend `screen/caption.py` calls that route, caches caption per frame_id. `screen.read`/`screen.recent` tools now implemented in `ToolExecutorV2` for isolated testing.
- **New per-user flag**: `screen_caption_enabled` (default OFF, fail-closed). Enclave env: `FEEDLING_SCREEN_VLM_API_KEY` (required dstack secret; absent → fail-closed `screen_caption_unconfigured`), optional `FEEDLING_SCREEN_VLM_MODEL` (default `qwen/qwen3-vl-8b-instruct`), `FEEDLING_SCREEN_VLM_BASE_URL` (default OpenRouter). Deployed config documented in `deploy/DEPLOYMENTS.md` § Enclave configuration.
- **Task 6 (docs-only)**: Updated `deploy/DEPLOYMENTS.md` with VLM secret + optional env overrides, non-code privacy prerequisites (user disclosure + OpenRouter zero-retention config). Added to changelog.
- **Follow-up (now resolved)**: at the time of this entry the model multi-turn tool-execution loop (D11) was still pending, so these tools were tested in isolation only. D11 landed the same day (see the D11 entry above) — `screen.read`/`screen.recent` are now reachable by the live agent on both hosted and resident paths.
- **未做（不在本计划范围）**: Proactive frame captioning、per-user API key、on-device VLM、legacy caption deletion。

## 2026-06-20

### [DONE] 三个用户开关（陪伴/定时任务/提醒）端到端落地
- **后端**（test `b4386f9` → 合 test）：`/v1/proactive/state` GET/POST 暴露
  `ambient` / `scheduled` / `reminders_delivery`；`scheduled` 升为 first-class
  持久化；映射单一真相（`enabled=ambient`、`dnd=!reminders_delivery`、scheduled
  独立）。**iOS**（main `c1dbbbc`/rebase）：Settings 三个 RailToggle，按 subset-accept
  只发改动键；**清掉死代码** `ProactiveUserState`/`user_state`/`ai_state`（不符合
  D6，且无处引用）。
- **为什么**：spec §8.2 的三层 gate（Wake/Voice/Delivery）需要三个用户可见开关，
  且**定时任务独立于陪伴**（D16，关陪伴不该连坐闹钟）。此前 iOS 根本没有这几个开关
  入口，后端 settings 也只有 enabled/dnd。

### [DONE] 感知能力 iOS↔后端全量打通（parity review 后收口）
- **起因**：一次跨仓库 parity review 发现 iOS 发的一批信号后端不接、后端能 wake 的
  事件 iOS 不发。逐项收口（后端 test `85adbfb`+`dad4900`，CI 绿、镜像已发；iOS main
  `7663631`）：
  - **weather / health（睡眠·运动·体征）/ focus** → 注册为加密 **pull-only** 信号，
    字段名逐字对齐 iOS，走 enclave 解密 + resolver 丢原始；focus 出 `in_focus`
    pull 提示，**删掉映射到已删 user_state 的死代码**（`resolve_focus`/`_apply_focus`）。
  - **audio_route**（蓝牙锚点的可行子集）→ iOS 读 `AVAudioSession.currentRoute`
    （车机/耳机+设备名），加密 pull-only。任意系统级蓝牙连接 iOS 不给第三方 app。
  - **久别解锁** → iOS 在"前台回归 after >30min 空闲"（gap 端上算）发
    `unlock_after_absence`；后端早已接好（零改动）。第三方 app 拿不到可用的硬件解锁
    事件，"重新在场"才是可落地的最直接信号。
  - **WiFi 锚点 wake（§3.3/D13）** → iOS 发 `wifi_anchor_id`＝BSSID 的端上 HMAC
    （真 BSSID 永不出设备）；后端把解密 token 喂差分器产 `arrived_at_anchor`，**仅在
    iOS `changed=true` 时喂**（挡掉"部署后差分器内存态清空+静止用户被批量误唤醒"）。
  - **后台到达 wake（option B）** → iOS 低功耗 SLC + visit 监测、Always 升权、
    `location` 后台模式。Seven 选 B（"一到某地就主动找你"）而非 A（只 pull 上下文）。
- **设计决策（Seven 拍板）**：连续信号一律 pull-only / 不 wake（§3.1/D5）；focus 只作
  pull 在场提示、绝不复活 user_state（D6/D15）；蓝牙走音频路由子集；WiFi 锚点做哈希
  指纹**自动学**（不靠用户命名，D13）。
- **影响文档**：`PROACTIVE_PERCEPTION_SPEC_V2.md`（§2.1/§3.1/§9 B1↓依赖+B1b）、
  iOS `PERCEPTION_BACKEND_TODO.md`，新增 iOS `PERCEPTION_HANDOFF_2026-06-20.md`。
- **仍需（工程师，非代码）**：后台定位整条链真机验证（无法在本机 build/跑）、Apple
  开发者后台开 HealthKit + WeatherKit capability。

## 2026-06-16

### [DONE] 解除"单 worker 天花板"——后端可跑 `-w N`（LISTEN/NOTIFY 唤醒总线 + advisory-lock 选主）
- **动机**：生产 `gunicorn -w 1 --threads 32`，32 线程是全部并发预算，而
  `/v1/chat/poll`、`/v1/proactive/jobs/poll` 天然挂线程（≤30s）。活跃用户一多，
  等待者吃光线程池、正常请求排队 → 已观察到的 prod 慢/502；且永远无法加 worker。
  根因是 4 类绑死单进程的状态：① UserStore 进程内写穿缓存 ② threading.Event
  长轮询 waiter ③ :9998 WS 在 import 期绑端口 ④ 必须单例的 hosted tick/consumer
  + 明文 `last_seen_api_key`（仅内存）。
- **Layer A 跨进程唤醒/失效**：新增 `backend/core/wake_bus.py`，用 Postgres
  LISTEN/NOTIFY（不引新组件）。写 chokepoint（`store.append_chat` /
  `append_proactive_job` / frame 落库 / 注册表编辑）落库后发 `NOTIFY`；每 worker
  一个常驻 listener 收到非自己来源的通知就 `_evict_store`（就地 reload + 唤醒本地
  waiter）。db 层加 `pg_notify` / `listen_connection`（SQL 归 db.py，协议归 core）。
- **暗雷修复**：① chat-poll 的 reply claim 从"读缓存判可领 + 写穿"改成
  `db.chat_try_claim_reply` 的 DB 条件 CAS（两 worker 不再双投同一回复）；
  ② 用户注册表 `_users`/`_key_to_user` 进程内、查 miss 不回库——register/发 key 等
  真实编辑走 `_save_users(broadcast=True)` 发 `users` 通道，各 worker reload，
  否则新用户在别的 worker 会 401。
- **Layer B 单例选主**：新增 `backend/core/leader.py`（`pg_try_advisory_lock`），
  WS ingest 收进 `run_singleton("ws", …)`，只有持锁 worker 绑 :9998、挂了别的
  worker 接管。
- **Layer C hosted wake 分布式 + 按 key 在位执行**（比原计划更简）：发现 job 认领
  已经是 `update_proactive_job(only_if_status="pending")` 的原子状态 CAS，**无需
  新表/迁移**。tick 改成每 worker 各跑、只处理本 worker 持 key 的用户
  （`_hosted_keyholder_user_ids`，创建+模型调用都需明文 key 故必须在 key 所在
  worker 跑）；重复创建用 `db.try_stamp_hosted_tick` 原子心跳槽 CAS 防住；
  `try_consume_pending_for_user` 作 `proactive` 通道 handler 跨 worker 即时认领。
- **compose**：三个 compose `-w 1` → `-w 2`（先小、可灰度再提 N），注释更新；
  改 compose 字面量会改 `compose_hash`，**部署需重新上链**（CONTRIBUTING §7 /
  DEPLOYMENTS.md）。每 worker 约 +17 个 DB 连接（池 16 + listener 1），调大 `-w`
  前核对库 `max_connections`。
- **验证**：本地 `gunicorn -w 2` 端到端——注册落一个 worker 后 whoami 40/40 全 200
  （users 通道）；一 worker 长轮询、另一 worker 发消息，10/10 轮真实停泊后均
  ~10ms 内被唤醒（跨进程唤醒总线）；advisory-lock 选主 + 接管、claim CAS 单赢家
  均有单测/集成验证。全量 pytest 450 passed（仅 2 个预先存在的 enclave 红用例，
  零新增失败）。新增测试：`test_wake_bus`、`test_chat_poll_claim_cas`、
  `test_hosted_wake_distribution`。
- **Codex review 修复（两轮）**：① 注册表所有真实单用户编辑（注册/发 key/key
  恢复/link-token/access-binding/公钥/偏好）从 `_save_users` 全表
  DELETE+重插改成 `registry.persist_user` 单行 upsert + `users` 广播——否则两
  worker 并发编辑不同用户时，陈旧快照全表重写会抹掉对方刚建的用户（已用 -w 2 并发
  注册 16 用户验证零丢失）；`_save_users` 全表重写只留给 normalization/测试。
  ② `load_users()` 整个 reload 包进 `_users_lock`（它现在也在监听线程上跑，与请求
  线程并发改注册表）。③ chat-poll claim 的 DB CAS 补 replied 状态拒绝
  （`reply_status='replied'` / `reply_message_id`），防别的 worker 已回复后本
  worker 凭陈旧缓存重复认领。④ 账号删除路径补 `users` 广播（否则别的 worker 仍
  鉴权已删账号）。⑤ 缓存型 blob（`tokens`/`push_state`/`live_activity_state`/
  `frames_meta`）写 chokepoint 补 `blob`/`frames` 广播——否则别的 worker 用陈旧
  token/推送冷却到 15min TTL，坏掉推送投递/去重；用线程局部 `_reload_guard` 抑制
  reload 期写穿归一化的回广播（防 NOTIFY 风暴）。⑥ **部署安全**：phala / phala.test
  两个 compose 用 pinned 旧镜像（`857c09e`/`b14c3db`，import 期绑 :9998），保持
  `-w 1`，注释写明须与"换含本 patch 的镜像"同一次部署一起提 `-w`；只有带 `build:`
  的 base `docker-compose.yaml` 设 `-w 2`（从源码构建，安全）。
- **影响文档**：CONTRIBUTING §7 不变量（单 worker → 多 worker 已支持）、
  `core/store.py` / `db.py` 模块 docstring、三个 `deploy/docker-compose*.yaml`。

### [DONE] enclave 改用 gunicorn gthread（撤掉 Werkzeug 开发服务器）
- `backend/enclave_app.py` 的入口从 `app.run(threaded=True)`（Flask 自带
  Werkzeug 开发服务器，非生产级 WSGI）换成**编程方式内嵌的 gunicorn**
  （`BaseApplication`）：`worker_class=gthread`、单 worker、32 线程、
  `timeout=120` / `graceful_timeout=30`。单 worker 精确沿用原"单进程多线程"
  模型——进程内 whoami / content-key 缓存与 singleflight（见文件头）保持一致，
  挡住 history-import 触发的回环鉴权线程风暴。
- **关键约束：不动 compose。** gunicorn 内嵌进 `__main__`，compose 入口仍是
  `python -u backend/enclave_app.py`，所以 `compose_hash` 不变、无需重新上链
  （CONTRIBUTING §7 不变量）。
- **保住自签 TLS + cert-DER pinning。** bootstrap() 派生的 PEM 写到 tmpfs 临时
  文件（0600，atexit 清理；TDX 下 /tmp 是内存盘，密钥不落盘）供 gunicorn 翻开
  `is_ssl`；实际 SSLContext 走 gunicorn 的 `ssl_context` 钩子，复刻原
  `_build_ssl_context` 的精确姿态——裸 `PROTOCOL_TLS_SERVER`、min TLS 1.2、无
  客户端证书校验、无 HTTP/2 ALPN，确保握手服务的正是 REPORT_DATA 里 pin 的那张
  leaf，iOS 审计卡的 `sha256(cert.DER)` 校验不受影响。
- gunicorn import 延迟到入口，`import enclave_app`（测试套件）不强依赖它。
- 验证：自签证书冒烟测试确认 gunicorn 起 https、`/healthz` 走 TLS 返回、服务的
  证书指纹 == 注入证书、明文打 TLS 端口被拒、SIGTERM 优雅退出；
  `tests/test_enclave_route_errors.py` 11 例全过。
- `backend/requirements.txt` gunicorn 注释补上 enclave 用法（`ssl_context` 钩子
  需 >=21.0；已 pin >=23）。**部署：只需 bump CVM 镜像，compose 文件不动。**

### [DONE] 文档：补全历史导入端到端流程（RUNTIME_FLOWS §3.6）
- 把 `RUNTIME_FLOWS.md` §3.6 从"两遍蒸馏"一句话扩成完整阶段流水线：
  异步 job + 轮询、job 复用 / stale 判定、解析历史与支撑材料（过滤账号
  元数据）、关系起点与 small/large 分级、时间窗口提候选、聚类写记忆卡、
  派生身份卡、生成开场问候、`chat_ready` 首批放行、large/ultra 后台续抽。
- §4.3 同步补一句指回 §3.6。
- 只动文档，对照 `hosted/history_import.py:_process_history_import_sync` 与
  `_HISTORY_IMPORT_PHASES` 校对阶段名，未改代码。

### [DONE] 收尾：撤未接线的 wake_interval、加固捕获锁、补测试（push 前清理）
- **撤掉死旋钮**：P4 初版加了 `wake_interval_sec`（唤醒频率），但只存储、无
  代码读它生效——正是 P1 批判的"写了没人读"。按"凡发出去的旋钮都得是活的"
  原则，本批从 `core/store.py` 撤掉它（默认/白名单/校验三处），只保留已真接
  线的 `wake_directive`。频率旋钮连同**实际接线 + iOS + 测试**整体延到后续
  （task #6）。
- **加固捕获锁**：P2 给记忆捕获加的每用户锁，存在一个泄漏窗口——`_start_`
  占用后若 `_append_memory_capture_job`/线程启动抛异常，`finish()` 不会执行、
  用户被永久挡在捕获之外。`hosted/turn.py` 用 try/except 包裹交接段，异常时
  释放守卫再抛。
- **补测试（纯单元，本地全过）**：
  - `tests/test_context_memories.py` +3：strict 软召回 `index_sample`（排除已
    选中、上限 20、转折优先）。
  - `tests/test_model_api_wake.py` +2：`user_directive` 进/不进 wake payload。
  - 新增 `tests/test_history_import_identity.py`（5）：`_normalize_identity_payload`
    的语气字段透传/净化/截断。
  - 新增 `tests/test_model_api_prompts.py`（5）：前台 prompt 含 custom_persona_prompt
    优先级指令 + memory_index 召回指令；persona/索引值进 prompt。
  - 两个文件加入 `conftest.py` 的 `_PURE_UNIT` 白名单（无 DB 也收集）。
  - 全量无 DB 跑：**107 passed**；显式 context 跑：62 passed。
- **补测试（DB 依赖，CI 验）**：`tests/test_identity_actions.py` +2，照搬现有
  通过模式——`custom_persona_prompt` 经 profile_patch 可写回身份体；
  `wake_directive` 白名单/截断/拒未知键。本地无 Postgres 跑不了，CI 验。

### [DONE] P4(backend)：proactive 自定义（D2 power-user）；iOS 待做
- D2 定的是"全自定义（默认值 + 高级区）"。本批后端落 `wake_directive`：
  用户自己的"什么时候来找我"自然语言指令（`proactive_settings`，≤1000 字，
  现有 `GET/POST /v1/proactive/settings` 即可读写）。
- **吸取 P1 教训，不加死字段**：`wake_directive` 已**真接进** hosted wake
  prompt——`model_api_runtime/wake.py` `build_wake_event_message` 增可选
  `user_directive`，`hosted/wake_consumer.py` 从 settings 取并传入，wake
  事件 payload 带 `user_wake_directive`，agent 据此权衡发不发（用户指令、
  非硬规则）。
- **未做（明确标注待做，task #6）**：唤醒**频率**旋钮（连同 hosted tick
  cadence + resident consumer 端接线）、**iOS UI**（proactive 设置面板 +
  `custom_persona_prompt` 编辑入口，在 feedling-mcp-ios 仓库）。

### [DONE] P3：API 召回加"软召回索引"（D3 LLM 软召回，加性、可回退）
- **问题**：model_api 的 strict 召回只放 corrections + 词面命中阈值的卡
  （`context_memory_selection.py` 严格分支），语义相关但词面不重叠的卡被
  硬丢——即 feedback point 2"召回太硬，只是文字对应"。
- **改法（加性，不删词面路径）**：strict 分支用**同一批已解密 moments**额外
  构造一个紧凑 `index_sample`（id/type/title/occurred_at，转折优先+最近，
  上限 20），零额外 provider/enclave 调用。`hosted/context.py` 把它作为
  `context_memory_selection.memory_index` 注入 prompt；`prompts.py` 加一句
  指令：标题/日期相关时可自然"想起"，但不得编造标题外细节、也不强行召回。
  → 模型自己软召回，而非关键词过滤替它决定。
- **性质**：现有 `selected`/词面选择完全不变（可回退）；index 只是补充面。
  index 的字段/条数/措辞属可调内容，留待迭代。
- **验证**：`tests/test_context_memories.py` 59 个纯单元测试全过（含本次改的
  strict 分支）；新增 `index_sample` key 不影响既有断言。

### [DONE] P2(API)：history import 蒸馏语气 + 记忆捕获加每用户锁
- **蒸馏语气（修 4a 角色漂移的"蒸馏端"）**：`hosted/history_import.py` 的身份
  派生 `_derive_identity_with_provider` 之前只产
  `agent_name/self_introduction/category/signature/dimensions`，语气只能塞进
  自我介绍。现在 prompt 增产 `tone_style`（怎么说话：语域/口头禅/称呼/句式，
  要求引用真实例句）、`agent_role`、`do_not_say`、`boundaries`；
  `_normalize_identity_payload` 对这四个字段做净化（长度上限、zh/en 一致性、
  list 清洗）并对空值省略。它们随密文身份体落库 → 经 enclave 解密 →
  P1a 已把它们接进 prompt，**蒸馏端 + 读取端闭环**：API 用户的语气现在
  能跨 import 存活，而不只是 fact。
- **记忆捕获加每用户锁（修 USER_PATHS_REVIEW §8）**：`hosted/turn.py` 的状态
  动作和 recap 各有每用户锁，唯独记忆捕获没有——turn-24 的捕获还在跑时
  turn-48 又触发会重叠、产重复卡。新增 `_model_api_capture_active_users`
  守卫：`_start_model_api_memory_capture_job` 入口检查/占用，运行体的唯一出口
  `finish()` 释放（幂等、覆盖所有 return 分支）。镜像 recap 既有模式。
- 性质：prompt 内容（蒸馏措辞）后续可调；字段结构与锁是骨架。语法校验通过。
- **未做（属调参/成本）**：捕获 cadence 仍是默认 24 轮
  （`FEEDLING_MODEL_API_CAPTURE_TURN_INTERVAL` 可配）；要更"持续"可调小，
  但有 provider 调用成本，归 P4 自定义一并考虑。

### [DONE] P1b：新增 custom_persona_prompt 用户可编辑 persona 覆盖槽（D1 用户层 / feedback 4b）
- 用户反馈想要"一个能自己加 prompt 精准定位角色的地方"。新增单个自由文本
  字段 `custom_persona_prompt`，与系统蒸馏的 `tone_style` 分开、优先级最高。
- 借现成白名单机制，改动最小：
  - `identity/service.py`：`custom_persona_prompt` 加入
    `_IDENTITY_PROFILE_STRING_FIELDS`，故 `identity.profile_patch` 自动支持
    写入（iOS 编辑入口留待 P4）。
  - `identity/actions.py`：两处 max_len 把它归入 1200 字一档（自由 prompt
    需要更长，区别于 240 字的短字段）。
  - `hosted/context.py`：接进 `identity_summary`，随 P1a 一并进 prompt（前台
    聊天 + 记忆捕获 + wake 都读 `context_payload["identity"]`）。
  - `model_api_runtime/prompts.py`：前台 system prompt 加一句——
    `custom_persona_prompt` 存在时视为**最高优先级** persona 指令，压过其余
    identity/profile 文本（安全边界除外）。
- 性质：纯加性，零迁移；空值不渲染。四个改动文件语法校验通过。

### [DONE] P1a：把 persona/语气字段接进 hosted 聊天 prompt（修 API 角色漂移根因）
- **背景（决策 D0–D3）**：本轮定了记忆/identity 重设计的四个地基决策——
  D0 卡库=权威外置记忆库（插件模型）、D1 persona 双层（系统蒸馏 + 用户可
  编辑覆盖）、D2 proactive 全自定义（默认值 + 高级区）、D3 召回改 LLM 软
  召回。落地按 P1（schema 地基）→ P2（持续落卡 + 蒸馏语气）→ P3（软召回）
  → P4（自定义暴露）分阶段推进。本条是 P1 的第一个最小落地。
- **诊断**：`tone_style` / `agent_role` / `do_not_say` / `boundaries` /
  `stable_definitions` 等 persona 字段在 identity 密文体里**能写**（经
  `identity.profile_patch`，见 `identity/actions.py` `_IDENTITY_PROFILE_FIELDS`），
  但 hosted 聊天 prompt 的两个 identity 入口（`hosted/context.py`
  `identity_summary` 与 `history_import.py` `_model_api_agent_profile_context`）
  都**不读**它们——persona 是 write-only 死字段。这是 model_api 用户反馈
  "角色漂移"（只蒸馏 fact 不蒸馏语气）的结构性真凶：两头断（蒸馏不写、
  prompt 不读）。
- **改动**：`backend/hosted/context.py` 的 `identity_summary` 补上述 persona
  字段。prompt builder（`model_api_runtime/prompts.py`）是整包
  `json.dumps(context_payload)` 注入、不挑 key，故此一处接线即同时惠及
  **前台聊天**与**记忆捕获 worker**（两者都读 `context_payload["identity"]`）。
- **性质**：纯读取侧加性改动，零 schema 改动、零迁移；字段为空时只渲染空值，
  模型忽略。值在 P2 蒸馏阶段填入——"先接线、后填值"。
- **验证**：语法通过；未跑 DB 测试（本地无 Postgres，按 repo 约定 CI 跑）。
  无测试断言 `identity_summary` 的 key 集合，改动不影响
  `test_identity_actions.py` 的已存断言（它校验存盘明文，非 prompt 摘要）。
- **下一步**：P1b 加"用户可编辑 persona 覆盖槽"（D1 的用户层，4b）——需先定
  字段命名/语义。

## 2026-06-16

### [DONE] 新增 docs/USER_PATHS_REVIEW.md：BPS/API 两路功能总览 + 缺漏盘点
- 把 resident（BPS，自建服务器）与 model_api（API，托管）两条用户路的
  Onboarding / Chat+Memory / Proactive 运行方式并排梳理成一份功能向文档
  （不含加密/部署）。
- Part 2 盘点了一次系统性代码阅读发现的缺漏，按 🔴/🟡/🟢 分级：
  - 🔴 假完成/静默失败：model_api 记忆门槛不分档、实时连接没真验证、
    provider 失败致用户消息孤儿且无退避、resident proactive job 无回收超时。
  - 🟡 状态错乱：中途切路由搁浅、记忆删除无引用完整性、天数锚点漂移、
    后台记忆捕获无每用户锁、`tool_action_enabled` 门形同虚设、import job
    无恢复、official_import 疑似死代码。
- 元观察：claim 租约 / 分档门 / 活性验证 / 每用户锁等"三处都该有"的机制
  普遍只实现一两处——根因是缺一张强制对齐两条路的 capability matrix。
- 文档明示：行号为阅读近似值，修复前需就地核对；条目待逐项落进
  OPTIMIZATION_BACKLOG.md。

## 2026-06-12

### [DONE] 测试文件统一收口到 tests/
- `backend/` 下最后 4 个测试迁入 `tests/`：`test_api.py`（活服务器集成
  脚本）、`test_model_api_wake.py`、`test_perception.py`、
  `test_semantic_analysis.py`；后三个加了 tests/ 惯例的
  `sys.path.insert(..., "backend")` 头。
- 本地全量命令简化为 `pytest tests/ -q --ignore=tests/e2e_model_api_test.py
  --ignore=tests/test_api.py`（不再需要带 `backend/`）；CI 的
  test_api.py 调用路径同步更新。
- `tests/conftest.py` 的「无 Postgres 全部跳过」改为豁免 `_PURE_UNIT`
  集合（semantic_analysis / model_api_wake / perception / provider_client），
  没有数据库的机器仍能跑 95 个纯单元用例。
- CONTRIBUTING.md §1 决策表与 §6 测试规范新增硬规则：测试只放 tests/。
- 验证：418 通过 + 2 个已知长期红，零新增失败。

### [DONE] 新增 CONTRIBUTING.md：后端代码组织规范
- 把拆分重构沉淀成团队规则：app.py 只做装配；新路由进领域包 Blueprint、
  新逻辑进 service 层；依赖只准向下（向上用注入钩子）；跨模块调用
  `module.func()` 形式保证 monkeypatch 单点生效；全局单例只就地变更
  不重绑；COMPAT 段只减不增；单文件 800 行预警 / 1500 行强拆；附 PR
  自查清单。
- `CLAUDE.md` 阅读顺序加入该文档（写后端代码前必读）。


## 2026-06-12

### [DECISION][DONE] 移除 MCP 用户条线（路由 A）
- **拍板**：不再支持 MCP 客户端（Claude.ai / Claude Desktop）直连这条
  用户线。现存接入只剩路由 B（Resident Consumer）和路由 C（Model API
  托管）。
- **删了什么**：
  - `backend/mcpsrv/`（13 文件 ~2,180 行）+ `backend/mcp_server.py` 入口
    + `backend/acme_dns01.py`（MCP LE 证书插件，383 行）
  - consumer 的 MCP 解密回退路径（`tools/chat_resident_consumer.py` 的
    `FEEDLING_MCP_URL`/`_fetch_from_mcp`/transport 探测，~250 行）——
    **resident 用户现在必须配置 `FEEDLING_ENCLAVE_URL` 直连 enclave**
  - `tests/test_mcp_session_isolation.py`（2026-05-11 P0 回归套件，保护
    对象已不存在）+ consumer 测试里 5 个 MCP transport 用例；CI 同步去掉
  - 三个 docker-compose 的 `mcp:` 服务块、ingress 的 `mcp.feedling.app`
    域名/路由、`FEEDLING_MCP_TLS_IN_ENCLAVE`；`deploy/feedling-mcp.service`、
    Caddyfile 的 mcp 站点、SELF_HOSTING Option B
  - `fastmcp` 依赖（requirements.txt + lock 重新生成，纯减 587 行）
- **attestation 兼容**：enclave 删除了 MCP 证书指纹派生路径
  （`MCP_TLS_IN_ENCLAVE` / `MCP_TLS_KEY_PATH`），但 bundle 里保留
  `mcp_tls_cert_pubkey_fingerprint_hex` 字段恒为空——iOS 审计卡走既有的
  "Pre-Phase-C.2 deployment" 披露行，不破坏解析。生产 compose 本就设
  `FEEDLING_MCP_TLS_IN_ENCLAVE=false`，行为一致。
- **留了什么（不是 MCP 专属）**：bootstrap 门禁、`/v1/chat/verify_loop`、
  consumer 心跳、`official_import` access_mode（默认值未动，是否砍另议）、
  enclave 解密端点、identity/memory 的 HTTP envelope-action 端点。
- **部署影响**：compose 变更 → 需要新 compose_hash 上链；Cloudflare 的
  `mcp.feedling.app` CNAME/TXT/CAA 记录可清理。
- **外部跟进（其他仓库）**：io-onboarding 的 `skill.md`（MCP agent 说明书）
  需改写或归档；iOS 的 `ChatEmptyStateView.skillURL` 入口与 MCP String
  相关 UI 需同步调整。
- 文档同步：PROJECT_OVERVIEW（§5.2/§5.5 墓碑化 + 拓扑图）、RUNTIME_FLOWS
  （顶部历史注记）、AUDIT.md（row 5/7 措辞）、README、SELF_HOSTING。


## 2026-06-12

### [DONE] backend 单体拆分：app.py 17.6K 行 → 14 个领域包 + 898 行装配层
- 按「功能域分包为主 + hosted 条线单独成包」拆分（方案见 2026-06-11 拍板）：
  `core/`（config/util/enclave/envelope/store——UserStore+缓存）、`accounts/`
  （registry/auth/onboarding/access/recover/routes）、`push/`（apns/tokens/
  live_activity/service/routes）、`screen/`（frames/ws/summary/routes）、
  `proactive/`（service/gate/dashboard/routes）、`identity/`、`memory/`、
  `bootstrap/`（gates+routes）、`chat/`（service/consumer/routes/verify_loop）、
  `tracking/`、`admin/`、`content/`、`hosted/`（model_api 托管条线 8 模块：
  config_store/setup_routes/history_import/context/turn/chat_routes/
  onboarding_validation/wake_consumer）。`mcp_server.py` 2,029 行 → `mcpsrv/`
  包（session/client/server/tools_{push,screen,chat,identity,memory,meta}/tls）
  + 75 行入口。
- 路由全部转 Blueprint，url_map 与拆分前逐条 diff 为零；gunicorn `app:app`
  入口、四容器部署拓扑、`python -u backend/mcp_server.py` 入口零改动。
- 解耦手段：`core/store.py` 新增 `on_proactive_job_appended` 钩子（替代
  UserStore→hosted 的向上调用）；`core/envelope.get_user_public_key`、
  `push/live_activity.load_identity`、`hosted/wake_consumer.flask_app`、
  admin 的 onboarding 验证函数均由 app.py 装配段注入。`_load_users` 改为
  `_users[:]` 就地替换（避免 re-export 分叉）；pepper 改 lazy（import 不再
  要求 DB 可达）。
- 决策变更：`hosted_runtime.py` 与 `model_api_runtime/` 保持原位不吸收进
  hosted/（本就是独立清晰模块，吸收只增加 shim 风险）。
- app.py 仍保留「COMPAT re-exports（迁移期）」段 + hosted 符号兜底回灌循环，
  供测试/工具按旧路径取符号；收敛为白名单是后续独立 PR（见 backlog #6）。
- 测试：436 通过，与拆分前基线完全一致（仅剩 2 个迁移前就长期红的
  enclave 依赖用例）；测试的 monkeypatch 目标已同步迁到新模块。
- 依赖层级（低→高）：db/content_encryption/provider_client → core →
  accounts → push/screen → proactive/identity/memory → bootstrap.gates →
  chat → tracking/admin/content → hosted → app.py（装配）。跨模块调用一律
  `from pkg import module` + `module.func()`，保证 monkeypatch 单点生效。


---

## 2026-06-07

### [DONE] UserStore 缓存加 TTL + 定向 evict 接口（修 register-orphan 恢复不可见）

- **背景**：排查"内测者历史 chat/memory 全没了"，只读直连 prod RDS 证实**数据没丢、
  可解密**——是 `/v1/users/register` 在重装/重连时铸新空账号、孤儿化老账号（详见
  `docs/orphan-account-recovery-plan.md` 与 `tools/recover_orphan_accounts.py`）。合并恢复
  后发现 `/v1/chat/history` 仍显示旧值：chat 读的是进程内 `UserStore.chat_messages`
  缓存，`_load_chat` 只在 store 首建时读一次 DB、之后常驻，无良性重载接口（只有
  delete-all-data 里的 `_stores.pop`）。memory/identity/model_api 实时读 DB，合并后即刻可见。
- **改动**（`backend/app.py`）：`gunicorn -w 1` → 全后端单一共享 `_stores`。UserStore 是
  写穿透缓存（DB 唯一真相），故可安全丢弃重建。
  - `get_store` 加 **TTL**（`STORE_CACHE_TTL_SECONDS=900`）+ `loaded_at`，过期即刷新。
  - 新增 `UserStore.reload()` + `_evict_store()` + `POST /v1/admin/store/evict`（admin
    鉴权）。TTL 与 evict 都走**原地刷新（refresh-in-place，保持对象身份不变）**而非换对象——
    避免 Codex review 指出的竞态：若某请求在过期/驱逐前拿到旧 store、在新对象装入后才写入，
    写会落 DB 但只进旧实例，新实例漏看（chat 走内存缓存不实时读 DB）。原地刷新下永远只有一个
    实例，写穿透不被遮蔽；`reload()` 在 `chat_lock` 内重读，与并发 append 串行化、不丢写。
  - 刷新时 `_wake_store_waiters()` 唤醒长轮询 waiter（chat/proactive），让 park 的 poll 立即
    返回重连、读到刚浮现的消息。
  - 恢复工具 survivor 选择修复（Codex P1）：`_pick_survivor` 改为优先「有 live api_key + 最新
    注册」而非「chat 量最大」——否则刚重装、暂无 chat 的活跃账号会输给有 chat 的死孤儿，把数据
    搬进死账号。修复后 dry-run 还多识别出 1 个被旧逻辑漏掉的用户（`hlv5AVd7…`，98 chat/39 mem/
    identity/model_api 滞留死账号）。
- **部署即生效**：部署本次改动会重启 backend → 冲掉所有陈旧 store → canary 测试者
  `usr_0f93a433a006b702` 的 209 chat 当场现身；之后带外改动走 evict/TTL，不必再重部署。
- 测试：新增 `tests/test_store_cache.py`（TDD，5 项：TTL 命中/过期重载、evict 失效/鉴权/
  刷出带外写）。全套 `tests/` **316 passed**（唯一 1 个 `test_model_api...relationship_days`
  失败为既有、需 enclave attestation 可达的环境依赖，已 git stash 在干净树复现证明无关）。

### [DONE] 密钥找回账号（堵住 register-orphan 根因：换机/重装丢 api_key）

- **根因确认（确是 app 程序问题）**：DB 证据——同一 public_key 名下的账号在**同一秒/隔
  2–3 秒**被批量创建（monster lineage 16s 内 6 个），不可能是人手 → 客户端并发/循环连发
  `/v1/users/register`。两个子 bug：(A) **爆发型**（并发 register）——iOS 已用
  `registrationTask` 串行化修掉；(B) **换机/重装型**——仍漏：内容密钥对走 iCloud Keychain
  **同步**跨设备存活，而 api_key 为修 5/10 重启竞态被改成**仅本机**，换机恢复时密钥对在、
  api_key 没了，守卫标记（UserDefaults）又被重装清空 → 照常 register → 同密钥铸新孤儿。
- **修复（keypair proof-of-possession 找回）**：让"会同步存活"的密钥对当身份锚。
  - backend（`app.py`）：`POST /v1/account/recover/challenge`（按 public_key 找规范账号
    `_canonical_account_for_pubkey`，`build_envelope` local_only 把随机挑战封给该公钥）+
    `POST /v1/account/recover/verify`（设备回传解密结果证明持私钥 → 为既有账号
    `_issue_api_key_for_user_locked` 重签 api_key，**不铸新账号**）。挑战一次性 + 300s TTL +
    `hmac.compare_digest`；复用现有信封 scheme，iOS 用既有解密路径即可解。
  - iOS（`FeedlingAPI.swift`）：`ensureRegisteredIfCloud` 在 register 前先调
    `recoverViaKeypairIfPossible()`——用 `loadPrivateKeysForDecryption()`（含**可同步槽**，
    换机后存活的那把）遍历候选公钥走 challenge/verify，成功即 `setCredentials` 不再 register；
    404/离线/无密钥则回退原守卫与注册。
  - **Codex review 跟进修复（iOS）**：(P1) 把 keypair 找回纳入既有 `registrationTask` 单一在途
    任务（抽出 `acquireCloudCredentials()`）——`@MainActor` 下 guard→任务安装之间无 await 即原子,
    避免并发启动各自找回、各领一把 api_key 互相覆盖；(P2) `wipeLocalAccountState()`（"删除我的
    数据"/重置走这里）补 `DiagnosticLog.shared.clear()`,清掉残留 userId/agent 名等可导出 PII；
    (P3 二轮) 找回从 Bool 改为三态 `KeypairRecoveryOutcome{recovered/noAccount/transientFailure}`:
    **只有"对所有候选公钥都确定 404"才允许注册**;离线/超时/5xx/解密失败等瞬时错误返回
    `transientFailure` → 阻止注册并重试,不再因瞬时失败而铸新号孤儿化(纵深防御,与服务端去重叠加)。
  - **服务端兜底（register 去重）**：`/v1/users/register` 收到**已有账号的 public_key** 时
    直接返回 **409**（提示走找回），**绝不铸第二个账号**。这关一拦，无论客户端在线与否、
    版本新旧、iCloud 同步时序如何，孤儿都创建不出来。空 public_key（legacy 客户端）仍放行；
    Reset-and-reimport 先清密钥对 → 新公钥，不受影响。
- 测试：`tests/test_account_recover.py`（8 项 TDD，含**完整 X25519+ChaCha20 往返**证明与
  iOS `box_seal`/`unseal` 线格式一致、多账号共用公钥落到最新活跃账号、错答/重放/未知公钥拒绝、
  register 去重拒绝重复公钥/放行新公钥/放行空公钥）。因去重改动同步把 `test_data_track` 的
  注册 helper 改为每次唯一公钥。全套 `tests/` **326 passed**（同上 1 个既有 enclave 环境依赖
  失败无关）。iOS 侧无法在此跑 XCTest，需设备/CI 构建验证。

---

## 2026-06-02

### [DONE] 引入 Alembic 管理数据库 schema

- schema 改为由 **Alembic 单一真相源**管理（`backend/alembic/versions/`），取代
  原先 `db.init_schema()` 里内联的一段 `CREATE TABLE IF NOT EXISTS` DDL。
- `db.init_schema()` 现在跑 `alembic upgrade head`（programmatic），从
  `DATABASE_URL` 读连接（`backend/alembic/env.py` 把 `postgresql://` 映射到
  psycopg3 方言 `postgresql+psycopg://`）。app 启动、migrate 容器、测试 conftest
  都走同一条路，不会双真相源漂移。
- baseline 迁移 `0001_baseline` 用幂等 DDL（`IF NOT EXISTS`），所以对**已经建好
  8 张表的线上 RDS** 安全——下次部署 `upgrade head` 时只是 no-op + 记录
  `alembic_version=0001_baseline`，不动数据。
- 依赖：`requirements.txt` + `requirements.lock`（uv 重新生成）加 `alembic>=1.13`
  （带 SQLAlchemy 2.x，仅用于驱动迁移；请求路径仍走 psycopg pool）。
- 脚手架：`backend/alembic.ini`、`alembic/env.py`、`alembic/script.py.mako`、
  `alembic/versions/0001_baseline.py`。已确认 `.dockerignore` 不排除、Dockerfile
  `COPY backend/` 会带进镜像。
- **后续改 schema 的流程**：`cd backend && alembic revision -m "..."` 写迁移 →
  提交 → 部署时 `init_schema()`（=`upgrade head`）自动应用；也可手动
  `DATABASE_URL=... alembic upgrade head`。
- 验证：本地临时 PG 实测「全新库建表+打戳 / 幂等重跑 / 已有表的库安全 no-op+打戳」
  三场景全过；`alembic current|history|revision` CLI 正常；全套测试 **213 passed**。

### [DONE] 老数据迁移方案：Phala CVM 内一次性 migrate 容器

- 老用户数据在 Phala CVM 的 `feedling_backend_data` volume（`/data` 下的旧
  JSON/JSONL 文件）。TDX enclave 不便把数据导出，故采用「CVM 内自迁移」：
  `docker-compose.phala.yaml` 新增一次性 `migrate` service（同镜像，挂同一
  `feedling_backend_data` volume，注入 `DATABASE_URL`，跑
  `migrate_to_pg.py --data-dir /data`），`backend` 通过
  `depends_on: migrate: condition: service_completed_successfully` 等它跑完再起。
- **防重复覆盖**：`migrate_to_pg.py` 加 `migration_done` 标记（写在
  `server_config`）。首次成功后置标记，之后每次启动（CVM 重启会重跑该一次性
  容器）检测到标记即 no-op，绝不用旧文件覆盖用户已写入 RDS 的新数据。`--force`
  可强制重跑。→ 该 migrate service 可永久留在 compose；确认无误后也可在后续
  常规部署中移除（移除会再变 compose_hash → 多一次 attest）。
- 加 service 会改变 compose_hash → 上链 + iOS consent（部署本就会弹）。
- 验证：本地临时 PG 实测「首次迁移 → 重启 no-op 保留新数据 → --force 重导入」
  三场景全过；`docker compose config` 解析新 compose 正常。
- 线上 RDS 8 张表已建好（init_schema 幂等，启动自动建）；唯一需要的 GitHub
  secret `DATABASE_URL` 已配置。

### [DONE] 迁移已完成 + 移除 migrate service（修 starting）

- **迁移事实上已完成**：线上含 migrate service 的版本部署后，`migrate` 在
  2026-06-02 03:42 UTC 跑完，把老数据导入 RDS 并置 `migration_done` 标记。RDS
  现有 107+ 用户、chat 时间跨 2026-04-21~至今、且在实时增长（backend 正常服务）。
- **CVM starting 的成因 + 处理**：部署期间 CVM 一度长时间停在 `updating/starting`
  （一次性 `restart:no` 容器退出 + `depends_on: service_completed_successfully`
  在 dstack-dev-0.5.8 下的表现），最终自行恢复 `running`。为避免复发，已从
  `docker-compose.phala.yaml` **移除 `migrate` service 及 backend 的
  `depends_on: migrate`**——迁移已完成、`migration_done` 标记已在，不再需要它。
- 安全性：`migration_done` 已设 → 即便将来再跑 migrate 也永久 no-op（除非
  `--force`），不会覆盖 RDS 现有数据。
- 后续如需再迁移：手动运行 `migrate_to_pg.py`（marker 会让它 no-op，需要时加
  `--force`），不再走常驻 compose service。

### [DONE] 数据缺失补救工具：migrate_to_pg 加 --merge + SSH 执行路径

- 用户反馈 03:42 那次迁移**不完整**（RDS 缺数据）。`migrate_to_pg.py` 新增
  **`--merge`** 模式：跳过 `delete_user_data`，只 upsert/append → 补回缺失数据
  而**不回退** live backend 在迁移后写入 RDS 的新行（row-per-item 表按 id 幂等
  upsert；append-only user_logs 可能产生重复，非关键）。`--merge` 隐含绕过
  `migration_done` 标记。本地临时 PG 验证：现有新数据保留 + 缺失用户/记录补回。
- **执行路径**（TDX CVM 内,SSH 直连默认被 publickey 挡）：下次
  `phala deploy ... --ssh-pubkey ~/.ssh/id_rsa.pub` 注入 SSH 公钥后,可
  `phala ssh <cvm> -- docker exec <backend容器> python backend/migrate_to_pg.py
  --data-dir /data --verify|--merge` 精确对账与补缺。容器名用 `phala ps` 确认。
- 三种模式:`--verify`(只读对账文件 vs RDS)、`--merge`(安全补缺)、`--force`
  (全量 delete+reimport,会回退增量,仅维护窗口用)。

---

## 2026-06-01

### [DONE] PostgreSQL 迁移扩展覆盖新功能 + 解决 stash 冲突

- 迁移期间 main 合入了新功能（`Add identity and memory agent actions`、
  `Add beta data track dashboard` 等），`git stash pop` 在 `backend/app.py`
  留下 3 处未解决的冲突标记（"Updated upstream" vs "Stashed changes"）。已逐一
  解决：保留上游新功能 + 保留迁移的 `import db` / db 持久化。
- 修复合并引入的断裂引用（上游新代码调用了迁移已删除的方法）：
  `_set_user_public_key` / content-rewrap 里的 `_persist_chat()`、
  tracking/memory dashboard 里的 `self._append_jsonl` / `store._read_jsonl`
  —— 全部改为 `db.*`。
- 把新功能引入的文件持久化也迁到 PostgreSQL：`tracking_events` /
  `memory_changes` / `memory_capture_jobs` 三个 JSONL 流 → `user_logs`；
  `onboarding_route` / `model_api` → `user_blobs`；`history_import_jobs`
  （原每任务一个 .json 的目录）→ `user_blobs`（kind 前缀 `history_import_job:`）。
  无需改 schema（复用 user_logs / user_blobs 表）。
- `db.py` 新增 `delete_blob` / `list_blobs(prefix)`，删除 `app.py` 中已无用的
  `_read/_write_json_object`、模块级 `_append_jsonl`、全部 `_*_file` 路径属性。

### [DONE] 第二轮：合并再次拉取的后端 + 覆盖用户模型重构

- 又拉取了后端代码（`Keep history import out of visible chat`、
  `Omit oversized chat bodies from lightweight history` 等），`backend/app.py`
  再次出现冲突标记，已解决。
- **用户模型重构**：上游把 users 从单 `api_key_hash` 改成富模型
  （`principal_id` + `api_keys[]` 多密钥/吊销 + access bindings），且 `_save_users()`
  被重新引入并在 11 处调用。把 `db.py` 的 `users` 表改为**整文档 JSONB**
  （`user_id` PK + `doc`），新增 `save_all_users`；`_save_users()` 改为 db 持久化，
  所有 key 管理调用点无需改动即可工作。
- 新增的文件持久化继续迁到 DB：`access_link_tokens`（全局）→ 新增 `global_blobs`
  表 + `get/set_global_blob`。修复上游 `_set_user_public_key` 调用已删除
  `_save_users` 的断裂引用。
- 迁移脚本同步扩展：覆盖整文档 users（保留 api_keys/principal_id，api_key 仍有效）、
  onboarding_route / model_api blob、tracking/memory_changes/memory_capture 日志、
  history_import_jobs（每任务一 blob）、全局 access_link_tokens。
- 适配新增功能测试文件中残留的文件存储断言（`USERS_FILE` monkeypatch、
  `identity_file`/`memory_file`/`model_api.json` 直读、`_persist_chat`）→ 全部改走 `db.*`。
- 验证：全套测试对真实 Postgres 全绿（**206 passed**），迁移 e2e（含新用户模型 +
  全部新文件 + 全局 blob + api_key 存活）通过。后端仍无任何用户数据落地文件。

---

## 2026-05-31

### [DONE] 持久层从本地文件迁移到外部 PostgreSQL

- 新增 `backend/db.py`：psycopg3 + 连接池存储层，承载所有原本写在
  `FEEDLING_DATA_DIR` 下的 JSON/JSONL 文件。Schema 为混合模型：小的单例
  blob（push_state / live_activity_state / tokens / proactive_settings /
  identity / bootstrap / consumer_state / frames_meta）进 `user_blobs` KV 表；
  高频集合用 row-per-item 表（`chat_messages` / `memory_moments` /
  `frame_envelopes` / `user_logs`）。全局 `users` 表 + `server_config`（pepper）。
- `backend/app.py`：`UserStore` 与全局 users 注册表保留各自的内存缓存与
  `threading.Lock`，只把 `_load_X`/`_save_X` 的函数体换成调用 `db.py`。仍是单
  gunicorn worker；长轮询 waiter 仍基于内存 `threading.Event`，未占用 DB 连接。
  chat 不再每次重写整个文件——append 现在是单行 INSERT + 有界 trim（O(1)）。
- 加解密**未改动**：服务器从不解密，envelope 的 `body_ct`/`nonce`/`K_user`/
  `K_enclave` 等字段逐字节存为 JSONB 并原样返回，enclave 解密路径不受影响。
  `/v1/content/swap` 的可见性切换（含 K_enclave 增删）走整行替换以保证语义。
- 新增 `backend/migrate_to_pg.py`：一次性、幂等地把现有文件树导入 PG。**会导入
  `.pepper`**，使既有 api_key_hash 继续有效（否则所有 api_key 失效）。`--verify`
  复核计数。文件系统保留作回滚。
- 依赖：`requirements.txt` + `requirements.lock`（uv 重新生成，含 hash）加
  `psycopg[binary,pool]>=3.2`。
- 部署：`docker-compose.yaml` / `docker-compose.phala.yaml` /
  `feedling.env.example` 加 `DATABASE_URL`（`:?` 形式，未设置即 fail-fast；
  必须 `sslmode=require`）。
- **为什么 / 安全影响**：选了外部托管 PG（数据离开 enclave）。数据在库内是
  E2E 密文，但库能看到明文元数据（user_id / 时间戳 / app 名 / visibility）。
  `DATABASE_URL` 因是 `${VAR}` 插值不进 compose_hash，运营方理论上可重定向到
  另一个 PG 而不重新 attest——已在 compose 注释里标注为 attested config 并提示
  需在部署信任文档中披露。

---

## 2026-05-21

### [DONE] Resident onboarding path made reusable after live iPhone test

- Re-centered onboarding on an independent `feedling-chat-resident` /
  IO resident consumer service for Hermes / OpenClaw / Mac / server agents.
  The live path is poll `/v1/chat/poll` → call the agent HTTP/CLI entry →
  POST `/v1/chat/response`, verified by `feedling_chat_verify_loop`.
- Simplified iOS onboarding copy to three handoff items: skill URL,
  path-specific IO connection details, and a short start prompt. Detailed
  CLI/HTTP/systemd choices now live in the public `io-onboarding` skill.
- Updated resident consumer docs and examples to use Hermes/OpenClaw CLI
  `HERMES_HOME=<real profile> hermes chat -Q --source tool --max-turns 60 -q "{message}"`,
  persist session id for `--resume`, avoid wrapper persona prompts, and keep
  user-visible fallback templates off by default.
- Updated README inventories for current verification endpoints/tools and
  clarified that direct MCP is enough for bootstrap/tool calls, while reliable
  ongoing IO Chat needs an always-on owner.

---

## 2026-05-18

### [DONE] Onboarding protocol docs synced to floor-based memory standard

- Synced backend bootstrap instructions and README bootstrap flow with the
  public `io-onboarding` skill: Step 0, four memory passes, 7-dimension
  identity, verify tools, chat-loop verification, and broadcast as the final
  onboarding step.
- Changed memory verification from target-based gating back to floor-based
  gating: relationship floors are still exposed, but hitting the floor now
  passes unless metadata issues are present.
- Clarified `docs/DESIGN_E2E.md` as historical derivation rather than current
  wire-format source of truth, and refreshed deployment records from live
  `/attestation` (`b1e72a6`, compose hash `0xf09f1ddc...`).

---

## 2026-05-14

### [DONE] Production docs and privacy sweep after prod9 redeploy

- Refreshed live deployment records against `/attestation` after the
  privacy cleanup redeployed prod9: running commit
  `0573be37114c61ef2d55bf36ac57c2f06e1bdc7f`, compose hash
  `0x01dd452868a645a830642af6e122e882f34a40a436d22e4ad4a2978e1dd6570f`.
- Removed stale private deployment references from the production compose,
  made `tools/audit_live_cvm.py` portable instead of using a local absolute
  path, and updated sample attestation URLs to the current GitHub repo.
- Re-ran the targeted privacy/stale scan, DCAP parser tests, syntax check,
  and GitHub Actions CI/publish flows.

---

## 2026-05-13

### [DONE] README caught up to prod9 pure-CVM architecture

- Updated `README.md` to describe the current production shape:
  `dstack-ingress`, Flask backend, FastMCP, and enclave all run inside
  the Phala prod9 TDX CVM.
- Rewrote stale VPS/Caddy and Phase C.2 MCP-TLS claims: custom-domain
  TLS now terminates at `dstack-ingress`; the attestation port keeps
  its own pinnable TLS; content privacy rests on v1 envelopes sealed to
  `enclave_content_pk`.
- Refreshed the audit command, status checklist, deploy notes, HTTP
  endpoint inventory, MCP tool count, and config reference to match the
  current source.
- Updated `tools/README.md` so the audit utility snippet uses the
  current env+curl flow instead of the retired `--cvm-url` flag.
- Updated `docs/AUDIT.md`, `docs/DESIGN_E2E.md`,
  `deploy/DEPLOYMENTS.md`, `deploy/BUILD.md`,
  `ios/FeedlingDCAP/README.md`, and `CLAUDE.md` for prod9/current-state
  clarity and redacted retired host/user/APNs identifiers from tracked
  docs.
- Corrected the changelog preamble itself now that `HANDOFF.md` is gone.

---

## 2026-05-10

### [DONE] CVM deploy CI now follows the repo's GHCR owner

- Fixed the `deploy-cvm` job after the repo/package moved from
  `account-link` to `teleport-computer`: CI was publishing
  `ghcr.io/teleport-computer/feedling:<sha>` but waiting for
  `ghcr.io/account-link/feedling:<sha>`, so it timed out before
  `phala deploy`.
- `ci.yml` now derives the GHCR owner from `github.repository_owner`,
  checks the image with `docker manifest inspect`, and pins
  `deploy/docker-compose.phala.yaml` to the same owner dynamically.
- Updated the current Phala compose image references to
  `ghcr.io/teleport-computer/feedling:*`; the next push to `main`
  should publish the new image, pin the compose to the new short SHA,
  and continue through the real CVM deploy.

---

## 2026-04-21

### [DONE] Code + CI ready for prod9 migration (pure-CVM, ingress-terminated)

Endgame direction per user: VPS is going away, prod users re-onboard from
scratch, dstack-ingress 2.2 inside the CVM terminates TLS for both
`api.feedling.app` and `mcp.feedling.app`. Required prod9 (only gateway
that supports `_dstack-app-address.<domain>` TXT routing per
dstack-tutorial 04) — new node → new app_id → new compose_hash to
authorize on-chain.

**Compose** (`deploy/docker-compose.phala.yaml`):
- Added `ingress` service: `dstacktee/dstack-ingress:2.2@sha256:d05a7b3…`,
  multi-domain mode (`DOMAINS` newline-list, `ROUTING_MAP
  domain=host:port` to backend:5001 and mcp:5002), mounts
  `/var/run/tappd.sock` per upstream multi-domain example.
- `mcp` service dropped its in-enclave ACME: `FEEDLING_MCP_TLS=false`,
  removed CF env + `/var/run/dstack.sock` mount + the
  `feedling_mcp_tls_data_v2` volume.
- `enclave` service adds `FEEDLING_MCP_TLS_IN_ENCLAVE=false` — see
  backend change below.
- Dry-run `compose_hash` with `:78b51a6` pin =
  `0x1f0169bab4b1ee19058bd72bdb1fb46cc9b1b9de75a1e2a348134959c908efb9`
  (real hash TBD after CI repins image).

**Backend** (`backend/enclave_app.py`): new env var
`FEEDLING_MCP_TLS_IN_ENCLAVE` (default `true` for backward compat) gates
the `mcp_tls_cert_pubkey_fingerprint_hex` derivation. When false the
field stays empty in the attestation bundle — iOS falls through to the
existing "Pre-Phase-C.2 deployment" disclosure row, and
`audit_live_cvm.py` Row 8 becomes a pass-with-disclosure.

**iOS** (`testapp/FeedlingTest/CVMEndpoints.swift` NEW + edits): all
four URL shapes (attestation, ws ingest, api, mcp) now come from a
single `CVMEndpoints` enum driven by `appId` + `gatewayDomain`.
Overridable via `FEEDLING_CVM_APP_ID`/`FEEDLING_CVM_GATEWAY_DOMAIN` env
or UserDefaults. `FeedlingAPI.swift`
(`resolveIngestWSEndpoint`+`attestationURL`) and `AuditCardView.swift`
(3 sites) no longer hardcode app_id or gateway. Registered new file in
the xcodeproj (PBXBuildFile + PBXFileReference + Group + Sources
phase). Defaults still point at prod5 so pre-cutover builds work; flip
to prod9 in a follow-up commit once app_id is known.

**Broadcast extension**: `SharedConfig.defaultIngestEndpoint` replaces
three `ws://[retired VPS IP redacted]:9998/ingest` fallbacks. Extension is a
separate target and can't import `CVMEndpoints`; the real endpoint is
still written by `FeedlingAPI.init` to App Group UserDefaults, so the
fallback only matters on very first broadcast.

**Audit tool** (`tools/audit_live_cvm.py`): default URLs derived from
`FEEDLING_CVM_APP_ID`/`FEEDLING_CVM_GATEWAY_DOMAIN` env. Row 8
(`mcp_tls_cert_pubkey_fingerprint_hex`) treats empty value as
pass-with-disclosure (ingress-terminated TLS; content-layer envelope
crypto remains the real trust boundary).

**CI** (`.github/workflows/ci.yml`): `deploy-vps` job deleted;
`deploy-cvm` now gates on the test jobs directly. Added
`FEEDLING_COMPOSE_FILE: deploy/docker-compose.phala.yaml` to the
`publish-compose-hash.sh` step — fixes a pre-existing bug where the
script hashed `docker-compose.yaml` (local-dev compose) instead of the
phala compose.

**Validation (2026-04-21)**:
- `docker compose -f deploy/docker-compose.phala.yaml config --quiet` OK
- `python -m compileall backend tools` OK
- `xcodebuild` for scheme `FeedlingTest` succeeded (iPhone 17 / iOS 26.4 sim)
- `xcodebuild` for scheme `FeedlingBroadcast` succeeded
- Compose_hash dry-run reproducible.

**Next (fully CI-driven — two `workflow_dispatch` triggers, no manual
CLI)**: trigger `bootstrap-prod9.yml` with `confirm=yes` → it purges
stale CF records, `phala deploy`s to node 18, polls for LE readiness,
publishes compose_hash, flips `CVM_ID` repo var, auto-commits iOS
`CVMEndpoints` bump `[skip ci]`. Run `audit_live_cvm.py` + fresh iOS
install to confirm 8/8 + 6/6. Then trigger `retire-prod5-vps.yml` with
`confirm=yes-delete-prod5` → `phala cvms delete` prod5, SSH stop+mask
VPS systemd units + tombstone, purge retired VPS DNS from CF, delete
stale `VPS_*` repo vars/secret. `HANDOFF.md` was later retired; current
deployment state lives in `deploy/DEPLOYMENTS.md`.

### [DONE] Bootstrap + retire workflows for CI-driven prod9 migration

Added `.github/workflows/bootstrap-prod9.yml` and
`.github/workflows/retire-prod5-vps.yml`. Both are `workflow_dispatch`
only with mandatory `confirm` inputs.

- `bootstrap-prod9.yml` does the full "stand up replacement CVM" flow
  (purge conflicting CF records → `phala deploy -c phala-compose
  --node-id 18 -j --wait` → readiness probes → publish compose_hash →
  `gh variable set CVM_ID` → auto-commit iOS `CVMEndpoints` defaults
  bump tagged `[skip ci]`). Pre-flight gate aborts if a CVM named
  `feedling-enclave-v2` already exists (prevents double-deploy).
- `retire-prod5-vps.yml` deletes the prod5 CVM, SSHes the VPS as
  `[retired service user]` and stops/disables/masks the `feedling-backend`
  + `feedling-mcp` systemd-user units (drops `~/RETIRED.md`), purges
  any CF record still pointing at `[retired VPS IP redacted]`, and removes
  `VPS_HOST`/`VPS_USER`/`VPS_DEPLOY_KEY` from repo state. Safety
  gate refuses to run unless `CVM_ID` has already been flipped away
  from the hardcoded prod5 UUID (i.e., bootstrap ran successfully
  first).

Zero secrets move through a human. Everything runs on GitHub-hosted
runners using the repo's existing `PHALA_CLOUD_API_KEY`, `CF_API_TOKEN`,
`CF_ZONE_ID`, `ETH_DEPLOYER_KEY`, and `VPS_DEPLOY_KEY`.

---

## 2026-04-20

### [DONE] Phase D deploy — multi-tenant-only CVM live

Pairs with the v0 / SINGLE_USER strip below. After the strip landed,
the VPS data directory was wiped (kept `.pepper` + APNs key), VPS
services restarted on the new code, then the CVM was redeployed.

- Image: `ghcr.io/account-link/feedling:78b51a6`
- Compose hash:
  `0xd92bcd3cb1713ffe8e152417ab46e8179510c37ceed5ae6d423c586a2cd60049`
- On-chain (Sepolia): tx
  `0x235f0120d6982cbf8872e927ee2e59133627177ca9d3f862554d748ac6e60c7c`
  at block 10696873.
- CLI audit: `tools/audit_live_cvm.py` → 8/8 green.
- Remaining: prod user reinstalls fresh and verifies the in-app audit
  card shows 8/8 green + the new compose-hash-changed consent modal
  fires on first launch (task #36).

Task #35 closed.

### [DONE] v0 / SINGLE_USER strip — backend is envelope-only

Closes tasks #23 and #33 in a single commit. The one real prod user
OK'd wiping her data + fresh multi-tenant reinstall, so instead of
keeping rewrap as a 30-day compatibility shim we retired the entire
v0 stack in one go.

**Backend (`backend/`)**
- `app.py`: removed all `SINGLE_USER` branches, v0 plaintext accept
  branches in `/v1/chat/message`, `/v1/chat/response`, `/v1/memory/add`,
  `/v1/identity/init`; removed the HTTP `/v1/identity/nudge` endpoint
  (identity mutation now only lives in MCP `feedling.identity.nudge`);
  removed `/v1/content/rewrap` and all `_rewrap_*` helpers.
- `app.py`: added `/v1/content/export` inlining full v1 frame envelopes
  (schema bumped 1→2, cap 50→80 MiB — frames are now part of the
  portable dataset the user walks away with).
- `app.py`: restored a purpose-built `/v1/content/swap` endpoint for
  ongoing in-place envelope swaps (used by iOS visibility-toggle).
  Same validation shape as old rewrap minus the `already_v1` status —
  no v0 concept left in the response.
- `mcp_server.py`: dropped the `SINGLE_USER` constant and every v0
  fallback in `chat_post_message`, `identity_init`, `memory_add_moment`,
  `identity_nudge` — they now fail loud when pubkeys are unavailable.
- `enclave_app.py`: dropped `if v == 0:` pass-throughs in chat, memory,
  and identity decrypt loops.
- `chat_bridge.py` + `deploy/feedling-chat-bridge.service`: deleted.
  MCP's in-enclave `feedling.chat.post_message` replaces them (and
  avoids the April spam-reply incident where a systemd restart race
  caused duplicate Hermes replies).

**Deploy (`deploy/`)**
- `docker-compose.yaml`, `docker-compose.phala.yaml`: removed
  `SINGLE_USER` env, shared `FEEDLING_API_KEY` stubs. Backend is always
  multi-tenant.
- `setup.sh`, `feedling.env.example`: removed shared-key / SINGLE_USER
  provisioning. Fresh VPS bootstrap now produces a multi-tenant box.

**iOS (`testapp/FeedlingTest/`)**
- `FeedlingAPI.swift`: removed `runSilentV1MigrationIfNeeded`,
  `RewrapSummary`, `collectV0Chat/MemoryEnvelopes`, `postRewrap`, the
  `@Published migrationProgress` state, and the 403-SINGLE_USER branch
  in `ensureRegisteredIfCloud`. `flipMemoryVisibility` now POSTs to
  `/v1/content/swap`.
- `ContentView.swift`: removed `MigrationProgressRow` + its usage in
  the Privacy hero.
- `FeedlingTestApp.swift`: removed the migration kickoff call from the
  `.task { … }` startup block.
- `ChatViewModel.swift`, `SampleHandler+WebSocketQueue.swift`: removed
  plaintext fallbacks and dead `WebSocketManager.sendFrame` — backend
  now rejects non-envelope writes, so silent fallbacks would just
  produce invisible 400s / dropped frames.

**Tests + CI**
- `backend/test_api.py`: removed `/v1/identity/nudge` cases, added
  header note that write-path tests POST plaintext and will 400 against
  the v1-only backend until they're rewritten to build envelopes
  client-side.
- `tools/e2e_encryption_test.py`, `.github/workflows/ci.yml`: dropped
  `SINGLE_USER` env + the CI matrix dimension (no more `single-user` +
  `multi-tenant` rows — multi-tenant is the only mode).

**Docs**
- `HANDOFF.md`, `docs/NEXT.md`, `docs/AUDIT.md`, `docs/DESIGN_E2E.md`,
  `CLAUDE.md`: updated to reflect the stripped state. Phase 5's
  "retire v0 over 30 days" checkbox flipped to done.

**Exit criterion**: `grep -r "SINGLE_USER\|single_user" backend/ deploy/`
returns no hits outside this file. Server never stores unencrypted
content and no longer exposes a path to write plaintext.

---

### [DONE] Phase C.2 — ACME-DNS-01 Let's Encrypt cert inside the CVM

`mcp.feedling.app` now serves a real CA-signed Let's Encrypt cert
whose private key is provably inside the TDX enclave. Closes task #30.

**backend/acme_dns01.py (new, ~260 lines, zero new deps)**
- Pure-Python ACME v2 client (RFC 8555) — JWS ES256, JWK thumbprints,
  order/auth/challenge/finalize flow, all over `httpx`.
- `CfDns` helper talks to Cloudflare API to create/delete the
  `_acme-challenge` TXT record for DNS-01.
- `get_or_renew()` caches the cert PEM at `/tls/<domain>.cert.pem`
  (volume-backed, survives restarts) and re-issues when <30 days
  left. LE rate limit is 5 certs/week/domain — 30-day buffer means
  ~12 reissues/year worst case.
- `start_renewal_watchdog()` spawns a daemon thread that checks
  daily; on renewal it `os._exit(0)`s to let Docker restart the
  container and pick up the fresh cert.

**backend/dstack_tls.py (extended)**
- New path constant `MCP_TLS_KEY_PATH = "feedling-mcp-tls-v1"` +
  `derive_key_only(dstack, path)` helper. Cert private key is
  derived from dstack-KMS with a stable hash, so LE renewals rotate
  the cert but NOT the key — audit Row 8 stays green indefinitely.

**backend/mcp_server.py (extended)**
- Replaces `_materialize_tls_cert` with `_acquire_tls_cert`. Priority:
  ACME (when `FEEDLING_ACME_DOMAIN` is set) > dstack-KMS self-signed
  (Phase C.1 fallback) > plain HTTP. Surfaces the pubkey fingerprint
  via a module-level `_mcp_cert_pubkey_fingerprint_hex` that gets
  baked into `/attestation` alongside the attestation-port fingerprint.

**backend/enclave_app.py (extended)**
- `bootstrap()` derives the MCP cert pubkey from dstack-KMS and
  computes sha256(SubjectPublicKeyInfo DER). Result is served as
  `mcp_tls_cert_pubkey_fingerprint_hex` in `/attestation`.
- Stable-per-app-id (not per-compose) because the derivation path
  is constant — same rationale as `enclave_tls_cert_fingerprint_hex`
  and `enclave_content_pk`.

**deploy/docker-compose.phala.yaml**
- Added `FEEDLING_ACME_DOMAIN=mcp.feedling.app`, `FEEDLING_ACME_EMAIL`,
  `FEEDLING_TLS_CACHE_DIR=/tls`, `FEEDLING_CF_ZONE_ID=${CF_ZONE_ID}`,
  `FEEDLING_CF_API_TOKEN=${CF_API_TOKEN}` to the mcp service.
- CF_* are injected at deploy time via `phala deploy -e KEY=VAL`
  (encrypted env channel, never in the compose file, never hashed
  into compose_hash). Zone ID is non-secret; API token is
  `Zone:DNS:Edit`-scoped to `feedling.app`.
- New named volume `feedling_mcp_tls_data_v2` mounted at `/tls`.
  The `_v2` suffix forces Docker to create a fresh volume because
  the v1 volume was root-owned (Docker initializes empty named
  volumes as root when the container image doesn't pre-create the
  mount path). The MCP process runs as `feedling` UID 1000 so
  root-owned `/tls` = `EACCES` on first cert write → ACME silently
  fell back to the dstack-KMS self-signed cert on the first deploy.

**deploy/Dockerfile**
- Pre-creates `/tls` with `feedling:feedling` ownership alongside
  `/data`. New named volumes get initialized from the container's
  directory state, so this guarantees feedling ownership for any
  future fresh volume.

**tools/audit_live_cvm.py**
- Row 8 rewritten for the real LE path. Uses `openssl s_client -showcerts`
  to fetch the full cert chain (Python's `ssl.getpeercert` returns
  only the leaf); builds an `x509.verification.PolicyBuilder` chain
  from the system CA bundle; calls `build_server_verifier(DNSName(
  "mcp.feedling.app")).verify(leaf, intermediates)` to CA-validate
  the cert for the expected name. Then pins the cert's SPKI pubkey
  sha256 against the attested value.
- SNI workaround: Phala dstack-gateway routes by SNI and only
  accepts `-PORTs.*.phala.network` hostnames — sending
  `mcp.feedling.app` as SNI gets the gateway to drop the TCP
  connection before the TLS handshake reaches the CVM. Fix:
  send the gateway hostname as SNI, then verify the cert manually
  for `mcp.feedling.app`.

**deploy/Caddyfile**
- `mcp.feedling.app` reverse proxy: `tls_server_name` changed
  from `mcp.feedling.app` to the gateway hostname + added
  `tls_insecure_skip_verify`. Same SNI-routing reason. The real
  trust root is the attestation; Caddy is just a compatibility
  shim for Claude.ai and other MCP clients that expect a stable
  hostname and a CA-valid cert.

**Operational**
- CF DNS: new A record `mcp.feedling.app → [retired VPS IP redacted]` (VPS
  where Caddy runs). DNS-only (not Cloudflare-proxied) so Caddy
  can do its own HTTP-01 ACME for the public-facing `mcp.feedling.app`
  cert without Cloudflare terminating first.
- New compose_hash `0x23a2c2869567d15220383e4acb5ceb5cf27d78e087d2d4e357e4b3c053a5dc68`
  published on-chain: Sepolia tx `0xe2a9ceab…`.
- MCP cert pubkey fingerprint: `e98665a3e94ac90a0a26453a73e16d5a569f791c181cfbc6ba98598f358cf63e`
  — expect this to stay constant across all future deploys (stable
  dstack-KMS derivation).
- CLI audit: **8/8 green**.

### [DONE] Phase B wave-2 + MIGRATION.md

Finishing out the Phase B surface + directly answering "what does
the one prod user actually do to migrate to E2E?".

**docs/MIGRATION.md (new)**
- Three concrete options for a self-hosted VPS user to move to
  Feedling Cloud's TEE-backed encryption. Option 1 (recommended)
  uses the Phase B Reset & re-import pipeline; the user's agent
  re-adds content via MCP tools, which now wrap everything into
  v1 envelopes on the way in. Option 2 keeps self-hosted without
  encryption (legitimate — they own the server). Option 3 is
  self-hosted with their own TEE (documented, not recommended).
- Linked from the in-app audit card footer alongside AUDIT.md +
  the repo root.

**Per-item memory visibility toggle**
- `FeedlingAPI.flipMemoryVisibility(moment, toLocalOnly:)` — builds
  a fresh envelope with the new visibility from the plaintext iOS
  already has in memory, POSTs to `/v1/content/rewrap`. No server
  trip for re-decryption.
- MemoryGardenView: long-press context menu on each card with
  "Hide from agent" / "Share with agent"; subtle `eye.slash`
  indicator in the card header when `local_only`. Reloads the
  garden after a successful flip.
- Chat is intentionally skipped — many items, transient; the
  "hide from agent" affordance matters more on persistent
  memory-garden entries.

**Inline migration progress**
- `FeedlingAPI` gains `@Published migrationProgress: (done, total)?`.
  `runSilentV1MigrationIfNeeded` sets it before the batching loop,
  updates per batch, clears on completion or error.
- New `MigrationProgressRow` renders an inline `ProgressView` with
  label "Upgrading your old data — N of M" beneath the Privacy
  hero when migrationProgress is non-nil. Hidden otherwise.

**iOS verification**
- `xcodebuild BUILD SUCCEEDED` on iPhone 16 Pro sim with all
  wave-2 changes.

**No backend change** — wave-2 is iOS-only on top of existing
`/v1/content/rewrap`. CVM does not need a redeploy for this ship.

---

### [DONE] Phase C.3 — encrypted identity.nudge + encrypted agent chat reply + UX fixes

Closes the last two plaintext-at-rest write paths. Also applies the
user's last-round UX feedback (privacy hero tap, audit-card on-chain
copy, GitHub + agent-audit-guide links).

**Backend (backend/app.py)**
- New `POST /v1/identity/replace` — accepts a v1 envelope and replaces
  the existing card in place, preserving `created_at`. Used by MCP
  to implement nudge on v1 cards. Same envelope field validation as
  `/v1/identity/init`.
- `POST /v1/chat/response` now accepts `envelope` in addition to
  `content`. Mirrors what `/v1/chat/message` does for user-authored
  chat. Push-live-activity sidecar still works via a `push_body`
  companion field (the push payload is plaintext metadata by
  necessity — APNs doesn't see inside the envelope).

**MCP (backend/mcp_server.py)**
- `feedling.chat.post_message`: wraps `content` in a v1 envelope
  before POSTing when pubkeys are available. Same fallback rule
  as `memory.add_moment` (v0 plaintext when no enclave reachable).
- `feedling.identity.nudge`: new orchestration. Tries legacy v0
  endpoint first; if server responds 409 with
  `error="nudge_not_supported_on_v1_cards_yet"`, catches and falls
  through to the new `_identity_nudge_v1` helper which: fetches
  the decrypted card from the enclave's `/v1/identity/get`,
  mutates the named dimension (clamped [0,100], records
  `last_nudge_reason`), re-wraps the whole card via
  `build_envelope`, POSTs to `/v1/identity/replace`. Plaintext
  lives inside the MCP process only — inside the TDX-attested
  container boundary — for the duration of one RPC.

**iOS (testapp/FeedlingTest/ContentView.swift)**
- Privacy hero row in Settings → Privacy wrapped in a
  `NavigationLink` to `AuditCardPage`. Previously the tap did
  nothing (the user caught this).
- Dropped the hand-drawn chevron from the row since the
  NavigationLink adds its own.

**iOS (testapp/FeedlingTest/AuditCardView.swift)**
- Divider label "On-chain audit (public transparency, not security)"
  → "Public release log" — the parenthetical was confusing and
  undersold what the log is.
- Etherscan link label "View AppAuth deploy on Etherscan" →
  "View on Etherscan".
- Rewrote `AuditMechanismCopy.onChainAudit` to describe what the
  release log *is* rather than inventing a cryptographic
  guarantee it doesn't provide. Previous copy implied the
  on-chain log gates key release, which is future work.
- Two new footer links: "Read the audit guide (for your agent)"
  → `docs/AUDIT.md` on GitHub, and "Browse the source on GitHub"
  → the repo root. Closes the "user hands their agent a repo and
  asks 'is this safe'" gap.

**docs/AUDIT.md (new)**
- Agent-consumable "is this safe?" guide, ~260 lines, 7 sections:
  plain-English trust model; a 10-item mechanical-verification
  checklist with effort estimates per item; key files to read by
  concern; known caveats (things we DO claim vs things we DON'T);
  runnable verifier snippet; an honest-asterisk section about iOS
  binary provenance we don't currently solve for; responsible-
  disclosure pointer. Written so an agent can walk through it end
  to end without needing external context.

**Live verification (Phala CVM)**
- Running: git_commit `cc329a8`, compose_hash
  `0xa04608c72639c66a625706b7ac4b9f1ac8dd449c690a0544b173ecede265e83e`,
  Sepolia tx `0x7873c5dd4c9b6636994d9a3adda7ded8618394ce1a9f577a1ba9c74dc5acf7b0`.
- CLI auditor **8/8 green**.
- `TLS fingerprint` now stable across **six** compose rotations —
  `5698f0ade4bb412d…` unchanged from Phase 3 through Phase C.3.
  Phala dstack-KMS per-app derivation confirmed load-bearing.
- Live E2E: `/v1/identity/replace` correctly rejects missing
  envelope (400), `/v1/chat/response` envelope-branch field-
  validates (400 on malformed), plaintext content path still
  works (200, back-compat preserved). Full decrypt-mutate-rewrap
  flow validated against dstack simulator before deploy.

**What's left on Phase C**
- Phase C part 2: ACME-DNS-01 for `mcp.feedling.app` so
  Claude.ai sees a CA-signed cert issued inside the enclave.
  Needs a DNS API token + renewal scheduler. Task #30.

---

### [DONE] Phase C (part 1) — MCP in-enclave TLS + audit card Row 8

Closes the last plaintext-metadata gap at the TLS layer on the
pinnable path: MCP port 5002 now terminates TLS inside the enclave
with the same dstack-KMS-derived cert that port 5003 uses. The
`-5002s.` passthrough URL becomes pinnable end-to-end.

**Backend**
- New shared `backend/dstack_tls.py` — pulls `derive_tls_cert_and_key`
  and `TLS_KEY_PATH` out of `enclave_app.py` so both services use
  one source of truth for the cert (deterministic ECDSA-P256 derived
  from dstack-KMS at path `feedling-tls-v1`). Cert DER byte-stable
  across reboots of the same compose; matches across ports.
- `backend/enclave_app.py` — dropped inline derivation + a pile of
  crypto imports; imports from `dstack_tls` now. Behavior identical.
- `backend/mcp_server.py` — new `_materialize_tls_cert()`: when
  `FEEDLING_MCP_TLS=true`, derive the cert via dstack-KMS at boot,
  write cert + key to tempfiles, hand paths to uvicorn via
  `ssl_certfile` / `ssl_keyfile`. Plain HTTP otherwise so local
  dev stays simple. Logs the scheme on boot.

**Compose (deploy/docker-compose.phala.yaml)**
- `mcp` service: `FEEDLING_MCP_TLS=true` + mounts
  `/var/run/dstack.sock` so it can derive via dstack-sdk at boot.

**Audit (tools/audit_live_cvm.py)**
- Row 8 (new): MCP TLS cert bound to attestation. Raw TLS handshake
  against `-5002s.*`, compare `sha256(peer cert DER)` to the
  bundle's `enclave_tls_cert_fingerprint_hex`. Skipped with
  disclosure when attestation-side is still pre-Phase-3.
- Docstring refreshed "7-row" → "8-row".

**iOS (testapp/FeedlingTest/AuditCardView.swift)**
- `AuditReport` gains `mcpTlsCertBindingChecked` +
  `mcpTlsDisclosure`.
- After the attestation-port pin, a second `PinningCaptureDelegate`
  session opens a TLS handshake against the MCP URL and compares
  its captured `sha256(cert.DER)` to the same attested fingerprint.
- New "MCP port TLS bound to attestation" row with its own
  tap-to-expand mechanism copy explaining that the MCP port is the
  one the agent connects to, and that this second pin catches a
  middleman sitting between agent and enclave.

**Live verification (Phala CVM)**
- Running: git_commit `60014a7`, compose_hash
  `0x14cd6edb382b3229ebe36bf030f1bdc087765a9004d1ad323af58904c72df38f`,
  Sepolia tx
  `0xa6e0282c698cbe8e925c968624a2f2315bad5cc868568053598ccb6071984252`.
- CLI auditor **8/8 green** against the live CVM — MCP port
  fingerprint `5698f0ade4bb412d…` === attested fingerprint
  `5698f0ade4bb412d…` === attestation-port handshake fingerprint.
- `enclave_content_pk` + `enclave_tls_cert_fingerprint` unchanged
  across FIVE compose rotations now (Phase 3 → A.1 → A.1 fixed →
  A.6 → B → C). Phala dstack-KMS derivation is stable per app_id,
  confirmed once more.

**mcp.feedling.app unchanged**
- The `mcp.feedling.app` hostname (what Claude.ai uses) still
  terminates TLS at Caddy on the VPS for now, so no existing MCP
  connection breaks. The pinnable path is the
  `-5002s.dstack-pha-prod5.phala.network` URL.
- Moving `mcp.feedling.app` to layer4 SNI passthrough + ACME-DNS-01
  inside the enclave is the next Phase C sub-ship (requires a DNS
  API token + renewal logic; flagged in `docs/NEXT.md` §Phase C).

**Still pending for Phase C**
- ACME-in-enclave for `mcp.feedling.app`.
- Identity nudge decrypt-mutate-rewrap (MCP now runs inside the
  TDX boundary; can orchestrate the dance now — need a new
  `/v1/identity/replace` endpoint).
- Agent-authored chat reply encryption (`feedling.chat.post_message`
  — wrap plaintext before `/v1/chat/response` POST, extend endpoint
  to accept envelopes like `/v1/chat/message` does).

---

### [DONE] Phase B — Privacy UX + onboarding + audit card expansion

After `/plan-design-review` (9/10 overall) and `/plan-eng-review`
(scope accepted, 3 architectural fixes applied in-line), shipped the
full Phase B user-visible surface. The audit card explicitly promoted
to a first-class treatment per @sxysun's request to preserve the
attestation-details page and its "how we get them" affordance.

**Backend (backend/app.py)**
- `GET /v1/content/export` — caller's chat + memory + identity as
  one JSON blob, 50 MiB cap, ciphertext returned verbatim (iOS
  decrypts client-side). Attestation snapshot (compose_hash +
  enclave_content_pk at export time) bundled so future agents can
  verify origin. Frames excluded in Phase B (too large, low
  continuity).
- `POST /v1/account/reset` — destructive, requires
  `{"confirm": "delete-all-data"}` body token as a second signal of
  intent. Wipes user dir, removes user from users.json, revokes
  api_key cache. Idempotent in the safe-to-retry sense (second call
  401s because the user no longer exists).
- `Response` added to Flask imports.

**iOS (testapp/FeedlingTest/)**
- Design tokens (DESIGN.md mirror) inlined in `FeedlingAPI.swift`:
  `Color.feedlingSage / feedlingPaper / feedlingSurface / feedlingInk
  / feedlingInkMuted / feedlingDivider`; serif display font via
  `.system(design: .serif)` (iOS New York — zero asset loading);
  `Spacing.*` + `Radius.*` + `FeedlingMotion.*`;
  `FeedlingPrimaryButtonStyle` + `FeedlingSecondaryButtonStyle`.
  Kept inline in FeedlingAPI.swift because Xcode's `project.pbxproj`
  requires coordinated edits for new source files — documented.
- `ContentView` now wraps the tab bar in an onboarding gate.
  Gate flips on user action.
- `ComposeHashChangeConsentView` (full-screen modal): triggered when
  `/attestation` returns a `compose_hash` that differs from
  `UserDefaults "feedling.lastAcceptedComposeHash"`. Per the dstack
  tutorial §1 catch, trigger is **compose_hash**, NOT MRTD —
  MRTD/RTMR0-2 are dstack-OS platform signals that change for
  reasons unrelated to our app.
- `OnboardingView` (3 slides, SwiftUI `TabView.page`):
  lock.shield / arrow.triangle.branch / hand.raised.square.on.square
  as the single glyph anchor per slide. No custom illustrations
  (AI-slop-free). Decision tokenized in `docs/PHASE_B_PLAN.md`.
- `PrivacyPageView` — NavigationLink destination from Settings.
  Hero row + Your data + Where your data lives + Advanced sections.
- `ExportSheet` — export → iOS share sheet, with an explicit iCloud
  Drive caveat in the copy.
- `DeleteSheet` — "download my data first" checkbox defaults to ON
  (decision `2A` from `/plan-design-review`). If checked,
  pipeline exports through iOS share sheet, then deletes after
  dismissal; if unchecked, delete is immediate.
- `ResetAndReimportSheet` — 3-step pipeline (export / delete /
  re-register) with visible step indicator.
- `RunbookView` — fetches `skill/SKILL.md` from GitHub raw so users
  can pass a live copy to their agent. Offline fallback included.
- `StorageBackendView` — thin wrapper around the existing storage
  toggle so Privacy's "Where your data lives" row has a destination.
- `AuditCardView` extended: `AuditRowView` per-row tap-to-expand
  mechanism panel (plain-language explanations naming primitives
  honestly — TDX, PCK, `mr_config_id` — but with analogies); new
  collapsed "Show raw /attestation (for auditors)" footer panel
  with SF Mono horizontally-scrollable pretty-printed JSON;
  existing PinningCaptureDelegate + DCAP + SPKI-pin flow unchanged
  (security primitives preserved per eng review).
- `FeedlingAPI.exportMyData` / `deleteMyDataAndResetLocalState` /
  `acceptComposeHashChange` / `signOutForComposeChange` /
  `hasCompletedOnboardingV1` + `evaluateComposeHashChange` wired
  into `refreshEnclaveAttestation`.
- `ContentKeyStore.wipeKeypair` + `KeyStore.wipeKeypair` for the
  delete path.

**Live verification (Phala CVM)**
- Running: git_commit `123a45b`, new compose_hash published on
  Sepolia (see DEPLOYMENTS.md §Phase B for the tx hash).
- CLI auditor 7/7 green against the new image.
- Export + reset endpoints verified locally end-to-end:
  register → seed → export → `Content-Disposition` filename valid;
  reset w/o confirm → 400; reset with confirm → 200; post-reset
  call → 401. Same behavior on the live CVM.
- iOS build: `xcodebuild BUILD SUCCEEDED` on iPhone 16 Pro sim.
  First-launch screenshot captured at
  `docs/screenshots/onboarding_slide1_phase_b.png`.

**Deferred to Phase B wave-2**
- Per-item visibility toggles (endpoint exists via rewrap; UI is
  a list + switch per row — ~2h of iOS).
- Inline migration-progress row in the Privacy hero (wire in
  `runSilentV1MigrationIfNeeded` progress stream).
- `docs/screenshots/` captures of Slides 2-3, Privacy page, Delete
  sheet, audit-card expanded state — need UI automation to drive
  the sim without controlling the user's mouse.
- Copy review by @sxysun — the audit-card mechanism reveals, the
  compose-hash consent copy, the onboarding headlines. The register
  is load-bearing ("name primitives honestly + analogies") and
  needs the product-voice pass flagged in `PHASE_B_PLAN.md §4`.

---

### [DONE] Phase A.6 — Silent v0→v1 migration on first launch

**Backend (backend/app.py)**
- New `POST /v1/content/rewrap` endpoint. Batched, idempotent. Takes `{items: [{type, id, envelope}]}` for `type ∈ {chat, memory}` and swaps the item's v0 plaintext fields with the v1 envelope fields in place, preserving metadata (ts/role/source for chat; occurred_at/created_at/source for memory). Per-item result + summary counts. Owner binding enforced: `envelope.owner_user_id` must match the caller's resolved `user_id`, else the item is rejected before storage. Identity intentionally not supported — would trap users pre-Phase-C because `nudge` can't mutate a v1 card.
- `/v1/identity/nudge`: when called against a v1 card, now returns `409 {"error": "nudge_not_supported_on_v1_cards_yet", "phase_reference": "docs/NEXT.md §Phase C"}` instead of silently 404'ing because `dimensions` is encrypted inside `body_ct`.

**iOS (testapp/FeedlingTest/)**
- `FeedlingAPI.ensureUserIdIfNeeded()` — when an api_key is present but `userId` is empty (env-injected creds, self-hosted handoff), populate via `/v1/users/whoami`. Needed so migration can bind AEAD AAD to the right owner.
- `FeedlingAPI.runSilentV1MigrationIfNeeded()` — gated on dated `UserDefaults` flag. Fetches chat (up to 500) + memory (up to 200), collects v0 items, wraps each via `ContentEncryption.envelope`, POSTs in batches of 100 to `/v1/content/rewrap`. Sets flag only when all batches complete with no errors; transient failures retry on next launch.
- `FeedlingTestApp.swift` wires the new startup steps in sequence: register → ensureUserId → content keypair → attestation refresh → migration. Non-blocking.

**Live verification (Phala CVM)**
- Running: git_commit `90c8ff6`, compose_hash `0x9f7fe0a823bf2820877851863d322b0f3be7fff819a40a8826e6ca994597cf48`, Sepolia tx `0xb3b434b6db6abd45eb492d2a708d8d7d6b99d5af59d5f01bc1686a74ed3e6c27`.
- `enclave_content_pk` + `enclave_tls_cert_fingerprint` unchanged from Phase A (confirms the dstack-KMS key-derivation-independent-of-compose-hash observation is stable across two more compose rotations).
- CLI auditor 7/7 green. `/v1/content/rewrap` reachable on prod.
- Local E2E against dstack simulator: seeded 3 v0 chat + 3 v0 memory, iOS launched with seeded api_key, migration reported `ok=6`, server afterwards had 0 v0 / 3+3 v1 items, enclave decrypt returned correct plaintext for all.

**Follow-up (A.6e)**
- Only one real prod user today (a private tester). After her iOS launches the updated app and the migration flips her data to v1, strip the v0 accept branches in backend handlers, the v0 fallback paths in MCP tools, and the `/v1/content/rewrap` endpoint itself (single-use). Tracked as task #23.

---

### [DONE] Phase A — Content encryption rollout for agent-authored writes

**Backend**
- `/v1/users/whoami` now returns `public_key` (user's X25519 content pubkey from users.json)
  and `enclave_content_public_key_hex` (cached from enclave `/attestation`, 60s TTL).
  One round trip gives MCP everything it needs to wrap an envelope.
- New `backend/content_encryption.py` — Python counterpart to iOS `ContentEncryption.swift`.
  `box_seal` uses HKDF-SHA256(salt=None, info="feedling-box-seal-v1"), nonce=SHA256(ek||rcp)[:12],
  ChaChaPoly. `build_envelope` produces the `{"envelope": …}` shape POSTed to
  `/v1/{chat/message,memory/add,identity/init}`.

**MCP (backend/mcp_server.py)**
- `feedling.memory.add_moment` wraps `{title, description, type}` into a v1 envelope
  before POSTing. Plaintext metadata (`occurred_at`, `source`) rides alongside inside
  the envelope dict for server-side sorting.
- `feedling.identity.init` applies the same wrap to `{agent_name, self_introduction, dimensions}`.
- `feedling.identity.nudge` intentionally left on the plaintext path — in-place mutation of an
  encrypted card requires decrypt→mutate→rewrap, cleanly solved by Phase C (MCP-in-TEE).
- New `_get_decrypted()` — when `FEEDLING_ENCLAVE_URL` is set, MCP routes `memory.list`,
  `identity.get`, `chat.get_history` through the enclave's decrypt proxy so agents see
  plaintext. Unset → fall back to Flask.
- Fallback: pre-v1 users (no uploaded pubkey) or unreachable enclave → v0 plaintext POST,
  so agents never lose write capability mid-session.

**Compose (deploy/docker-compose.phala.yaml)**
- `backend.FEEDLING_ENCLAVE_URL = https://enclave:5003` (Phase 3 missed this — backend calls
  enclave `/attestation` to cache content pubkey for whoami).
- `mcp.FEEDLING_ENCLAVE_URL = https://enclave:5003` (routes MCP reads through decrypt proxy).
- `enclave.FEEDLING_FLASK_URL = http://backend:5001` (fixes a latent bug: enclave's decrypt
  handlers call `/v1/users/whoami` on Flask, but the 127.0.0.1 default doesn't resolve
  across distinct compose containers — returned 500 on the first test deploy).

**Live verification (Phala CVM)**
- Running: git_commit `8b53404`, compose_hash `0x593cb8aaa1fd5ed964fdb3a1718200114ab36537f1cf551fd5162fc02512eb80`,
  Sepolia tx `0x5b5a933dfc6e1f6376a32029d7a31632723dcc75447104b12ebd5da5e2f3e825`.
- CLI auditor 7/7 green. End-to-end: register a fresh user → MCP-side wrap via
  `backend/content_encryption.build_envelope` → server stores ciphertext only (no plaintext
  `title`/`description`/`type`) → enclave `/v1/memory/list` returns plaintext via `K_enclave`.
- Observation worth recording: Phala dstack-KMS derives per-app keypairs from
  `(kms_root, app_id, path)`, NOT from `compose_hash`. So `enclave_content_pk` and the
  TLS cert are stable across compose rotations for this app_id. This is stronger than
  `docs/DESIGN_E2E.md` §5.3 assumed — no re-wrap dance is needed after a compose update,
  which simplifies operational rollouts.

**Still pending for Phase A**
- A.6 silent migration of pre-existing v0 plaintext rows (chat/memory/identity) into v1
  envelopes on first iOS launch post-update. Design in NEXT.md; needs a
  `POST /v1/content/rewrap` endpoint.
- Chat replies from agent (`feedling.chat.post_message`) still POST plaintext — paired
  with `identity.nudge` as the "Phase C dependencies" bucket, for the same
  decrypt-mutate-or-write-through-TEE reason.

---

## 2026-04-20

### [DONE] Phase 3 — TLS-in-enclave + iOS cert pinning

**Enclave (backend/enclave_app.py)**
- 新增 `FEEDLING_ENCLAVE_TLS=true` 开关；启用后从 dstack-KMS 派生 ECDSA-P256 keypair
  (`feedling-tls-v1` path)，用 RFC-6979 deterministic ECDSA 签发自签 cert —
  同一 compose_hash 下跨 reboot 的 cert.DER 完全一致（本地 simulator 验证过两次 boot 哈希相同）。
- `build_report_data()` 现在把 sha256(cert.DER) 真正填入，替换原先的 32-byte 零占位符。
- Flask `app.run(ssl_context=…)` — SSL 材料先写入临时文件、`load_cert_chain` 后立即 unlink，
  cert/key 不落盘。
- `/attestation` bundle 新增 `tls_in_enclave: true` 标志 + 更新 notes；`phase` 字段从 1 跳到 3。

**Compose (deploy/docker-compose.phala.yaml)**
- enclave service 加 `FEEDLING_ENCLAVE_TLS: "true"`。
- healthcheck 从 `curl http://127.0.0.1:5003` 改成 `curl -k https://127.0.0.1:5003`。

**iOS (testapp/FeedlingTest/)**
- attestation URL 从 `-5003.` 切到 `-5003s.`（dstack-gateway TLS passthrough 后缀）。
- `PinningCaptureDelegate`（AuditCardView.swift）在 TLS 握手时记录 leaf cert sha256(DER)，
  审计流程把它和 bundle 的 `enclave_tls_cert_fingerprint_hex` 比对 —
  匹配 = 绿；不匹配 = 硬红 "MITM detected."；全零 = 沿用原 amber 免责声明。
- `FeedlingAPI.refreshEnclaveAttestation` 的那条启动时 fetch 用 `AttestationTrustShim`
  接受自签 cert（只是预热 content pubkey，真正的 pin 在审计卡里）。
- `AuditCardView` 底部的 TLS row 文案更新：不再提 "Phase 1 placeholder"。

**CLI auditor (tools/audit_live_cvm.py)**
- 新增 Row 7：raw TLS 握手取 peer cert DER，sha256 和 bundle 的 fingerprint 比对。
  全零 fingerprint 走 pre-Phase-3 disclosure 分支（不算 pass 但也不算 fail）。
  文件开头的 docstring 从 "6-row audit" 改为 "7-row audit"。

**Live 验证**
- Phala CVM `feedling-enclave` (UUID `4386636e-1325-4b92-99d8-f2ca00befdb4`) 跑在 git_commit `451b5b0`。
- 新 compose_hash `0xb0fb1f848151ec8fb39c4814f138b1d1b143d4d729dc800302d5123c1c0f2163` 已在
  Eth Sepolia FeedlingAppAuth 上 authorize（tx `0x8de67abaf677e221ba4ee34b5a004753d0f4981bdc3c952cbcb4112a652a169c`）。
- TLS cert fingerprint: `5698f0ade4bb412d6b0847a62d695138f3bbd287dc7d1dbdeb67b15dc445e5ef`。
- CLI 7/7 green；iOS 6/6 green（见 `docs/screenshots/audit_card_phase3_tls_pinned.png`）。

**Trust model note**: self-signed cert 是有意的；用户信任链不是 CA chain，而是
"TDX-attested REPORT_DATA 里有这张 cert 的 fingerprint"。伪造 TLS cert 的操作员也
必须同时伪造 REPORT_DATA，而 REPORT_DATA 由 Intel PCK 签名 — 做不到。

**Deferred**
- `docs/NEXT.md` 里 "Phase 4-6" 的内容迁移 / 全量加密 / 用户自放 enclave job 未动。
- iOS 审计卡文案 copy review 仍然待 @sxysun 过一遍。
- Sepolia → Base 迁移（详见 deploy/DEPLOYMENTS.md §Planned）。

---

## 2026-04-19

### [DONE] NEXT.md Steps 1-5：multi-tenant backend + MCP SSE + iOS onboarding + self-hosted runbook

**后端 (backend/app.py)**
- 引入 `SINGLE_USER` 环境变量：`true` = 兼容旧的 flat layout；`false` = 多租户 `~/feedling-data/<user_id>/…`。
- 新增 `POST /v1/users/register` + `GET /v1/users/whoami`。
- 新增 `require_user()` 中间件：接受 `X-API-Key` / `Authorization: Bearer` / `?key=` 任何一种形式；
  SHA-256(HMAC+pepper) 哈希比对，per-process 缓存避免 bcrypt 开销。
- 所有 module-level state（frames/chat/tokens/push cooldown/live activity dedupe/bootstrap/identity/memory）
  重构为 `UserStore` 类，每用户一份；waiters 也按用户隔离，防止跨用户唤醒。
- WebSocket ingest handler 也接 `?key=` 或 `Bearer <key>`；`SINGLE_USER=true` 时跳过鉴权。

**MCP server (backend/mcp_server.py)**
- 切换 transport 到 SSE（保留 streamable-http 作为备选，走 `FEEDLING_MCP_TRANSPORT` 环境变量）。
- 新增 `KeyCaptureMiddleware`（ASGI 层）：监听每个 HTTP 请求，从 query/header 抓取 `key`，映射到 session_id；
  未知 session_id 时按 client IP 回溯 pending_keys，保证 SSE GET 与后续 POST tool call 能绑定。
- 每个 tool 调 `_current_api_key()` 拿当前 session 的 key，作为 `X-API-Key` 转发给 Flask；
  Flask 里 401 会自动冒泡成 tool error。

**iOS (testapp/)**
- `FeedlingAPI.swift` 完全重写：@MainActor ObservableObject + legacy static accessors；
  持久化到 UserDefaults + app-group shared defaults；`authorizedRequest(path:…)` 自动注入 X-API-Key。
- `KeyStore`：首次启动生成 Curve25519 keypair，private 存 Keychain（accessibleAfterFirstUnlockThisDeviceOnly）。
- `ensureRegisteredIfCloud()`：在 cloud mode 下如缺 api_key 自动注册；403（后端为 single-user）时记号跳过，不再重试。
- Settings tab 加了 Storage 切换、Agent Setup 复制按钮、用户 ID 展示、Regenerate key 按钮。
- broadcast extension 通过 app-group key（`ingest_ws_token`）自动拿到 api_key 作为 WS Bearer。

**Skill runbook (skill/SKILL.md)**
- 新增 Self-Hosted Setup 小节：0 前置 → 1 clone → 2 `openssl rand -hex 32` → 3 venv+deps → 4 env → 5 systemd → 6 smoke → 7 Caddy（可选）→ 8 告诉用户 → 9 端到端验收；每一步都带 **Verify** 行。
- 新增 troubleshooting 表（chat bridge / MCP 401 / Live Activity / frames not arriving）。

**部署 (deploy/)**
- `feedling.env.example` 加 `SINGLE_USER`、`FEEDLING_MCP_TRANSPORT`。
- `setup.sh` 新增 `--install-caddy` 开关，能自动 `openssl rand -hex 32` 并写 env 文件。
- `Caddyfile` 给 `/v1/chat/poll` 长轮询放宽 response timeout 到 90s。

**测试**
- `backend/test_api.py` 加 `--multi-tenant` 和 `--key <shared>` 两种模式；
  新增 Section 8（isolation + 401 + Bearer + query-key + whoami）；single-user 也保持全绿。
- 本地和测试 EC2 (`ec2-34-228-180-146`) 全通过；MCP SSE 端到端：initialize → tools/list → tools/call bootstrap 在正确/错误 key 下表现都对。

---

## 2026-04-18

### [DONE] Phase 0 T0.1 + Phase 1 T1.1/T1.2/T1.3 + Phase 2 T2.1-T2.5 + T3.1/T3.2/T4.2

**后端 (backend/app.py)**
- 新增 identity card HTTP endpoint（init/get/nudge），5 维固定
- 新增 memory garden HTTP endpoint（add/list/get/delete）
- 新增 bootstrap endpoint（first_time 返回 instructions，already_bootstrapped 防重复）
- T3.1：删除 `should_notify`，改为 `rate_limit_ok`（纯平台层 flag）
- T3.2：push payload 通用化，ContentState 改为 title/subtitle/body/personaId/templateId/data
- T4.2：`bootstrap_events.jsonl` 日志（bootstrap_started / identity_written / memory_moment_added）

**MCP server (backend/mcp_server.py)**
- 新建 FastMCP server，14 个 tool，全部调 localhost:5001
- push tool 参数同步更新为新 ContentState 字段

**部署 (deploy/)**
- Caddyfile：mcp.feedling.app → 5002，api.feedling.app → 5001
- 3 个 systemd service（feedling-backend / feedling-mcp / feedling-chat-bridge）
- setup.sh + feedling.env.example

**iOS (testapp/)**
- T3.2：ScreenActivityAttributes.ContentState 改为通用字段
- T3.2：ScreenActivityWidget.swift 渲染 title/body/subtitle
- T2.4：AppTab 扩展为 chat/identity/garden/settings 四 tab
- T2.1：IdentityView.swift + IdentityViewModel.swift（radar chart，5 维，10s 轮询）
- T2.2：MemoryGardenView.swift + MemoryViewModel.swift（卡片列表，10s 轮询，新卡片高亮）
- T2.3：Settings 加 Connection section（API URL + pairing code 占位符）
- T2.5：bootstrap 检测（identity nil → non-nil 时自动切到 Identity tab）
- FeedlingTestApp.swift：注入 IdentityViewModel / MemoryViewModel

### [DECISION] chat_bridge 改为 opt-in，默认不启动

- chat_bridge.py 是临时 Hermes 自动回复桥，有了真 MCP Agent 后会冲突
- 迁移到 systemd 后 feedling-chat-bridge service 只 install 不 enable
- Hermes 用户手动 `systemctl enable feedling-chat-bridge`，Claude.ai / OpenClaw 用户不需要跑

### [DECISION] 身份卡维度固定 5 个

- v1 先硬编码 5 维，UI 定稿后再调整
- 影响：T1.1 数据库 schema dimensions 数组长度验证改为 exactly 5；Open Decision #1 关闭

### [DECISION] 删除 T0.2 OAuth server，不做

- Claude.ai connector UI 只需填 Name + URL，不需要 OAuth
- 删除 ROADMAP T0.2（OAuth 2.1 + Dynamic Client Registration）
- 删除 Open Decision #1（自建 vs Auth0）
- 影响：PROJECT_BRIEF Section 6.1 和 Section 7.1 去掉 OAuth 相关描述

---

## 2026-04-18

### [BRIEF][ROADMAP] 项目起点 / Project kickoff

- 建立 `PROJECT_BRIEF.md` 和 `ROADMAP.md` 两份文档
- 两周 roadmap：Phase 0（MCP server 层）→ Phase 1（身份卡 + 记忆花园后端）→ Phase 2（iOS UI 粗糙版）→ Phase 3（技术债）→ Phase 4（内测准备）
- 目标用户：人机恋群体 + 用 Claude / ChatGPT / 自跑 Agent 的技术派
- 内测渠道：300 人的人机恋群里挑 30-100 人
- 核心原则：Feedling 不替换只增补；身体 vs 大脑；Feedling 没有意见
- 关键产品决定：Claude.ai 用户的记忆花园数据来源 = Agent 自己用 `conversation_search` 搜历史，Feedling 不导入任何 Claude 数据

### [ROADMAP] 记下 4 个 Open Decisions 待定

1. OAuth server 自建还是 Auth0
2. 身份卡维度数量严格 3-5 还是完全自由
3. Persona 系统 v1 要不要对用户可见
4. UI 设计稿定稿时间

---

## 模板示例（删掉或保留都行）

以下是几条示例，展示不同情境下该怎么记：

---

## 2026-04-22（示例）

### [DONE] Phase 0 完成
- T0.1 FastMCP server 层跑通
- T0.2 OAuth 用了 Auth0 免费 tier（见下面 DECISION）
- T0.3 Caddy + Let's Encrypt 部署在 `mcp.feedling.app`
- T0.4 claude.ai 里成功添加 custom connector，推送 "hello from Claude" 到灵动岛成功
- **实际用时：2.5 天（估计 3 天，稍快）**

### [DECISION] OAuth 用 Auth0 不自建
- **选择**：Auth0 免费 tier
- **原因**：两周 scope 下自建 OAuth 2.1 + DCR 风险太高，Auth0 能节省 2 天
- **影响**：v2 可能会自建替换，届时需要迁移策略
- **影响文档**：ROADMAP Open Decisions #1 勾掉

---

## 2026-04-25（示例）

### [UI] 设计师给出身份卡/记忆花园定稿
- 新增 `docs/UI_SPEC.md`
- 身份卡确定为六边形（不是五边形），6 维
- **影响 Open Decision #2**：维度数量定为严格 6 维（不是 3-5）
- **影响 ROADMAP**：
  - 新增 Phase 2.5 "UI polish"，预计 2 天
  - Phase 1 的 identity schema 里 dimensions 数组长度约束改为 6
  - 已写入数据库的测试数据需要 migration

### [ROADMAP] 删除一个 task
- T3.5 "Mock endpoint 清理" 挪到 v2，v1 不做

---

## 2026-05-02（示例）

### [FEEDBACK] 内测第一周反馈
- 15 个用户接入成功，3 个卡在 Claude.ai connector 授权环节
- 共同痛点：onboarding guide 里没讲"为什么要 OAuth"，用户警惕
- **动作**：改 `docs/onboarding/claude_ai.md`，加一段"Feedling 拿到什么、拿不到什么"的解释
- 2 个用户反馈身份卡维度看不懂——维度名字是 Agent 写的，但没有解释文字默认不展开
  - **动作**：T2.1 改，默认展开第一维的 description

### [PIVOT] 削减 ChatGPT 用户支持
- 内测发现 ChatGPT Developer Mode 流程太复杂，3 个想接的都放弃了
- **决定**：v1 内测不再主推 ChatGPT 路径
- **影响**：`PROJECT_BRIEF.md` Section 6.3 → 改成"Claude.ai / Claude Desktop / 自跑 Agent"三类；ChatGPT 挪到"Not in scope"
- **不删除相关代码**——只是不在 onboarding 里提

---

（示例结束。实际使用中，删除上面三条示例，只保留真实发生的记录。）
