# IO V2（pettorokku）服务架构

Date: 2026-08-17

Status: 已批准的 IO V2 起步规格

## 1. 定位

`pettorokku` 是 IO V2 的 Python 服务架构规格，不再是未绑定业务的通用模板。

IO V2 使用 FastAPI + asyncio 提供 REST 与 SSE 接口，使用自建 TEE PostgreSQL 保存
业务明文，使用同样自建在 TEE 内的 Redis 协调会话、缓存、SSE 和 Celery。API 与
Celery Worker 使用同一代码和镜像，通过不同入口启动，并支持多 worker、多节点独立
扩缩容。生产环境继续部署在 Phala 平台，运行单元为 Phala CVM/dstack TEE workload。

本文定义运行时边界、数据与鉴权模型、迁移方式和交付要求。本文暂不冻结源码目录，
只规定 Router 必须使用独立包，与业务代码和基础设施实现分开。

## 2. 基础原则

- **无应用层内容加密**：取消 V1 信封加密。API、Worker 和 TEE PostgreSQL 处理、
  存储业务明文。
- **保留基础设施加密**：公网和数据库连接强制 TLS；磁盘、备份和 secrets 仍需加密。
- **TEE PostgreSQL 是唯一主库**：不保留 RDS 主库、影子库或双写。
- **数据库为事实来源**：Redis 和进程内缓存均可丢弃，不承担最终一致性。
- **数据库结构可迁移**：稳定 ID、明确租户字段、版本化 schema、幂等迁移和完整校验。
- **多 worker 缓存一致**：共享 Redis、版本化缓存失效事件、短 TTL 和数据库回源。
- **Router 与业务分离**：Router 只处理 HTTP/SSE 适配，不写 SQL、不操作具体缓存。
- **多节点部署**：API、Worker 都可以增加副本，节点不依赖粘性会话。
- **Phala 是生产部署平台**：API、Worker、Migration、自建 PostgreSQL 与 Redis 均作为
  Phala CVM/dstack workload 交付；应用层不直接依赖 Phala SDK。
- **SSE 可恢复**：事件进入 Redis Streams，客户端可以通过 `Last-Event-ID` 续传。
- **默认拒绝**：除明确公开探针和登录接口外，HTTP/SSE 默认需要认证。

## 3. 非目标

第一阶段不包含：

- V1 应用层信封加密、内容密钥、envelope rewrap 或客户端本地解密流程。
- RDS/TEE 双库、明文影子库、best-effort 双写或 reconcile。
- 微服务拆分、服务网格或跨服务分布式事务。
- 多条 Celery 队列、任务编排 DSL 或通用工作流引擎。
- ORM、自动生成 repository 或覆盖所有业务的万能 Store。
- 单个 API 容器内运行多个 Web 进程。
- 数据库自动故障切换或多主写入。
- 在业务代码中引入 Phala SDK、写死 CVM/app ID、环境域名或资源名称。
- 把其他云厂商 SDK、资源命名或发布流程带入 IO V2。
- 把 Compose 当作正式生产发布系统。

## 4. 运行时拓扑

```text
Clients
   │ HTTPS / SSE
   ▼
Phala ingress / Load Balancer
   │
   ├────► Phala CVM: FastAPI Node A ─┐
   ├────► Phala CVM: FastAPI Node B ─┼──► Phala CVM: TEE PostgreSQL
   └────► Phala CVM: FastAPI Node N ─┘             （唯一主库，业务明文）
             │
             ├──► Phala CVM: TEE Redis Session / Cache / Streams
             │
             └──► TEE Redis Celery Broker ──► Phala CVM: Celery Worker Nodes
                                             │
                                             └──► TEE PostgreSQL
```

同一镜像支持三个角色：

1. **API**：提供 REST、登录/刷新/登出和 SSE。
2. **Worker**：消费 Celery 任务并调用业务 Job Handler。
3. **Migration**：执行 `alembic upgrade head` 或 V1→V2 数据迁移后退出。

单个 API 容器运行一个 Uvicorn 进程，通过增加容器副本扩容。Worker 独立扩缩容。
TEE Redis 和 TEE PostgreSQL 使用独立 Phala CVM workload 部署，不与 API 进程混跑。

