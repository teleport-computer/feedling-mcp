# Hosted model_api 退役路线图（API-key 用户统一到 agent-runner）

日期：2026-06-25
配套：`docs/AGENT_RUNTIME_CC_CODEX_PLAN.zh.md`（agent-runner 总设计）、
`backend/agent_runtime/README.md`（P0–P5 现状）。

## 目标 / 终态

让 **API-key 用户变成"托管版的 resident 用户"**：每个用户有自己独立的
agent（独立 Claude/Codex 会话 + 独立 home + 独立 provider key），通过
agent-runner consumer 走与 VPS/resident 用户**同一套**后端工具、加密、chat
协议。新路径稳定后，**删除现有 hosted model_api 的 inline-agent 代码**，
`/v1/model_api/chat/send` 收敛成"只投递 + agent-runner 处理"。

**这是一个多阶段迁移，不是一次性删除。** 当前 P3 cutover 故意保留 legacy 作
rollback；在下列 gate 全绿之前删 legacy 都是不安全的。

## Consumer 收敛策略（已定：以 resident 为主）

决策（2026-06-25）：**canonical consumer = VPS 现存的
`tools/chat_resident_consumer.py`**，不再单独维护一份 hosted consumer。
`backend/agent_runtime/` 只做 resident consumer 缺的**多租户托管层**
（supervisor + lease + 每用户 spawn/隔离），用 `cli` 模式驱动 `claude` /
`codex exec`。

据此**已删除**先前自写的 native consumer 层（`consumer.py` / `consumer_main.py`
/ `drivers/` / `mcp_server.py` / `events.py` / `feedling_client.py` 及其测试）；
**保留** `supervisor.py` / `spawners.py` / `leases.py` / `tokens.py` / 迁移
`0005` / `hosted/agent_runtime_cutover.py`。

**直接收益**：Stage A 的多数对等项（verify-ping、输出清洗、图片、proactive、
屏幕上下文）resident **本来就有**，托管它即"免费"获得 → Stage A 大幅缩小。
工具模型也跟随 resident：cli agent（Claude Code）通过 **feedling-io-tools MCP
插件**拿工具（不是先前的进程内 SDK MCP）。

## 现状盘点（代码面）

精确依赖（grep 实测，2026-06-25）：

- `model_api_runtime/`（prompts / tools / memory_tools / wake）**只被**
  `app.py` + `hosted/*` import；perception/proactive 仅引用 config_store 的
  *runtime profile*（同名字符串），不 import 这个包。
  > 注：`CONTRIBUTING.md` §1 称 model_api_runtime「被 hosted·proactive·perception
  > 复用」——按当前代码这条**已过时**，删除前需再确认一次。
- `hosted.context` / `hosted.turn` / `hosted.wake_consumer` **只被** hosted/ +
  app.py import → 可成块移除。

### hosted/ 文件分类

| 文件 | 处置 | 原因 |
|---|---|---|
| `setup_routes.py` | **保留** | provider key 配置 `/v1/model_api/setup\|get\|test\|delete\|runtime\|memory/repair`；存 `api_key_envelope`（agent-runner 复用） |
| `config_store.py` | **保留(瘦身)** | model_api config 存储 + `agent_runtime_driver` flag + runtime profile 保留；`_load_runtime_provider_config`（服务端 inline 解密）退役后变死代码可删 |
| `history_import.py` | **保留** | 聊天历史导入（onboarding），与 inline loop 无关 |
| `onboarding_validation.py` | **保留** | onboarding 门禁，通用 |
| `agent_runtime_cutover.py` | **保留** | 成为唯一路径 |
| `chat_routes.py` | **改** | 删 `model_api_chat_send` 的 inline-agent body，端点保留为「投递 + cutover」 |
| `turn.py` | **删** | inline turn 解析 / pending 确认 / action trace |
| `context.py` | **删** | inline LLM 的上下文拼装（agent-runner 用工具自取上下文） |
| `wake_consumer.py` | **删** | legacy 后端内 proactive wake（P4 agent-runner wake 取代）；需在 app.py 解线 |
| `model_api_runtime/` 整包 | **删(待解耦)** | 仅 inline 线使用；但 setup/config/history/onboarding 也 import 了它（多为 profile/contract message），删前要把这些保留文件对它的依赖摘干净 |

### app.py 接线（删除时要动的装配点）
- L55–L70 `from model_api_runtime.prompts/tools/wake import (...)`
- L84–L92 `from hosted import chat_routes/config_store/context/.../turn/wake_consumer`
- L325–L332 hosted wake consumer 的 COMPAT re-export
- L371–L373 `core_wake_bus.register_handler("proactive", hosted_wake_consumer.try_consume_pending_for_user)`
- hosted tick 的启动（`_maybe_start_hosted_wake_consumer` / `_run_hosted_tick_once`）

## 特性对等清单（删 legacy 前必须在 agent-runner 补齐）

