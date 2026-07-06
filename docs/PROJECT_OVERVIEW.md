# Feedling 项目总览（功能 · 架构 · 信任链）

> 面向第一次接触本项目的工程师 / 审计者 / 合作者的导读文档。
> 撰写日期 2026-06-10。部署相关的具体数值（镜像 tag、compose_hash、CVM ID）
> 会随发布变化，以 `deploy/DEPLOYMENTS.md` 为准。

---

## 1. 这是什么项目

**一句话：Agent 是大脑，Feedling 是身体。**

Feedling 让用户的 Personal Agent（本体 AI）在 iOS 上拥有"身体"——
Dynamic Island、Live Activity、聊天、身份卡（Identity Card）、记忆花园
（Memory Garden）、屏幕感知。所有用户内容在服务端**以密文存储**，只能在
Intel TDX enclave 内解密；运行的代码镜像由以太坊链上合约授权，并可由
iOS 端实时验证（attestation 审计卡）。

核心价值主张：

- **隐私**：聊天、记忆、身份卡、屏幕帧等所有内容写入磁盘前都封装成
  v1 加密信封，后端、运维、磁盘备份看到的只有密文（明文写入直接返回
  `400 plaintext_write_rejected`）。
- **可验证**：跑的是什么代码不靠口头承诺——TDX DCAP attestation +
  链上 compose_hash 白名单 + iOS 证书 pin，三件套闭环。
- **开放接入**：用户可以自带 Agent（自建服务器）、自带模型 API key
  （托管运行时），或从官方 App 导入历史。

## 2. 仓库分布

本项目横跨三个仓库：