第一阶段的 TEE PostgreSQL 是单主库。自动备份、异地保存和恢复演练提供可恢复性，
但不把单主库描述为高可用。数据库自动故障切换需要后续独立设计。

## 5. 明文与信任边界

“全程数据不加密”在本文中专指不使用 V1 的应用层内容加密：

- 客户端通过 HTTPS 把业务明文发送给 API。
- API 和 Worker 可以在内存中读取业务明文。
- TEE PostgreSQL 直接存储业务明文列和 JSONB。
- TEE Redis 的 cache、Streams、Celery broker/result 可能短暂保存必要的业务明文；
  session 区保存 token 摘要和会话状态，不保存密码或 token 原文。
- V1 迁移从已经存在的 V1 明文解密数据库读取数据。

以下保护仍是强制要求：

- 公网 HTTPS；
- API/Worker 到 PostgreSQL 的 TLS；
- TEE Redis 认证与受限网络，跨不可信网络时使用 TLS；
- TEE PostgreSQL 磁盘和备份加密；
- secret manager 管理 JWT signing key、password pepper、数据库和 Redis 凭证；
- 数据库、备份和运行节点的最小权限访问。

对外文档必须明确：TEE 降低宿主机读取运行时和数据库明文的风险，但 IO V2 不是端到端
加密系统。API、Worker、TEE PostgreSQL、TEE Redis 和被调用的外部服务都是明文处理
边界。本文不再为 TEE 内的 Redis 设计额外应用层加密。

## 6. 分层与 Router 边界

### 6.1 Router

所有 REST 与 SSE Router 放在独立 Router 包中，与 Service、Store、Job 和基础设施实现
分开。Router 只负责：

- HTTP/SSE 输入解析和 Pydantic 校验；
- 认证依赖和 principal 注入；
- 请求模型与业务命令之间的映射；
- 调用业务 Service；
- 把结果或事件映射成 HTTP/SSE 输出。

Router 不打开数据库连接、不写 SQL、不直接操作 Redis key、不导入 Celery，也不调用
`send_task()`。Router 不逐个捕获领域异常并翻译状态码。

### 6.2 Service

Service 负责用例编排、权限检查、事务边界、缓存策略和领域不变量。Service 依赖小而
专用的 Protocol，不依赖 FastAPI、Celery 或具体数据库实现。

不要创建覆盖整个系统的巨型 `StoreProtocol`。每个领域声明最小读写能力，基础设施层
可以组合实现。

### 6.3 Job Handler

Job Handler 是 Worker 调用的普通函数或异步函数。它接收版本化 JSON payload，通过
Worker Runtime 获取 Service，并更新业务状态。

Job Handler 不获得 Task Backend，禁止在任务内隐式递归投递 Celery。需要扇出、链式
任务或工作流时必须单独设计。

## 7. Runtime Container

Runtime Container 保存完成校验和装配的运行时依赖：

- 冻结的 Settings；
- 领域 Store 实现集合；
- 领域 Service 集合；
- Task Backend；
- Redis Session、Cache 和 Stream adapters。

提供两个装配入口：

- `create_api_container()`：装配 API 所需的 PostgreSQL、Redis 和 Celery 提交端。
- `create_worker_container()`：装配 PostgreSQL 和业务 Service，但不向 Job Handler 暴露
  任务提交能力。

Container 统一负责 `setup()` 与 `aclose()`。Router 只从 `app.state` 获取已经装配的
依赖，不读取环境变量或自行创建客户端。

## 8. 配置与 Redis 隔离

配置使用 Pydantic v2 与 `pydantic-settings`，在启动时解析一次并冻结。生产缺少必要
URL 或 secret 时必须启动失败，禁止回落到内存实现。

基础配置面：

| 变量 | 用途 |
|---|---|
| `DATABASE_URL` | TEE PostgreSQL app 角色连接串 |
| `REDIS_SESSION_URL` | Access/Refresh session、登录限流、SSE ticket |
| `REDIS_CACHE_URL` | 共享缓存和失效事件 |
| `REDIS_STREAM_URL` | SSE Redis Streams |
| `CELERY_BROKER_URL` | Celery broker |
| `CELERY_RESULT_BACKEND` | Celery task 状态 |
| `JWT_SIGNING_KEY` | 至少 256 bit 的 JWT HS256 签名 secret |
| `JWT_ISSUER` / `JWT_AUDIENCE` | JWT 校验边界 |
| `PASSWORD_PEPPER` | 生成 password lookup 的服务端 secret |
| `V1_PEPPER` | 仅迁移服务读取的 V1 确定性 lookup secret；API/Worker 不注入 |