legacy inline 线现在做、agent-runner **还没做**的：

1. **图片 / 多模态**：当前 cutover **特意把 `has_image` 留在 legacy**。consumer
   需能解密图片信封（二进制，非 utf-8）并作为图像输入喂给 Claude/Codex。
2. **memory capture**：legacy 每轮后抽取记忆并落库。agent-runner 需要一个
   写类工具 `memory_write` 或一个 post-turn capture 步骤。
3. **web 搜索**：legacy 用 `model_api_runtime/tools.py`(DuckDuckGo)。agent-runner
   可直接用 Claude/Codex **原生联网工具**替代（更简单），按 allowlist 放开。
4. **identity / memory 改动的待确认（pending confirmation）流程**：需要写类工具
   + 策略门 + 可选用户确认（plan 安全要求）。
5. **proactive wake 语义**：`sleep` / `send_message` / `set_ai_state` / schedule。
   P4 已做「可见输出→主动消息 / 空→沉默」，其余动作（schedule_wake/set_ai_state）
   待补。
6. **action trace / usage 观测**：legacy 写 `model_api_action_traces`。agent-runner
   需补等价埋点（运维要求里也列了 turn latency / provider errors 等指标）。
7. **provider key 自测**：`/v1/model_api/test` 现在用 `_load_runtime_provider_config`
   做一次真实 provider 调用——保留该端点即可（它独立于 inline chat）。
8. **verify-loop ping 处理**（之前漏列，关键）：resident 专门识别
   `/v1/chat/verify_loop` 的存活探针——内容带 `__VERIFY_PING__` 标记的合成消息，
   回一条罐头 `__verify_ack__`（`suppress_push`，不进用户可见聊天），并跑一次
   **有界 probe** 真打一遍回复管道（抓"回复链路坏了"而非靠罐头蒙混）。
   agent-runner **完全没有**：会把 verify ping 当普通用户消息丢给 agent 真跑一轮，
   既浪费一次 provider 调用，又可能让 verify_loop 拿不到预期 ack。
   **影响**：onboarding 绿灯依赖 verify_loop 成功；这条不补，被迁用户可能卡在
   `needs_live_connection`（见 memory `verify-loop live-connection wedge`）。
   做法：consumer 在 `process_message` 前先识别 ping（`__VERIFY_PING__` 标记 +
   `source`/合成标识），命中就走"罐头 ack +（可选）有界 probe"短路，不进 driver。
9. **输出清洗层**：resident 有 `_strip_reasoning_sections` / `_sanitize_thinking_*`
   （剥思考过程、防身份/系统提示泄漏、把"思考摘要"清成可展示短文本）。
   agent-runner 直接拿 Claude/Codex 的 `TextBlock` 当回复，**没有这层保险**。
   托管 driver 输出通常较干净，但仍需：① 用 `system_prompt` 约束不外露推理/工具叙述
   （已部分做）；② 一个轻量出站清洗 + thinking 分桶（可借鉴 resident 的实现），
   避免模型偶发把思考/身份串进用户可见消息。

## 迁移机制（已具备 / 待补）

- **provider key**：已存为 `api_key_envelope`；agent-runner 经 enclave JIT 解密。
  **【已补 Stage B】** supervisor `_resolve_roster` 用用户 api_key 调
  `GET /v1/model_api/key_envelope` 自取密文信封再 JIT 解 → roster 只需 api_key。
- **agent 选择 = 按 provider 派生(不让用户选)【改 2026-06-26:codex 兜底 + 只给
  codex 包 LiteLLM】**:实测各 CLI 锁 wire 格式(claude code=Anthropic Messages、
  codex 0.136=OpenAI **Responses**,已砍 chat)。**最终映射**:
  - `anthropic`/`deepseek`(走其 `/anthropic` 端点)→ **claude**(claude code **只**
    支持这两家,不接 LiteLLM,保最稳 + prompt caching);
  - `openai` → **codex (native)**:用原始 OpenAI key 直连 `api.openai.com/v1/responses`;
  - `gemini`/`openrouter`/`openai_compatible`/其余 → **codex (gateway)**:codex 是
    **兜底 agent**,这些经 **in-CVM LiteLLM** 翻译(codex 只暴露 Responses,LiteLLM
    fan-out 到真 provider);
  - provider 缺失/未知 → `legacy`(不托管)。
  实现:`cutover.driver_for_provider()` + 新 `cutover.codex_transport()`(native/
  gateway)+ `db.list_agent_runtime_enabled_users` SQL(CASE 默认 `codex` + 回传
  provider)+ `spawners`(codex gateway 写 `codex-home/config.toml` 指 LiteLLM、
  `CODEX_API_KEY`=网关 key 而非上游 key)。**为何只给 codex 包 LiteLLM**:claude 这
  侧原生只两家、够稳且省成本;扩覆盖面的复杂度全压到 codex 一条线,**单一网关、单一
  agent×后端 eval 面**,claude 线零网关风险。
