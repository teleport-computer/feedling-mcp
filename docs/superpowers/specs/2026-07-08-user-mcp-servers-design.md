# 用户 MCP 服务器（user_mcp）设计 v2 —— 配置分发模型

> **RETIRED / DO NOT DEPLOY.** Historical design; host-all token-file and
> supervisor materialization paths no longer exist.

日期：2026-07-08
状态：v2 已与用户确认方向（v1 的「后端代理」模型已废弃），待实施计划
目标分支：**test**（pre 暂不稳定；test 无 pi driver，托管 driver 仅 claude/codex）
> ⚠️ **2026-07-17 订正**：本行的「test 无 pi driver」在 2026-07-13 已过期（pi driver
> 于 `1e01ef7e` 合流 test）。pi 现承载 gemini / openrouter / openai_compatible。
> 本行仅作历史记录，**不得据此推论 pi 路线状态**。§11 的 pi MCP extension 已由
> `2026-07-17-pi-user-mcp-bridge-design.md` 实施。

## 0. 一句话

用户在 iOS 设置页添加「远程 HTTP MCP server + 自定义请求头」，配置加密落库后
经 **consumer 的 poll 下发通道**分发：托管 agent 由 consumer 物化成 claude/codex
的**原生 MCP 配置**（保证可用）；自跑（VPS）agent 收到标准化配置文件自行加载
（只保证送达）。后端不做 MCP 代理层。

命名统一用 `user_mcp`，避免与仓库历史遗留的 "MCP"（enclave 端口旧称）混淆。

## 1. 范围与已定决策

| 决策点 | 结论 |
|---|---|
| 覆盖用户 | 托管（API）用户：**保证** agent 能用 MCP；自跑（VPS）用户：**保证配置送达**，生效 best-effort |
| 传输类型 | 只支持远程 streamable HTTP MCP server；不支持 stdio（不执行任意用户命令） |
| 密钥存储 | url + headers 整体走 X25519 shared 信封（沿用 BYOK key 模式），服务器不留明文 |
| 生效范围 | 仅聊天回合（proactive/后台回合不注入，避免静默消耗用户第三方 API 额度） |
| 架构 | **配置分发**：consumer 经 poll 感知变更 → 拉信封 → enclave 解密 → 物化原生配置；后端无 MCP 代理 |
| 落地范围 | 本仓（后端 + consumer/spawners）+ iOS 交互契约 + io-onboarding 文档更新 |
| pi | ~~test 分支无 pi driver，本期不涉及~~ **（2026-07-13 起过期）**；pi MCP extension 见 `2026-07-17-pi-user-mcp-bridge-design.md` |

### 1.1 为什么是配置分发而不是后端代理（v1→v2 的变化）

v1 方案是后端当统一 MCP client、agent 经 io_cli 动词调用。用户明确要求
「不需要加 MCP 中间层，只要 VPS 用户能配置、托管 agent 能用」。且关键事实变化：

- 自跑用户和托管用户跑的是**同一份** `tools/chat_resident_consumer.py`
  （hosted 判定：`_HOSTED = bool(FEEDLING_RUNTIME_TOKEN_FILE)`，consumer:579），
  都长轮询 `GET /v1/chat/poll`。
- poll 响应已有现成的配置下发通道：`poll_context`（`backend/chat/poll_core.py:22-34`）
  每次都带 `runtime_v2`（flag 下发）和 `client_release.expected_consumer_commit`
  （自更新指令）。user_mcp 照此模式加一个字段即可，一套机制覆盖两类用户。
- test 分支托管 driver 只有 claude/codex，两者都原生支持 HTTP MCP + 自定义头：
  - claude：`.mcp.json` / `--mcp-config`，HTTP 条目 `{"type":"http","url":...,"headers":{...}}`。
  - codex 0.142：`config.toml` `[mcp_servers.<name>]`：`url` / `http_headers` /
    `env_http_headers` / `bearer_token_env_var`（经二进制字符串实测确认）。

原生注入工具是原生广告给模型的，体验优于 prompt 告知的 io_cli 桥。

## 2. 架构与数据流

### 2.1 配置写入（iOS → 后端）

iOS 设置页把 `{name, url, headers}` 经 TLS POST 给后端（与 `/v1/model_api/setup`
同模式），后端用 `core/envelope.py` 的 `_build_shared_envelope_for_store` 路径
（用户内容公钥 + enclave 内容公钥）把 **url+headers 整体**加密成信封落 DB blob。
明文只留 `name`、`enabled`、host hint、header 名（不含值）供列表展示。
write-only：iOS 不能读回明文，编辑即整体重传。