这些 URL 在第一阶段可以指向同一套自建 TEE Redis，但必须使用独立 key namespace；
逻辑数据库不能提供独立的 `maxmemory`、淘汰策略或持久化策略，因此共享部署只能采用
一套满足最严格用途的全局策略。第一阶段固定 `maxmemory-policy=noeviction`；所有临时
key 必须有 TTL，Streams 必须有 `MAXLEN` 或保留期，并为认证与任务写入保留至少 30%
容量余量。内存超过 70% 持续 15 分钟或出现任意 `OOM command not allowed` 即告警并触发
拆分评审；不能靠淘汰认证状态腾空间。不能用一个含义模糊的 `REDIS_URL` 同时表示所有
用途。

`.env` 只服务本地开发，不进入镜像或版本控制。生产 secrets 由部署平台注入。

## 9. 用户密码与登录

### 9.1 密码定义

V1 自动生成的私钥字符串在 IO V2 中直接作为用户密码。IO V2 不再使用它做应用层内容
加密。密码通过 HTTPS 提交，原文只在登录请求的内存中短暂存在，不写数据库、不进日志、
不进入 metrics、Celery payload 或 Redis Streams。

密码必须保持系统生成的高熵随机值。若未来允许用户设置人类可记忆的短密码，必须增加
独立登录标识并重新评审账号找回和密码策略。

### 9.2 只输入密码定位用户

随机 salt 的 Argon2id hash 不能直接用于数据库等值查询。为了支持“不输入用户名，
只输入密码”，每个用户保存两个字段：

```text
password_lookup = HMAC-SHA256(PASSWORD_PEPPER, password)
password_hash   = Argon2id(password, random_per_user_salt)
```

- `password_lookup` 建唯一索引，只用于定位候选用户。
- `PASSWORD_PEPPER` 是独立高熵 secret，不写数据库。用户记录保存
  `pepper_version`；轮换时同时加载 active/previous pepper，成功登录后重算 lookup，
  只有对应 previous `pepper_version` 的用户行数归零后才能移除 previous pepper；否则
  必须继续保留，或让剩余用户通过独立的账号所有权证明重置密码。
- `password_hash` 使用 `pwdlib[argon2]` 生成和验证；编码结果包含随机 salt 和参数。
- 找到用户后仍必须验证 Argon2id，不能把 lookup 命中视为认证成功。
- lookup 未命中时也执行一次固定 dummy Argon2id verify，避免通过响应耗时判断用户是否
  存在。Argon2id 参数由配置统一冻结；成功登录时执行 `needs_rehash` 并按新参数更新。

V1→V2 不能从旧 hash 推导这两个字段。用户迁移或首次 V2 登录时自行通过 HTTPS 提交
V1 私钥密码。迁移服务使用 V1 当前已有的确定性索引
`HMAC-SHA256(V1_PEPPER, submitted_password)` 查询唯一 V1 用户，不使用公钥、不扫描全表；
命中后在同一次请求内生成 V2 `password_lookup` 和 Argon2id hash。V1 pepper 只在迁移
服务内可用。它必须保留到所有可迁移账号已完成迁移、按政策过期或明确转入账号所有权
证明的重置流程后才能下线；迁移截止日前必须通知仍未迁移的用户。密码原文只存在于该
请求内存，不写迁移 checkpoint、数据库或日志。若 V1 用户没有可匹配的旧索引，则不能
自动迁移；密码重置必须走另行定义的账号所有权证明，不能仅凭一个未知密码认领历史
数据。

### 9.3 登录限流

登录按 IP 和 password lookup 前缀进行 Redis 限流。失败响应统一为 401，不区分账号不
存在、密码错误、账号不可用。限流记录只保存不可逆标识和计数，不保存密码。

## 10. JWT、Refresh Token 与登出

### 10.1 Access Token

- Access Token 使用 JWT，默认有效期 15 分钟。
- 第一阶段固定使用 HS256；解码器只允许 HS256，禁止信任 token header 自选算法。
- JWT 至少包含 `sub`、`jti`、`sid`、`iat`、`exp` 和 `session_version`；`sid` 是稳定的
  设备 session/token-family ID。