- **in-CVM LiteLLM 网关(codex 专用,已选为默认而非可选 B 计划)**:只服务 codex 的
  `/v1/responses`(websocket),fan-out 到 gemini/openrouter/openai_compatible:
  ```
  Codex (gateway 档,wire_api=responses, base_url=litellm/v1, CODEX_API_KEY=网关key)
     └─ LiteLLM /v1/responses ──fan-out──→ Gemini / OpenRouter / OpenAI-compatible / …
                                            (上游 provider key 在 LiteLLM 配置里,
                                             永不进 consumer 进程)
  Claude Code 不经网关:anthropic 直连、deepseek 走其 /anthropic 端点。
  ```
  **实测(2026-06-25,LiteLLM 1.89.4 + codex 0.136)**:
  - `codex → LiteLLM /v1/responses(websocket) → Claude haiku`:**完整工具循环**
    (发 `command_execution` 真跑 shell、文件真建出、答对字节数)✓ → 证明 codex 经
    LiteLLM Responses 端点的连接 + 工具调用都成立。
  - codex 直连 OpenRouter/DeepSeek **不行**(它们只有 Chat、没 `/v1/responses`);
    `wire_api=chat` 已被 codex 0.136 移除 → **必须经 LiteLLM 的 Responses 端点**。
  **代价 / 约束**:① LiteLLM 看到**明文 prompt + 上游 provider key** → 必须跑在
  **enclave 度量域内**(不能用云端 LiteLLM),纳入 compose-hash;② 丢 prompt caching
  → 成本上升;③ **工具 schema 兼容因「codex×后端」而异(已实锤)**:`codex → DeepSeek`
  **失败**——codex 工具定义带 `type:"namespace"`,GPT/Claude 都吃,但 DeepSeek Chat
  API 只认 `type:"function"` → 400(**所以 deepseek 派给 claude 而非 codex,正好绕开**);
  `codex → Gemini` 本地没测成(GEMINI key 地理封锁,非翻译问题)→ **放开 gemini/
  openrouter 前必须各自过一遍 codex 工具 eval**;④ 跟着 codex 的 wire 演进维护。
  **LiteLLM 服务 + 按用户配置生成【已实现 2026-06-26】**:
  - 纯模块 `backend/agent_runtime/litellm_gateway.py`:按 gateway 用户生成 LiteLLM
    `model_list`(`model_name=gw-<uid>` → `<prefix>/<真实model>`,**api_key 用
    `os.environ/FEEDLING_UPKEY_<uid>` 引用,密文绝不入盘**)。**配置用 `json` 渲染
    (JSON 是 YAML 合法子集,LiteLLM 的 `yaml.safe_load` 照样解析)→ 模块 import 期
    零 PyYAML 依赖(Codex 第三轮 P2 修复:supervisor import 它,须在不含 PyYAML 的
    hash-lock 后端依赖下可加载,否则 CI/镜像 `ModuleNotFoundError: yaml`)。** 另含
    `drop_params`/
    `additional_drop_params`(剥 `reasoning`/`thinking`,非 Anthropic 后端不 400)+
    `master_key=os.environ/FEEDLING_LITELLM_API_KEY`(codex 的 bearer)。
  - `GatewayManager`:把上游 key 注入 LiteLLM **子进程 env**(在内存、不落盘)。重启
    条件(**Codex 第二轮 P2 修复**):**路由签名**(user_id/provider/model/base_url,
    **不含密钥**)变化 **或 上游 key 轮换**(单独比对注入的 env map——key 只在启动注入,
    不重启则代理一直用旧 key)**或 子进程已死**(`reconcile` no-op 分支先 `poll()` 探活,
    崩溃则重启,不必等 supervisor 重启);无 gateway 用户则停代理。launcher/stopper/
    writer 可注入,已单测(写配置/注入 key/不重启/key 轮换重启/崩溃自愈/增删用户/停代理)。
  - supervisor 接线:`_gateway_entries`(挑 codex-gateway 用户,带真实 model+上游 key)
    + `_wire_gateway_models`(把这些用户的 codex 请求模型改写成 `gw-<uid>`,真实模型留给
    LiteLLM 路由);discovery 现回带 `model`;主循环 behind **`FEEDLING_LITELLM_ENABLE`**
    (默认关=零行为变化)起 `GatewayManager` 每 tick `reconcile`,codex 经
    `127.0.0.1:<port>/v1` 命中。
  - 部署:`Dockerfile.agent-runner` 装 `litellm[proxy]==1.89.4`;
    `docker-compose.agent-runner.yaml` 加 `FEEDLING_LITELLM_ENABLE`/`_PORT`/`_API_KEY`
    (默认关;master key 走加密 env,不入 compose_hash)。
  - 测试:`tests/test_litellm_gateway.py`(配置/env/签名/Manager 生命周期)+
    supervisor `_gateway_entries`/`_wire_gateway_models` 用例。
  **剩余(Stage E 前 gate)**:真起一遍跑通 codex→LiteLLM→Gemini/OpenRouter 的**工具
  循环 eval**(本地 gemini 地理封锁未测成,放开前逐家过);`litellm[proxy]` 折进
  `requirements.lock`(哈希锁,Dockerfile 已留 TODO);压测 LiteLLM 子进程对 CVM 资源
  /enclave decrypt 的影响。