### 2.2 变更感知与下发（后端 → consumer）

```
iOS 保存/删除/开关
  └─ 后端更新 user_mcp blob，重算 fingerprint（配置内容哈希）
       └─ poll_context 增加 "user_mcp": {"fingerprint": "<hash>"}（学 client_release 模式）
            └─ consumer 每次 poll 比对本地已应用指纹，不一致时：
                 ├─ GET /v1/mcp/envelopes  （拉全量：name/enabled/envelope/元数据）
                 ├─ 经 FEEDLING_ENCLAVE_URL 解密各信封（purpose=mcp_server_config）
                 │    认证：hosted 用 runtime-token，自跑用 api_key——两路第一天都要通
                 └─ 物化本地 agent 配置（§2.3），记录已应用指纹
```

- 密钥明文不进 poll 响应、不进聊天记录，只在 consumer 进程内存与其管理的
  agent home 配置文件中。
- 用户消息入库时已有 `notify_chat_waiters` 唤醒机制，配置保存也调同一唤醒
  （复用 wake_bus），让空闲 park 中的 poll 立即带新指纹返回，变更近实时生效。
- 删除所有 server 时 fingerprint 变化同样触发物化（物化为空 = 清掉本地配置文件）。

### 2.3 物化（consumer 内，按 driver/runtime 分支）

**托管 claude driver / 自跑 Claude Code：**

- consumer 写 `{home}/claude-home/user-mcp.json`：
  ```json
  {"mcpServers": {"jira": {"type": "http", "url": "https://...", "headers": {"Authorization": "Bearer ..."}}}}
  ```
- **聊天回合**的 CLI 命令追加 `--mcp-config {home}/claude-home/user-mcp.json`；
  非聊天回合不加该 flag（门控即天然完成）。
- allow-rules 追加 `mcp__<name>__*`（每个 enabled server 一条），同时进
  `--allowed-tools` 与 `settings.json` 的 `permissions.allow`（沿用双保险惯例，
  spawners.py:476-482）。

**托管 codex driver：**

- consumer/spawners 写 `{home}/codex-home/config.toml`：
  ```toml
  [mcp_servers.jira]
  url = "https://..."
  http_headers = { "Authorization" = "Bearer ..." }
  startup_timeout_sec = 20
  ```
- ⚠️ `spawners.py:498` 的 `stale_home_files` 现在会主动 prune
  `codex-home/config.toml`（LiteLLM 退役遗留），必须把该文件从 prune 列表移出，
  改为由物化逻辑全权管理（无 MCP 配置时写空/删除）。
- **门控**：config.toml 是静态的。非聊天回合计划以 `-c` 覆盖清空 `mcp_servers`
  （如 `-c mcp_servers={}`）——**实现时必须实测 codex 0.142 对该覆盖的解析**；
  若不可行，记录为已知限制（codex 后台回合能看到 MCP 工具），并在
  agent prompt 中声明后台回合勿调用 MCP 工具（软门控兜底）。

**自跑其他 runtime（Hermes / OpenClaw / 任意 CLI）：**

- consumer 物化标准格式 `<consumer-state-dir>/user-mcp.json`
  （结构同 claude 的 mcpServers 段，通用性最好）。
- `io-onboarding/skill-resident-agent.md` 新增一节：告知 agent 该文件的位置与
  含义，「若你的 runtime 支持 MCP，请加载它；配置变更时文件会被更新」。
- **best-effort**：只保证文件送达与更新，不保证 agent 真的加载生效。

### 2.4 能力告知（prompt）

用户有 ≥1 个 enabled server 时，聊天回合的 system prompt
（`agent-tools-prompt.md` 或 consumer 注入段）追加一句：可用的外部 MCP 工具
来自用户自己的配置、仅聊天回合可用、调用失败时如实告知用户。
无配置时不加，避免噪音。

## 3. 数据模型与加密

DB blob，kind=`user_mcp`，每用户一个（`db.get_blob/set_blob`，load/save 封装
仿 `backend/hosted/config_store.py`）：

```json
{
  "fingerprint": "sha256:...",
  "servers": [
    {
      "id": "srv_a1b2c3",
      "name": "jira",
      "enabled": true,
      "config_envelope": { "...X25519 shared envelope..." },
      "url_hint": "mcp.example.com",
      "header_names": ["Authorization"],
      "created_at": "2026-07-08T00:00:00Z",
      "updated_at": "2026-07-08T00:00:00Z"
    }
  ]
}
```