- 必须校验签名算法、issuer、audience、签发时间和过期时间。
- Redis 保存 `auth:access:{jti}`，TTL 与 JWT 剩余有效期一致。
- Redis 保存用户到 `sid`、family 和 `jti` 的索引，支持按当前设备或全部设备撤销。
- 每个受保护请求在验证 JWT 后，还必须确认 Redis access session 存在，并从 PostgreSQL
  读取当前 `session_version` 与 JWT 比较；该安全判断第一阶段不使用可过期缓存。
- Redis 不可用时 fail closed，不能退化成只验证 JWT。

### 10.2 Refresh Token

- Refresh Token 是不可解析的高熵 opaque token，不使用 JWT。
- 默认有效期 30 天。
- Redis 只保存 Refresh Token 的 SHA-256、用户 ID、token family、状态和 TTL。
- 每次刷新通过 Redis Lua script 或事务原子地消费旧 token、创建新 token 并更新 family
  索引；不允许“先读后写”。
- 已使用的 Refresh Token 再次出现时，撤销整个 token family。
- replay 撤销 family 下的 Refresh Token 和所有 Access `jti`。
- consumed Refresh Token 的 tombstone 保留到整个 family 的绝对过期时间，不能在单个
  token 原 TTL 到期前提前清除。
- 若刷新响应丢失，客户端不重放旧 Refresh Token，直接重新登录；第一阶段不为刷新提供
  会削弱 replay 检测的宽限窗口。
- 登录、刷新和 replay 事件写安全审计日志，但不记录 token 原文。

### 10.3 登出

- 当前设备登出：撤销当前 `sid`/family 下的全部 Access `jti` 和 Refresh Token，而不只
  删除发起请求的单个 Access session。
- 全部设备登出：先在 PostgreSQL 事务内增加单调递增的 `session_version` 并记录唯一
  revocation operation ID 和 pending 状态，再清理 Redis 中该用户的全部 session/family。
  Redis 清理失败返回 503；独立补偿进程扫描 PostgreSQL pending operation 幂等重试，
  不依赖本次请求成功投递 Celery。旧 JWT 已因 PostgreSQL 版本变化立即失效。
- 客户端同时删除本地 token。

已经签发的 JWT 字符串无法从客户端或网络中“删除”；Redis session 撤销和
`session_version` 才是立即失效机制。

## 11. TEE PostgreSQL 与 Schema

业务访问使用 psycopg 3 原生 SQL 和 `psycopg_pool.AsyncConnectionPool`，不引入 ORM。
连接池显式打开和关闭。

建议连接默认值：

- `connect_timeout=5s`
- `statement_timeout=60s`
- `lock_timeout=10s`
- `idle_in_transaction_session_timeout=30s`

Schema 规则：

- 所有用户数据表包含明确的 `user_id`/owner 外键。
- 唯一约束和常用查询索引包含租户维度。
- 主键使用稳定、与部署环境无关的 ID，迁移时不重编号。
- 时间统一使用 UTC `TIMESTAMPTZ`。
- 需要查询、关联、约束和排序的字段使用普通列。
- 只把真正可扩展、无需强关系约束的内容放入 JSONB。
- 可并发修改的记录增加 `version` 并使用乐观并发控制。
- 不保留 V1 的 `body_ct`、envelope、双写状态等加密结构。

数据库至少使用独立的 `app`、`migration`、`monitoring` 和备份角色。API/Worker 不拥有
DDL 权限。

## 12. Schema 迁移

Alembic 是 schema 版本的唯一事实来源。Migration 可以通过 `op.execute()` 使用原生
SQL；业务代码仍只使用 psycopg，不引入 SQLAlchemy ORM。

API 和 Worker 不在启动时自动升级 schema。本地、CI 和生产都显式执行：

```bash
uv run --frozen alembic upgrade head
```

部署时 Migration 成功后才启动新版本 API/Worker。破坏性变更采用
expand → migrate/backfill → contract，不假设全部节点同时升级。

## 13. V1→V2 数据迁移

迁移源是已经存在的 V1 明文解密数据库，不设计客户端本地解密。用户只在主动迁移或
首次 V2 登录时提交一次 V1 私钥密码用于凭证验证和生成 V2 密码字段，不用于解密业务
数据。