- **per-user flag → 启用开关**:`agent_runtime_driver` 退化成**是否启用 hosted**
  (legacy/off=关,非关值如 `auto`=开),**不再承载 agent 选择**。
  `POST /v1/model_api/driver` body 改为 `{"enabled": bool}`,响应回**派生**出的
  driver;`resolve_driver` = 启用时按 provider 派生否则 legacy。新建 setup 默认不
  启用(现网安全)。客户端入口(iOS 开关)待补。
- **用户发现**：**【已补 Stage C,2026-06-26 扩 codex 兜底 + 网关门控】**
  `db.list_agent_runtime_enabled_users(include_gateway=bool)`(扫 model_api blob:
  `test_status==ok` + hosting 启用),回
  `[{user_id, driver, provider, model, base_url}]`——**driver 默认 codex(只
  anthropic/deepseek→claude)** + 回带 provider/model/base_url 供 supervisor 在 spawn
  时给 codex 选 native(openai)/gateway(其余) + 建 LiteLLM 路由。
  **网关门控(Codex P1 修复)**:`include_gateway` 跟随 `FEEDLING_LITELLM_ENABLE`——
  **网关关时,gemini/openrouter/openai_compatible 这些「仅网关」provider 不进发现集**
  (否则会被以 gateway transport 拉起、打向不存在的代理而坏掉;关时它们保持 inert)。
  supervisor 另有 `_drop_gateway_users` 对**静态 roster** 同样兜底(网关关则丢弃 gateway
  codex 条目)。`_apply_discovery` 把 driver/provider/model/**base_url** 一并盖到条目
  (**Codex 第一轮 P2 修复**:openai_compatible 的自定义 `base_url` 现沿发现链传到
  `_gateway_entries`→LiteLLM `api_base`,不再丢)。**约束**:api_key 后端只存哈希,
  supervisor 仍靠 roster 提供凭据 → "无 roster" 自动发现要等 Stage D。
- **supervisor 长驻化(为可部署 + 实时控制面,2026-06-26)**:`main()` 凭据一次解析,
  `_effective_roster()` **每 tick 重算**(autodiscover 交集 + 网关 drop/wire),空
  roster **空转不退出**(不再 crashloop);`tick` 新增两条:用户掉出 roster 即
  kill+release、**配置变更(`_spawn_identity` 比对 driver/provider/model/key)即就地
  重启子进程**(Codex 第五轮 P2:否则切 provider/网关旧进程一直跑到自己死)。
  **脚本启动 import 顺序(Codex 第五轮 P1 修复)**:`supervisor.py` 作为脚本直跑时
  (`python backend/agent_runtime/supervisor.py`,镜像 cwd=/app),sys.path[0] 是脚本
  自身目录,故把 `sys.path.insert(backend)` **挪到 `import db`/`from core` 之前**,
  否则一启动就 `ModuleNotFoundError` crashloop。
- **cutover 路由判定同步门控(Codex 第二轮 P1 修复)**:hosted chat 走不走 runtime 的
  判定在**后端**(`cutover.resolve_driver`),与 supervisor 是两个容器。网关关时 supervisor
  不为 gemini/openrouter/openai_compatible 拉 consumer,但若 cutover 仍判 codex,
  `/v1/model_api/chat/send` 会把这轮交给 runtime → **无人服务、卡 `processing` 而非回退
  legacy inline**。修复:`resolve_driver` 对 `codex+gateway` provider 也加 `gateway_enabled()`
  门(读后端的 `FEEDLING_LITELLM_ENABLE`),关时回 legacy;native openai 不受影响。
  → **`FEEDLING_LITELLM_ENABLE` 必须同时设在 backend 与 agent-runner 两个服务**
  (prod compose backend env 已加 `${FEEDLING_LITELLM_ENABLE:-}` 引用,默认关;`/v1/model_api/driver`
  启用端点回的 driver 也随之:网关关时给 gemini 启用回 legacy,与实际行为一致)。
- **runtime-token 鉴权（#2，最硬）**：让 consumer 不再持用户长期 API key。
  **【切片 1–3 已补,端到端通(密钥设置后)】** 后端 `require_user` + enclave 转发
  + supervisor 铸发/刷新文件 + consumer 读取发 header,feature-flag 默认关零回归。
  **仍待**:按路由 scope 强制(切片 4)+ 把 `FEEDLING_API_KEY` 彻底移出 consumer env。
  详见下方 Stage D 切片。

## 分阶段路线

- **Stage A — 特性对等**：因 canonical = resident，多数对等项**已由 resident
  自带**（#1 图片、#5 wake 动作、#8 verify-ping、#9 输出清洗、屏幕上下文都在
  resident 里）。Stage A 实际工作是**让 resident 在托管下跑通 claude/codex**：
  - A0（先做）：在 agent-runner 里真起一个 resident 子进程（cli 模式 + 一个真实
    用户的 provider key），验证 **verify-ping/清洗/proactive 在托管路径下也工作**、
    onboarding 能绿灯。调好默认 `AGENT_CLI_CMD`（claude/codex）+ resident 的
    cli session 处理。
    - **契约层已验（2026-06-25）**：`spawners.consumer_env` 设的 env 名与
      `tools/chat_resident_consumer.py` 实读的一致；默认 `claude -p {message}`
      在托管 env 下渲染为 `claude --output-format json -p <msg>`，后续轮自动注入
      `--resume <sid>`（session 续接）；verify-ping 罐头 ack 等常量随 resident
      自带。回归测试见 `tests/test_agent_runtime_resident_contract.py`。
    - **已知缺口**：`_prepare_cli_command` 只对 hermes / claude-code 注入
      `--resume` 续接；**codex 默认 `codex exec --json {message}` 无 session 续接
      分支** → codex 驱动当前不续会话。A0 出口判据是 cli=claude,可接受,迁 codex
      前需补 codex 的 cli session 处理。
    - **剩余（需真基础设施)**：真起后端 + enclave + 真实 provider key 跑一遍
      verify_loop/onboarding 绿灯 + 聊天/图片/记忆/主动消息端到端。
  - A1：给托管的 cli agent 接 Feedling 工具。**更正(2026-06-25)**：
    `deploy/openclaw-plugins/feedling-io-tools` 是 **OpenClaw 专用插件,Claude
    Code / Codex 用不了**（见 `docs/AGENT_CLI_INTEGRATION_SURVEY.md`）。两条可选路:
    - **A1-lite(skill + Bash)【已选 + 已落地骨架,2026-06-25】**：
      - 指令文档 `backend/agent_runtime/agent_tools_prompt.md`(随 `COPY backend/`
        进镜像;兼作 claude 的 `--append-system-prompt-file` 和 codex 的 `AGENTS.md`)。
      - `spawners`:claude 默认命令改为 `claude --allowed-tools '<io_cli perception
        三个 verb 的 Bash 允许规则>' --append-system-prompt-file {home}/agent-tools-
        prompt.md -p {message}`(resident 仍注入 `--output-format json`/`--resume`);
        `ProcessSpawner.spawn` 起子进程前 `agent_home_files()` 把 prompt + claude
        `settings.json`(permissions.allow 同一授权,防御纵深)写进 per-user home。
      - io_cli 用**绝对路径**(dev=仓库根、镜像=/app 均解析正确);verb 集 =
        `perception` / `perception-trend` / `perception-history`。
      - 测试:`tests/test_agent_runtime_spawners.py`(授权/种子文件)+
        `tests/test_agent_runtime_resident_contract.py`(完整命令经 resident 加工后
        grant/prompt-file 保留 + json/resume 注入)。
      - **CLAUDE.md 泄漏注记**:claude 子进程继承 resident 的 cwd;镜像只 `COPY
        backend/ tools/`,根 `CLAUDE.md` 不在镜像内 → 生产无泄漏;仅 dev 从仓库根跑
        时会读到根 `CLAUDE.md`,故指令统一走 `--append-system-prompt-file` 不依赖 cwd。
      - **待 A0 实跑验证(无法离线证)**:`claude -p` 是否真按需调 io_cli、Bash
        允许规则前缀匹配是否命中、`--append-system-prompt-file` 行为;codex 的
        AGENTS.md/sandbox 审批路径未验(A1 先 claude)。非 typed 工具,可靠性中(survey#4)。
    - **A1-full(Feedling MCP server)**：写一个小的 stdio MCP server 包
      `io_cli.py perception`,在 per-user claude/codex 配置里注册(`claude mcp add`
      / `codex` config)。typed + 跨 runtime 复用;我们同时托管 claude+codex,正好
      命中 survey 建议#5 的"≥2 runtime → 建 MCP"阈值。
    - 核对 #2 memory capture / #3 web 搜索由 resident + 该工具层覆盖。
  - A2：#2/#4/#6/#7 核对【已审计 2026-06-25】：
    - **#2 memory capture — 消费端已覆盖**：resident 有 `execute_memory_actions`
      (POST `/v1/memory/actions`) + `execute_identity_actions`
      (POST `/v1/identity/actions`),从 agent 输出的 `{"actions":[...],"messages":
      [...]}` 信封按 `memory.`/`identity.` 前缀分发(`_dispatch_actions`)。开放点:
      托管 claude `--output-format json` 返回 text result,**不是** Feedling actions
      信封 → 需在 agent prompt 指示其输出 actions 信封(或给写类工具)。与 A1 重叠,
      A0 实跑时一并验。
      **解析链已确认支持(2026-06-25)**:`_agent_turn_from_obj` 对 `_JSON_REPLY_FIELDS`
      里的 `result` 字段,若值是字符串会跑 `_json_objects_from_cli_output` 再下钻 →
      **会自动把 claude `result` 里的内层 `{"actions":[...],"messages":[...]}` 解出并
      分发**。故 #2/#4 是 **prompt 问题(+ bootstrap 协议移植),非消费端代码阻塞**:
      只需在 `agent_tools_prompt.md` 教 claude 何时输出 actions 信封 + actions schema
      (`memory.add`/`memory.supersede`/`identity.*`),onboarding 的 bootstrap
      Pass1-3/Step5(`bootstrap_incomplete` 409 gate)也要一并移植进托管 prompt。
    - **#4 确认流程 — 缺口**:resident `_dispatch_actions` **直接落** identity/memory
      动作,**无 pending-confirm 门**(legacy 有"提议→用户确认再落")。实际写入走后端
      `/v1/identity|memory/actions`,服务端是否有策略门需另查;若需复现确认 UX,是新
      工作。**待定**:服务端门是否足够 vs 需补 hosted 确认流程。
    - **#6 action-trace / usage 观测 — 真实缺口**:resident **不**往后端写 trace,只把
      `usage/modelUsage` 从可见输出里剥掉。legacy 写 `model_api_action_traces`。
      需补等价埋点(turn latency / provider error / usage),也是删除前 gate 里"观测"
      项的前提。
    - **#7 provider 自测 — 已覆盖**:`/v1/model_api/test`(setup_routes.py:164,
      `_load_runtime_provider_config` 真打一次 provider)独立于 inline chat,保留即可。
  出口判据：一个真实 API-key 用户在托管 resident（cli=claude）下，
  **能过 onboarding 绿灯（verify_loop 满足）**，聊天/图片/记忆/主动消息表现
  ≥ legacy，用户可见消息无思考过程/身份泄漏。
- **Stage B — 信封自取 + flag setter【已实现 2026-06-25】**：
  - 后端两个新端点(`backend/hosted/setup_routes.py`):
    - `POST /v1/model_api/driver` — **【改 2026-06-25】启用/停用 hosted 的开关**,
      body `{"enabled": bool}`(不再让用户选 agent;agent 按 provider 派生),
      响应回派生 driver;未配 model_api 返回 404。
    - `GET /v1/model_api/key_envelope` — 返回调用者自己的 `api_key_envelope` **密文**
      (服务端解不开,只 enclave 能解 → 不泄 provider key)。
  - supervisor `_resolve_roster` 自取(`_fetch_key_envelope`):条目若无显式
    `provider_key`/`provider_key_envelope`,就用其 api_key 调上面端点拉信封再 JIT
    解密。优先级:显式 `provider_key`(dev) > roster `provider_key_envelope` > 自取。
    **→ roster 现在只需 api_key**(为 Stage C 自动发现铺路)。
  - 测试:`tests/test_hosted_agent_runtime_driver.py`(端点)+
    `tests/test_agent_runtime_supervisor.py`(自取/优先级)。
  - 待补:`agent_runtime_driver` 的客户端入口(iOS 设置开关)。
- **Stage C — 自动用户发现【已实现 2026-06-25】**：
  - **架构约束(已查实)**：api_key 在后端**只存 HMAC-SHA256 哈希**(`_hash_api_key`),
    明文不可恢复 → supervisor **拿不到** DB 里的用户 api_key。故"完全无 roster 密钥"
    的自动发现**必须等 Stage D(runtime-token)**;Stage C 先做"发现启用集"。
  - `db.list_agent_runtime_enabled_users()`:扫 `user_blobs` kind='model_api'、
    `test_status=='ok'`、`agent_runtime_driver ∈ {claude,codex}`,返回
    `[{user_id, driver}]`。supervisor 已为 lease 直连 DB,直接查,**不新增 HTTP/admin
    鉴权面**。
  - supervisor `_discover_enabled()` + `_apply_discovery(roster, enabled)`:把
    (带凭据的)roster **过滤到后端已启用集**,driver 取后端 flag。**→ Stage B 的
    `/v1/model_api/driver` 成为激活控制面:翻 flag 即激活/停用,不必改 roster**(灰度)。
  - 主循环 behind `AGENT_RUNTIME_AUTODISCOVER`(默认关,向后兼容);开则
    roster = roster ∩ enabled。凭据仍来自 roster(Stage D 去掉)。
  - 测试:`tests/test_agent_runtime_discovery.py`(DB 查询 + 纯合并)。
  - 待补(plan):按需 spawn / 空闲退出(当前仍是 roster 全量常驻)。
- **Stage D — runtime-token 鉴权（#2，最硬）**：让 consumer 不再持用户长期 API key。
  **信任模型**:agent-runner(supervisor)可信(同 TDX 域),持共享密钥
  `FEEDLING_RUNTIME_TOKEN_SECRET`,按用户铸短期 token;后端/enclave 用同密钥验。
  **红线**:密钥**绝不进 consumer**(否则可给任意用户铸 token)。
  - **切片 1【已实现 2026-06-25】后端 require_user 接受 token**:纯原语挪到
    `core/runtime_token.py`(stdlib-only,合 §1 分层;`agent_runtime/tokens.py` 改 shim);
    新 `accounts/runtime_auth.py` + `require_user()` 先验 `X-Feedling-Runtime-Token`
    (present+enabled+invalid → 失败闭合 401;无 token 或密钥未设 → 回落 API key,
    **零回归**,全量 789 passed / 失败数与基线一致)。测试
    `tests/test_runtime_token_auth.py`(6)。
  - **切片 2【已实现 2026-06-25】enclave 认 token**:`enclave_app.py` 把 caller 的
    `X-Feedling-Runtime-Token` 转发给后端 whoami(切片1 已认)解析身份。
    2a `/v1/envelope/decrypt`(provider-key + 信封解密,supervisor 路径);
    2b decrypt-and-serve 路由(`/v1/chat/history` 等)经 `_whoami_cached` 也认
    token(从 request 读、按凭据缓存,**7 个路由零改动**)。api-key 路径完全不变。
    测试 `tests/test_enclave_runtime_token.py`(5)。
  - **切片 3【已实现 2026-06-25】supervisor 铸发 + 刷新 + consumer 读取**:
    - supervisor:密钥设则 `token_writer` 每用户 `runtime_token.mint`(scope=
      chat/memory/identity/perception/envelope_decrypt,TTL 默认 900s),spawn 后 +
      每次 renew 写 `{home}/runtime-token`(0600,刷新);密钥未设则 None=零行为变化。
    - consumer(`tools/chat_resident_consumer.py`):`FEEDLING_RUNTIME_TOKEN_FILE`
      存在且有 token → 每轮 poll 原地把 `_HEADERS` 切到 `X-Feedling-Runtime-Token`
      (去掉 `X-API-Key`);文件不存在(自托管 VPS)→ 保持 api key,**零影响**。
    - 测试:`tests/test_agent_runtime_spawners.py`(token 文件)+
      `tests/test_agent_runtime_supervisor.py`(spawn/renew 刷新)+
      `tests/test_agent_runtime_resident_contract.py`(consumer 切 header)。
    - **端到端**:密钥设置后 supervisor→文件→consumer→token→后端&enclave 验,全通。
  - **切片 4【已实现 2026-06-25】按路由 scope 强制**:`accounts/runtime_auth.authorize_scope(scope)`
    ——token 认证时校 `scope`(`core.runtime_token.authorize`),不含则 403;api_key
    认证=全权(no-op);feature 关 no-op。已接 `/v1/memory/actions`(scope `memory`)
    + `/v1/identity/actions`(scope `identity`)。测试 `tests/test_runtime_token_auth.py`。
    待推广:chat/perception 等其余 consumer 路由 + 把铸的 token scope 收窄(目前铸全
    scope,故强制的安全收益要等"按需窄 scope"才完全兑现)。
  - **两个 bug【已修 2026-06-25】**:
    - enclave `_whoami_cache` 随 token 轮换无界增长 → `_prune_whoami_cache` 写时淘汰
      过期项(`tests/test_enclave_runtime_token.py`)。
    - consumer 过期 token 楔住 → `_refresh_auth_header` 解 token `exp`,无新鲜 token
      回退 api_key(`tests/test_agent_runtime_resident_contract.py`)。
  - **残留(待做)**:
    - **backend→enclave 转发 token**:`core.enclave._decrypt_envelope_via_enclave` 仍只
      转 X-API-Key;token 认证(api_key=None)下,memory patch/supersede 等需解旧密文的
      写操作会断。需让它在请求带 token 时转发 token(enclave 切片 2 已认)。**这是
      memory/identity 写路径在 token 下真正可用的前置**。
    - consumer 仍在 env 持 `FEEDLING_API_KEY`(import 必需 + 本地 session 路径哈希)。
      token 化后请求不再发它,但进程内仍在;彻底"不持长期 key"需重构 consumer 不再要求
      该 env(session 路径改用 user_id/CONSUMER_ID)。
  （可与 Stage C 并行，但工作量最大。）
- **Stage E — 部署 + 灰度迁移 + soak**：agent-runner 上 test→prod；把 API-key
  用户分批 flip 到 claude/codex；观测 N 天无回退 legacy。
- **Stage F — 删除 legacy**：见下「删除清单」+「删除前 gate」。

> 顺序建议：A 是硬阻塞，先做。B/C 让运维可规模化。D 是安全收尾（也可放 F 前）。
> E 是把人迁过去。F 最后。

## 删除清单（Stage F，精确）

**删文件**：`hosted/turn.py`、`hosted/context.py`、`hosted/wake_consumer.py`、
`model_api_runtime/`（待 Stage A 把 setup/config/history/onboarding 对它的依赖
摘净后）。

**改文件**：
- `hosted/chat_routes.py`：`model_api_chat_send` 只保留「投递用户消息 +
  `agent_runtime_cutover.handle_send`」，删掉 provider 调用 / tool loop / 解析 /
  pending / trace 那一大段；删对应 import。
- `hosted/config_store.py`：删 `_load_runtime_provider_config` 等 inline 专用项；
  保留 config 存储 + flag + profile。
- `app.py`：解上面列的接线（imports / wake_bus handler / tick / COMPAT 段）。

**保留**：`setup_routes.py`、`config_store.py`(瘦身后)、`history_import.py`、
`onboarding_validation.py`、`agent_runtime_cutover.py`。

**路由集变更**（CONTRIBUTING §7 要求 PR 显式列出，url_map 是回归基线）：
- 改：`POST /v1/model_api/chat/send`（语义变投递+202，不再同步 reply）。
- 不变：`/v1/model_api/setup|get|test|delete|runtime|memory/repair`。
- 新增（Stage B，已上）：`POST /v1/model_api/driver`（hosted 启用/停用开关,body
  `{"enabled": bool}`；agent 按 provider 派生）、`GET /v1/model_api/key_envelope`
  （返回自己的 provider-key 信封密文）。
- 可能删：legacy proactive wake 相关的内部端点（若有）。

## 删除前 gate（全绿才执行 Stage F）

```
[ ] Stage A 特性对等已上线并验证（verify-ping/清洗/图片/记忆/web/确认/wake/trace）
[ ] verify_loop 探针在 agent-runner 路径下满足，onboarding 能绿灯（A0 出口）
[ ] 抽样核对：被迁用户的可见消息无思考过程/身份/系统提示泄漏（#9）
[ ] 100% API-key 用户 agent_runtime_driver != legacy，且各自有运行中的 consumer
[ ] 线上 soak ≥ N 天，期间无回退 legacy、无 provider/解密失败激增
[ ] enclave decrypt 并发复核——每用户一个常驻 consumer 对 enclave decrypt QPS
    的影响评估过。⚠️ 注意：enclave **不是单线程**（gunicorn gthread，
    `FEEDLING_ENCLAVE_WORKERS` compose 默认 2 × 每 worker 32 线程），reentrant
    whoami 也已优化大半（`_local_user_id_from_token` 本地 HMAC 跳过回调 backend）。
    2026-07-02 调查结论：decrypt 丢周期是 **backend 线程饱和 + 内存墙**的下游，
    不是 enclave 并发问题——见 2026-07-02 longpoll 并发调查稿（已删，见 git 历史）
[ ] url_map diff 已 review；客户端（iOS）已适配 chat/send 的 202 异步契约
[ ] 全量 pytest 零新增失败；pyflakes 干净；`python -c "import app"` 通过
[ ] 删除在 PR 描述里列清路由集 + compose/加密路径变更
```

## 风险 / 开放问题

- **enclave 扩展性**：每用户一个常驻 consumer，会放大 backend→enclave decrypt
  调用，规模化前要压测。⚠️ 但「单线程 enclave 是瓶颈」的旧说法已过时：enclave 跑
  gthread（compose 默认 2 worker × 32 线程），reentrant whoami 已本地 HMAC 优化。
  2026-07-02 复核认定真正的墙是 **backend 线程饱和 + 内存**（4 worker≈2.4GB，纵向
  扩容被内存否决），enclave decrypt 丢周期只是 backend 饱和的下游症状。详见
  2026-07-02 longpoll 并发调查稿（已删，见 git 历史）。
- **provider 成本 / 延迟**：真实 agent loop 比一次 LLM call 贵；需成本观测。
- **契约破坏**：`/v1/model_api/chat/send` 由「200+reply」变「202 异步」，被迁的
  用户其客户端必须支持「走 poll 取密文回复」。迁移名单要和客户端版本对齐。
- **model_api_runtime 删除耦合**：setup/config/history/onboarding 仍 import 它
  （多为 contract message / profile）；删包前必须先解耦，否则保留文件会断。
- **runtime-token 鉴权范围**：#2 要改 chat/memory/identity 多个路由 + enclave，
  是整个迁移里最大的单块，建议独立设计评审。

## 非目标

- 不在本路线图内做 per-user 强隔离（容器/microVM）——见
  `docs/AGENT_RUNTIME_ISOLATION.md`，可选、非默认。
- 不改 resident/VPS 线的外部行为；两线共用后端工具不变。