- `name`：用户起的 slug（`[a-z0-9_-]{1,32}`），每用户唯一，也是原生配置里的
  server 名（claude 工具名前缀 `mcp__<name>__`、codex `[mcp_servers.<name>]`）。
- `config_envelope` 密文内容：`{"url": "https://...", "headers": {"Authorization": "Bearer ..."}}`。
- `fingerprint`：对 servers 的稳定序列化（含信封密文与 enabled 位）取 sha256，
  保存时重算；poll 只下发这一个短字段。
- 信封 purpose：`mcp_server_config`，加入 enclave 解密端点 purpose 白名单；
  解密请求支持 runtime-token 与 api_key 两种认证（别复刻 memory 读侧只认
  api_key 的坑）。

### 3.1 限额

- 每用户最多 **10** 个 server。
- 每 server headers 最多 20 个、总大小 ≤ 8KB。
- URL 强制 `https://`，明文 http 拒绝（400，说明原因）。
- `name` 保留字校验：不得与内建工具/既有 io_cli 动词冲突的前缀（实施时定黑名单）。

## 4. HTTP API

新模块：`backend/hosted/mcp_routes_asgi.py` + `mcp_core.py`（+ `mcp_probe.py`
做 /test 的轻量探测）。按 CONTRIBUTING：APIRouter + `register_asgi(app)`，注册进
`asgi_app._ASGI_PACKAGES`；路由体委托 core，阻塞 DB 走 `await threadpool.run_db(...)`。

### 4.1 管理端点（iOS 用，api_key 认证）

| 端点 | 语义 |
|---|---|
| `GET /v1/mcp/servers` | 列表：id/name/url_hint/header_names/enabled/时间戳；永不回明文 |
| `POST /v1/mcp/servers` | 新建或整体覆盖（同 name 即覆盖）：`{name, url, headers, enabled?}`；校验限额/https/私网后建信封落库、更新 fingerprint、唤醒 poll |
| `PATCH /v1/mcp/servers/{name}` | 只改 `enabled`，不动信封；更新 fingerprint、唤醒 poll |
| `DELETE /v1/mcp/servers/{name}` | 删除；更新 fingerprint、唤醒 poll |
| `POST /v1/mcp/servers/{name}/test` | 连通性测试（§6）：返回 `{ok, tool_count, tool_names}` 或结构化错误 |

更新语义：编辑必须重传完整 url+headers（信封整体重建）；只开/关用 PATCH。
不做「部分更新密文」。

### 4.2 consumer 端点（api_key / runtime-token 双认证）

| 端点 | 语义 |
|---|---|
| `GET /v1/mcp/envelopes` | 全量下发：`{fingerprint, servers: [{name, enabled, config_envelope}]}`；consumer 凭此物化 |

（v1 的 `/v1/mcp/tools`、`/v1/mcp/call` 代理端点与 io_cli mcp-* 动词全部取消。）

### 4.3 poll 契约变更

`backend/chat/poll_core.py` 的 `poll_context` 增加：

```json
"user_mcp": {"fingerprint": "sha256:..."}
```

无配置的用户下发空指纹（consumer 据此清理本地物化文件）。

## 5. consumer / spawners 变更

`tools/chat_resident_consumer.py`：

- poll 处理处比对 `user_mcp.fingerprint` 与本地已应用值，变化时执行
  「拉信封 → enclave 解密 → 物化 → 记录指纹」流程（失败重试下次 poll 再试，
  不阻塞消息处理；解密/物化错误落 user_logs 便于排查）。
- 聊天回合构建 CLI 命令时按 driver 追加 `--mcp-config`（claude）；
  非聊天回合不追加（codex 的 `-c` 清空覆盖见 §2.3）。

`backend/agent_runtime/spawners.py`：

- `codex-home/config.toml` 移出 `stale_home_files` prune 列表（§2.3）。
- claude allow-rules 生成逻辑支持追加 `mcp__<name>__*`（数据来自物化后的配置）。

**托管侧物化归属说明**：托管 consumer 由 supervisor 拉起、home 文件由 spawners
生成，但 MCP 配置的**运行时变更**走 consumer 的 poll 物化路径（与自跑一致），
spawners 只负责 allow-rules/prune 的静态部分。边界细节在实施计划中定。

## 6. /test 连通性探测（唯一的后端出站调用）

- 不引 `mcp` SDK。手写单次 JSON-RPC over streamable HTTP：
  POST `initialize` → POST `tools/list`，带用户 headers，httpx async，
  connect 10s / 总计 30s 超时。约几十行。