迁移要求：

- V1 数据库只读；IO V2 使用专门 migration role。
- 按用户和表分批执行，支持暂停、重试和断点续跑。
- 每批记录 source ID、target ID、状态、行数、校验摘要和错误原因。
- 使用确定性 ID 或幂等 upsert；重复执行不产生重复数据。
- 迁移前后按用户核对行数、关键字段、外键和引用完整性。
- 凭证迁移由用户提交 V1 私钥密码触发；校验 V1 verifier 后只写 password lookup、
  `pepper_version` 和 Argon2id hash，不持久化密码原文。
- 迁移失败不影响已经成功的批次；修复后从 checkpoint 继续。
- 迁移记录 source high-water mark；切流前冻结 V1 写入，执行 final delta，核对总量与
  摘要。未达到预设校验阈值时终止切流并继续使用 V1，不带病进入 V2。
- final delta 和校验通过后记录不可逆的 cutover commit point。在开放 V2 写入前仍可回退
  到冻结的 V1；V2 接受首笔写入后不再回切 V1，只能 forward-fix 或从 V2 备份恢复。
  冻结的 V1 库至少保留一个已批准的审计/回溯周期，确认无需回溯后再按独立审批销毁。

V1 schema 只作为迁移输入，不在 IO V2 业务代码中保留兼容读写层。

## 14. 多 Worker 缓存一致性

PostgreSQL 是业务事实来源。Redis 是跨进程共享 L2 缓存；进程内缓存只允许作为可丢弃、
短 TTL 的 L1。

一致性规则：

- 可变业务数据不得只存在进程内缓存。
- 数据库事务提交成功后删除或更新 Redis cache，并发布
  `entity_type + entity_id + version` 失效事件。
- 所有 API/Worker 节点订阅失效事件并清理对应 L1。
- Cache miss 回源 PostgreSQL。
- L1/L2 cache value 携带数据库版本；旧版本不得覆盖新版本。
- L1 默认 TTL 不超过 5 秒，L2 默认 TTL 不超过 5 分钟；具体领域可以进一步缩短，不得
  无界延长。
- 缓存失效第一阶段使用 Redis Pub/Sub；订阅断线或重连时无条件清空本节点全部 L1，
  不假装检测断线期间不存在的 cursor。随后通过 TTL 和版本检查自愈。
- cache fill 使用版本比较的原子脚本，旧版本不得覆盖新版本；失效发布失败有限重试并
  记录可观测错误。
- 鉴权、租户所有权和权限判断不依赖长 TTL 本地缓存。
- Redis lock 只用于限制重复重建缓存，不是数据一致性的事实来源。

业务读取接受上述上限内的最终一致性。鉴权、权限与明确要求读己之写的接口直接读
PostgreSQL，不经过可能陈旧的 L1/L2。

## 15. Celery Worker

Celery Worker 执行耗时、可重试、无需占用 HTTP/SSE 连接的任务。API 通过
Task Backend Protocol 提交，不直接使用 Celery 对象。

基础规则：

- 初始只使用 `default` 队列。
- 任务名是稳定、显式注册的字符串。
- Payload 是带版本字段的 JSON，不传 Python 对象、数据库连接或 Request。
- Worker 使用同一领域 Service 和 Store Protocol。
- Worker 使用 prefork 时，父进程不得创建或继承 async pool。每个 worker child 在
  `worker_process_init` 后创建自己的 asyncio event loop 与 AsyncConnectionPool，并在
  `worker_process_shutdown` 关闭；同步 Celery task 只把 Job Handler 调度到该 child 的
  固定 event loop。`AsyncConnectionPool.open()` 与 `close()` 都必须在该 loop 上执行，
  task 使用 `run_until_complete` 或等价同步桥接实际驱动该 loop。
- Job Handler 不获得任务提交能力，禁止递归投递。
- 默认任务不重试。
- 可重试任务必须定义次数、退避、抖动、错误分类、幂等键和 soft/hard time limit。
- 启用 late acknowledgement 或 worker-lost 重投时，按“至少一次执行”设计。
- Celery payload 和 result backend 不保存密码、token 或不必要的业务明文。

Celery result backend 是通用任务状态来源。若某类任务需要长期业务审计，应写入自己的
PostgreSQL 业务表，不依赖 Redis 永久保存结果。