| 仓库 | 内容 |
|------|------|
| **feedling-mcp**（本仓库） | FastAPI/ASGI 后端、enclave 应用、部署编排、链上合约、审计工具、测试（FastMCP 服务器已于 2026-06-12 移除；Flask→ASGI 迁移 2026-07-04 完成） |
| [feedling-mcp-ios](https://github.com/teleport-computer/feedling-mcp-ios) | iOS 客户端：Chat / Identity / Garden / Settings、Live Activity / Dynamic Island、屏幕采集 Broadcast Extension、实时审计卡 |
| [io-onboarding](https://github.com/teleport-computer/io-onboarding) | 公开 onboarding 文档：`skill.md`（agent 行为规范；原供 MCP 客户端拉取，MCP 线下线后待改写）、`quickstart.md`、`troubleshooting.md`。独立成库是为了**热更新**——改文档不需要重建 iOS 或 CVM 镜像 |

本仓库目录结构：

```
feedling-mcp/
├── backend/        ← ASGI(5001) + enclave_app(5003) + 数据层
├── deploy/         ← 本地/自托管 compose + 生产 CVM compose + DEPLOYMENTS.md
├── contracts/      ← FeedlingAppAuth（Solidity，Sepolia）
├── tools/          ← audit_live_cvm.py · DCAP 解析器 · 常驻消费者 · 恢复工具
├── tests/          ← 多租户隔离 / 缓存等 pytest 套件
├── docs/           ← 本文档 · CHANGELOG · AUDIT · DESIGN_E2E · 各专题设计
├── DESIGN.md       ← 视觉/UI 设计 token（iOS 侧遵循）
└── CLAUDE.md       ← repo 约定
```

## 3. 产品功能

### 3.1 用户可见的"身体"（iOS）

- **Chat**：与本体 AI 的持续对话，长轮询实时送达。
- **Identity Card**：AI 的自我描述卡片，由 agent 初始化/编辑，
  用户在 App 内可见；带验证（verify）流程防静默篡改。
- **Memory Garden**：持久记忆库。每条 memory 有可见性
  （`shared` 给 agent 可读 / `local_only` 只有手机能解）。
- **Dynamic Island / Live Activity**：agent 的"存在感"通道——推送状态、
  主动消息、锁屏卡片。
- **屏幕感知**：Broadcast Extension 把屏幕帧加密上传，agent 经
  enclave 解密后"看到"用户在做什么。
- **扩展感知（Extended Perception）**：位置标签（粗粒度 geofence，不传
  坐标）、运动状态、日历下一事件、正在播放、电量等 8 类信号，
  默认全关、逐项授权；照片走专用加密管线，V2 不做敏感场景硬拦截
  （详见 `docs/EXTENDED_PERCEPTION_API.md`）。
- **审计卡**（Settings → Privacy）：iOS 内置的实时信任验证器，逐行检查
  attestation / 链上授权 / 证书 pin（见 §8）。

### 3.2 三条接入路由（onboarding routes）

| 路由 | 谁是大脑 | 形态 |
|------|---------|------|
| **Resident Consumer（自建服务器）** | 用户自己的 agent 运行时（VPS / Hermes / Claude Code 等） | 跑 `tools/chat_resident_consumer.py` 长轮询聊天、调用 agent、回写加密回复 |
| **Model API（托管运行时）** | 用户提供的模型 API key（OpenAI / Anthropic / Gemini 等） | 后端托管运行时：加密保存 key、导入聊天历史提取记忆/身份、代理聊天调用（`docs/MODEL_API_PATH_P0.md`） |
| **官方 App 导入** | — | 历史数据迁移入口 |

> **已移除（2026-06-12）**：MCP 直连（Claude.ai / Claude Desktop 经
> `mcp.feedling.app`）这条 agent 协议层已整体下线——`backend/mcpsrv/`、
> `mcp_server.py`、mcp 容器与域名均已删除，见 CHANGELOG 2026-06-12。
> agent 操作身体改走 HTTP API（resident 线）或由后端托管（model_api 线）。

### 3.3 主动唤醒 / 感知（Round 3）

Round 3 的主方案是 `docs/PROACTIVE_PERCEPTION_SPEC_V2.md`。迁移策略是
strangler fig：V2 spine 是新主干，旧 proactive 系统只允许作为输入
adapter 或输出 compatibility layer 存在，执行约束见
`docs/PROACTIVE_PERCEPTION_RUNTIME_V2_MIGRATION.md`。

核心原则：

- Chat 与 proactive 同构：`user_message`、`heartbeat`、`perception_event`、
  `scene_change`、`scheduled_wake` 和内部 `background_result` 都进入同一个
  wake inbox。
- 每用户同一时刻最多一个 foreground turn；近同时 wake 先合并，再由
  `TurnRunnerV2` 执行。后台慢路径不直接写 chat，必须作为
  `background_result` 回灌 inbox。
- 感知信号先过 `PerceptionDifferV2`。连续信号只能 pull；离散事件才可能 wake。
- 新控制面是 Ambient / Scheduled / Delivery 三层 gate。旧
  `enabled/dnd/user_state/ai_state` 只属于过渡兼容语义，不能继续扩展新 runtime。
- Hosted 与 resident 必须共用同一份 tool catalog、wake contract 和 differ
  语义；差异只在 agent 执行位置。

当前生产代码仍保留 `/v1/proactive/jobs/*` 和旧 dashboard 作为过渡通道。
新入口应先用 per-user flag 切到 V2，并保留旧执行体作为短期 dormant
fallback；观察窗口健康后再逐条删除旧执行体，而不是长期并存。

## 4. 系统架构

### 4.1 服务拓扑（生产 CVM 内四个容器）

```
            iOS App                Claude.ai / Claude Desktop
               │                            │
               │ HTTPS (LE TLS)             │ SSE ?key=<api_key>
               ▼                            ▼
        ┌─────────────────────────────────────────┐
        │  dstack-ingress（CVM 内终止 TLS）        │
        │  api.feedling.app                       │
        └──────┬──────────────────────────────────┘
               │
               ▼
   ┌────────────────────┐
   │ backend (ASGI)     │
   │ :5001 + :9998(WS)  │
   │ 88 条 /v1/* 路由    │
   │ 唯一数据出入口      │
   └──────┬──────────┬──┘
          │          │ 解密请求（可选）
          ▼          ▼
   ┌────────────┐ ┌──────────────────────────┐
   │ PostgreSQL │ │ enclave (enclave_app.py)  │
   │ (外部，存   │ │ :5003 自有 TLS（KMS 派生） │
   │  密文信封)  │ │ /attestation + 解密代理    │
   └────────────┘ └──────────────────────────┘
                    ▲ iOS 经 dstack-gateway "-5003s." 直连做证书 pin
```

- **backend**（`backend/` 领域包 + `asgi_app.py` 装配层，~132 条路由；
  FastAPI，2026-07-04 完成 Flask→ASGI 迁移，见 CHANGELOG）：iOS API、
  resident-consumer API、proactive、model_api、admin。gunicorn +
  UvicornWorker，默认单 worker（可经 `FEEDLING_BACKEND_WORKERS` 多
  worker，leader 选举管 WS/后台服务）。
- **enclave**（`backend/enclave_app.py`，8 条路由）：持有内容私钥的唯一
  进程。职责：从 dstack-KMS 派生密钥、出 `/attestation`（TDX quote）、
  做解密代理（chat 历史 / memory / identity / 屏幕帧的明文只在这里产生）。
  它不直接碰数据库，密文一律从 backend 取。
- **ingress**（dstack-ingress）：CVM 内终止公网 TLS（Let's Encrypt，
  Cloudflare DNS-01），路由 api 域名到 backend。

两个 Python 服务之间是普通内部 HTTP（backend→enclave 走其自签 TLS）。

### 4.2 数据层

- **PostgreSQL（外部托管，sslmode=require）** 是唯一持久真相，Alembic
  管理 schema（`backend/alembic/`，表结构详见 §7）。主要表：`users`、`user_blobs`（按
  kind 的 per-user 文档）、`chat_messages`、`memory_moments`、
  `frame_envelopes`（屏幕帧大信封）、`user_logs`（proactive jobs /
  decisions 等流式日志）、`server_config`（pepper 等）。所有用户内容列
  存的是 v1 信封 JSONB——库管理员只见密文。
- **UserStore 写穿缓存**（`backend/core/store.py`）：per-user 内存工作副本 +
  细粒度锁，写操作同步落库；带 15 分钟 TTL 原地刷新（refresh-in-place，
  不换对象，避免写入竞态）和 `POST /v1/admin/store/evict` 定向驱逐
  （2026-06-07 引入，修复带外改库后缓存陈旧问题）。
- **长轮询**：`/v1/chat/poll`、`/v1/proactive/jobs/poll` 用
  `threading.Event` waiter 挂起，新消息/新 job 到达即唤醒。
- **多租户**：所有表以 `user_id` 分区；API key 经 HMAC-SHA256(pepper)
  哈希存储；每个 key 绑定 access_mode（resident / model_api /
  official_import）。`tests/test_multi_tenant_isolation.py` 做 8 并发
  用户全流程交叉验证回归。

### 4.3 backend 内的辅助模块

| 模块 | 职责 |
|------|------|
| `content_encryption.py` | v1 信封构建（与 iOS / enclave 三方一致的参考实现） |
| `provider_client.py` | 模型 API 路由的 LLM provider 客户端（key 校验、chat completion） |
| `hosted_runtime.py` | 托管运行时的后台执行合约（工具调用、待确认动作） |
| `model_api_runtime/` | 托管聊天的 prompt 构建与工具（web search 等） |
| `context_memory_selection.py` | 记忆检索与相关性打分，组上下文窗口 |
| `perception/` | 扩展感知：信号目录、权限、快照、wake 触发 |
| `dstack_tls.py` | dstack-KMS 密钥派生 + 确定性 TLS 证书生成 |
| `acme_dns01.py` | ACME DNS-01（历史 Phase C.2 在 enclave 内签 LE 证书的路径） |

## 5. 三个服务的分工细节与 Agent 接入

> §4.1 讲拓扑，本节讲职责边界和实际接入操作。一句话分工：
> **backend 管数据（只见密文），enclave 管钥匙
> （明文只在 TDX 里出现）。**
> 两条 agent 路线在运行时拿到什么数据、prompt 长什么样，逐字引用见
> `docs/RUNTIME_FLOWS.md`。

### 5.1 `asgi_app.py` — ASGI 后端（:5001），基底层

**定位**：唯一的数据出入口，其余两个服务都不直接碰数据库；它自己也
**只见密文**——所有内容以 v1 信封形式过手、原样落库。职责分块：

| 职能 | 代表路由 | 说明 |
|------|---------|------|
| 账号与鉴权 | `/v1/users/register` · `/v1/users/whoami` · `/v1/access/link-token` + `claim-token` | 发放/校验 api_key（HMAC-SHA256+pepper 哈希存储）；link-token 用于换设备/多端配对，避免重复 register 铸新账号 |
| 内容存储 | `/v1/chat/*` · `/v1/memory/*` · `/v1/identity/*` · `/v1/screen/*` | 只收 v1 加密信封，明文写入返回 400；信封原样存 PostgreSQL |
| 实时通道 | `/v1/chat/poll` · `/v1/proactive/jobs/poll` | 长轮询，消息落库即唤醒挂起请求 |
| 推送 | `/v1/push/dynamic-island` · `/v1/push/live-activity` | APNs 通道，agent 的"存在感"出口 |
| 主动唤醒 | `/v1/proactive/tick` · `/v1/proactive/jobs/*` | 机械的 wake event 生成 + job 队列（queued→claimed→posted），不做语义判断 |
| 托管运行时 | `/v1/model_api/*` · `/v1/history_import/*` | 模型 API 路由的服务端 agent 循环（§5.7） |
| 引导验收 | `/v1/bootstrap` · `/v1/memory/verify` · `/v1/identity/verify` · `/v1/onboarding/validate` · `/v1/chat/verify_loop` | onboarding 各阶段的服务端验收门 |

**关键内部机制**：

- **UserStore 写穿缓存**：per-user 内存工作副本 + 细粒度锁（chat /
  frames / memory / proactive 各自一把），写操作同步落库；15 分钟 TTL
  **原地刷新**（不换对象，避免写入竞态）+ `POST /v1/admin/store/evict`
  定向驱逐。注意各域策略不同：chat 走缓存，memory / identity /
  model_api 实时读 DB。
- **长轮询 waiter**：`threading.Event` 挂起 poll 请求，新消息/新 job
  落库即唤醒；缓存刷新时也会唤醒 waiter，让挂着的 poll 立刻重连。
- **运行形态**：gunicorn + UvicornWorker，默认单 worker（进程内缓存
  约束，见优化清单 #1；多 worker 需 `FEEDLING_BACKEND_WORKERS` +
  leader 选举），另开 :9998 WebSocket 端口收屏幕帧；chat 每用户
  5000 条环形缓冲，O(1) 修剪。

### 5.2 ~~`mcp_server.py` — FastMCP 服务器~~（已移除 2026-06-12）

MCP 用户条线（路由 A）整体下线：`backend/mcpsrv/` 包、`mcp_server.py`
入口、mcp 容器、`mcp.feedling.app` 域名与 ACME 插件（`acme_dns01.py`）
均已删除；attestation bundle 中的 `mcp_tls_cert_pubkey_fingerprint_hex`
字段保留但恒为空（iOS 审计卡走既有的 "Pre-Phase-C.2" 披露行）。
历史设计见 git 历史与 CHANGELOG 2026-06-12 条目。

### 5.3 `enclave_app.py` — Enclave 应用（:5003），密钥的家

**定位**：跑在 TDX 可信域内，全系统唯一能产生明文的进程。三块职责：

- **密钥派生**：启动时从 dstack-KMS 按 `(kms_root, app_id, path)` 派生
  内容私钥（X25519）与 attestation 端口 TLS 私钥。compose_hash 不在
  链上白名单则 KMS 不放钥匙、服务起不来；同一 app_id 下密钥跨版本
  稳定，发版不需要 rewrap。
- **`/attestation`**：返回 DCAP 签名的 TDX quote + 度量值。
  `REPORT_DATA` 绑定 `sha256(内容公钥 ‖ TLS 证书指纹 ‖ 版本串)`——
  "CEK 包给了谁"和"你在跟谁说话"都被硬件度量背书，iOS 审计卡和
  `tools/audit_live_cvm.py` 验的就是这个端点。
- **解密代理**（8 条路由：envelope/decrypt、chat/history、memory/list、
  identity/get、屏幕帧解密/取图等）：
  校验调用者 api_key → 从 backend 取密文信封 → 校验 `owner_user_id`
  所有权 → 解开 `K_enclave` → AEAD 解密（AAD 绑定 owner|v|id）→ 返回明文。

**设计原则**：不直接连数据库，密文一律从 backend 取——数据面与密钥面
物理分离。鉴权依赖回环调用 backend `/v1/users/whoami`，用短 TTL 缓存 +
in-flight 合并压掉了批量解密时的回环风暴（残余耦合见优化清单 #3）。

### 5.4 接入前提：api_key

三条路由的前提一样：用户先有账号和 **api_key**——iOS 首装时
`POST /v1/users/register` 发放；换设备 / 接多个客户端用
`/v1/access/link-token` + `claim-token` 配对，**不要**重复 register
（会铸新空账号、孤儿化老账号，见 CHANGELOG 2026-06-02/06-07 条目）。

### 5.5 ~~路由 A：MCP 直连~~（已移除 2026-06-12）

原 Claude.ai / Claude Desktop 经 MCP 工具接入的条线已下线（连同
bootstrap 的 MCP 工具序列）。现存接入方式只剩路由 B（Resident
Consumer）与路由 C（Model API 托管）；`/v1/bootstrap`、
`/v1/onboarding/validate` 等 HTTP 端点保留，服务于 resident 线。

### 5.6 路由 B：Resident Consumer（自建服务器，用户自己的 agent 当大脑）

用户在自己的 VPS 跑 `tools/chat_resident_consumer.py`——一个把"任意
agent"桥接成 Feedling 回复管线的常驻进程：

```
用户在 iOS 发消息
  → consumer 长轮询 GET /v1/chat/poll 拿到（密文）
  → 经 FEEDLING_ENCLAVE_URL（直连 enclave）解密
  → 调用 agent：
      AGENT_MODE=http → POST 到 AGENT_HTTP_URL（简单 JSON 或 OpenAI 兼容协议，如 Hermes）
      AGENT_MODE=cli  → 执行 AGENT_CLI_CMD 模板（如 hermes chat -q "{message}"，自动 --resume 续会话）
  → 回复用 build_envelope() 封成 v1 信封
  → POST /v1/chat/response 回写
```

它同时是主动唤醒的执行端：默认开启 `PROACTIVE_TICK`（屏幕共享开着每
5 分钟、关着每 30 分钟发一次 wake tick）和 `PROACTIVE_POLL`（领取
proactive job，走同一个 agent 入口实现）。另有断点文件防重复消费、
`SCREEN_CONTEXT_MODE` 自动附带屏幕上下文等。配置全走环境变量文件
（`CHAT_RESIDENT_ENV_FILE`），密钥不进代码。

agent 对身体的主动操作（推送、写记忆/身份）走 HTTP API 的
`/v1/identity/actions`、`/v1/memory/actions` 等 envelope-action 端点。

### 5.7 路由 C：Model API 托管（没有自己 agent 的用户）

用户只提供一把模型厂商 API key（OpenAI / Anthropic / Gemini）：

1. `POST /v1/onboarding/route` 选 `model_api` → `POST /v1/model_api/setup`
   保存 provider 配置（key 本身也封成 v1 信封存储）。
2. 可选 `POST /v1/history_import/upload` 导入旧聊天记录，后端用该 provider
   提取记忆、初始化身份卡。
3. 之后聊天走 `POST /v1/model_api/chat/send`：托管运行时
   （`hosted_runtime.py` + `model_api_runtime/`）经 enclave 解密必要上下
   文、用 `context_memory_selection.py` 挑选相关记忆组 prompt、调
   provider、把用户消息和回复各自封信封落库。

这条路里**后端自己就是 consumer**，用户零部署，代价是运行时进程内会
短暂持有明文（文档明确披露的边界）。

### 5.8 隐私梯度

**A/B 路由明文只出现在 enclave 和用户自己的 agent 侧；C 路由为了零部署，
接受后端托管运行时短暂接触明文。**

## 6. 加密设计（v1 信封）

威胁模型（详见 `docs/DESIGN_E2E.md`）：防的是**后端磁盘/运维/备份/日志**；
不防 agent 读明文（那是产品功能），也不加密元数据（时间戳、消息数、
push token 是明文——文档里明确坦白）。

每条内容（chat 消息、memory、身份卡、屏幕帧、agent 回复）写入前封装为：

```json
{
  "v": 1,
  "id": "<item_id>",
  "owner_user_id": "<user_id>",
  "visibility": "shared | local_only",
  "body_ct": "ChaCha20-Poly1305(CEK, plaintext, aad=owner|v|id)",
  "nonce": "12B random",
  "K_user":    "BoxSeal(CEK → 用户设备 X25519 公钥)",
  "K_enclave": "BoxSeal(CEK → enclave 内容公钥)  // local_only 时省略",
  "enclave_pk_fpr": "..."
}
```

要点：

- 每条消息随机 CEK，**双重包裹**：包给用户设备公钥（手机永远能读）+
  包给 enclave 公钥（agent 只有经 TDX 内的解密代理才能读明文）。
- AAD 绑定 `owner_user_id|v|id`，防跨用户信封替换；enclave 解密前还校验
  所有权。
- iOS 的密钥对生在 Keychain，永不出设备；enclave 的内容私钥由
  dstack-KMS 在 CVM 启动时按 `(kms_root, app_id, path)` 派生——同一
  app_id 下跨 compose 升级**密钥稳定**，发版不需要全量 rewrap。
- `/v1/content/swap` 支持在位换信封（可见性切换）。

## 7. 数据库设计与 Memory 系统

### 7.1 总体取向：文档型 JSONB + 少量明文索引列

整个持久层是 PostgreSQL（外部托管，psycopg 连接池），但用法接近文档数据
库：**每行的主体是一个 JSONB `doc` 列**（通常就是完整的 v1 加密信封），
旁边只放服务端排序/分区/检索所需的少量明文列（`user_id`、`ts`、`seq`、
`item_key` 等）。这是加密设计的直接推论——服务端读不了内容，所以一切
服务端逻辑（多租户分区、时间排序、计数、验收门）都必须建立在**有意暴露
的明文元数据**上，其余全部进密文。

Schema 由 Alembic 管理（`backend/alembic/versions/`，目前 3 个 revision），
DDL 全部幂等（`IF NOT EXISTS`），所以 baseline 可以安全地 stamp 到
Alembic 出现之前就已建表的生产 RDS 上。

### 7.2 表清单（`0001_baseline` + `0002_perception_items`）

| 表 | 主键 / 索引 | 存什么 |
|----|------------|--------|
| `server_config` | `key` | 服务器级配置（如 api_key 哈希用的 pepper），BYTEA |
| `global_blobs` | `key` | 全局 JSONB 文档（配置/缓存） |
| `users` | `user_id` | 用户记录：api_key 哈希、access_bindings、设备公钥等，全在 `doc` |
| `user_blobs` | `(user_id, kind)` | per-user 键值文档：identity、model_api 配置、bootstrap 状态、push tokens、perception 状态、history_import job 等，按 `kind` 区分 |
| `chat_messages` | `(user_id, msg_id)`；`(user_id, seq)` 索引，seq 自增 | 聊天消息信封。**环形缓冲**：每用户上限 5000 条，超限按 seq 做 O(1) 修剪 |
| `memory_moments` | `(user_id, moment_id)`；`(user_id, occurred_at)` 索引 | 记忆卡片信封（见 §7.4） |
| `frame_envelopes` | `(user_id, frame_id)`；`(user_id, ts)` 索引 | 屏幕帧大信封（可 >150KB） |
| `user_logs` | `(user_id, stream, seq)`；另有 ts / item_key 部分索引 | **通用 append-only 流**：proactive_jobs、proactive_decisions、memory_changes、memory_capture_jobs、perception_events 等共用一张表，按 `stream` 命名空间区分 |
| `perception_items` | `(user_id, kind, item_id)`；`(user_id, kind, ts DESC)` + `expires_at` 部分索引 | 行式感知条目（photo / calendar / workout / sleep / vitals），带可选 TTL；照片的 `doc` 里同时带加密内容信封——后端从不持有明文像素 |

两个值得注意的模式：

- **`user_blobs` 当 per-user KV 用**：新功能的单例状态（perception 状态、
  托管运行时配置）不开新表，加一个 `kind` 就行；只有需要"逐行 + 时间序 +
  TTL"的数据（如 perception_items）才升级成独立表。
- **`user_logs` 当事件总线用**：所有审计轨迹和队列语义（proactive job 的
  claim/status 流转、memory 变更史）共用一张流表，`item_key` 部分索引支
  持按业务 id 反查。

### 7.3 明文 / 密文分界线

以一条 memory 为例，落库的 `doc` 长这样：

```json
{
  "v": 1, "id": "mom_…",
  "type": "fact",                  // ← 明文：枚举校验、按 tab 计数、验收门
  "occurred_at": "2026-05-01…",    // ← 明文：时间排序、时间分布检查
  "created_at": "…", "source": "live_conversation",
  "visibility": "shared",
  "owner_user_id": "usr_…",        // ← 明文：所有权校验 + AAD 绑定
  "anchor_memory_ids": ["mom_…"],  // ← 明文：insight/reflection 的底料校验
  "body_ct": "…", "nonce": "…",    // ← 密文：{title, description,
  "K_user": "…", "K_enclave": "…"  //    her_quote?, context?, linked_dimension?}
}
```

原则：**服务端要执行的每条规则，对应一个明文字段；用户内容本身全在
`body_ct` 里。**（`type` 在密文体内也复制了一份供客户端渲染，但服务端
只认信封上的明文副本。）

### 7.4 Memory 系统（记忆花园）怎么做的

**类型与分区**（`backend/memory/service.py` 的 `MEMORY_TYPES` / `TAB_FOR_TYPE`）：
6 种类型映射到 iOS 三个 tab——

| 类型 | Tab | 语义 |
|------|-----|------|
| `moment` / `quote` | Story | 你们之间发生的事 / 原话 |
| `fact` / `event` | About me | 用户的偏好、关系、习惯 / 用户生活里的具体事件 |
| `insight` / `reflection` | TA 在想 | agent 对用户的理解 / agent 的独立思考 |

**写入门（防灌水的结构性约束）**：

- `insight` 必须带 `anchor_memory_ids` ≥1，引用已存在且属于本人的记忆
  ——"理解"必须落在具体卡片上，指不出卡片就先写 fact/event。
- `reflection` 必须 ≥2 个 anchor，**并且有按关系阶段分档的时间频控**
  （超频返回 429）——思考需要底料积累，不许刷屏。
- anchor 一律服务端校验存在性与归属。

**验收门（bootstrap 的 gate，`/v1/memory/verify`）**：每个 tab 有按
"关系天数"分档的卡片数下限（floors）——

| 关系时长 | story / about_me / ta_thinking |
|---------|-------------------------------|
| ≥6 个月 | 15 / 60 / 12 |
| ≥1 个月 | 8 / 25 / 5 |
| ≥2 天 | 3 / 8 / 2 |
| 刚认识 | 1 / 1 / 0 |

`passing`（= Story + About me 达标）是 `identity_init` 的硬前置，不过不让
写身份卡；`passing_full`（三 tab 全达标）是建议目标。verify 还做**时间
分布检查**：关系超过 14 天但所有卡片 `occurred_at` 挤在 7 天内 → 判定
"只扫了最近的历史"，要求回头补扫。响应里还带 `archive_language` 字段，
锁定记忆花园的书写语言、防止 agent 随聊天语言漂移。

**生命周期**：删除走归档而非物理删（`is_archived`/`archived_at`，归档卡
不计入 floors）；`retype` 支持重新分类（转成 reflection 时豁免时间频控）；
所有变更写入 `user_logs` 的 `memory_changes` 流；批量捕获（聊天历史蒸馏
成记忆卡）走 `memory_capture_jobs` 流跟踪进度。

**检索（喂给 agent 的上下文怎么选）**：`backend/context_memory_selection.py`，
纯函数、不依赖向量库——

- resident 路径（宽松）：最多 3 张转折卡（标题前缀 `转折｜`，最新
  优先）+ 2 张最近创建 + 3 张与最新用户消息相关，按 id 去重、总数封顶 8。
- 托管 model_api 路径（严格）：记忆只是**候选**而非平台注入的真相——
  实体/短语命中才能入选，泛词（"project"、"项目"、"今天"这类中英停用词
  和工程常用词）只能作为辅助信号，不能单独召回 persona 卡，避免
  "普通的 project 一词召回 TOHO Project 专属记忆"式误命中。相关性用
  字符 bigram Jaccard 等轻量文本特征算。

注意这一步发生在**enclave/托管运行时解密之后**的明文上（选择逻辑独立成
模块正是为了不带 nacl 依赖就能单测）。

## 8. 信任链：从硬件到链上

完整推导见 `docs/DESIGN_E2E.md`，审计操作手册见 `docs/AUDIT.md`（10 项
检查清单），CLI 复现见 `tools/audit_live_cvm.py`。链条如下：

1. **TDX attestation**：enclave 出 DCAP 签名的 quote。`REPORT_DATA` 绑定
   `sha256(enclave 内容公钥 ‖ sha256(attestation 端口 TLS 证书 DER) ‖ 版本串)`
   ——所以"你在跟谁说话"和"CEK 包给了谁"都被硬件度量背书。
2. **compose_hash 度量**：dstack 把 `sha256(canonical docker-compose)` 写进
   quote 的 `mr_config_id`（RTMR3 事件日志可重放验证），证明跑的就是
   仓库里这份 compose。
3. **链上授权**：`contracts/src/FeedlingAppAuth.sol`（Ethereum Sepolia，
   `0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F`）维护 compose_hash 白名
   单。dstack-KMS 启动时调 `isAppAllowed(compose_hash)`——**未授权的镜像
   拿不到密钥，起不来**。`addComposeHash` 历史公开可查，作为发布透明日志。
4. **iOS 证书 pin**：审计卡直连 attestation 端口（dstack-gateway
   `-5003s.` 直通），比对活跃 TLS 证书的 sha256(DER) 与 quote 内指纹，
   MITM 直接红行报警。
5. **公网域名 TLS**（api.feedling.app）是普通 Let's Encrypt——它保护
   的是传输层；**内容机密性不依赖它**，靠的是信封密文本身。MCP 端口的
   旧 pubkey pin 已随 MCP 条线移除（2026-06-12），审计卡将其展示为
   透明披露而非失败项。

诚实边界（AUDIT.md 明示不声称的）：元数据不加密；"Feedling 永远看不到
数据"的强度依赖 Intel TDX 信任根；基础镜像 apt 包尚未 hash-pin；合约
目前在 Sepolia 测试网。

## 9. 部署与 CI/CD

### 9.1 生产环境（Phala Cloud TDX CVM）

- 单个 CVM（Phala prod9）内跑 §4.1 的四个容器，compose 文件
  `deploy/docker-compose.phala.yaml`。
- 公网入口 `api.feedling.app` / `mcp.feedling.app`；机密配置
  （Cloudflare token、APNs key、DATABASE_URL、LLM keys）经 Phala 加密
  环境通道注入，不进 compose_hash。
- test 与 prod 是两台 CVM、两个链上合约（test 用
  `0x9AC0…F2D5`），分支隔离，互不污染发布日志。
- 当前 CVM ID / 镜像 tag / compose_hash 见 `deploy/DEPLOYMENTS.md`。

### 9.2 CI/CD（`.github/workflows/ci.yml`）

```
forge-test ┐
python-tests ├→ detect-cvm-changes → deploy-cvm        (main → prod CVM)
docker-build ┤                     → deploy-test-cvm   (test → test CVM)
lint / dcap ┘
```

- 测试齐过 + 路径过滤命中（backend/、deploy/ 等）才触发部署。
- 部署流程：等 GHCR 镜像就绪 → 把 tag pin 进 compose 并提交
  `deploy: bump CVM image [skip ci]` → `phala deploy --wait` →
  `deploy/publish-compose-hash.sh` 用 `cast send` 把新 compose_hash 上链。
- Foundry 钉在 1.7.1（避免 toolchain 下载限流）。

### 9.3 自托管（`deploy/SELF_HOSTING.md`）

纯 Python + systemd（不需要 Docker/TDX）：`feedling-backend.service` +
`feedling-mcp.service`，可选 Caddy 反代。配合
`tools/chat_resident_consumer.py` 即"自建服务器"路由的完整形态。

## 10. 测试与工具

- **测试**（`tests/`）：多租户隔离回归
  （`test_multi_tenant_isolation.py`）、DB 层、缓存 TTL/evict
  （`test_store_cache.py`）、信封 rewrap、resident consumer、proactive
  jobs、账号恢复等。`pytest tests/ -v` 运行（个别用例依赖可达的
  enclave attestation）。
- **审计 CLI**：`tools/audit_live_cvm.py`——逐行镜像 iOS 审计卡的检查
  （quote 解析、度量、链上授权、证书 pin），任何人可对生产 CVM 复跑。
- **DCAP 解析器**：`tools/dcap/` Python 参考实现 + 单测。
- **信封往返测试**：`tools/v1_envelope_roundtrip_test.py` 等，保证
  Python / iOS / enclave 三方加密实现一致。
- **运维工具**：`tools/recover_orphan_accounts.py`（重装铸新账号的孤儿
  数据合并，dry-run 优先）、`tools/check_chat_pipeline.py`（链路健康
  检查）。

## 11. 设计体系（UI）

`DESIGN.md` 定义全部视觉决策，方向是 **Warm Minimalism / iOS-native
Artful**：文字与留白为主、单一主色、iOS 原生质感（New York 衬线做
display、SF Pro 正文、SF Mono 展示 hash/key 类数据）。iOS 代码中禁止裸
hex / 裸字号 / 裸字体串，必须用 `Color.feedling…` / `Font.feedling…` /
`Spacing.*` / `Radius.*` token。

## 12. 延伸阅读（按需）

| 想了解 | 读 |
|--------|----|
| 最近改了什么、为什么 | `docs/CHANGELOG.md`（倒序，含决策记录） |
| 加密设计推导 | `docs/DESIGN_E2E.md` |
| 怎么审计一台活的 CVM | `docs/AUDIT.md` + `tools/audit_live_cvm.py` |
| 主动唤醒 / 感知 Round 3 | `docs/PROACTIVE_PERCEPTION_SPEC_V2.md`（PR 执行和审计见 `docs/PROACTIVE_PERCEPTION_ROUND3_EXECUTION_PLAN.md`；迁移契约见 `docs/PROACTIVE_PERCEPTION_RUNTIME_V2_MIGRATION.md`） |
| 扩展感知 API | `docs/EXTENDED_PERCEPTION_API.md` |
| 托管模型 API 路由 | `docs/MODEL_API_PATH_P0.md` |
| 部署历史与链上记录 | `deploy/DEPLOYMENTS.md` |
| 运行时流程与 prompt 原文（onboarding 后日常 / 两条路线的数据流） | `docs/RUNTIME_FLOWS.md` |
| 已知技术债与优化方向 | `docs/OPTIMIZATION_BACKLOG.md` |
| Agent 行为规范（公开） | io-onboarding 仓库 `skill.md` |