- **SSRF 防护**（此端点后端会向用户给的任意 URL 发请求）：
  - 拒绝：私网段（10/8、172.16/12、192.168/16）、loopback、link-local
    （169.254/16 含云 metadata、fe80::/10）、其他非全局地址——
    `ipaddress.is_global` 一票制，对解析出的每个 IP 校验。
  - 禁止跨 host 重定向跟随。
  - POST 创建时也提前做同样的 URL 校验（用户即时看到友好报错）。
- 错误结构化：`{"error": {"kind": "<dns|timeout|tls|http_401|http_5xx|protocol|blocked_url>", "detail": "..."}}`。

（agent 实际调用 MCP 的出站请求由 claude/codex CLI 从 agent 环境直接发起，
不经后端；CVM 上 agent 本就可任意出网，不构成新增攻击面。）

## 7. 安全

- https-only（§3.1）。
- headers 键名黑名单：不允许 `Host`；`Content-Type`/`Accept`/MCP 会话头由
  client 控制，冲突时以 client 为准。
- 明文密钥生命周期：consumer 进程内存 + agent home 配置文件
  （`user-mcp.json` / `config.toml`，权限 0600，per-user home 隔离内）。
  这是相对 v1 的有意让步：原生注入必然要求配置文件落 agent home 明文，
  信任边界与 `models.json`/`CODEX_API_KEY` 既有惯例一致。
- 后端日志不落 header 值（只落 header 名）。

## 8. 错误处理

- 管理端点与 /test：结构化错误（§6），iOS 直接展示。
- consumer 物化失败（信封解密失败/写文件失败）：不阻塞聊天，错误落 user_logs
  （对齐 proactive_jobs.status_reason 的可排查性），下次 poll 重试。
- agent 调 MCP 失败：CLI 原生处理（工具报错回给模型），prompt 要求 agent
  如实向用户转述，不编造。

## 9. iOS 交互契约（独立仓库实施）

- 设置页新增「MCP 服务器」入口 → 列表页：每行 name + host hint + enabled 开关
  （PATCH），左滑删除。
- 添加/编辑页：名称、URL、headers 键值对编辑器（值输入密文样式）。
  编辑已有条目 header 值显示掩码占位；任何修改要求重填完整头值（write-only）。
- 保存后自动调 `/test`，就地展示「✓ 已连接，发现 N 个工具」或具体错误文案。
- 固定提示：「工具仅在你主动聊天时可被 AI 使用，后台不会调用」；
  保存后正常在下一次对话即生效（poll 下发近实时）。
- UI 细节遵循该仓 DESIGN.md tokens。

## 10. 测试策略

- **单测（make_client 套路）**：
  - mcp_core：CRUD、限额、https/SSRF 预校验、同名覆盖、PATCH 只动 enabled、
    fingerprint 重算、poll_context 带 user_mcp 字段。
  - spawners：config.toml 不再被 prune、claude allow-rules 含 `mcp__*`
    （照抄 `tests/test_agent_runtime_spawners.py` 纯函数断言模式）。
  - consumer 物化：给定信封解密结果 → 断言写出的 user-mcp.json / config.toml
    内容与文件权限；指纹不变时不重物化；空配置清理文件。
  - 门控：聊天回合命令含 `--mcp-config`、非聊天回合不含。
- **/test 探测集成测试**：tests 内进程 fake MCP server（ASGI 小应用，
  initialize/tools-list），覆盖 headers 透传、超时、SSRF 拒绝、错误分类。
- **信封往返**：mock enclave，断言 purpose=`mcp_server_config` 且
  runtime-token 与 api_key 两种认证都通。
- **手工 E2E**（test 环境）：iOS/curl 建配置 + 真实公共 MCP server，
  claude、codex 两 driver 各聊一轮验证工具可用；proactive 回合验证门控；
  自跑 consumer 验证 user-mcp.json 物化与变更更新。

## 11. 后续项（本期不做）

- **pi MCP extension**：pi 官方不支持 MCP（README 明示走 extension 路径），
  但有 `pi.registerTool()` API（docs/extensions.md）。等 pi driver 随 pre
  合流后，写一个 js extension 读取物化的 user-mcp.json、把每个 MCP 工具注册成
  pi 工具。数据模型与下发链路无需改动。
- codex 非聊天回合硬门控（若 `-c mcp_servers={}` 实测不可行）。
- OAuth 流程、MCP resources/prompts/sampling（只做 tools）、stdio server。

## 12. 配套文档变更

- `io-onboarding/skill-resident-agent.md`：新增 user-mcp.json 说明一节（§2.3）。
- 本仓 `docs/CHANGELOG.md`：落地后按惯例记 landmark。