## 16. SSE

需要可靠续传的业务事件与业务变更在同一 PostgreSQL 事务内写入 transactional outbox，
并在创建时生成稳定唯一的业务 `event_id`、`stream_key` 和该 stream 内单调
`stream_sequence`（从 1 开始）。每个可恢复 feed 使用独立 Redis Stream；entry ID 固定为
`<stream_sequence>-0`，同时作为 SSE `id`，因此客户端回传的 `Last-Event-ID` 可以直接
用于 `XREAD`。业务 `event_id` 作为独立字段用于端到端去重。

publisher 使用 PostgreSQL advisory lock 或等价机制按 `stream_key` 串行处理，只 claim
最小未发送 sequence，并把事件至少一次投递到 Redis。若在 `XADD` 后、标记 outbox 已
发送前崩溃，lease 到期后按同一 entry ID 重试：已存在且 `event_id` 相同则视为已投递，
不同则报数据完整性错误并停止该 stream。只保证单个 stream 内有序，不承诺全局顺序。
纯瞬时进度事件可以直接写 Stream，但必须明确标记为 best-effort，不承诺无缺口续传。

事件格式至少包含：

- `id`
- `event_id`
- `event`
- `version`
- `occurred_at`
- `data`

连接规则：

- 客户端使用 `Last-Event-ID` 断线续传。
- Stream 设置最大长度或保留时间，不能无限增长。
- 每 15–30 秒发送 heartbeat。
- 单连接设置发送队列上限和慢客户端处理策略。
- 响应使用 `text/event-stream`，禁止代理缓冲和 gzip。
- 服务关闭时停止接收新连接，并在期限内结束现有连接。
- Cursor 超出保留窗口时返回稳定的 `cursor_expired`，客户端改走快照接口。

原生客户端通过 `Authorization: Bearer <access_jwt>` 连接。若浏览器原生
`EventSource` 需要支持，则先用 Access Token 换取一次性、短 TTL、绑定用户和 stream
的 SSE ticket。Ticket 使用后立即删除。禁止把长期 JWT 放入 URL query。
Ticket 必须通过 Redis `GETDEL` 或 Lua 原子消费，并校验 user、stream、audience 和过期
时间；SSE Router 仍独立执行资源授权。代理、访问日志和错误日志必须脱敏 ticket query，
响应设置合适的 `Cache-Control` 与 `Referrer-Policy`。

## 17. HTTP、中间件与日志

公开路由使用精确集合管理，至少包含 `/healthz`、`/readyz`、登录和刷新。`/metrics`
只允许集群内部监控网络或独立监控凭证访问，不对公网匿名开放。
其余 REST/SSE 默认认证，不使用路径子串豁免。

入站中间件执行顺序：

1. CORS；
2. request ID 与访问日志；
3. 鉴权；
4. Router。

Starlette 后添加的中间件位于外层，因此注册时按执行顺序的逆序添加。合法 `OPTIONS`
预检必须在鉴权前由 CORS 处理。

每个请求生成或接受经过长度/字符集校验的 `X-Request-ID`，响应回传最终 request ID。
结构化日志至少包含 method、路由模板、status、`latency_ms`、request ID 和 principal
类型。

应用代码统一使用 Loguru JSON sink。Uvicorn 与 Celery 的标准 `logging` 通过单向
interception handler 转发到 Loguru，禁止同时保留两套输出 handler。

日志不记录密码、Authorization header、JWT、Refresh Token、SSE ticket、完整敏感
query 或默认记录请求/响应正文。

## 18. 健康检查与指标

### 18.1 `/healthz`

只证明进程可以及时响应，不访问 PostgreSQL、Redis 或 Celery。容器存活探针使用这里。

### 18.2 `/readyz`

检查当前角色所需的 PostgreSQL、Redis 和 Task Backend。任何必要依赖不可用或节点开始
关闭时返回 503。检查必须有短超时。

### 18.3 `/metrics`

使用 `prometheus-client`。HTTP route label 使用 FastAPI 路由模板，不使用原始 URL；
未匹配路由统一标记为 `unmatched`。

`/healthz`、`/readyz` 和 `/metrics` 不进入业务 latency Histogram。request ID、user ID、
JWT `jti`、password lookup 和 SSE event ID 不得作为 Prometheus label。

## 19. 错误模型

领域异常由 FastAPI 全局 exception handler 映射，Router 不重复翻译。

| 情况 | HTTP |
|---|---:|
| 登录凭证或 Token 无效 | 401 |
| 已认证但权限不足 | 403 |
| 资源不存在，或属于其他用户 | 404 |
| 输入格式或字段无效 | 422 |
| 业务状态或幂等冲突 | 409 |
| 登录/刷新/SSE ticket 触发限流 | 429 |
| PostgreSQL、Redis 或 broker 不可用 | 503 |
| 服务端处理期限耗尽 | 504 |
| 未捕获异常 | 500 |

适合重试的 503 携带 `Retry-After`。500 只返回稳定错误码和 request ID，不返回 traceback、
SQL、密码或内部异常文本。

Refresh replay 返回 401 并撤销整个 token family。SSE cursor 过期返回稳定业务错误，
不能静默从最新事件继续造成数据缺口。

## 20. 生命周期与多节点关闭

API 启动顺序：

1. 校验 Settings。
2. 创建 Runtime Container。
3. 打开 PostgreSQL 和 Redis 连接池。
4. 建立缓存失效订阅。
5. 检查必要依赖。
6. 将 readiness 标记为 ready。

API 关闭顺序：

1. readiness 立即变为 not-ready。
2. 停止接收新请求和 SSE 连接。
3. 通知并在期限内结束现有 SSE。
4. 等待有限时间让在途请求完成。
5. 停止缓存订阅并关闭 Redis 客户端。
6. 关闭 PostgreSQL 连接池。

负载均衡器必须支持长连接、关闭响应缓冲并设置合理 idle timeout。节点不依赖粘性会话；
滚动发布时客户端可以连接其他节点并使用 `Last-Event-ID` 恢复。

## 21. 测试策略

最低测试集：

1. Password lookup 能定位用户，`pwdlib[argon2]` 能验证带随机 salt 的 hash。
2. 错误密码、统一 401、登录限流和日志脱敏。
3. Access JWT 的算法、issuer、audience、时间和 Redis session 校验。
4. Refresh rotation、重复使用检测、当前设备登出和全部设备登出。
5. 所有用户表的租户隔离和跨用户 404。
6. V1 明文迁移可断点续跑、重复执行、行数校验和引用校验。
7. 多 API/Worker 节点的缓存失效、版本保护和 TTL 自愈。
8. SSE 鉴权、ticket 单次使用、heartbeat、续传和过期 cursor。
9. 节点滚动关闭期间 readiness、SSE 和在途请求行为。
10. Celery 的真实 Redis broker 闭环、重试和幂等行为。
11. TEE PostgreSQL 从空库 upgrade、备份恢复和最小权限。
12. 用户提交 V1 私钥密码的迁移注册、原文不落盘和无 verifier 时拒绝自动迁移。
13. Refresh 原子消费、family replay 撤销 access session，以及 pepper 轮换。
14. PostgreSQL outbox 到 Redis Stream 的崩溃恢复和重复投递去重。
15. lookup hit/miss 的响应时序分布、dormant 用户 pepper 轮换和退休条件。

架构护栏：

- Router 不导入 psycopg、Celery 或具体 Redis adapter。
- Domain 不导入 FastAPI、Celery 或 redis-py。
- Job Handler 不调用任务提交接口。
- 除精确公开集合外，所有 Router 默认认证。
- 指标不使用高基数或敏感 label。
- 日志测试确认密码和 Token 不会被输出。

## 22. 验证与 CI

统一验证应覆盖：

```bash
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
docker compose -f compose.yml -f compose.local.yml config
docker build .
```

CI 至少包含：

1. 单元测试和架构护栏；
2. Ruff lint 与格式检查；
3. PostgreSQL/Redis 集成测试；
4. Alembic 单一 head 与从空库 `upgrade head`；
5. V1→V2 小型固定数据集迁移；
6. API → Redis → Celery Worker → PostgreSQL 闭环；
7. 双 API 节点的缓存一致性与 SSE 续传模拟；
8. 镜像构建。

## 23. 依赖与版本政策

项目声明 `requires-python = ">=3.12,<3.14"`，开发环境与 Runtime 镜像默认固定
Python 3.13。升级 Python minor 版本前，先确认 FastAPI、Celery、psycopg 和密码库均
支持，并在 CI 与镜像中验证。

```toml
[project]
requires-python = ">=3.12,<3.14"
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "pydantic",
  "pydantic-settings",
  "celery[redis]",
  "redis",
  "psycopg[binary,pool]",
  "alembic",
  "pyjwt",
  "pwdlib[argon2]",
  "loguru",
  "prometheus-client",
  "python-dotenv",
]

[dependency-groups]
test = [
  "pytest",
  "httpx",
]
lint = [
  "ruff",
]
```

Python、uv、TEE PostgreSQL、Redis Server、Docker 和 Compose 是语言工具或外部组件，
不写入 `project.dependencies`。`uv.lock` 固定实际构建版本。

暂不引入 SQLAlchemy ORM、LangChain、RabbitMQ、Kafka、MySQL、Poetry、pip-tools、
Flask 或 Django。新依赖必须由真实需求触发。

## 24. 本地与生产交付

使用一个多阶段、非 root 运行的镜像。同一镜像提供 API、Worker、Migration 三个入口。
API 默认监听 `0.0.0.0:8000`，Dockerfile、Compose、探针和文档统一端口。

本地 Compose 可以启动 API、Worker、Migration、PostgreSQL 和 Redis。生产使用独立的
自建 TEE PostgreSQL 与 TEE Redis，不把本地 Compose 直接当生产发布拓扑。

生产部署平台固定为 Phala。Phala 部署规格负责定义 API/Worker CVM、PostgreSQL CVM、
Redis CVM、ingress、网络策略、持久卷、环境隔离和资源配额；本文不写死任何具体 app ID、
CVM 名或域名。生产 manifest 必须固定镜像 digest，并把会影响信任边界的 Compose 配置
纳入可验证 measurement/compose hash。test、pre、prod 使用独立 workload 与 secrets，
不得共享数据库、Redis namespace 或签名密钥。

生产上线前必须具备：

- TEE PostgreSQL 自动备份与异地保存；
- 定期恢复演练；
- 明确的 RPO/RTO；
- Redis session、cache、streams 和 Celery 的独立容量告警；
- API/Worker/数据库角色最小权限；
- JWT signing key 与 password pepper 的备份、访问审计和轮换方案；
- 多节点滚动发布和 SSE 恢复验证。
- Phala CVM 部署、attestation/measurement 校验和镜像 digest/compose hash 留档；
- 在 test → pre 验证后再发布 prod，并保留上一版可恢复镜像与数据库兼容回滚窗口。

TEE 的具体信任根、远程证明、measurement 白名单、secret release、重启/unseal、管理员
权限和备份密钥托管由部署规格定义；本服务规格不把“运行在 TEE”本身等同于这些控制已
自动满足。

## 25. 延后决策

以下能力等真实需求出现后再设计：

- TEE PostgreSQL 自动故障切换或只读副本；
- 多 Celery 队列和工作流；
- 独立任务状态表与长期任务审计；
- 对象存储和大文件上传；
- 外部 HTTP client 熔断策略；
- 向量检索；
- Webhook 验签；
- 多进程 Web 容器；
- Phala 之外的多云发布和通用基础设施即代码。

## 26. 与 V1/当前 Feedling 的边界

可以复用的经验：

- FastAPI/ASGI app assembly 与 lifespan；
- Router 和业务逻辑分离；
- 廉价 `/healthz` 与依赖型 `/readyz`；
- request ID、结构化日志和路由模板指标；
- psycopg 连接池和 PostgreSQL 超时治理；
- PostgreSQL schema migration；
- Redis/PostgreSQL 驱动的跨 worker 一致性；
- 同一镜像的 API/Worker 多入口；
- 通过测试锁定租户隔离和安全边界。

不复制的 V1 实现：

- 信封加密和内容密钥；
- RDS 密文主库、TEE 明文影子库和双写；
- enclave 解密代理和双入口；
- V1 兼容 Router、旧认证 header 和 query key；
- Runtime V2 的自建 PostgreSQL durable queue、lease、reaper 和多池调度；
- V1 产品路由、环境变量、云资源名和发布拓扑。

IO V2 只迁移 V1 已解密的业务数据；V2 凭证字段由用户迁移时提交 V1 私钥密码后重新
生成，不从旧 hash 转换，也不继承 V1 的加密存储与兼容层。
